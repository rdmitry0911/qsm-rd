"""One file manager session: its WebRTC peer process and the relay to the guest.

The peer parses everything that arrives from the network, so it runs apart from
the console's media loop, and without privileges. This module adds what the peer
may not choose: the session id and the rights, both of which come from PVE's
authorization. See docs/FILE_MANAGER.md.
"""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from typing import Callable

from direct_guest.qsm_guest_channel import GuestChannelError, QsmGuestChannel

PEER_PATH = Path(__file__).resolve().parents[1] / "browser_bridge" / "qsm_file_peer.py"
MAX_LINE = 96 * 1024
MAX_HANDSHAKE = 1024 * 1024
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
        self._link = link
        self._rights = rights
        self._end_listeners: list[Callable[[], None]] = [on_end] if on_end is not None else []
        self._owns_link = owns_link
        self._peer_command = peer_command or [sys.executable, "-I", str(PEER_PATH)]
        self._peer: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._condition = threading.Condition()
        self._queue: deque[bytes] = deque()
        self._queued = 0
        self._closed = False
        self._guest_open = False
        self._ready_seen = False
        self.candidates: list[list] = []

    def start(self, sdp: str, timeout: float = 20.0) -> str:
        """Start the peer and return its SDP answer."""
        try:
            if not self._link.files_capable():
                raise FileSessionError("the guest agent has no file support")
            ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket = ours
            try:
                self._peer = subprocess.Popen(
                    [*self._peer_command, "--fd", str(theirs.fileno())], pass_fds=(theirs.fileno(),),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=None, close_fds=True)
            finally:
                theirs.close()
            ours.settimeout(timeout)
            ours.sendall(json.dumps({"offer": sdp}, separators=(",", ":")).encode("ascii") + b"\n")
            reply = self._read_handshake(ours, time.monotonic() + timeout)
            answer, candidates = reply.get("answer"), reply.get("candidates")
            if not isinstance(answer, str) or not isinstance(candidates, list):
                raise FileSessionError("the file peer gave an invalid answer")
            self.candidates = candidates
            ours.settimeout(None)
            self._link.open_file_session(self.identifier, self._rights, self._from_guest)
            self._guest_open = True
            threading.Thread(target=self._to_peer, name="qsm-files-down", daemon=True).start()
            threading.Thread(target=self._from_peer, name="qsm-files-up", daemon=True).start()
            return answer
        except (OSError, ValueError, GuestChannelError) as error:
            self.close()
            raise FileSessionError(f"file session failed: {type(error).__name__}") from error
        except BaseException:
            self.close()
            raise

    def _read_handshake(self, connection: socket.socket, deadline: float) -> dict:
        buffered = bytearray()
        while b"\n" not in buffered:
            if time.monotonic() > deadline or len(buffered) > MAX_HANDSHAKE:
                raise FileSessionError("the file peer did not answer")
            block = connection.recv(65536)
            if not block:
                raise FileSessionError("the file peer ended during negotiation")
            buffered.extend(block)
        value = json.loads(bytes(buffered).split(b"\n", 1)[0])
        if not isinstance(value, dict):
            raise FileSessionError("the file peer gave an invalid answer")
        return value

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
                    line = self._queue.popleft()
                    self._queued -= len(line) + LINE_OVERHEAD_BYTES
                connection.sendall(line)
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
