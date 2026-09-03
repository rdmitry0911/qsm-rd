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
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as xml_etree
from collections import deque
from pathlib import Path
from typing import Any

# The package invokes this file directly, and the unit suite imports it by
# file location.  Keep its sibling encoder policy module discoverable in both
# cases without making the project depend on an ambient PYTHONPATH.
_MODULE_DIRECTORY = Path(__file__).resolve().parent
if str(_MODULE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIRECTORY))
from q_sunshine_encoder_probe import EncoderProbeError, select_h264_encoder


MAX_CLIPBOARD_BYTES = 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CONTROL_LINE = 4 * 1024 * 1024
MAX_AGENT_LINE = 4 * 1024 * 1024
# A normal command on the guest virtio-serial channel has its own bounded
# response window.  Keep the historical public name for callers such as the
# local control service, but do not use it as the budget for a multi-step
# display-profile transaction below.
AGENT_REQUEST_TIMEOUT_SECONDS = 35.0
REQUEST_TIMEOUT_SECONDS = AGENT_REQUEST_TIMEOUT_SECONDS
# The guest may spend as long as 60 seconds restarting its compositor and
# proving a new VirGL scanout.  connection_optimize consists of several agent
# round trips plus an optional Sunshine probe and QEMU SetUIInfo call, so all
# of them share one absolute deadline with a small scheduling margin.  Giving
# every hop a fresh 35-second timer would otherwise permit an unbounded chain
# and let an older profile race a newer one.
PROFILE_TRANSACTION_TIMEOUT_SECONDS = 75.0
QEMU_SET_UI_INFO_TIMEOUT_SECONDS = 5.0
SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
CAPABILITY_PROTOCOL_VERSION = 2
MIN_CAPABILITY_WIDTH = 64
MAX_CAPABILITY_WIDTH = 16384
MIN_CAPABILITY_HEIGHT = 64
MAX_CAPABILITY_HEIGHT = 16384
MIN_CAPABILITY_FPS = 10
MAX_CAPABILITY_FPS = 240
MIN_CAPABILITY_BITRATE_KBPS = 500
MAX_CAPABILITY_BITRATE_KBPS = 500000
SUPPORTED_VIDEO_CODECS = frozenset({"H264", "HEVC", "AV1"})
SUNSHINE_SERVERINFO_MAX_BYTES = 256 * 1024
SUNSHINE_SERVERINFO_TIMEOUT_SECONDS = 3


class ControlError(RuntimeError):
    """A request rejected by the QSF control boundary."""


def audit_connection_profile(stage: str, **fields: object) -> None:
    """Emit bounded, non-secret negotiation evidence for an operator trace.

    Clipboard text, file names/data, the local token, TLS material, and QEMU
    replies are deliberately excluded. A selected virtual scanout and codec
    are necessary to diagnose a user-requested profile transition, including
    the fullscreen handoff, and are not credentials.
    """
    rendered = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"qsf-control: connection-profile stage={stage} {rendered}",
          file=sys.stderr, flush=True)


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


def _bounded_environment_int(name: str, minimum: int, maximum: int, default: int) -> int:
    """Read an intentional operator cap, never a free-form capability value."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    if not value.isascii() or not value.isdecimal():
        raise ControlError(f"{name} must be a decimal integer in {minimum}..{maximum}")
    parsed = int(value, 10)
    if not minimum <= parsed <= maximum:
        raise ControlError(f"{name} must be in {minimum}..{maximum}")
    return parsed


def _codec_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(SUPPORTED_VIDEO_CODECS):
        raise ControlError(f"{label} must contain one to three codec names")
    codecs: list[str] = []
    for codec in value:
        if not isinstance(codec, str) or codec not in SUPPORTED_VIDEO_CODECS or codec in codecs:
            raise ControlError(f"{label} contains an unsupported or duplicate codec")
        codecs.append(codec)
    return tuple(codecs)


def _codec_list_environment(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    if not value.isascii():
        raise ControlError(f"{name} must be comma-separated ASCII codec names")
    return _codec_list(value.split(","), name)


def _require_bounded_object_integer(payload: dict[str, Any], name: str,
                                    minimum: int, maximum: int) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ControlError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def client_stream_capabilities(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the bounded receiver information supplied by the Qt client."""
    client = payload.get("client")
    if not isinstance(client, dict):
        raise ControlError("connection_optimize requires a client capability object")
    return {
        # This is the size explicitly selected in the client UI. It is an
        # upper bound for the virtual guest scanout, not a host guess.
        "requested_width": _require_bounded_object_integer(
            client, "requested_width", MIN_CAPABILITY_WIDTH, MAX_CAPABILITY_WIDTH),
        "requested_height": _require_bounded_object_integer(
            client, "requested_height", MIN_CAPABILITY_HEIGHT, MAX_CAPABILITY_HEIGHT),
        "max_fps": _require_bounded_object_integer(
            client, "max_fps", MIN_CAPABILITY_FPS, MAX_CAPABILITY_FPS),
        # Codec names are a capability claim only; no OS/GPU model, driver,
        # screen identifier, or raw display inventory crosses this boundary.
        "decoder_codecs": _codec_list(client.get("decoder_codecs"), "client.decoder_codecs"),
    }


