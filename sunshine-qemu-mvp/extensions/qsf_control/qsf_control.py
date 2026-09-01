#!/usr/bin/env python3
"""Local QSF control side-channel for a single QEMU guest.

GameStream carries video, audio, and input but has no interoperable clipboard
or file-transfer protocol.  This deliberately small companion channel is
bound to one VM's private Unix sockets.  It bridges an authenticated local
client to a guest agent on a QEMU virtio-serial port.

The wire protocol is newline-delimited JSON on the client socket and a compact
ASCII protocol on the guest socket.  Both sides enforce byte limits before
decoding base64 payloads.  The control socket is local-only (AF_UNIX, 0600);
remote clients should forward it over the same authenticated transport that
launches their Moonlight session rather than exposing it directly on TCP.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import json
import os
import re
import secrets
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


MAX_CLIPBOARD_BYTES = 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CONTROL_LINE = 4 * 1024 * 1024
MAX_AGENT_LINE = 4 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 8.0
SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ControlError(RuntimeError):
    """A request rejected by the QSF control boundary."""


def decode_b64(value: Any, limit: int, label: str) -> bytes:
    if not isinstance(value, str):
        raise ControlError(f"{label} must be a base64 string")
    if len(value) > ((limit + 2) // 3) * 4 + 4:
        raise ControlError(f"{label} exceeds its encoded size limit")
    try:
        data = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise ControlError(f"{label} is not valid base64") from error
    if len(data) > limit:
        raise ControlError(f"{label} exceeds its decoded size limit")
    return data


def encode_b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def encode_agent_b64(value: bytes) -> str:
    """Represent an empty binary payload without making the line ambiguous."""
    return "-" if not value else encode_b64(value)


def decode_agent_b64(value: str, limit: int, label: str) -> bytes:
    return b"" if value == "-" else decode_b64(value, limit, label)


def require_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ControlError(f"{label} must be UTF-8") from error


def require_file_name(value: Any) -> str:
    if not isinstance(value, str) or not SAFE_FILE_NAME.fullmatch(value):
        raise ControlError("file name must be a plain relative basename")
    if value in {".", ".."} or ".." in value:
        raise ControlError("file name may not contain traversal")
    return value


class AgentChannel:
    """Serialize requests to the single guest virtio-serial connection."""

    def __init__(self, path: Path, on_clipboard: callable) -> None:
        self._path = path
        self._on_clipboard = on_clipboard
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.connect(str(path))
        self._socket.settimeout(1.0)
        self._closed = threading.Event()
        self._responses: deque[str] = deque()
        self._condition = threading.Condition()
        self._request_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, name="qsf-agent-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._closed.set()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        self._reader.join(timeout=1.0)

    def _read_loop(self) -> None:
        buffer = bytearray()
        try:
            while not self._closed.is_set():
                try:
                    chunk = self._socket.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > MAX_AGENT_LINE:
                    raise ControlError("guest agent emitted an oversized line")
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    try:
                        line = raw.decode("ascii")
                    except UnicodeDecodeError:
                        continue
                    # `CLIP` is the synchronous response to CLIP_GET.  An
                    # unsolicited guest clipboard change has a distinct
                    # prefix so it cannot steal a response from a concurrent
                    # (serialized) request.
                    if line.startswith("EVENT_CLIP "):
                        try:
                            text = require_utf8(
                                decode_agent_b64(line[11:], MAX_CLIPBOARD_BYTES, "guest clipboard"),
                                "guest clipboard",
                            )
                        except ControlError:
                            continue
                        self._on_clipboard(text)
                        continue
                    with self._condition:
                        self._responses.append(line)
                        self._condition.notify_all()
        except (OSError, ControlError):
            pass
        finally:
            self._closed.set()
            with self._condition:
                self._condition.notify_all()

    def request(self, command: str, expected_prefix: str) -> str:
        if "\n" in command or "\r" in command:
            raise ControlError("invalid agent command")
        with self._request_lock:
            if self._closed.is_set():
                raise ControlError("guest agent transport is closed")
            try:
                self._socket.sendall(command.encode("ascii") + b"\n")
            except OSError as error:
                raise ControlError("cannot write to guest agent") from error
            deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
            with self._condition:
                while True:
                    if self._responses:
                        line = self._responses.popleft()
                        if line.startswith("ERR "):
                            raise ControlError(f"guest agent rejected request: {line[4:]}")
                        if line.startswith(expected_prefix):
                            return line
                        # READY and unrelated asynchronous status messages are
                        # harmless while a request is in flight.
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._closed.is_set():
                        raise ControlError("timed out waiting for guest agent")
                    self._condition.wait(remaining)


class Broker:
    """Owns the guest channel and dispatches authenticated client operations."""

    def __init__(self, agent_socket: Path, enable_qemu_resize: bool) -> None:
        self._clipboard_lock = threading.Lock()
        self._clipboard = ""
        self._clipboard_generation = 0
        self._agent = AgentChannel(agent_socket, self._set_guest_clipboard)
        self._enable_qemu_resize = enable_qemu_resize

    def close(self) -> None:
        self._agent.close()

    def _set_guest_clipboard(self, text: str) -> None:
        with self._clipboard_lock:
            self._clipboard = text
            self._clipboard_generation += 1

    @staticmethod
    def _dimensions(payload: dict[str, Any]) -> tuple[int, int]:
        width = payload.get("width")
        height = payload.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            raise ControlError("width and height must be integers")
        if not 64 <= width <= 16384 or not 64 <= height <= 16384:
            raise ControlError("resolution is outside the supported range")
        return width, height

    @staticmethod
    def _mm_for_pixels(pixels: int) -> int:
        return max(1, min(65535, round(pixels * 25.4 / 96.0)))

    def _set_qemu_ui_info(self, width: int, height: int) -> str:
        if not self._enable_qemu_resize:
            return "disabled"
        command = [
            "busctl", "--user", "call", "org.qemu",
            "/org/qemu/Display1/Console_0", "org.qemu.Display1.Console",
            "SetUIInfo", "qqiiuu",
            str(self._mm_for_pixels(width)), str(self._mm_for_pixels(height)),
            "0", "0", str(width), str(height),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
                env=os.environ.copy(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ControlError("QEMU SetUIInfo could not be invoked") from error
        if result.returncode != 0:
            detail = result.stdout.strip().replace("\n", " ")[:300]
            raise ControlError(f"QEMU SetUIInfo failed: {detail}")
        return "applied"

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = payload.get("op")
        if operation == "status":
            self._agent.request("PING", "OK PONG")
            with self._clipboard_lock:
                return {
                    "agent": "ready",
                    "clipboard_generation": self._clipboard_generation,
                    "qemu_resize": self._enable_qemu_resize,
                }
        if operation == "clipboard_set":
            text = require_utf8(decode_b64(payload.get("text_b64"), MAX_CLIPBOARD_BYTES, "clipboard"), "clipboard")
            self._agent.request(f"CLIP_SET {encode_agent_b64(text.encode('utf-8'))}", "OK CLIP_SET")
            self._set_guest_clipboard(text)
            return {"bytes": len(text.encode("utf-8"))}
        if operation == "clipboard_get":
            response = self._agent.request("CLIP_GET", "CLIP ")
            text = require_utf8(decode_agent_b64(response[5:], MAX_CLIPBOARD_BYTES, "guest clipboard"), "guest clipboard")
            self._set_guest_clipboard(text)
            with self._clipboard_lock:
                return {"text_b64": encode_b64(self._clipboard.encode("utf-8")), "generation": self._clipboard_generation}
        if operation == "upload":
            name = require_file_name(payload.get("name"))
            data = decode_b64(payload.get("data_b64"), MAX_FILE_BYTES, "file")
            self._agent.request(f"FILE_PUT {name} {encode_agent_b64(data)}", "OK FILE_PUT")
            return {"name": name, "bytes": len(data)}
        if operation == "download":
            name = require_file_name(payload.get("name"))
            response = self._agent.request(f"FILE_GET {name}", f"FILE {name} ")
            data = decode_agent_b64(response[len(name) + 6:], MAX_FILE_BYTES, "guest file")
            return {"name": name, "data_b64": encode_b64(data), "bytes": len(data)}
        if operation == "resize":
            width, height = self._dimensions(payload)
            self._agent.request(f"RESIZE {width} {height}", "OK RESIZE")
            qemu = self._set_qemu_ui_info(width, height)
            return {"width": width, "height": height, "qemu_set_ui_info": qemu}
        raise ControlError("unknown operation")


class ControlRequestHandler(socketserver.StreamRequestHandler):
    """One authenticated JSON request per local client connection."""

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, ControlServer)
        raw = self.rfile.readline(MAX_CONTROL_LINE + 1)
        if not raw or len(raw) > MAX_CONTROL_LINE:
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ControlError("request must be a JSON object")
            token = payload.pop("token", None)
            if not isinstance(token, str) or not hmac.compare_digest(token, server.token):
                raise ControlError("authentication failed")
            result = server.broker.dispatch(payload)
            response: dict[str, Any] = {"ok": True, "result": result}
        except (json.JSONDecodeError, UnicodeDecodeError, ControlError) as error:
            response = {"ok": False, "error": str(error)}
        self.wfile.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Unix socket server carrying the QSF side-channel."""

    daemon_threads = True

    def __init__(self, path: Path, token: str, broker: Broker) -> None:
        self.token = token
        self.broker = broker
        super().__init__(str(path), ControlRequestHandler)


