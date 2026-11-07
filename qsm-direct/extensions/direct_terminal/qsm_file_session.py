"""One file manager session: its peer process and the relay to the guest.

The peer parses everything that arrives from the network, so it runs apart from
the console's media loop, and without privileges. This module adds what the peer
may not choose: the session id and the rights, both of which come from PVE's
authorization. See docs/FILE_MANAGER.md.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
from typing import Callable

from direct_guest.qsm_guest_channel import GuestChannelError, QsmGuestChannel

PEER_PATH = Path(__file__).resolve().parents[1] / "browser_bridge" / "qsm_file_peer.py"
MAX_LINE = 96 * 1024
# At a high rate one write per line costs the console's process a thread switch per line.
MAX_WRITE_BYTES = 1024 * 1024
# The browser's credit bounds what an honest guest sends. Past this the guest is
# not honest, or the viewer has stalled, and only this session ends.
MAX_QUEUED_BYTES = 16 * 1024 * 1024
LINE_OVERHEAD_BYTES = 1024


class FileSessionError(RuntimeError):
    """A file session could not start; ``reason`` is shown to no one but the journal."""


class FileSession:
    def __init__(self, link: QsmGuestChannel, rights: str, *, on_end: Callable[[], None] | None = None,
                 owns_link: bool = False, peer_command: list[str] | None = None) -> None:
        if rights not in ("r", "rw"):
            raise ValueError("invalid file rights")
        self.identifier = secrets.token_hex(8)
        # What the browser presents to reach this session's peer, once.
        self.token = secrets.token_hex(16)
        self._link = link
        self._rights = rights
        self._end_listeners: list[Callable[[], None]] = [on_end] if on_end is not None else []
        self._owns_link = owns_link
        self._peer_command = peer_command or [sys.executable, "-I", str(PEER_PATH)]
        self._peer: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._network: socket.socket | None = None
        self._condition = threading.Condition()
        self._queue: deque[bytes] = deque()
        self._queued = 0
        self._closed = False
        self._guest_open = False
        self._ready_seen = False

    def start(self) -> None:
        """Start the peer and open the guest's session. The browser connects to it next."""
        try:
            if not self._link.files_capable():
                raise FileSessionError("the guest agent has no file support")
            ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            network, peer_network = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket, self._network = ours, network
            try:
                self._peer = subprocess.Popen(
                    [*self._peer_command, "--fd", str(theirs.fileno()), "--network-fd", str(peer_network.fileno())],
                    pass_fds=(theirs.fileno(), peer_network.fileno()),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=None, close_fds=True)
            finally:
                theirs.close()
                peer_network.close()
            self._link.open_file_session(self.identifier, self._rights, self._from_guest)
            self._guest_open = True
            threading.Thread(target=self._to_peer, name="qsm-files-down", daemon=True).start()
            threading.Thread(target=self._from_peer, name="qsm-files-up", daemon=True).start()
        except (OSError, ValueError, GuestChannelError) as error:
            self.close()
            raise FileSessionError(f"file session failed: {type(error).__name__}") from error
        except BaseException:
            self.close()
            raise

    def take_network(self) -> socket.socket | None:
        """The browser's side of the peer, for whoever presented the token. Only once."""
        with self._condition:
            network, self._network = self._network, None
            return None if self._closed else network

    def _from_guest(self, kind: str, rest: bytes) -> None:
        if kind == "D":
            line = b"D " + rest + b"\n"
        elif kind == "R":
            # Tiny lines cost the peer far more than their bytes. Only the first means anything.
            if self._ready_seen:
                return
            self._ready_seen = True
            line = b"R\n"
        elif kind == "C":
            line = b"C " + rest[:32] + b"\n"
        else:
            return
        with self._condition:
            if self._closed:
                return
            cost = len(line) + LINE_OVERHEAD_BYTES
            overflow = self._queued + cost > MAX_QUEUED_BYTES
            if not overflow:
                self._queue.append(line)
                self._queued += cost
                self._condition.notify_all()
        if overflow:
            # Runs on the guest channel's reader, which must not wait for a close.
            threading.Thread(target=self.close, name="qsm-files-close", daemon=True).start()

    def _to_peer(self) -> None:
        connection = self._socket
        try:
            while connection is not None:
                with self._condition:
                    while not self._queue and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    lines, total = [], 0
                    while self._queue and total < MAX_WRITE_BYTES:
                        line = self._queue.popleft()
                        self._queued -= len(line) + LINE_OVERHEAD_BYTES
                        lines.append(line)
                        total += len(line)
                connection.sendall(b"".join(lines))
        except OSError:
            self.close()

    def _from_peer(self) -> None:
        connection = self._socket
        try:
            with connection.makefile("rb", buffering=MAX_LINE) as reader:
                while True:
                    line = reader.readline(MAX_LINE + 1)
                    if not line or len(line) > MAX_LINE or not line.endswith(b"\n") or not line.startswith(b"D "):
                        return
                    self._link.send_file_frame(self.identifier, line[2:-1])
        except (OSError, ValueError, GuestChannelError):
            pass
        finally:
            self.close()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
            self._condition.notify_all()
            network, self._network = self._network, None
        if network is not None:
            network.close()
        if self._guest_open:
            self._link.close_file_session(self.identifier)
        if self._owns_link:
            self._link.close()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
        if self._peer is not None:
            try:
                self._peer.kill()
                self._peer.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for listener in self._end_listeners:
            listener()

    def add_end_listener(self, listener: Callable[[], None]) -> None:
        with self._condition:
            closed = self._closed
            if not closed:
                self._end_listeners.append(listener)
        if closed:
            listener()