def _serverinfo_codec_mask(xml_payload: bytes) -> int:
    """Extract Sunshine's published, actual encoder codec mask from XML."""
    try:
        root = xml_etree.fromstring(xml_payload)
    except xml_etree.ParseError as error:
        raise ControlError("Sunshine /serverinfo returned invalid XML") from error
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "ServerCodecModeSupport":
            continue
        value = (element.text or "").strip()
        if not value.isascii() or not value.isdecimal():
            break
        return int(value, 10)
    raise ControlError("Sunshine /serverinfo did not publish ServerCodecModeSupport")


def _local_serverinfo_url() -> str | None:
    """Accept only a root-configured loopback /serverinfo endpoint.

    The broker never follows a client-supplied URL.  Keeping this request on
    loopback also avoids turning an optimisation click into a generic SSRF
    primitive when an operator makes a typo in the instance environment.
    """
    value = os.environ.get("QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL", "").strip()
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or \
            parsed.query or parsed.fragment or parsed.path != "/serverinfo" or \
            parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ControlError(
            "QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL must be a loopback http(s) /serverinfo URL")
    return value


def _remaining_deadline_seconds(deadline: float, error: str) -> float:
    """Return the remaining monotonic budget or fail before starting work."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ControlError(error)
    return remaining


def _sunshine_serverinfo_encoder_codecs(
        *, deadline: float | None = None) -> tuple[str, ...] | None:
    """Query only Sunshine's proven codec mask, never throughput guesses."""
    url = _local_serverinfo_url()
    if url is None:
        return None
    context: ssl.SSLContext | None = None
    if url.startswith("https:"):
        ca_file = os.environ.get("QSUNSHINE_QSF_SUNSHINE_SERVERINFO_CA_FILE", "").strip()
        if not ca_file:
            raise ControlError(
                "QSUNSHINE_QSF_SUNSHINE_SERVERINFO_CA_FILE is required for an HTTPS /serverinfo probe")
        try:
            context = ssl.create_default_context(cafile=ca_file)
        except (OSError, ssl.SSLError) as error:
            raise ControlError("cannot load the Sunshine /serverinfo CA file") from error
    timeout = SUNSHINE_SERVERINFO_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, _remaining_deadline_seconds(
            deadline, "connection profile transaction timed out"))
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            payload = response.read(SUNSHINE_SERVERINFO_MAX_BYTES + 1)
    except (OSError, urllib.error.URLError, ssl.SSLError) as error:
        raise ControlError("cannot query Sunshine /serverinfo encoder capabilities") from error
    if len(payload) > SUNSHINE_SERVERINFO_MAX_BYTES:
        raise ControlError("Sunshine /serverinfo response exceeds its size limit")
    mode_support = _serverinfo_codec_mask(payload)
    codecs: list[str] = []
    # These constants are the public moonlight-common ServerCodecModeSupport
    # bits. They indicate codecs Sunshine actually accepted in its encoder
    # probe; they do not state a maximum resolution, FPS, or bitrate.
    if mode_support & 0x00000001:
        codecs.append("H264")
    if mode_support & 0x00000100:
        codecs.append("HEVC")
    if mode_support & (0x00010000 | 0x00020000):
        codecs.append("AV1")
    if not codecs:
        raise ControlError("Sunshine /serverinfo reported no usable encoder codec")
    return tuple(codecs)