def read_existing_token(path: Path) -> str:
    """Read a session token without following aliases or weak permissions."""
    if not os.path.lexists(path):
        raise ControlError("token file does not exist")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ControlError("existing token file must be a regular file owned by this user")
    if metadata.st_mode & 0o077:
        raise ControlError("existing token file must have mode 0600")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ControlError("cannot securely open existing token file") from error
    with os.fdopen(descriptor, "r", encoding="ascii") as file:
        token = file.read().strip()
    if re.fullmatch(r"[0-9a-f]{64}", token):
        return token
    raise ControlError("existing token file has invalid contents")


def read_or_create_token(path: Path) -> str:
    if os.path.lexists(path):
        return read_existing_token(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as file:
        file.write(token + "\n")
    return token


def serve(arguments: argparse.Namespace) -> int:
    control_socket = Path(arguments.control_socket)
    token_file = Path(arguments.token_file)
    if control_socket.exists():
        if not control_socket.is_socket():
            raise ControlError("control socket path exists and is not a socket")
        control_socket.unlink()
    control_socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = read_or_create_token(token_file)
    broker = Broker(Path(arguments.agent_socket), not arguments.no_qemu_resize)
    server = ControlServer(control_socket, token, broker)
    os.chmod(control_socket, 0o600)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        broker.close()
        try:
            control_socket.unlink()
        except FileNotFoundError:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-socket", required=True, help="QEMU virtio-serial Unix socket")
    parser.add_argument("--control-socket", required=True, help="local QSF control Unix socket")
    parser.add_argument("--token-file", required=True, help="0600 per-session token file")
    parser.add_argument("--no-qemu-resize", action="store_true", help="do not call QEMU Console.SetUIInfo")
    arguments = parser.parse_args()
    try:
        return serve(arguments)
    except ControlError as error:
        print(f"qsf-control: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
