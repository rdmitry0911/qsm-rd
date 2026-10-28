"""Private QSM guest clipboard and advisory desktop channel for the console.

The only peer is a QEMU ``socket`` chardev below a root-owned per-VM runtime
directory.  This is intentionally not a network service: PVE has already
authorised the WebRTC console session before this module is reachable.
"""

from __future__ import annotations

import base64
import binascii
import copy
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .qsm_desktop_metadata import DesktopMetadataError, MAX_METADATA_BYTES, parse_metadata


MAX_CLIPBOARD_BYTES = 1024 * 1024
MAX_AGENT_LINE = 4 * 1024 * 1024
# A missing optional in-guest package must not make a connected desktop
# console appear frozen for half a minute after Copy or Paste.
REQUEST_TIMEOUT_SECONDS = 5.0
DESKTOP_LEASE_SECONDS = 3.0


class GuestChannelError(RuntimeError):
    """A bounded, non-sensitive guest-side-channel failure."""


def _decode_b64(value: Any, limit: int, label: str) -> bytes:
    if not isinstance(value, str) or len(value) > ((limit + 2) // 3) * 4 + 4:
        raise GuestChannelError(f"{label} is invalid")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise GuestChannelError(f"{label} is invalid") from error
    if len(decoded) > limit:
        raise GuestChannelError(f"{label} is too large")
    return decoded


def _agent_b64(value: bytes) -> str:
    return "-" if not value else base64.b64encode(value).decode("ascii")


def _decode_agent_b64(value: str, limit: int, label: str) -> bytes:
    return b"" if value == "-" else _decode_b64(value, limit, label)


def _text(value: bytes, label: str) -> str:
    if b"\x00" in value:
        raise GuestChannelError(f"{label} is invalid")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GuestChannelError(f"{label} is not UTF-8") from error


class QsmGuestChannel:
    """Serialize a reconnectable QSM guest-agent channel for one VM.

    One QEMU chardev accepts one host peer, so the terminal owns exactly one
    instance per VM and browser sessions merely subscribe to its clipboard
    notifications.  Commands are not replayed after an interrupted write.
    """

    def __init__(self, path: Path | None = None, *,
                 connector: Callable[[float], socket.socket] | None = None,
                 keep_connected: bool = False) -> None:
        """``path``: the QEMU chardev socket of the QSM desktop agent.

        ``connector`` instead returns a connected stream speaking the same
        protocol (raising OSError while none is available) -- the media
        worker's bridge to QEMU's D-Bus clipboard, or the clipboard agent in
        a container's console session.
        """
        if (path is None) == (connector is None):
            raise ValueError("exactly one of path or connector is required")
        self._path = path
        self._connector = connector
        self._keeper: threading.Thread | None = None
        self._stopped = threading.Event()
        self._condition = threading.Condition()
        self._request_lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._generation = 0
        self._responses: deque[str] = deque()
        self._listeners: set[Callable[[str], None]] = set()
        self._desktop_listeners: set[Callable[[dict[str, Any] | None], None]] = set()
        self._desktop_value: dict[str, Any] | None = None
        self._desktop_received = 0.0
        self._desktop_session = ''
        self._desktop_sequence = 0
        self._desktop_layout = 0
        self._desktop_probe_generation = -1
        self._desktop_enabled = False
        self._agent_revision = 0
        self._keep_connection = keep_connected
        self._keeper_wake = threading.Event()
        if keep_connected:
            # The peer comes and goes (a container session's clipboard agent
            # restarts with every login): reconnect in the background, so its
            # clipboard events reach the browser without waiting for a request.
            self._keeper = threading.Thread(target=self._keep_connected, name="qsm-guest-keeper", daemon=True)
            self._keeper.start()

    def _keep_connected(self) -> None:
        while not self._stopped.is_set():
            with self._condition:
                wanted = bool(self._desktop_listeners)
                needed = self._keep_connection or wanted or self._desktop_enabled
            if needed:
                try:
                    _, generation = self._connect(time.monotonic() + 1.0)
                    with self._condition:
                        probe = wanted and self._desktop_probe_generation != generation
                        if probe:
                            # Events may precede the subscription acknowledgement
                            # in the same read. Mark the attempt before sending.
                            self._desktop_probe_generation = generation
                            agent_revision = self._agent_revision
                    if probe:
                        try:
                            self._request('DESKTOP_SUBSCRIBE 1', 'OK DESKTOP_SUBSCRIBE')
                        except GuestChannelError:
                            # Old agents reject this optional command. Retry
                            # only after reconnect/READY, preserving clipboard.
                            pass
                        else:
                            with self._condition:
                                self._desktop_enabled = (self._generation == generation and
                                                         self._agent_revision == agent_revision)
                    elif not wanted and self._desktop_enabled:
                        self._request('DESKTOP_SUBSCRIBE 0', 'OK DESKTOP_SUBSCRIBE')
                        with self._condition:
                            self._desktop_enabled = False
                            self._desktop_probe_generation = -1
                except GuestChannelError:
                    pass
            self._expire_desktop()
            self._keeper_wake.wait(.25)
            self._keeper_wake.clear()

    def connect_now(self, timeout: float = 1.0) -> None:
        """Connect at once, so guest clipboard events flow before any request."""
        self._connect(time.monotonic() + timeout)

    def add_clipboard_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        with self._condition:
            if self._stopped.is_set():
                raise GuestChannelError("guest side channel is closed")
            self._listeners.add(listener)

        def remove() -> None:
            with self._condition:
                self._listeners.discard(listener)
        return remove

    def add_desktop_listener(self, listener: Callable[[dict[str, Any] | None], None]) -> Callable[[], None]:
        """Subscribe to bounded hints; None revokes them after loss/expiry.

        Consumers must retain pixel fallback and must not treat geometry as
        proof that a corresponding capture has reached the video decoder.
        """
        with self._condition:
            if self._stopped.is_set():
                raise GuestChannelError('guest side channel is closed')
            self._desktop_listeners.add(listener)
            if self._keeper is None:
                self._keeper = threading.Thread(target=self._keep_connected, name='qsm-guest-keeper', daemon=True)
                self._keeper.start()
        self._keeper_wake.set()

        def remove() -> None:
            with self._condition:
                self._desktop_listeners.discard(listener)
            self._keeper_wake.set()
        return remove

    def _expire_desktop(self) -> None:
        with self._condition:
            if self._desktop_value is None or time.monotonic() - self._desktop_received <= DESKTOP_LEASE_SECONDS:
                return
            self._desktop_value = None
            listeners = tuple(self._desktop_listeners)
        self._notify_desktop(listeners, None)

    def _invalidate_desktop(self, generation: int | None = None) -> None:
        with self._condition:
            if self._desktop_value is None or (generation is not None and generation != self._generation):
                return
            self._desktop_value = None
            listeners = tuple(self._desktop_listeners)
        self._notify_desktop(listeners, None)

    @staticmethod
    def _notify_desktop(listeners, value) -> None:
        for listener in listeners:
            try:
                listener(copy.deepcopy(value))
            except Exception:
                pass

    def _publish_desktop(self, encoded: str, generation: int) -> None:
        with self._condition:
            if self._generation != generation or self._desktop_probe_generation != generation or not self._desktop_listeners:
                return
        try:
            value = parse_metadata(_decode_agent_b64(encoded, MAX_METADATA_BYTES, 'desktop'))
        except (GuestChannelError, DesktopMetadataError):
            self._invalidate_desktop(generation)
            return
        with self._condition:
            if self._generation != generation or self._socket is None:
                return
            same = value['session'] == self._desktop_session
            if same and (value['sequence'] <= self._desktop_sequence or value['generation'] < self._desktop_layout):
                return  # Replays never renew the lease.
            self._desktop_session = value['session']
            self._desktop_sequence = value['sequence']
            self._desktop_layout = value['generation']
            self._desktop_value = value
            self._desktop_received = time.monotonic()
            listeners = tuple(self._desktop_listeners)
        self._notify_desktop(listeners, value)

    def close(self) -> None:
        self._stopped.set()
        self._keeper_wake.set()
        self._invalidate_desktop()
        with self._condition:
            channel, reader = self._socket, self._reader
            self._socket = None
            self._reader = None
            self._generation += 1
            self._responses.clear()
            self._listeners.clear()
            self._desktop_listeners.clear()
            self._condition.notify_all()
        if channel is not None:
            try:
                channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            channel.close()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        if self._keeper is not None and self._keeper is not threading.current_thread():
            self._keeper.join(timeout=1.0)

    def _disconnect(self, channel: socket.socket, generation: int) -> None:
        with self._condition:
            if self._socket is not channel or self._generation != generation:
                return
            self._socket = None
            self._reader = None
            self._responses.clear()
            self._condition.notify_all()
        try:
            channel.close()
        except OSError:
            pass
        self._invalidate_desktop()
        self._keeper_wake.set()

    def _publish_clipboard(self, encoded: str) -> None:
        try:
            text = _text(_decode_agent_b64(encoded, MAX_CLIPBOARD_BYTES, "clipboard"), "clipboard")
        except GuestChannelError:
            return
        with self._condition:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(text)
            except Exception:
                # A departed browser must never poison a VM-wide guest port.
                pass

    def _read_loop(self, channel: socket.socket, generation: int) -> None:
        buffered = bytearray()
        try:
            while not self._stopped.is_set():
                try:
                    block = channel.recv(65536)
                except socket.timeout:
                    self._expire_desktop()
                    continue
                except OSError:
                    break
                if not block:
                    break
                buffered.extend(block)
                if len(buffered) > MAX_AGENT_LINE:
                    break
                while b"\n" in buffered:
                    raw, _, trailing = buffered.partition(b"\n")
                    buffered = bytearray(trailing)
                    try:
                        line = raw.decode("ascii")
                    except UnicodeDecodeError:
                        continue
                    if line == "READY QSF1":
                        # QEMU keeps the host socket open across an agent or
                        # guest restart. The new process has no subscription,
                        # even while PING works on this same stream. Revoke
                        # hints and resubscribe without replaying user actions.
                        # A greeting may also arrive during the first probe;
                        # its revision prevents that old acknowledgement from
                        # permanently suppressing the replacement subscription.
                        with self._condition:
                            if self._socket is not channel or self._generation != generation:
                                continue
                            self._agent_revision += 1
                            self._desktop_probe_generation = -1
                            self._desktop_enabled = False
                        self._invalidate_desktop(generation)
                        self._keeper_wake.set()
                        continue
                    if line.startswith("EVENT_CLIP "):
                        self._publish_clipboard(line[11:])
                    elif line.startswith('EVENT_DESKTOP '):
                        self._publish_desktop(line[14:], generation)
                    else:
                        with self._condition:
                            if self._socket is channel and self._generation == generation:
                                if len(self._responses) >= 64:
                                    return
                                self._responses.append(line)
                                self._condition.notify_all()
                self._expire_desktop()
        finally:
            self._disconnect(channel, generation)

    def _connect(self, deadline: float) -> tuple[socket.socket, int]:
        while True:
            if self._stopped.is_set():
                raise GuestChannelError("guest side channel is closed")
            with self._condition:
                active, generation = self._socket, self._generation
            if active is not None:
                return active, generation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GuestChannelError("guest agent is unavailable")
            if self._connector is not None:
                try:
                    candidate = self._connector(min(1.0, remaining))
                    candidate.settimeout(1.0)
                except OSError:
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                    continue
            else:
                candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    candidate.settimeout(min(1.0, remaining))
                    candidate.connect(str(self._path))
                    candidate.settimeout(1.0)
                except OSError:
                    candidate.close()
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                    continue
            with self._condition:
                if self._stopped.is_set() or self._socket is not None:
                    candidate.close()
                    continue
                self._generation += 1
                generation = self._generation
                self._socket = candidate
                self._desktop_enabled = False
                self._desktop_session = ''
                self._desktop_sequence = 0
                self._desktop_layout = 0
                self._responses.clear()
                self._reader = threading.Thread(target=self._read_loop, args=(candidate, generation),
                                                name="qsm-guest-channel", daemon=True)
                self._reader.start()
                return candidate, generation

    def _request(self, command: str, prefix: str) -> str:
        if "\n" in command or "\r" in command:
            raise GuestChannelError("guest request is invalid")
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        if not self._request_lock.acquire(timeout=REQUEST_TIMEOUT_SECONDS):
            raise GuestChannelError("guest agent is busy")
        try:
            channel, generation = self._connect(deadline)
            try:
                channel.sendall(command.encode("ascii") + b"\n")
            except OSError as error:
                self._disconnect(channel, generation)
                raise GuestChannelError("guest channel was interrupted; retry") from error
            with self._condition:
                while True:
                    if self._responses:
                        response = self._responses.popleft()
                        if response.startswith("ERR "):
                            raise GuestChannelError("guest agent rejected the operation")
                        if response.startswith(prefix):
                            return response
                    if self._socket is not channel or self._generation != generation:
                        raise GuestChannelError("guest channel was interrupted; retry")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # QSF1 has no request IDs. A late acknowledgement must
                        # not become an error for the next clipboard request.
                        self._disconnect(channel, generation)
                        raise GuestChannelError("guest agent did not respond")
                    self._condition.wait(remaining)
        finally:
            self._request_lock.release()

    def dispatch(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise GuestChannelError("guest request is invalid")
        operation = payload.get("op")
        if operation == "clipboard_set" and set(payload) == {"op", "text_b64"}:
            text = _text(_decode_b64(payload["text_b64"], MAX_CLIPBOARD_BYTES, "clipboard"), "clipboard")
            data = text.encode("utf-8")
            response = self._request(f"CLIP_SET {_agent_b64(data)}", "OK CLIP_SET")
            # The bare legacy reply only confirms the constrained state file.
            # A Desktop Agent bridge returns its unique generation after the
            # actual Wayland/Klipper selection has accepted it.  Expose that
            # boundary to the browser so it never guesses with a timer before
            # synthesizing Ctrl+V.
            return {"bytes": len(data), "applied": response.startswith("OK CLIP_SET ")}
        if operation == "clipboard_get" and set(payload) == {"op"}:
            response = self._request("CLIP_GET", "CLIP ")
            data = _decode_agent_b64(response[5:], MAX_CLIPBOARD_BYTES, "clipboard")
            return {"text_b64": base64.b64encode(_text(data, "clipboard").encode("utf-8")).decode("ascii")}
        if operation == "status" and set(payload) == {"op"}:
            self._request("PING", "OK PONG")
            return {"agent": "ready"}
        raise GuestChannelError("guest request is invalid")