def detected_host_encoder_capabilities(
        *, deadline: float | None = None) -> dict[str, Any]:
    """Read the explicit host envelope and a proven direct encoder backend.

    The primary browser route probes its own H.264 encoder, so it does not
    depend on a Sunshine HTTP endpoint or turn a GPU device node into an
    unsupported claim.  ``sunshine_compat`` preserves the former native
    GameStream diagnostic path while it remains available as a compatibility
    transport; it is never selected implicitly.
    """
    envelope = {
        "max_width": _bounded_environment_int("QSUNSHINE_QSF_HOST_MAX_WIDTH",
                                               MIN_CAPABILITY_WIDTH, MAX_CAPABILITY_WIDTH,
                                               1920),
        "max_height": _bounded_environment_int("QSUNSHINE_QSF_HOST_MAX_HEIGHT",
                                                MIN_CAPABILITY_HEIGHT, MAX_CAPABILITY_HEIGHT,
                                                1080),
        "max_fps": _bounded_environment_int("QSUNSHINE_QSF_HOST_MAX_FPS",
                                             MIN_CAPABILITY_FPS, MAX_CAPABILITY_FPS, 60),
        "max_bitrate_kbps": _bounded_environment_int(
            "QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS", MIN_CAPABILITY_BITRATE_KBPS,
            MAX_CAPABILITY_BITRATE_KBPS, 18000),
        "encoder_codecs": _codec_list_environment(
            "QSUNSHINE_QSF_HOST_ENCODER_CODECS", ("H264",)),
    }
    mode = os.environ.get("QSUNSHINE_QSF_ENCODER_PROBE", "direct")
    if mode == "direct":
        try:
            timeout = None if deadline is None else _remaining_deadline_seconds(
                deadline, "connection profile transaction timed out")
            selection = select_h264_encoder(timeout_seconds=timeout)
        except EncoderProbeError as error:
            raise ControlError("no direct H.264 encoder is available on this host") from error
        envelope["encoder_codecs"] = tuple(
            codec for codec in envelope["encoder_codecs"] if codec == "H264")
        if not envelope["encoder_codecs"]:
            raise ControlError("direct browser transport requires H264 in the host envelope")
        envelope["encoder_backend"] = selection.name
        envelope["encoder_hardware"] = selection.hardware
    elif mode == "sunshine_compat":
        probed_codecs = _sunshine_serverinfo_encoder_codecs(deadline=deadline)
        if probed_codecs is not None:
            envelope["encoder_codecs"] = tuple(
                codec for codec in envelope["encoder_codecs"] if codec in probed_codecs)
            if not envelope["encoder_codecs"]:
                raise ControlError(
                    "the tested host encoder envelope and Sunshine /serverinfo have no common codec")
        envelope["encoder_backend"] = "sunshine_compat"
        envelope["encoder_hardware"] = None
    elif mode == "static":
        # Fixture-only / offline administrative mode.  It intentionally
        # retains an explicit envelope but makes no hardware assertion.
        envelope["encoder_backend"] = "static"
        envelope["encoder_hardware"] = None
    else:
        raise ControlError(
            "QSUNSHINE_QSF_ENCODER_PROBE must be direct, sunshine_compat, or static")
    return envelope


def _recommended_bitrate_kbps(width: int, height: int, fps: int, maximum: int) -> int:
    """Choose a bounded quality target from the resolved stream geometry."""
    pixels_per_second = width * height * fps
    if pixels_per_second >= 3840 * 2160 * 50:
        preferred = 45000
    elif pixels_per_second >= 2560 * 1440 * 50:
        preferred = 28000
    elif pixels_per_second >= 1920 * 1080 * 50:
        preferred = 18000
    elif pixels_per_second >= 1600 * 900 * 50:
        preferred = 12000
    else:
        preferred = 8000
    return max(MIN_CAPABILITY_BITRATE_KBPS, min(maximum, preferred))


