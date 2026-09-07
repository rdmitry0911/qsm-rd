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
from typing import Any, Callable


PACKAGE_DIRECTORY = Path(__file__).resolve().parents[1]
if str(PACKAGE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIRECTORY))

from browser_bridge.qsm_browser_bridge import (BrowserWebRtcBridge, BridgeError,
                                                SharedMediaIngress, UnixInputEgress)  # noqa: E402
from direct_guest.qsm_guest_channel import QsmGuestChannel  # noqa: E402
from direct_terminal.qsm_direct_encoder_probe import (DirectEncoderProbeError,
                                                       DirectEncoderSelection,
                                                       select_auto_h264_encoder,
                                                       select_hardware_h264_encoder,
                                                       select_hardware_hevc_encoder)  # noqa: E402


MIN_VMID = 100
MAX_VMID = 999_999_999
MAX_SDP_BYTES = 128 * 1024
MAX_REQUEST_BYTES = MAX_SDP_BYTES + 4096
REQUEST_TIMEOUT_SECONDS = 15.0
SESSION_IDLE_SECONDS = 10 * 60
SESSION_NEGOTIATION_SECONDS = 20.0
SESSION_WATCH_SECONDS = 0.25
MAX_SESSIONS = 16
PVE_CONFIG_MAX_BYTES = 256 * 1024
PVE_CONFIG_RECONCILE_SECONDS = 0.25
PVE_OPERATION = "pve_acl_webrtc"
PROTOCOL_VERSION = 1
_NODE_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.-]{0,62}\Z")
_SUBJECT_PATTERN = re.compile(r"\A[^\s\x00]{1,64}\Z")
_VMID_PATTERN = re.compile(r"\A[1-9][0-9]{1,8}\Z")
_ENCODER_PATTERN = re.compile(
    r"\A(?:auto|h264_nvenc|h264_qsv|h264_vaapi|libx264|hevc_nvenc|hevc_qsv|hevc_vaapi)\Z")
