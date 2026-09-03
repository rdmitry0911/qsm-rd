#!/usr/bin/env python3
"""PVE-authorized QEMU Display1 to browser-WebRTC terminal service.

This is the standalone direct transport. It has no pairing database, PIN,
external TLS endpoint, or PVE credential parser. PVE performs its ordinary authenticated
``VM.Console`` ACL check and gives this process exactly one VM-scoped SDP
offer over a root-only local Unix socket.  The response is the WebRTC answer;
media is DTLS-SRTP directly between the browser and this PVE node.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_DIRECTORY = Path(__file__).resolve().parents[1]
if str(PACKAGE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIRECTORY))

from browser_bridge.qsm_browser_bridge import BrowserWebRtcBridge, BridgeError  # noqa: E402


MIN_VMID = 100
MAX_VMID = 999_999_999
MAX_SDP_BYTES = 128 * 1024
MAX_REQUEST_BYTES = MAX_SDP_BYTES + 4096
REQUEST_TIMEOUT_SECONDS = 15.0
SESSION_IDLE_SECONDS = 10 * 60
SESSION_WATCH_SECONDS = 0.25
MAX_SESSIONS = 16
PVE_CONFIG_MAX_BYTES = 256 * 1024
PVE_CONFIG_RECONCILE_SECONDS = 0.25
PVE_OPERATION = "pve_acl_webrtc"
PROTOCOL_VERSION = 1
_NODE_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.-]{0,62}\Z")
_SUBJECT_PATTERN = re.compile(r"\A[^\s\x00]{1,64}\Z")
_VMID_PATTERN = re.compile(r"\A[1-9][0-9]{1,8}\Z")
_ENCODER_PATTERN = re.compile(r"\A(?:h264_nvenc|h264_qsv|h264_vaapi|libx264)\Z")
_RENDER_NODE_PATTERN = re.compile(r"\A/dev/dri/renderD[0-9]{1,4}\Z")
_PVE_VM_CONFIG_PATTERN = re.compile(r"\A([1-9][0-9]{1,8})\.conf\Z")
_ENVIRONMENT_KEYS = frozenset({
    "QSM_DIRECT_QEMU_DBUS_ADDRESS",
    "QSM_DIRECT_ENCODER",
    "QSM_DIRECT_VAAPI_RENDER_NODE",
})


class DirectTerminalError(RuntimeError):
    """A deliberately non-sensitive direct-terminal failure."""


def _valid_vmid(value: object) -> bool:
    return type(value) is int and MIN_VMID <= value <= MAX_VMID


def _read_root_file(path: Path, *, maximum: int = MAX_REQUEST_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DirectTerminalError("direct-terminal configuration is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or
                (os.geteuid() == 0 and metadata.st_uid != 0) or
                metadata.st_size < 0 or metadata.st_size > maximum):
            raise DirectTerminalError("direct-terminal configuration is unsafe")
        content = bytearray()
        while len(content) <= maximum:
            block = os.read(descriptor, maximum + 1 - len(content))
            if not block:
                break
            content.extend(block)
        if len(content) > maximum:
            raise DirectTerminalError("direct-terminal configuration is too large")
        return bytes(content)
    finally:
        os.close(descriptor)


def _load_instance(instance_directory: Path, vmid: int, vm_runtime_directory: Path) -> dict[str, str]:
    if not _valid_vmid(vmid) or not instance_directory.is_absolute() or not vm_runtime_directory.is_absolute():
        raise DirectTerminalError("direct-terminal VM policy is invalid")
    content = _read_root_file(instance_directory / f"{vmid}.conf", maximum=16 * 1024)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DirectTerminalError("direct-terminal VM policy is invalid") from error
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DirectTerminalError("direct-terminal VM policy is invalid")
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in _ENVIRONMENT_KEYS or key in values or "\x00" in value or len(value) > 4096:
            raise DirectTerminalError("direct-terminal VM policy is invalid")
        values[key] = value
    expected_bus = f"unix:path={vm_runtime_directory}/{vmid}/qemu-display1.bus"
    if values.get("QSM_DIRECT_QEMU_DBUS_ADDRESS") != expected_bus:
        raise DirectTerminalError("direct-terminal VM has an invalid Display1 address")
    encoder = values.get("QSM_DIRECT_ENCODER", "")
    if encoder and not _ENCODER_PATTERN.fullmatch(encoder):
        raise DirectTerminalError("direct-terminal VM has an invalid encoder")
    render_node = values.get("QSM_DIRECT_VAAPI_RENDER_NODE", "")
    if render_node and not _RENDER_NODE_PATTERN.fullmatch(render_node):
        raise DirectTerminalError("direct-terminal VM has an invalid render node")
    if encoder == "h264_vaapi" and not render_node:
        raise DirectTerminalError("direct-terminal VA-API encoder lacks a render node")
    return values


def _load_optional_instance(instance_directory: Path, vmid: int,
                            vm_runtime_directory: Path) -> dict[str, str]:
    """Load an optional root-owned codec policy, with a portable default.

    The PVE VM config is the source of truth for whether Display1 is enabled.
    A policy file only refines node-local encoder choices; requiring one before
    QEMU can start made the UI checkbox deceptively incomplete.
    """
    path = instance_directory / f"{vmid}.conf"
    try:
        path.lstat()
    except FileNotFoundError:
        return {
            "QSM_DIRECT_QEMU_DBUS_ADDRESS":
                f"unix:path={vm_runtime_directory}/{vmid}/qemu-display1.bus",
        }
    return _load_instance(instance_directory, vmid, vm_runtime_directory)


def _read_pve_vm_config(path: Path) -> str | None:
    """Read one PVE-owned VM config without following a substituted path."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DirectTerminalError("direct-terminal VM configuration is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or
                (os.geteuid() == 0 and metadata.st_uid != 0) or
                metadata.st_mode & 0o002 or metadata.st_size < 0 or
                metadata.st_size > PVE_CONFIG_MAX_BYTES):
            raise DirectTerminalError("direct-terminal VM configuration is unsafe")
        content = bytearray()
        while len(content) <= PVE_CONFIG_MAX_BYTES:
            block = os.read(descriptor, PVE_CONFIG_MAX_BYTES + 1 - len(content))
            if not block:
                break
            content.extend(block)
        if len(content) > PVE_CONFIG_MAX_BYTES or b"\x00" in content:
            raise DirectTerminalError("direct-terminal VM configuration is unsafe")
        try:
            return bytes(content).decode("utf-8")
        except UnicodeDecodeError as error:
            raise DirectTerminalError("direct-terminal VM configuration is unsafe") from error
    finally:
        os.close(descriptor)