def _fit_resolution(width: int, height: int, maximum_width: int,
                    maximum_height: int) -> tuple[int, int]:
    """Preserve the user-selected aspect ratio when a pair has a lower cap."""
    scale = min(1.0, maximum_width / width, maximum_height / height)
    fitted_width = max(MIN_CAPABILITY_WIDTH, int(width * scale))
    fitted_height = max(MIN_CAPABILITY_HEIGHT, int(height * scale))
    # Every GameStream codec we advertise needs even luma dimensions. Keep
    # them inside the negotiated bounds after rounding down.
    fitted_width = min(maximum_width, fitted_width - (fitted_width % 2))
    fitted_height = min(maximum_height, fitted_height - (fitted_height % 2))
    if fitted_width < MIN_CAPABILITY_WIDTH or fitted_height < MIN_CAPABILITY_HEIGHT:
        raise ControlError("the host/guest display envelope cannot represent the requested stream size")
    return fitted_width, fitted_height


class AgentChannel:
    """Serialize requests to a guest virtio-serial connection that may restart.

    A terminal worker and its QSF controller are deliberately independent of
    the VM lifecycle.  A guest reboot therefore closes the Unix chardev while
    the controller process remains healthy.  Conversely, a terminal worker
    restart disconnects the guest endpoint without changing its VM.  Treat
    that socket as a reconnectable transport, but never replay a command once
    it has been written: clipboard/file/profile mutations retain at-most-once
    semantics and the caller gets a clear retryable failure instead.
    """

    _RECONNECT_PAUSE_SECONDS = 0.10
    _READ_TIMEOUT_SECONDS = 1.0

    def __init__(self, path: Path, on_clipboard: callable) -> None:
        self._path = path
        self._on_clipboard = on_clipboard
        self._stopped = threading.Event()
        self._responses: deque[str] = deque()
        self._condition = threading.Condition()
        self._request_lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._generation = 0
        self._ready_generation: int | None = None

    def close(self) -> None:
        self._stopped.set()
        with self._condition:
            channel = self._socket
            reader = self._reader
            self._socket = None
            self._reader = None
            self._generation += 1
            self._ready_generation = None
            self._responses.clear()
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
        """Forget exactly one transport generation and wake waiting requests."""
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

    def _read_loop(self, channel: socket.socket, generation: int) -> None:
        buffer = bytearray()
        try:
            while not self._stopped.is_set():
                try:
                    chunk = channel.recv(65536)
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
                    if line == "READY QSF1":
                        with self._condition:
                            if self._socket is channel and self._generation == generation:
                                self._ready_generation = generation
                                self._condition.notify_all()
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
                        if self._socket is channel and self._generation == generation:
                            self._responses.append(line)
                            self._condition.notify_all()
        except (OSError, ControlError):
            pass
        finally:
            self._disconnect(channel, generation)

    def _wait_for_ready(self, channel: socket.socket, generation: int,
                        deadline: float, timeout_error: str) -> bool:
        """Wait for the guest's fixed protocol greeting on this connection."""
        with self._condition:
            while True:
                if self._stopped.is_set():
                    raise ControlError("guest agent transport is stopped")
                if self._socket is not channel or self._generation != generation:
                    return False
                if self._ready_generation == generation:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlError(timeout_error)
                self._condition.wait(remaining)

    def _connect(self, deadline: float, timeout_error: str) -> tuple[socket.socket, int]:
        """Return a live, greeted transport or wait only within the request budget."""
        while True:
            if self._stopped.is_set():
                raise ControlError("guest agent transport is stopped")
            with self._condition:
                existing = self._socket
                existing_generation = self._generation
            if existing is not None:
                if self._wait_for_ready(existing, existing_generation, deadline, timeout_error):
                    return existing, existing_generation
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlError(timeout_error)
            candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                candidate.settimeout(min(self._READ_TIMEOUT_SECONDS, remaining))
                candidate.connect(str(self._path))
                candidate.settimeout(self._READ_TIMEOUT_SECONDS)
            except OSError:
                candidate.close()
                with self._condition:
                    if self._stopped.is_set():
                        raise ControlError("guest agent transport is stopped")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ControlError(timeout_error)
                    self._condition.wait(min(self._RECONNECT_PAUSE_SECONDS, remaining))
                continue

            with self._condition:
                if self._stopped.is_set():
                    should_close = True
                elif self._socket is not None:
                    should_close = True
                else:
                    self._generation += 1
                    generation = self._generation
                    self._socket = candidate
                    self._responses.clear()
                    self._ready_generation = None
                    reader = threading.Thread(target=self._read_loop,
                                              args=(candidate, generation),
                                              name="qsf-agent-reader", daemon=True)
                    self._reader = reader
                    reader.start()
                    should_close = False
            if should_close:
                candidate.close()
                continue
            if self._wait_for_ready(candidate, generation, deadline, timeout_error):
                return candidate, generation

    def request(self, command: str, expected_prefix: str,
                *, deadline: float | None = None) -> str:
        """Send one serialized command before an optional absolute deadline.

        A profile transaction deliberately passes the same monotonic deadline
        to every command.  Acquiring this channel's ordinary request lock is
        part of that budget too: a busy clipboard poll must not extend a
        display reconfiguration past the lifetime reported to the client.
        """
        if "\n" in command or "\r" in command:
            raise ControlError("invalid agent command")
        if deadline is None:
            deadline = time.monotonic() + AGENT_REQUEST_TIMEOUT_SECONDS
            timeout_error = "timed out waiting for guest agent"
        else:
            timeout_error = "connection profile transaction timed out"
        lock_wait = _remaining_deadline_seconds(deadline, timeout_error)
        if not self._request_lock.acquire(timeout=lock_wait):
            raise ControlError(timeout_error)
        try:
            channel, generation = self._connect(deadline, timeout_error)
            try:
                channel.sendall(command.encode("ascii") + b"\n")
            except OSError as error:
                self._disconnect(channel, generation)
                # A Unix stream may have accepted a prefix of the command
                # before reporting an error.  Do not replay a potentially
                # mutating request; a later user action can establish a fresh
                # greeted channel safely.
                raise ControlError("guest agent transport interrupted; retry operation") from error
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
                    if self._socket is not channel or self._generation != generation:
                        raise ControlError("guest agent transport interrupted; retry operation")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ControlError(timeout_error)
                    self._condition.wait(remaining)
        finally:
            self._request_lock.release()