_RENDER_NODE_PATTERN = re.compile(r"\A/dev/dri/renderD[0-9]{1,4}\Z")
_CODEC_PATTERN = re.compile(r"\A(?:auto|h264|hevc)\Z")
_ENCODER_MODE_PATTERN = re.compile(r"\A(?:auto|hardware|software)\Z")
_PVE_VM_CONFIG_PATTERN = re.compile(r"\A([1-9][0-9]{1,8})\.conf\Z")
_ENVIRONMENT_KEYS = frozenset({
    "QSM_DIRECT_QEMU_DBUS_ADDRESS",
    "QSM_DIRECT_CODEC",
    "QSM_DIRECT_ENCODER",
    "QSM_DIRECT_ENCODER_MODE",
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
    codec = values.get("QSM_DIRECT_CODEC", "auto")
    if not _CODEC_PATTERN.fullmatch(codec):
        raise DirectTerminalError("direct-terminal VM has an unsupported browser codec")
    encoder_mode = values.get("QSM_DIRECT_ENCODER_MODE", "auto")
    if not _ENCODER_MODE_PATTERN.fullmatch(encoder_mode):
        raise DirectTerminalError("direct-terminal VM has an invalid encoder mode")
    render_node = values.get("QSM_DIRECT_VAAPI_RENDER_NODE", "")
    if render_node and not _RENDER_NODE_PATTERN.fullmatch(render_node):
        raise DirectTerminalError("direct-terminal VM has an invalid render node")
    if encoder in {"h264_vaapi", "hevc_vaapi"} and not render_node:
        raise DirectTerminalError("direct-terminal VA-API encoder lacks a render node")
    if encoder.startswith("h264_") or encoder == "libx264":
        if codec == "hevc":
            raise DirectTerminalError("direct-terminal encoder and browser codec disagree")
    if encoder.startswith("hevc_") and codec == "h264":
        raise DirectTerminalError("direct-terminal encoder and browser codec disagree")
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
            "QSM_DIRECT_CODEC": "auto",
            "QSM_DIRECT_ENCODER_MODE": "auto",
            "QSM_DIRECT_ENCODER": "auto",
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


def _managed_display_profile(config: str, vmid: int, vm_runtime_directory: Path) -> str | None:
    """Accept one explicitly owned Display1 profile from the PVE config.

    The VirGL profile owns its ``virtio-vga-gl`` adapter and needs ``gl=on``.
    The portable CPU profile intentionally owns no adapter in ``args``: PVE
    supplies Standard VGA or non-GL VirtIO VGA and Display1 uses ``gl=off``.
    Keeping those forms exact prevents this service from attaching a private
    bus to an administrator's unrelated QEMU display configuration.
    """
    if not _valid_vmid(vmid):
        return None
    args: str | None = None
    vga: str | None = None
    for line in config.splitlines():
        if line.startswith("args:"):
            if args is not None:
                return None
            args = line.removeprefix("args:").strip()
        if line.startswith("vga:"):
            if vga is not None:
                return None
            vga = line.removeprefix("vga:").strip()
    if args is None or len(args) > 8192 or any(ord(value) < 0x20 or ord(value) == 0x7f for value in args):
        return None
    address = re.escape(f"unix:path={vm_runtime_directory}/{vmid}/qemu-display1.bus")
    virgl_display = re.compile(
        rf"(?:^|\s)-display\s+dbus,addr={address},gl=on,rendernode=/dev/dri/renderD[0-9]{{1,4}}(?=\s|$)")
    cpu_display = re.compile(
        rf"(?:^|\s)-display\s+dbus,addr={address},gl=off(?=\s|$)")
    count = len(re.findall(r"(?:^|\s)-display(?:\s|$)", args))
    gpu = re.compile(r"(?:^|\s)-device\s+virtio-vga-gl(?:,[^\s]+)?(?=\s|$)")
    gpu_arguments = [value.strip() for value in gpu.findall(args)]
    if (count == 1 and len(gpu_arguments) == 1 and
            gpu_arguments[0] == "-device virtio-vga-gl,id=qsm-direct-gpu" and
            virgl_display.search(args) is not None):
        return "virgl"
    # PVE's implicit default is Standard VGA. An explicit vga line may carry
    # only the stock non-GL `std` or `virtio` profile in this mode.
    vga_type = "std" if vga is None else vga.split(",", 1)[0]
    if vga_type.startswith("type="):
        vga_type = vga_type.removeprefix("type=")
    if (count == 1 and not gpu_arguments and vga_type in {"std", "virtio"} and
            cpu_display.search(args) is not None):
        return "cpu"
    return None


def _managed_display_enabled(config: str, vmid: int, vm_runtime_directory: Path) -> bool:
    return _managed_display_profile(config, vmid, vm_runtime_directory) is not None


def _managed_guest_channel_enabled(config: str, vmid: int, vm_runtime_directory: Path) -> bool:
    """Recognise the exact optional QSM virtio-serial guest channel.

    Display1 remains usable without guest tools.  A partial or foreign chardev
    is never claimed: clipboard and files simply remain unavailable until the
    VM is saved with the complete managed argument set and restarted.
    """
    if not _valid_vmid(vmid):
        return False
    args = next((line.removeprefix("args:").strip() for line in config.splitlines()
                 if line.startswith("args:")), None)
    if args is None or len(args) > 8192 or any(ord(value) < 0x20 or ord(value) == 0x7f for value in args):
        return False
    expected = (
        f"-chardev socket,id=qsm-direct-agent,path={vm_runtime_directory}/{vmid}/qsm-agent.sock,server=on,wait=off",
        "-device virtio-serial-pci,id=qsm-direct-serial",
        "-device virtserialport,chardev=qsm-direct-agent,name=org.qsm.direct.agent",
    )
    return all(re.search(rf"(?:^|\s){re.escape(argument)}(?=\s|$)", args) for argument in expected)


def _qemu_process_generation(pid_directory: Path, vmid: int) -> str | None:
    """Return the live PVE QEMU generation for ``vmid``.

    A Display1 D-Bus address is intentionally persistent across a terminal
    service restart: QEMU has no mechanism to reconnect to a replacement
    bus.  That persistence makes a VM restart subtly different.  The old
    encoder can remain connected to the old ``org.qemu`` peer and continue to
    feed a browser its final firmware frame, while its input is no longer
    consumed by the new QEMU process.

    PVE writes one root-owned PID file per running VM.  Combine that PID with
    Linux' non-reusable process start time, and verify the command still names
    the requested VM.  ``None`` is a normal state while a VM is stopped or
    between PVE's stop/start phases; callers must retire, never reuse, a
    capture bound to a previous generation in that case.
    """
    if not _valid_vmid(vmid) or not pid_directory.is_absolute():
        return None
    try:
        content = _read_root_file(pid_directory / f"{vmid}.pid", maximum=32)
    except DirectTerminalError:
        return None
    try:
        pid_text = content.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not pid_text.isdecimal() or (pid_text.startswith("0") and pid_text != "0"):
        return None
    pid = int(pid_text)
    if pid <= 1:
        return None
    try:
        stat_text = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
        command_line = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\x00")
    except (OSError, UnicodeDecodeError):
        return None
    # proc(5): field 2 (comm) may contain spaces, so split only after its last
    # closing parenthesis.  The remaining sequence starts at field 3; start
    # time is field 22, at index 19.
    separator = stat_text.rfind(") ")
    if separator < 1:
        return None
    fields = stat_text[separator + 2:].split()
    if len(fields) <= 19 or fields[0] == "Z" or not fields[19].isdecimal():
        return None
    argv = [part.decode("ascii", "ignore") for part in command_line if part]
    if not argv:
        return None
    executable = os.path.basename(argv[0])
    if executable not in {"kvm", "qemu-system-x86_64"} and not executable.startswith("qemu-system-"):
        return None
    try:
        identity_index = argv.index("-id")
    except ValueError:
        return None
    if identity_index + 1 >= len(argv) or argv[identity_index + 1] != str(vmid):
        return None
    return f"{pid}:{fields[19]}"


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
    vmid: int
    bridge: BrowserWebRtcBridge
    worker: subprocess.Popen[bytes]
    directory: Path
    expires_at: float
    remove_guest_listener: Callable[[], None] | None = None
    negotiation_expires_at: float = 0.0


@dataclass
class DirectVmTransport:
    """One Display1 worker and shared encoded stream for a VM."""

    vmid: int
    worker: subprocess.Popen[bytes]
    media: SharedMediaIngress
    input: UnixInputEgress
    directory: Path
    qemu_generation: str
    codec: str
    guest: QsmGuestChannel | None = None


class DirectSessionManager:
    """Keep each one-off browser offer on one dedicated asyncio loop."""

    def __init__(self, *, instance_directory: Path, runtime_directory: Path,
                 vm_runtime_directory: Path, pve_config_directory: Path,
                 local_node: str | None,
                 qemu_pid_directory: Path = Path("/run/qemu-server")) -> None:
        self._instance_directory = instance_directory
        self._runtime_directory = runtime_directory
        self._vm_runtime_directory = vm_runtime_directory
        self._pve_config_directory = pve_config_directory
        self._qemu_pid_directory = qemu_pid_directory
        self._local_node = local_node
        self._dbus = DbusManager(vm_runtime_directory)
        self._reconcile_stop = threading.Event()
        self._start_configured_display_buses()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="qsm-direct-webrtc", daemon=True)
        self._sessions: dict[str, DirectSession] = {}
        # Display1 accepts one capture peer per VM. A transport owns that one
        # worker and fans its encoded media out to all PVE-authorized browser
        # sessions for the VM; it is not a single-viewer limitation.
        self._transports: dict[int, DirectVmTransport] = {}
        # A positive probe is cached per codec.  The cache contains an actual
        # initialized encoder rather than a GPU-name guess, so heterogeneous
        # nodes naturally advertise HEVC only where it can be used.
        self._auto_encoders: dict[str, DirectEncoderSelection] = {}
        self._vm_locks: dict[int, asyncio.Lock] = {}
        self._closed = False
        self._transport_reconcile_future: Future[None] | None = None
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

    def _start_configured_display_buses(self) -> tuple[int, ...]:
        """Create QEMU buses before the user starts a Display1-configured VM."""
        configured = self._configured_vms()
        for vmid in configured:
            policy = _load_optional_instance(
                self._instance_directory, vmid, self._vm_runtime_directory)
            self._dbus.ensure(vmid, policy["QSM_DIRECT_QEMU_DBUS_ADDRESS"])
        return configured

    async def _reconcile_active_transports(self, configured: frozenset[int]) -> None:
        """Disconnect browser peers when their VM's Display1 owner changed.

        A live WebRTC channel cannot be transparently switched between QEMU
        processes.  Closing it is deliberate: the PVE Console retry/reload
        creates a fresh media worker, instead of exposing a stale picture and
        silently dead mouse to the user.
        """
        for vmid, transport in tuple(self._transports.items()):
            generation = _qemu_process_generation(self._qemu_pid_directory, vmid)
            if vmid in configured and generation == transport.qemu_generation:
                continue
            reason = "configuration changed" if vmid not in configured else "QEMU generation changed"
            print(
                f"qsm-direct-terminal: retiring VM media transport vmid={vmid}: {reason}",
                file=sys.stderr,
                flush=True,
            )
            await self._close_vmid_sessions(vmid)

    def _schedule_transport_reconcile(self, configured: tuple[int, ...]) -> None:
        if self._closed:
            return
        if self._transport_reconcile_future is not None and not self._transport_reconcile_future.done():
            return
        self._transport_reconcile_future = asyncio.run_coroutine_threadsafe(
            self._reconcile_active_transports(frozenset(configured)), self._loop)

    def _reconcile_configured_display_buses(self) -> None:
        """Pick up a saved Display setting without an operator service restart."""
        while not self._reconcile_stop.wait(PVE_CONFIG_RECONCILE_SECONDS):
            try:
                self._schedule_transport_reconcile(self._start_configured_display_buses())
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
        # Do not retain a lock for an arbitrary VMID submitted by an
        # authenticated but otherwise invalid request.  Only a configured VM
        # may acquire the small, process-lifetime per-VM serialization entry.
        if not self._display_is_configured(vmid):
            raise DirectTerminalError("direct-terminal VM is not configured for Display1")
        lock = self._vm_locks.setdefault(vmid, asyncio.Lock())
        async with lock:
            return await self._create_locked(vmid, sdp, width, height, fps)

    async def _create_locked(self, vmid: int, sdp: str, width: int, height: int, fps: int) -> dict[str, str]:
        self._collect_expired()
        if not self._display_is_configured(vmid):
            raise DirectTerminalError("direct-terminal VM is not configured for Display1")
        if len(self._sessions) >= MAX_SESSIONS:
            raise DirectTerminalError("direct-terminal session capacity is exhausted")
        policy = _load_optional_instance(self._instance_directory, vmid, self._vm_runtime_directory)
        self._dbus.ensure(vmid, policy["QSM_DIRECT_QEMU_DBUS_ADDRESS"])
        existing = self._transports.get(vmid)
        selection: DirectEncoderSelection | None = None
        if (existing is not None and existing.worker.poll() is None and
                existing.qemu_generation == _qemu_process_generation(self._qemu_pid_directory, vmid)):
            codec = existing.codec
            if not BrowserWebRtcBridge.offer_supports_codec(sdp, codec):
                raise DirectTerminalError(
                    f"direct-terminal VM is already streaming {codec.upper()} to another Console; "
                    "this browser does not support that codec")
        else:
            codec, selection = self._codec_for_offer(sdp, policy)
        transport = await self._transport_for(vmid, policy, width, height, fps,
                                              codec=codec, selection=selection)
        identifier = secrets.token_hex(16)
        directory = self._runtime_directory / f"vm-{vmid}" / identifier
        _safe_runtime_directory(directory.parent)
        _safe_runtime_directory(directory)

        def retire_browser_peer() -> None:
            # aiortc invokes connection-state callbacks on this manager's
            # event loop.  Schedule rather than await so a remote tab close
            # cannot re-enter RTCPeerConnection.close().  If a browser fails
            # during the small SDP-answer window the identifier is not yet in
            # _sessions; the post-answer terminal check below handles it.
            if not self._closed:
                self._loop.call_soon(self._schedule_browser_retirement, identifier)

        bridge = BrowserWebRtcBridge(
            directory, fps=fps, expected_producer_uid=os.geteuid(),
            shared_media=transport.media, shared_input=transport.input,
            video_codec=transport.codec,
            guest_dispatch=transport.guest.dispatch if transport.guest is not None else None,
            on_terminal=retire_browser_peer)
        remove_guest_listener = (transport.guest.add_clipboard_listener(bridge.notify_guest_clipboard)
                                 if transport.guest is not None else None)
        try:
            bridge.start_taps()
            answer = await bridge.answer_offer(sdp)
            if bridge.terminal:
                raise DirectTerminalError("direct-terminal browser peer ended during negotiation")
            self._sessions[identifier] = DirectSession(
                vmid=vmid, bridge=bridge, worker=transport.worker, directory=directory,
                expires_at=time.monotonic() + SESSION_IDLE_SECONDS,
                remove_guest_listener=remove_guest_listener,
                negotiation_expires_at=time.monotonic() + SESSION_NEGOTIATION_SECONDS)
            asyncio.create_task(self._watch_session(identifier))
            return answer
        except BaseException:
            if remove_guest_listener is not None:
                remove_guest_listener()
            await bridge.close()
            self._remove_directory(directory)
            if not any(session.vmid == vmid for session in self._sessions.values()):
                await self._close_transport(vmid)
            raise

    def _schedule_browser_retirement(self, identifier: str) -> None:
        """Release one closed browser peer and its producer if it was last."""
        if identifier in self._sessions and not self._closed:
            asyncio.create_task(self._close_session(identifier))

    def _select_encoder(self, codec: str, policy: dict[str, str]) -> DirectEncoderSelection:
        """Resolve a verified encoder matching one negotiated browser codec."""
        configured_encoder = policy.get("QSM_DIRECT_ENCODER") or "auto"
        encoder_mode = policy.get("QSM_DIRECT_ENCODER_MODE") or "auto"
        if codec not in {"h264", "hevc"}:
            raise DirectTerminalError("direct-terminal selected an invalid browser codec")
        if configured_encoder != "auto":
            matches = (configured_encoder.startswith(codec + "_") or
                       (codec == "h264" and configured_encoder == "libx264"))
            if not matches:
                raise DirectTerminalError("direct-terminal encoder and browser codec disagree")
            return DirectEncoderSelection(configured_encoder,
                                          policy.get("QSM_DIRECT_VAAPI_RENDER_NODE") or None)
        if codec == "hevc":
            if encoder_mode == "software":
                raise DirectTerminalError("direct-terminal HEVC requires a hardware encoder")
            cached = self._auto_encoders.get("hevc")
            if cached is not None:
                return cached
            try:
                # HEVC deliberately has no software fallback: its automatic
                # selection promise means a working accelerator was verified.
                selection = select_hardware_hevc_encoder()
            except DirectEncoderProbeError as error:
                raise DirectTerminalError("direct-terminal has no usable hardware HEVC encoder") from error
            self._auto_encoders["hevc"] = selection
            return selection
        if encoder_mode == "software":
            return DirectEncoderSelection("libx264")
        cached = self._auto_encoders.get("h264")
        if cached is not None and encoder_mode == "auto":
            return cached
        try:
            selection = (select_hardware_h264_encoder() if encoder_mode == "hardware"
                         else select_auto_h264_encoder())
        except DirectEncoderProbeError as error:
            detail = "hardware H.264 encoder" if encoder_mode == "hardware" else "H.264 encoder"
            raise DirectTerminalError(f"direct-terminal has no usable {detail}") from error
        if encoder_mode == "auto":
            self._auto_encoders["h264"] = selection
        return selection

    def _codec_for_offer(self, sdp: str, policy: dict[str, str]) -> tuple[str, DirectEncoderSelection]:
        """Choose the best mutually usable codec before spawning a worker.

        An offer is the only trustworthy statement of the current browser's
        decoder support.  ``auto`` therefore tries hardware HEVC only after
        seeing H.265 in that offer, then falls back to the independently
        validated H.264 route.  A forced HEVC policy remains explicit.
        """
        preference = policy.get("QSM_DIRECT_CODEC") or "auto"
        if preference not in {"auto", "h264", "hevc"}:
            raise DirectTerminalError("direct-terminal VM has an unsupported browser codec")
        offered = BrowserWebRtcBridge.offered_video_codecs(sdp)
        if preference in {"auto", "hevc"} and "hevc" in offered:
            try:
                return "hevc", self._select_encoder("hevc", policy)
            except DirectTerminalError:
                if preference == "hevc":
                    raise
        if preference == "hevc":
            raise DirectTerminalError("browser does not offer WebRTC HEVC")
        if "h264" not in offered:
            raise DirectTerminalError("browser does not offer WebRTC H.264")
        return "h264", self._select_encoder("h264", policy)

    async def _transport_for(self, vmid: int, policy: dict[str, str], width: int, height: int,
                             fps: int, *, codec: str,
                             selection: DirectEncoderSelection | None = None) -> DirectVmTransport:
        """Return the sole capture/encoder worker for this VM, starting it once."""
        generation = _qemu_process_generation(self._qemu_pid_directory, vmid)
        existing = self._transports.get(vmid)
        if existing is not None and generation == existing.qemu_generation and existing.worker.poll() is None:
            if existing.codec != codec:
                raise DirectTerminalError("direct-terminal VM transport codec changed while active")
            existing.media.raise_if_failed()
            existing.input.raise_if_failed()
            return existing
        if existing is not None:
            # The old worker may still own a valid D-Bus connection, but it
            # belongs to a previous QEMU instance.  Retire every subscriber;
            # preserving one would retain a stale frame and dead input path.
            await self._close_vmid_sessions(vmid)
        if generation is None:
            raise DirectTerminalError("direct-terminal VM is not running")

        directory = self._runtime_directory / f"vm-{vmid}" / "producer"
        _safe_runtime_directory(directory.parent)
        _safe_runtime_directory(directory)
        media = SharedMediaIngress(
            directory, self._loop, fps=fps, expected_producer_uid=os.geteuid())
        input_egress = UnixInputEgress(directory, expected_uid=os.geteuid())
        worker: subprocess.Popen[bytes] | None = None
        try:
            media.start()
            input_egress.start()
            # Re-read the root-owned config immediately before spawning the
            # worker.  It is both the race-safe ownership check and the
            # profile source: CPU Display1 may expose a fixed Standard-VGA
            # scanout for which QEMU rejects SetUIInfo, while VirGL must keep
            # treating such a rejection as a real display failure.
            config = _read_pve_vm_config(self._pve_config_directory / f"{vmid}.conf")
            profile = (_managed_display_profile(config, vmid, self._vm_runtime_directory)
                       if config is not None else None)
            if profile is None:
                raise DirectTerminalError("direct-terminal VM Display1 configuration changed")
            configured_encoder = policy.get("QSM_DIRECT_ENCODER") or "auto"
            encoder_mode = policy.get("QSM_DIRECT_ENCODER_MODE") or "auto"
            resolved = selection or self._select_encoder(codec, policy)
            encoder = resolved.encoder
            vaapi_device = resolved.vaapi_device
            arguments = [
                "/usr/lib/qsm-pve-direct/bin/qsm-direct-media-worker",
                "--dbus-address", policy["QSM_DIRECT_QEMU_DBUS_ADDRESS"],
                "--video-socket", f"unix:{media.video_path}",
                "--audio-socket", f"unix:{media.audio_path}",
                "--input-socket", f"unix:{input_egress.path}",
                "--codec", codec,
                "--encoder", encoder,
                "--fps", str(fps),
                "--initial-size", f"{width}x{height}",
            ]
            if profile == "cpu":
                arguments.append("--allow-unsupported-ui-info")
            # The probe proves a hardware encoder is usable at service start,
            # but a driver reset or a live VirGL scanout replacement can still
            # kill it after its first frames. Automatic policy may recover to
            # the package's in-process libx264; a hardware-only choice must
            # remain strict and report its failure instead.
            if (codec == "h264" and configured_encoder == "auto" and encoder_mode == "auto" and
                    encoder != "libx264"):
                arguments.extend(["--fallback-encoder", "libx264"])
            if encoder in {"h264_vaapi", "hevc_vaapi"}:
                if not vaapi_device:
                    raise DirectTerminalError("direct-terminal VA-API encoder lacks a render node")
                arguments.extend(["--vaapi-device", vaapi_device])
            worker = subprocess.Popen(
                arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=None,
                env=self._child_environment(), close_fds=True, start_new_session=True,
            )
            await asyncio.sleep(0.12)
            if worker.poll() is not None:
                raise DirectTerminalError(
                    f"direct-terminal media worker failed to start (exit code {worker.returncode})")
            guest = (QsmGuestChannel(self._vm_runtime_directory / str(vmid) / "qsm-agent.sock")
                     if config is not None and _managed_guest_channel_enabled(
                         config, vmid, self._vm_runtime_directory) else None)
            transport = DirectVmTransport(
                vmid=vmid, worker=worker, media=media, input=input_egress, directory=directory,
                qemu_generation=generation, codec=codec, guest=guest)
            self._transports[vmid] = transport
            print(
                f"qsm-direct-terminal: VM media transport started vmid={vmid} codec={codec} encoder={encoder} pid={worker.pid}",
                file=sys.stderr,
                flush=True,
            )
            return transport
        except BaseException:
            if worker is not None:
                self._terminate_worker(worker)
            media.close()
            input_egress.close()
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
        await session.bridge.close()
        if session.remove_guest_listener is not None:
            session.remove_guest_listener()
        self._remove_directory(session.directory)
        if not any(other.vmid == session.vmid for other in self._sessions.values()):
            await self._close_transport(session.vmid)

    async def _close_transport(self, vmid: int) -> None:
        transport = self._transports.pop(vmid, None)
        if transport is None:
            return
        self._terminate_worker(transport.worker)
        transport.media.close()
        transport.input.close()
        if transport.guest is not None:
            transport.guest.close()
        self._remove_directory(transport.directory)

    async def _close_vmid_sessions(self, vmid: int) -> None:
        """Retire all subscribers after their shared VM transport has ended."""
        identifiers = tuple(
            identifier for identifier, session in self._sessions.items()
            if session.vmid == vmid)
        for identifier in identifiers:
            session = self._sessions.pop(identifier, None)
            if session is not None:
                if session.remove_guest_listener is not None:
                    session.remove_guest_listener()
                await session.bridge.close()
                self._remove_directory(session.directory)
        await self._close_transport(vmid)

    async def _watch_session(self, identifier: str) -> None:
        while not self._closed:
            session = self._sessions.get(identifier)
            if session is None:
                return
            exit_code = session.worker.poll()
            if exit_code is not None:
                print(
                    f"qsm-direct-terminal: VM media transport ended vmid={session.vmid} "
                    f"exit_code={exit_code}",
                    file=sys.stderr,
                    flush=True,
                )
                await self._close_vmid_sessions(session.vmid)
                return
            now = time.monotonic()
            if session.bridge.connection_state == "connected":
                # SESSION_IDLE_SECONDS is an idle lease, not a lifetime: a
                # browser whose WebRTC peer is still connected is not idle.
                # Without this refresh every Console closed exactly ten
                # minutes after it opened and reported the VM as stopped.
                # The lease now runs from the last moment the peer was
                # connected, which still reclaims a browser that vanished
                # without an ICE failure.
                session.expires_at = now + SESSION_IDLE_SECONDS
            elif session.expires_at <= now:
                await self._close_session(identifier)
                return
            if (session.negotiation_expires_at and
                    session.bridge.connection_state != "connected" and
                    session.negotiation_expires_at <= now):
                print(
                    f"qsm-direct-terminal: WebRTC negotiation timed out vmid={session.vmid}",
                    file=sys.stderr,
                    flush=True,
                )
                await self._close_session(identifier)
                return
            await asyncio.sleep(SESSION_WATCH_SECONDS)

    def _collect_expired(self) -> None:
        now = time.monotonic()
        for identifier, session in tuple(self._sessions.items()):
            if session.worker.poll() is not None:
                asyncio.create_task(self._close_vmid_sessions(session.vmid))
            elif session.expires_at <= now:
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
            for vmid in tuple(self._transports):
                await self._close_transport(vmid)
        future = asyncio.run_coroutine_threadsafe(close_all(), self._loop)
        try:
            future.result(timeout=5)
        except (TimeoutError, RuntimeError):
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)
        if not self._loop.is_running():
            self._loop.close()
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
    parser.add_argument("--qemu-pid-directory", type=Path, default=Path("/run/qemu-server"))
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
                                    local_node=arguments.local_node,
                                    qemu_pid_directory=arguments.qemu_pid_directory)
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