def _managed_display_enabled(config: str, vmid: int, vm_runtime_directory: Path) -> bool:
    """Accept the one GL Display1/GPU pair owned by this transport."""
    if not _valid_vmid(vmid):
        return False
    args: str | None = None
    for line in config.splitlines():
        if line.startswith("args:"):
            if args is not None:
                return False
            args = line.removeprefix("args:").strip()
    if args is None or len(args) > 8192 or any(ord(value) < 0x20 or ord(value) == 0x7f for value in args):
        return False
    address = re.escape(f"unix:path={vm_runtime_directory}/{vmid}/qemu-display1.bus")
    display = re.compile(
        rf"(?:^|\s)-display\s+dbus,addr={address},gl=on,rendernode=/dev/dri/renderD[0-9]{{1,4}}(?=\s|$)")
    count = len(re.findall(r"(?:^|\s)-display(?:\s|$)", args))
    gpu = re.compile(r"(?:^|\s)-device\s+virtio-vga-gl,id=qsm-direct-gpu(?=\s|$)")
    gpu_count = len(gpu.findall(args))
    return count == 1 and gpu_count == 1 and display.search(args) is not None


def _safe_runtime_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.stat()
    except OSError as error:
        raise DirectTerminalError("direct-terminal runtime directory is unavailable") from error
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077 or
            (os.geteuid() == 0 and metadata.st_uid != 0)):
        raise DirectTerminalError("direct-terminal runtime directory is unsafe")
    os.chmod(path, 0o700)


