"""Private QSM guest clipboard and file channel for the browser console.

The only peer is a QEMU ``socket`` chardev below a root-owned per-VM runtime
directory.  This is intentionally not a network service: PVE has already
authorised the WebRTC console session before this module is reachable.
"""

from __future__ import annotations

import base64
import binascii
import re
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable


MAX_CLIPBOARD_BYTES = 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILE_LIST_BYTES = 64 * 1024
MAX_AGENT_LINE = 4 * 1024 * 1024
# A missing optional in-guest package must not make a connected desktop
# console appear frozen for half a minute after Copy, Paste or Upload.
REQUEST_TIMEOUT_SECONDS = 5.0
_SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EXCHANGE_AREAS = frozenset(("incoming", "outgoing"))


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


def _name(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_FILE_NAME.fullmatch(value):
        raise GuestChannelError("file name is invalid")
    return value


def _area(value: Any) -> str:
    if not isinstance(value, str) or value not in _EXCHANGE_AREAS:
        raise GuestChannelError("exchange area is invalid")
    return value


def _file_list(value: str, area: str) -> list[dict[str, int | str]]:
    """Decode the bounded manifest emitted by the minimal C guest agent."""
    data = _decode_agent_b64(value, MAX_FILE_LIST_BYTES, "file list")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise GuestChannelError("file list is invalid") from error
    files: list[dict[str, int | str]] = []
    names: set[str] = set()
    if text and not text.endswith("\n"):
        raise GuestChannelError("file list is invalid")
    for line in text.splitlines():
        name, separator, size = line.partition("\t")
        if not separator or not _SAFE_FILE_NAME.fullmatch(name) or name in names or \
                not size.isascii() or not size.isdecimal():
            raise GuestChannelError("file list is invalid")
        bytes_count = int(size)
        if bytes_count > MAX_FILE_BYTES:
            # The agent can safely report a larger guest file, but it must
            # never lead the browser to offer a transfer it cannot complete.
            continue
        names.add(name)
        files.append({"name": name, "bytes": bytes_count})
    if len(files) > 256:
        raise GuestChannelError("file list is invalid")
    return files


class QsmGuestChannel:
    """Serialize a reconnectable QSM guest-agent channel for one VM.

    One QEMU chardev accepts one host peer, so the terminal owns exactly one
    instance per VM and browser sessions merely subscribe to its clipboard
    notifications.  Commands are not replayed after an interrupted write.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stopped = threading.Event()
        self._condition = threading.Condition()
        self._request_lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._generation = 0
        self._ready_generation: int | None = None
        self._responses: deque[str] = deque()
        self._listeners: set[Callable[[str], None]] = set()

    def add_clipboard_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        with self._condition:
            if self._stopped.is_set():
                raise GuestChannelError("guest side channel is closed")
            self._listeners.add(listener)

        def remove() -> None:
            with self._condition:
                self._listeners.discard(listener)
        return remove

    def close(self) -> None:
        self._stopped.set()
        with self._condition:
            channel, reader = self._socket, self._reader
            self._socket = None
            self._reader = None
            self._generation += 1
            self._ready_generation = None
            self._responses.clear()
            self._listeners.clear()
            self._condition.notify_all()
        if channel is not None:
            try:
                channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            channel.close()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)

    def _disconnect(self, channel: socket.socket, generation: int) -> None:
        with self._condition:
            if self._socket is not channel or self._generation != generation:
                return
            self._socket = None
            self._reader = None
            self._ready_generation = None
            self._responses.clear()
            self._condition.notify_all()
        try:
            channel.close()
        except OSError:
            pass

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
                        with self._condition:
                            if self._socket is channel and self._generation == generation:
                                self._ready_generation = generation
                                self._condition.notify_all()
                    elif line.startswith("EVENT_CLIP "):
                        self._publish_clipboard(line[11:])
                    else:
                        with self._condition:
                            if self._socket is channel and self._generation == generation:
                                self._responses.append(line)
                                self._condition.notify_all()
        finally:
            self._disconnect(channel, generation)

    def _connect(self, deadline: float) -> tuple[socket.socket, int]:
        while True:
            if self._stopped.is_set():
                raise GuestChannelError("guest side channel is closed")
            with self._condition:
                active, generation = self._socket, self._generation
            if active is not None:
                with self._condition:
                    while self._socket is active and self._generation == generation and \
                            self._ready_generation != generation:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise GuestChannelError("guest agent is unavailable")
                        self._condition.wait(remaining)
                    if self._socket is active and self._generation == generation and \
                            self._ready_generation == generation:
                        return active, generation
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GuestChannelError("guest agent is unavailable")
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
                self._ready_generation = None
                self._responses.clear()
                self._reader = threading.Thread(target=self._read_loop, args=(candidate, generation),
                                                name="qsm-guest-channel", daemon=True)
                self._reader.start()

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
            self._request(f"CLIP_SET {_agent_b64(data)}", "OK CLIP_SET")
            return {"bytes": len(data)}
        if operation == "clipboard_get" and set(payload) == {"op"}:
            response = self._request("CLIP_GET", "CLIP ")
            data = _decode_agent_b64(response[5:], MAX_CLIPBOARD_BYTES, "clipboard")
            return {"text_b64": base64.b64encode(_text(data, "clipboard").encode("utf-8")).decode("ascii")}
        if operation == "file_upload" and set(payload) == {"op", "name", "data_b64"}:
            name = _name(payload["name"])
            data = _decode_b64(payload["data_b64"], MAX_FILE_BYTES, "file")
            self._request(f"FILE_PUT {name} {_agent_b64(data)}", "OK FILE_PUT")
            return {"name": name, "bytes": len(data)}
        if operation == "file_download" and set(payload) == {"op", "name"}:
            name = _name(payload["name"])
            response = self._request(f"FILE_GET {name}", f"FILE {name} ")
            data = _decode_agent_b64(response[len(name) + 6:], MAX_FILE_BYTES, "file")
            return {"name": name, "data_b64": base64.b64encode(data).decode("ascii"), "bytes": len(data)}
        if operation == "file_list" and set(payload) == {"op", "area"}:
            area = _area(payload["area"])
            response = self._request(f"FILE_LIST {area}", f"FILES {area} ")
            return {"area": area, "files": _file_list(response[len(area) + 7:], area)}
        if operation == "status" and set(payload) == {"op"}:
            self._request("PING", "OK PONG")
            return {"agent": "ready", "max_file_bytes": MAX_FILE_BYTES}
        raise GuestChannelError("guest request is invalid")