class Broker:
    """Owns the guest channel and dispatches authenticated client operations."""

    def __init__(self, agent_socket: Path, enable_qemu_resize: bool) -> None:
        self._clipboard_lock = threading.Lock()
        self._clipboard = ""
        self._clipboard_generation = 0
        self._agent = AgentChannel(agent_socket, self._set_guest_clipboard)
        self._enable_qemu_resize = enable_qemu_resize
        # AgentChannel only serializes an individual virtio-serial request.
        # A display change is a larger state transition (guest capabilities,
        # QEMU scanout, commit, and compositor acknowledgement), so protect
        # it with one independent lock.  This also prevents legacy resize
        # requests from replacing the scanout in the middle of a negotiated
        # connection profile.
        self._display_transaction_lock = threading.Lock()

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

    def _set_qemu_ui_info(self, width: int, height: int,
                          *, deadline: float | None = None) -> str:
        if not self._enable_qemu_resize:
            return "disabled"
        command = [
            "busctl", "--user", "call", "org.qemu",
            "/org/qemu/Display1/Console_0", "org.qemu.Display1.Console",
            "SetUIInfo", "qqiiuu",
            str(self._mm_for_pixels(width)), str(self._mm_for_pixels(height)),
            "0", "0", str(width), str(height),
        ]
        timeout = QEMU_SET_UI_INFO_TIMEOUT_SECONDS
        if deadline is not None:
            timeout = min(timeout, _remaining_deadline_seconds(
                deadline, "connection profile transaction timed out"))
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                env=os.environ.copy(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ControlError("QEMU SetUIInfo could not be invoked") from error
        if result.returncode != 0:
            detail = result.stdout.strip().replace("\n", " ")[:300]
            raise ControlError(f"QEMU SetUIInfo failed: {detail}")
        return "applied"

    @staticmethod
    def _guest_display_capabilities(response: str) -> dict[str, int]:
        fields = response.split(" ")
        if len(fields) != 5 or fields[0] != "GUEST_CAPABILITIES_REQUEST" or \
                fields[1] != str(CAPABILITY_PROTOCOL_VERSION):
            raise ControlError("guest agent returned malformed display capabilities")
        try:
            if any(not value.isascii() or not value.isdecimal() for value in fields[2:]):
                raise ValueError
            width, height, fps = (int(fields[2], 10), int(fields[3], 10), int(fields[4], 10))
        except ValueError as error:
            raise ControlError("guest agent returned non-numeric display capabilities") from error
        if not (MIN_CAPABILITY_WIDTH <= width <= MAX_CAPABILITY_WIDTH and
                MIN_CAPABILITY_HEIGHT <= height <= MAX_CAPABILITY_HEIGHT and
                MIN_CAPABILITY_FPS <= fps <= MAX_CAPABILITY_FPS):
            raise ControlError("guest agent returned display capabilities outside the protocol range")
        return {"max_width": width, "max_height": height, "max_fps": fps}

    @staticmethod
    def _connection_profile(response: str, prefix: str,
                            pair_capabilities: dict[str, Any]) -> dict[str, Any]:
        fields = response.split(" ")
        if len(fields) != 8 or fields[0] != prefix:
            raise ControlError("guest agent returned a malformed connection profile")
        version, generation_text, width_text, height_text, fps_text, bitrate_text, codec = fields[1:]
        if version != str(CAPABILITY_PROTOCOL_VERSION) or codec not in SUPPORTED_VIDEO_CODECS:
            raise ControlError("guest agent returned an unsupported connection profile")
        try:
            if any(not value.isascii() or not value.isdecimal()
                   for value in (generation_text, width_text, height_text, fps_text, bitrate_text)):
                raise ValueError
            generation, width, height, fps, bitrate_kbps = (
                int(generation_text, 10), int(width_text, 10), int(height_text, 10),
                int(fps_text, 10), int(bitrate_text, 10)
            )
        except ValueError as error:
            raise ControlError("guest agent returned non-numeric connection profile limits") from error
        if not (1 <= generation <= (2 ** 64 - 1) and
                width == pair_capabilities["max_width"] and
                height == pair_capabilities["max_height"] and
                fps == pair_capabilities["max_fps"] and
                bitrate_kbps == pair_capabilities["max_bitrate_kbps"] and
                codec == pair_capabilities["video_codec"]):
            raise ControlError("guest agent returned a profile outside the negotiated pair capability")
        return {
            "version": CAPABILITY_PROTOCOL_VERSION,
            "generation": generation,
            "width": width,
            "height": height,
            "fps": fps,
            "bitrate_kbps": bitrate_kbps,
            # The public JSON spelling is exactly what stock Moonlight expects.
            "video_codec": "H.264" if codec == "H264" else codec,
        }

    @staticmethod
    def _profile_command(command: str, profile: dict[str, Any]) -> str:
        """Build an exact bounded command from an already validated profile."""
        codec = "H264" if profile["video_codec"] == "H.264" else profile["video_codec"]
        return (
            f"{command} {CAPABILITY_PROTOCOL_VERSION} {profile['generation']} "
            f"{profile['width']} {profile['height']} {profile['fps']} "
            f"{profile['bitrate_kbps']} {codec}"
        )

    def _acquire_display_transaction(self) -> None:
        """Fail fast instead of allowing two display generations to overlap."""
        if not self._display_transaction_lock.acquire(blocking=False):
            raise ControlError("another display profile transaction is already in progress")

    def optimize_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve client decoder, host encoder, and VirGL scanout constraints.

        The lock spans every display-affecting operation.  In particular, the
        previous AgentChannel lock was released between PAIR, SetUIInfo,
        COMMIT, and AWAIT, which allowed another optimize or legacy resize to
        replace the exact guest profile being acknowledged.
        """
        client = client_stream_capabilities(payload)
        self._acquire_display_transaction()
        deadline = time.monotonic() + PROFILE_TRANSACTION_TIMEOUT_SECONDS
        try:
            audit_connection_profile(
                "requested",
                requested=f"{client['requested_width']}x{client['requested_height']}",
                max_fps=client["max_fps"], codecs=",".join(client["decoder_codecs"]),
            )
            request = self._agent.request(
                "CONNECTION_OPTIMIZE", "GUEST_CAPABILITIES_REQUEST ", deadline=deadline)
            guest = self._guest_display_capabilities(request)
            host = detected_host_encoder_capabilities(deadline=deadline)
            common_codecs = set(client["decoder_codecs"]).intersection(host["encoder_codecs"])
            codec = next((candidate for candidate in ("AV1", "HEVC", "H264")
                          if candidate in common_codecs), None)
            if codec is None:
                raise ControlError("client decoder and host encoder have no common codec")
            width, height = _fit_resolution(
                client["requested_width"], client["requested_height"],
                min(host["max_width"], guest["max_width"]),
                min(host["max_height"], guest["max_height"]),
            )
            pair_capabilities = {
                "max_width": width,
                "max_height": height,
                "max_fps": min(client["max_fps"], host["max_fps"], guest["max_fps"]),
                "max_bitrate_kbps": 0,
                "video_codec": codec,
            }
            pair_capabilities["max_bitrate_kbps"] = _recommended_bitrate_kbps(
                width, height, pair_capabilities["max_fps"], host["max_bitrate_kbps"])
            accepted_response = self._agent.request(
                "PAIR_CAPABILITIES "
                f"{CAPABILITY_PROTOCOL_VERSION} {pair_capabilities['max_width']} "
                f"{pair_capabilities['max_height']} {pair_capabilities['max_fps']} "
                f"{pair_capabilities['max_bitrate_kbps']} {pair_capabilities['video_codec']}",
                "CONNECTION_PROFILE_ACCEPTED ", deadline=deadline,
            )
            accepted = self._connection_profile(
                accepted_response, "CONNECTION_PROFILE_ACCEPTED", pair_capabilities)
            audit_connection_profile(
                "accepted",
                generation=accepted["generation"],
                resolution=f"{accepted['width']}x{accepted['height']}",
                fps=accepted["fps"], bitrate_kbps=accepted["bitrate_kbps"],
                codec=accepted["video_codec"],
            )
            # SetUIInfo is advisory to QEMU, but it must be requested before
            # the guest desktop starts applying the profile.  The following
            # explicit acknowledgement is authoritative: it comes only after
            # the guest's actual VirGL scanout/compositor adapter completed.
            qemu_set_ui_info = self._set_qemu_ui_info(
                accepted["width"], accepted["height"], deadline=deadline)
            audit_connection_profile(
                "qemu-requested",
                generation=accepted["generation"],
                resolution=f"{accepted['width']}x{accepted['height']}",
                qemu_set_ui_info=qemu_set_ui_info,
            )
            pending_response = self._agent.request(
                self._profile_command("COMMIT_CONNECTION_PROFILE", accepted),
                "CONNECTION_PROFILE_PENDING ", deadline=deadline,
            )
            pending = self._connection_profile(
                pending_response, "CONNECTION_PROFILE_PENDING", pair_capabilities)
            if pending != accepted:
                raise ControlError("guest agent changed a profile between acceptance and commit")
            applied_response = self._agent.request(
                self._profile_command("AWAIT_CONNECTION_PROFILE", accepted),
                "CONNECTION_PROFILE ", deadline=deadline,
            )
            profile = self._connection_profile(
                applied_response, "CONNECTION_PROFILE", pair_capabilities)
            if profile != accepted:
                raise ControlError("guest agent acknowledged a different applied profile")
            audit_connection_profile(
                "guest-applied",
                generation=profile["generation"],
                resolution=f"{profile['width']}x{profile['height']}",
                fps=profile["fps"], bitrate_kbps=profile["bitrate_kbps"],
                codec=profile["video_codec"],
            )
            profile["requested_width"] = client["requested_width"]
            profile["requested_height"] = client["requested_height"]
            # JSON numbers cannot represent every uint64_t exactly in Qt/JS.
            # Keep the transaction identifier textual at the public boundary.
            profile["guest_profile_generation"] = str(profile.pop("generation"))
            profile["qemu_set_ui_info"] = qemu_set_ui_info
            profile["encoder_backend"] = host["encoder_backend"]
            profile["encoder_hardware"] = host["encoder_hardware"]
            return profile
        finally:
            self._display_transaction_lock.release()

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
            self._acquire_display_transaction()
            try:
                self._agent.request(f"RESIZE {width} {height}", "OK RESIZE")
                qemu = self._set_qemu_ui_info(width, height)
                return {"width": width, "height": height, "qemu_set_ui_info": qemu}
            finally:
                self._display_transaction_lock.release()
        if operation == "connection_optimize":
            return self.optimize_connection(payload)
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