class DbusManager:
    """Own only the per-VM private Display1 buses required by QEMU."""

    def __init__(self, vm_runtime_directory: Path) -> None:
        self._root = vm_runtime_directory
        self._lock = threading.Lock()
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    @staticmethod
    def _socket_is_live(path: Path) -> bool:
        """Return whether a root-owned Display1 socket still has a listener.

        QEMU connects once to its private D-Bus during VM startup and does not
        reconnect if the bus disappears.  A terminal service restart must
        therefore adopt a still-live, safe bus rather than unlink it beneath
        an already running VM.  A successful Unix ``connect`` is sufficient:
        no D-Bus message is sent and no peer-controlled input is involved.
        """
        connection: socket.socket | None = None
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(0.2)
            connection.connect(os.fspath(path))
            return True
        except OSError:
            return False
        finally:
            if connection is not None:
                connection.close()

    def ensure(self, vmid: int, address: str) -> None:
        expected = self._root / str(vmid) / "qemu-display1.bus"
        if address != f"unix:path={expected}":
            raise DirectTerminalError("direct-terminal VM has an invalid Display1 address")
        with self._lock:
            _safe_runtime_directory(self._root)
            directory = expected.parent
            _safe_runtime_directory(directory)
            previous = self._children.get(vmid)
            if previous is not None and previous.poll() is None:
                try:
                    metadata = expected.lstat()
                    if stat.S_ISSOCK(metadata.st_mode) and metadata.st_mode & 0o077 == 0:
                        return
                except OSError:
                    pass
                self._terminate(previous)
            self._children.pop(vmid, None)
            try:
                metadata = expected.lstat()
                if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_mode & 0o077:
                    raise DirectTerminalError("direct-terminal Display1 socket is unsafe")
                if self._socket_is_live(expected):
                    # It belongs to an earlier terminal process. Keep it
                    # alive so an already-running QEMU retains `org.qemu`;
                    # this manager will use the same root-only bus.
                    return
                expected.unlink()
            except FileNotFoundError:
                pass
            process = subprocess.Popen(
                ["/usr/bin/dbus-daemon", "--session", "--nofork", "--nopidfile",
                 f"--address=unix:path={expected}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
                close_fds=True, start_new_session=True,
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise DirectTerminalError("direct-terminal Display1 bus did not start")
                try:
                    metadata = expected.lstat()
                    if stat.S_ISSOCK(metadata.st_mode):
                        os.chmod(expected, 0o700)
                        if expected.lstat().st_mode & 0o077 == 0:
                            self._children[vmid] = process
                            return
                except OSError:
                    pass
                time.sleep(0.05)
            self._terminate(process)
            raise DirectTerminalError("direct-terminal Display1 bus did not start")

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass

    def close(self) -> None:
        # Each daemon is intentionally retained across terminal restarts.
        # The dedicated root-only runtime directory is a tmpfs cleared at
        # boot, while PVE owns VM stop/start. Killing a live bus here would
        # make QEMU's Display1 endpoint permanently unavailable until the VM
        # itself is restarted. The systemd unit uses KillMode=process and
        # SIGINT so this main process reaches its normal session cleanup.
        with self._lock:
            self._children.clear()


@dataclass
class DirectSession:
    bridge: BrowserWebRtcBridge
    worker: subprocess.Popen[bytes]
    directory: Path
    expires_at: float


class DirectSessionManager:
    """Keep each one-off browser offer on one dedicated asyncio loop."""

    def __init__(self, *, instance_directory: Path, runtime_directory: Path,
                 vm_runtime_directory: Path, pve_config_directory: Path,
                 local_node: str | None) -> None:
        self._instance_directory = instance_directory
        self._runtime_directory = runtime_directory
        self._vm_runtime_directory = vm_runtime_directory
        self._pve_config_directory = pve_config_directory
        self._local_node = local_node
        self._dbus = DbusManager(vm_runtime_directory)
        self._reconcile_stop = threading.Event()
        self._start_configured_display_buses()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="qsm-direct-webrtc", daemon=True)
        self._sessions: dict[str, DirectSession] = {}
        self._closed = False
        self._thread.start()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_configured_display_buses,
            name="qsm-direct-display-reconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    def _configured_vms(self) -> tuple[int, ...]:
        """Return VMIDs whose PVE config has our exact Display1 argument."""
        try:
            entries = tuple(self._pve_config_directory.iterdir())
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise DirectTerminalError("direct-terminal VM configuration is unavailable") from error
        result: list[int] = []
        for entry in sorted(entries, key=lambda item: item.name):
            match = _PVE_VM_CONFIG_PATTERN.fullmatch(entry.name)
            if match is None:
                continue
            vmid = int(match.group(1))
            if not _valid_vmid(vmid):
                continue
            config = _read_pve_vm_config(entry)
            if config is not None and _managed_display_enabled(config, vmid, self._vm_runtime_directory):
                result.append(vmid)
        return tuple(result)

    def _display_is_configured(self, vmid: int) -> bool:
        if not _valid_vmid(vmid):
            return False
        config = _read_pve_vm_config(self._pve_config_directory / f"{vmid}.conf")
        return config is not None and _managed_display_enabled(config, vmid, self._vm_runtime_directory)

    def _start_configured_display_buses(self) -> None:
        """Create QEMU buses before the user starts a Display1-configured VM."""
        for vmid in self._configured_vms():
            policy = _load_optional_instance(
                self._instance_directory, vmid, self._vm_runtime_directory)
            self._dbus.ensure(vmid, policy["QSM_DIRECT_QEMU_DBUS_ADDRESS"])

    def _reconcile_configured_display_buses(self) -> None:
        """Pick up a saved Display setting without an operator service restart."""
        while not self._reconcile_stop.wait(PVE_CONFIG_RECONCILE_SECONDS):
            try:
                self._start_configured_display_buses()
            except DirectTerminalError as error:
                # A transient pmxcfs read while PVE updates a config must not
                # take down already running VMs or unrelated browser sessions.
                print(f"qsm-direct-terminal: Display1 reconcile deferred: {error}", file=sys.stderr)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @staticmethod
    def _request_value(payload: Any, name: str, expected: type) -> Any:
        value = payload.get(name) if isinstance(payload, dict) else None
        if type(value) is not expected:
            raise DirectTerminalError("direct-terminal request is invalid")
        return value

    def _validate_request(self, payload: Any) -> tuple[int, str, str, int, int, int]:
        if not isinstance(payload, dict) or set(payload) != {
                "version", "op", "node", "vmid", "subject", "sdp", "sdp_type", "width", "height", "fps"}:
            raise DirectTerminalError("direct-terminal request is invalid")
        if (payload["version"] != PROTOCOL_VERSION or payload["op"] != PVE_OPERATION or
                type(payload["vmid"]) is not int or not _valid_vmid(payload["vmid"]) or
                not isinstance(payload["node"], str) or not _NODE_PATTERN.fullmatch(payload["node"]) or
                not isinstance(payload["subject"], str) or not _SUBJECT_PATTERN.fullmatch(payload["subject"]) or
                not isinstance(payload["sdp"], str) or not payload["sdp"].isascii() or
                not 1 <= len(payload["sdp"].encode("ascii")) <= MAX_SDP_BYTES or
                payload["sdp_type"] != "offer"):
            raise DirectTerminalError("direct-terminal request is invalid")
        if self._local_node is not None and payload["node"] != self._local_node:
            raise DirectTerminalError("direct-terminal request is not local")
        dimensions: list[int] = []
        for name, minimum, maximum in (("width", 64, 16384), ("height", 64, 16384), ("fps", 10, 240)):
            value = payload[name]
            if type(value) is not int or not minimum <= value <= maximum:
                raise DirectTerminalError("direct-terminal request is invalid")
            dimensions.append(value)
        if dimensions[0] % 2 or dimensions[1] % 2:
            raise DirectTerminalError("direct-terminal request is invalid")
        return payload["vmid"], payload["sdp"], payload["node"], *dimensions

    @staticmethod
    def _child_environment() -> dict[str, str]:
        return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}

    async def _create(self, vmid: int, sdp: str, width: int, height: int, fps: int) -> dict[str, str]:
        self._collect_expired()
        if len(self._sessions) >= MAX_SESSIONS:
            raise DirectTerminalError("direct-terminal session capacity is exhausted")
        if not self._display_is_configured(vmid):
            raise DirectTerminalError("direct-terminal VM is not configured for Display1")
        policy = _load_optional_instance(self._instance_directory, vmid, self._vm_runtime_directory)
        self._dbus.ensure(vmid, policy["QSM_DIRECT_QEMU_DBUS_ADDRESS"])
        identifier = secrets.token_hex(16)
        directory = self._runtime_directory / f"vm-{vmid}" / identifier
        _safe_runtime_directory(directory.parent)
        _safe_runtime_directory(directory)
        bridge = BrowserWebRtcBridge(directory, fps=fps, expected_producer_uid=os.geteuid())
        worker: subprocess.Popen[bytes] | None = None
        try:
            video_socket, audio_socket = bridge.start_taps()
            encoder = policy.get("QSM_DIRECT_ENCODER") or "libx264"
            arguments = [
                "/usr/lib/qsm-pve-direct/bin/qsm-direct-media-worker",
                "--dbus-address", policy["QSM_DIRECT_QEMU_DBUS_ADDRESS"],
                "--video-socket", video_socket,
                "--audio-socket", audio_socket,
                "--input-socket", bridge.input_context,
                "--encoder", encoder,
                "--fps", str(fps),
                "--initial-size", f"{width}x{height}",
            ]
            if encoder == "h264_vaapi":
                arguments.extend(["--vaapi-device", policy["QSM_DIRECT_VAAPI_RENDER_NODE"]])
            worker = subprocess.Popen(
                # Worker diagnostics contain only local capture/encoder
                # failures; retaining them in the service journal is required
                # to distinguish a QEMU Display1 setup error from a rejected
                # PVE request. Browser SDP and PVE credentials are never
                # written by the worker.
                arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=None,
                env=self._child_environment(), close_fds=True, start_new_session=True,
            )
            await asyncio.sleep(0.12)
            if worker.poll() is not None:
                raise DirectTerminalError("direct-terminal media worker failed to start")
            answer = await bridge.answer_offer(sdp)
            self._sessions[identifier] = DirectSession(
                bridge=bridge, worker=worker, directory=directory,
                expires_at=time.monotonic() + SESSION_IDLE_SECONDS)
            # The worker exits when QEMU closes its Display1 connection (for
            # example, on a VM shutdown).  Watch it independently of new
            # browser requests so the associated WebRTC peer is closed at
            # once; _close_session is idempotent and also covers expiration.
            asyncio.create_task(self._watch_session(identifier))
            return answer
        except BaseException:
            if worker is not None:
                self._terminate_worker(worker)
            await bridge.close()
            self._remove_directory(directory)
            raise

    @staticmethod
    def _terminate_worker(worker: subprocess.Popen[bytes]) -> None:
        if worker.poll() is not None:
            return
        try:
            os.killpg(worker.pid, signal.SIGTERM)
            worker.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def _remove_directory(directory: Path) -> None:
        # The directory is service-created beneath a root-only runtime parent.
        # Only known sockets/files are removed; never recurse through a path
        # supplied by PVE or the browser.
        try:
            for entry in directory.iterdir():
                metadata = entry.lstat()
                if stat.S_ISSOCK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
                    entry.unlink()
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass

    async def _close_session(self, identifier: str) -> None:
        session = self._sessions.pop(identifier, None)
        if session is None:
            return
        self._terminate_worker(session.worker)
        await session.bridge.close()
        self._remove_directory(session.directory)

    async def _watch_session(self, identifier: str) -> None:
        while not self._closed:
            session = self._sessions.get(identifier)
            if session is None:
                return
            if session.worker.poll() is not None or session.expires_at <= time.monotonic():
                await self._close_session(identifier)
                return
            await asyncio.sleep(SESSION_WATCH_SECONDS)

    def _collect_expired(self) -> None:
        now = time.monotonic()
        for identifier, session in tuple(self._sessions.items()):
            if session.expires_at <= now or session.worker.poll() is not None:
                asyncio.create_task(self._close_session(identifier))

    def answer(self, payload: Any) -> dict[str, str]:
        vmid, sdp, _node, width, height, fps = self._validate_request(payload)
        if self._closed:
            raise DirectTerminalError("direct-terminal is stopped")
        future: Future[dict[str, str]] = asyncio.run_coroutine_threadsafe(
            self._create(vmid, sdp, width, height, fps), self._loop)
        try:
            return future.result(timeout=REQUEST_TIMEOUT_SECONDS)
        except BridgeError as error:
            future.cancel()
            # The PVE API intentionally returns only a generic failure to the
            # browser.  Preserve this bounded, locally generated bridge
            # reason for the root-only service journal: otherwise an absent
            # H.264/Opus offer is indistinguishable from ICE gathering or a
            # local producer failure and every operator sees the same useless
            # "Could not create" dialog.
            raise DirectTerminalError(
                f"direct-terminal WebRTC negotiation failed: {error}") from error
        except (TimeoutError, OSError, asyncio.TimeoutError) as error:
            future.cancel()
            # As above, this is emitted only to the root-owned journal. Keep
            # the browser response generic, but retain the exception class
            # and errno (where applicable) so an operator can distinguish a
            # local UDP/ICE bind problem from a signalling timeout without
            # logging SDP, an address from the offer, or PVE credentials.
            suffix = type(error).__name__
            if isinstance(error, OSError) and error.errno is not None:
                suffix += f" errno={error.errno}"
            raise DirectTerminalError(
                f"direct-terminal WebRTC negotiation failed: {suffix}") from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reconcile_stop.set()
        self._reconcile_thread.join(timeout=2)
        async def close_all() -> None:
            for identifier in tuple(self._sessions):
                await self._close_session(identifier)
        future = asyncio.run_coroutine_threadsafe(close_all(), self._loop)
        try:
            future.result(timeout=5)
        except (TimeoutError, RuntimeError):
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)
        self._dbus.close()


def _read_line(connection: socket.socket) -> bytes:
    payload = bytearray()
    while b"\n" not in payload:
        block = connection.recv(min(65536, MAX_REQUEST_BYTES + 1 - len(payload)))
        if not block:
            break
        payload.extend(block)
        if len(payload) > MAX_REQUEST_BYTES:
            raise DirectTerminalError("direct-terminal request is invalid")
    if not payload or payload.count(b"\n") != 1 or not payload.endswith(b"\n"):
        raise DirectTerminalError("direct-terminal request is invalid")
    return bytes(payload[:-1])


def _require_root_peer(connection: socket.socket) -> None:
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (AttributeError, OSError, struct.error) as error:
        raise DirectTerminalError("direct-terminal peer is invalid") from error
    if uid != 0:
        raise DirectTerminalError("direct-terminal peer is invalid")


class PveRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, PveRequestServer)
        response: dict[str, Any] = {"ok": False}
        try:
            self.request.settimeout(REQUEST_TIMEOUT_SECONDS)
            _require_root_peer(self.request)
            payload = json.loads(_read_line(self.request).decode("utf-8"))
            answer = server.sessions.answer(payload)
            if set(answer) != {"type", "sdp"} or answer["type"] != "answer" or not isinstance(answer["sdp"], str):
                raise DirectTerminalError("direct-terminal response is invalid")
            response = {"ok": True, "result": answer}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, DirectTerminalError, BridgeError) as error:
            # All public responses stay deliberately generic.  The journal is
            # the operator-only diagnostic channel and the caught errors are
            # generated locally; no browser SDP or PVE authorization value is
            # interpolated here.
            print(f"qsm-direct-terminal: request rejected: {error}", file=sys.stderr)
        try:
            self.request.sendall(json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")
        except OSError:
            pass


class PveRequestServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, path: Path, sessions: DirectSessionManager) -> None:
        if not path.is_absolute() or len(os.fsencode(path)) >= 104 or path.parent == path:
            raise DirectTerminalError("direct-terminal socket is invalid")
        parent = path.parent
        try:
            metadata = parent.lstat()
        except OSError as error:
            raise DirectTerminalError("direct-terminal socket directory is unavailable") from error
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o022 or
                (os.geteuid() == 0 and metadata.st_uid != 0)):
            raise DirectTerminalError("direct-terminal socket directory is unsafe")
        try:
            metadata = path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_mode & 0o077:
                raise DirectTerminalError("direct-terminal socket is unsafe")
            path.unlink()
        except FileNotFoundError:
            pass
        self._path = path
        self.sessions = sessions
        old_umask = os.umask(0o077)
        try:
            super().__init__(str(path), PveRequestHandler)
            os.chmod(path, 0o600)
        finally:
            os.umask(old_umask)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            try:
                if stat.S_ISSOCK(self._path.lstat().st_mode):
                    self._path.unlink()
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-directory", type=Path, default=Path("/etc/qsm-pve-direct/instances.d"))
    parser.add_argument("--runtime-directory", type=Path, default=Path("/run/qsm-pve-direct-terminal/sessions"))
    parser.add_argument("--vm-runtime-directory", type=Path, default=Path("/run/qsm-pve-direct"))
    parser.add_argument("--pve-config-directory", type=Path, default=Path("/etc/pve/qemu-server"))
    parser.add_argument("--pve-socket", type=Path, default=Path("/run/qsm-pve-direct-terminal/pve-webrtc.sock"))
    parser.add_argument("--local-node")
    arguments = parser.parse_args()
    if arguments.local_node is not None and not _NODE_PATTERN.fullmatch(arguments.local_node):
        raise SystemExit("qsm-direct-terminal: invalid local node")
    _safe_runtime_directory(arguments.runtime_directory)
    sessions = DirectSessionManager(instance_directory=arguments.instance_directory,
                                    runtime_directory=arguments.runtime_directory,
                                    vm_runtime_directory=arguments.vm_runtime_directory,
                                    pve_config_directory=arguments.pve_config_directory,
                                    local_node=arguments.local_node)
    server: PveRequestServer | None = None
    try:
        server = PveRequestServer(arguments.pve_socket, sessions)
        print(f"QSM_DIRECT_TERMINAL_READY socket={arguments.pve_socket}", flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    except DirectTerminalError as error:
        print(f"qsm-direct-terminal: {error}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
        sessions.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
