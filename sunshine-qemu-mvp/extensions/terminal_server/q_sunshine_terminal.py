#!/usr/bin/env python3
"""One node-level, VM-oriented terminal service for q-sunshine.

The process has the same authority split as a conventional terminal server:
the already-authenticated control plane authorizes exactly one target, then a
private transport worker serves that target.  Here the target is a Proxmox
QEMU VM rather than a Unix desktop user.  Sunshine and Moonlight are therefore
transport adapters, not the source of identity or VM selection.

There is intentionally one externally reachable TLS listener, and it serves
only native descriptor redemption and VM-scoped transport leases.  A protected
PVE API handler authorizes ``VM.Console`` and passes the selected VM over a
root-only local Unix socket; it receives the one-use ``.qsm`` launch envelope
in return.  PVE passwords, cookies, CSRF values, and other bearer credentials
never reach a transport worker or the native protocol.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import ssl
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


_HERE = Path(__file__).resolve().parent
_SYSTEM_AUTH_DIRECTORY = _HERE.parent / "system_auth"
_GAMESTREAM_AUTH_DIRECTORY = _HERE.parent / "gamestream_auth"
for _directory in (_SYSTEM_AUTH_DIRECTORY, _GAMESTREAM_AUTH_DIRECTORY):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from q_sunshine_auth import (AuthError, TICKET_TTL_MAX_SECONDS,  # noqa: E402
                             TICKET_TTL_MIN_SECONDS, is_pve_proxy_tls_material, issue_ticket,
                             load_ticket_key, load_tls_server_cert_chain,
                             valid_audience, valid_subject)
from q_sunshine_gamestream_lease import (LeaseIssuerConfig, issue_lease,  # noqa: E402
                                          validate_config)


PROTOCOL_VERSION = 1
DESCRIPTOR_REDEEM_OPERATION = "redeem_launch"
GAMESTREAM_LEASE_OPERATION = "gamestream_lease"
LAUNCH_DESCRIPTOR_KIND = "q-sunshine-pve-launch"
PVE_ACL_LAUNCH_OPERATION = "pve_acl_launch"
DEFAULT_PVE_ACL_LAUNCH_SOCKET = "/run/q-sunshine-terminal/pve-launch.sock"
MAX_REQUEST_BYTES = 20 * 1024
MAX_PVE_ACL_REQUEST_BYTES = 1024
MAX_PVE_SUBJECT_BYTES = 64
MAX_VMID = 999_999_999
MIN_VMID = 100
TLS_HANDSHAKE_TIMEOUT_SECONDS = 12.0
TLS_REQUEST_TIMEOUT_SECONDS = 12.0
PVE_ACL_REQUEST_TIMEOUT_SECONDS = 3.0
DEFAULT_MAX_CONCURRENT_REQUESTS = 16
MAX_CONCURRENT_REQUESTS = 16
DEFAULT_TICKET_TTL_SECONDS = 600
DEFAULT_DESCRIPTOR_TTL_SECONDS = 60
MAX_DESCRIPTOR_TTL_SECONDS = 120
_ENVIRONMENT_KEY_PATTERN = re.compile(r"\A[A-Z][A-Z0-9_]{0,127}\Z")
_PVE_NODE_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.-]{0,62}\Z")
_DESCRIPTOR_PATTERN = re.compile(r"\Aqsd1\.[A-Za-z0-9_-]{43}\Z")

# Instance files are trusted root-local policy, but accepting arbitrary
# environment names would still make review unnecessarily difficult and could
# accidentally pass a loader or credential variable to a worker.  Keep this
# explicit list aligned with the documented example file.
_INSTANCE_ENVIRONMENT_KEYS = frozenset({
    "SUNSHINE_QEMU_DBUS_ADDRESS",
    "SUNSHINE_QEMU_DBUS_DESTINATION",
    "SUNSHINE_QEMU_DBUS_RENDER_NODE",
    "QSUNSHINE_DBUS_ADDRESS",
    "QSUNSHINE_ENCODER",
    "QSUNSHINE_MEDIA_PORT",
    "QSUNSHINE_QSF_PORT",
    "QSUNSHINE_QSF_AGENT_SOCKET",
    "QSUNSHINE_QSF_HOST_MAX_WIDTH",
    "QSUNSHINE_QSF_HOST_MAX_HEIGHT",
    "QSUNSHINE_QSF_HOST_MAX_FPS",
    "QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS",
    "QSUNSHINE_QSF_HOST_ENCODER_CODECS",
    "QSUNSHINE_QSF_ENCODER_PROBE",
    "QSUNSHINE_DIRECT_ENCODER",
    "QSUNSHINE_DIRECT_VAAPI_RENDER_NODE",
    "QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL",
    "QSUNSHINE_QSF_SUNSHINE_SERVERINFO_CA_FILE",
    "QSUNSHINE_GAMESTREAM_LEASE_ISSUER",
    "QSUNSHINE_GAMESTREAM_LEASE_CA_CERT",
    "QSUNSHINE_GAMESTREAM_LEASE_CA_KEY",
    "QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_CERT",
    "QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_KEY",
    "QSUNSHINE_GAMESTREAM_LEASE_TTL_SECONDS",
    "QSUNSHINE_ADVERTISE_HOST",
    "QSUNSHINE_PVE_NODE",
})


class TerminalError(RuntimeError):
    """A deliberately generic remote terminal-service failure."""


def encode_response(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii") + b"\n"


def read_line(connection: socket.socket, maximum: int, *, initial: bytes = b"") -> bytes:
    """Read exactly one bounded newline-framed request, with no pipelining."""
    payload = bytearray(initial)
    if len(payload) > maximum:
        raise TerminalError("authentication failed")
    while b"\n" not in payload:
        block = connection.recv(min(65536, maximum + 1 - len(payload)))
        if not block:
            break
        payload.extend(block)
        if len(payload) > maximum:
            raise TerminalError("authentication failed")
    if not payload or b"\n" not in payload:
        raise TerminalError("authentication failed")
    line, remainder = bytes(payload).split(b"\n", 1)
    if remainder:
        raise TerminalError("authentication failed")
    return line


class AttemptLimiter:
    """Bound online descriptor guessing without retaining claim values."""

    _MAX_FAILURES = 5
    _WINDOW_SECONDS = 300.0
    _LOCKOUT_SECONDS = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def allow(self, source: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            return self._locked_until.get(source, 0.0) <= now

    def record_failure(self, source: str) -> None:
        now = time.monotonic()
        with self._lock:
            failures = self._failures.setdefault(source, [])
            failures[:] = [when for when in failures if when > now - self._WINDOW_SECONDS]
            failures.append(now)
            if len(failures) >= self._MAX_FAILURES:
                self._locked_until[source] = now + self._LOCKOUT_SECONDS
                self._failures.pop(source, None)
            self._prune_locked(now)

    def _prune_locked(self, now: float) -> None:
        for source, until in tuple(self._locked_until.items()):
            if until <= now:
                self._locked_until.pop(source, None)
        for source, failures in tuple(self._failures.items()):
            failures[:] = [when for when in failures if when > now - self._WINDOW_SECONDS]
            if not failures:
                self._failures.pop(source, None)


class BoundedThreadingMixIn(socketserver.ThreadingMixIn):
    """Reject excess accepted sockets rather than creating unbounded workers."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, *arguments: Any, max_concurrent_requests: int,
                 **keyword_arguments: Any) -> None:
        if not 1 <= max_concurrent_requests <= MAX_CONCURRENT_REQUESTS:
            raise ValueError("invalid terminal request capacity")
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        super().__init__(*arguments, **keyword_arguments)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def _safe_ascii(value: object, *, maximum: int) -> bool:
    return isinstance(value, str) and value.isascii() and 0 < len(value) <= maximum and "\x00" not in value


def _normalise_host(value: object) -> str | None:
    """Return the shared PVE-map canonical DNS/IP spelling, or ``None``."""
    if (not isinstance(value, str) or not value.isascii() or not value or
            len(value) > 253 or "\x00" in value or
            any(character.isspace() for character in value) or
            any(character in value for character in "/?#@[]")):
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if value.endswith("."):
        value = value[:-1]
    if not value or len(value) > 253:
        return None
    labels = value.split(".")
    if not all(
        1 <= len(label) <= 63 and
        label[0].isalnum() and label[-1].isalnum() and
        all(character.isascii() and (character.isalnum() or character == "-")
            for character in label)
        for label in labels
    ):
        return None
    return value


def _valid_host(value: object) -> bool:
    """Accept a DNS hostname or unbracketed IPv4/IPv6 literal."""
    return _normalise_host(value) is not None


def _valid_port(value: object) -> bool:
    return type(value) is int and 1 <= value <= 65535


def _valid_vmid(value: object) -> bool:
    return type(value) is int and MIN_VMID <= value <= MAX_VMID


def _require_absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise TerminalError(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise TerminalError(f"{label} must be absolute")
    return path


def _read_root_local_file(path: Path, *, maximum_bytes: int = 64 * 1024,
                          private: bool = True) -> bytes:
    """Read one regular non-symlink local policy file with safe modes.

    Root-run deployment requires root ownership.  Source-tree tests are also
    useful without root, so the ownership condition intentionally follows the
    effective uid rather than pretending a developer fixture is production.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TerminalError("terminal instance configuration is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        unsafe_mode = 0o077 if private else 0o022
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & unsafe_mode or
                (os.geteuid() == 0 and metadata.st_uid != 0) or
                metadata.st_size < 0 or metadata.st_size > maximum_bytes):
            raise TerminalError("terminal instance configuration is unsafe")
        data = bytearray()
        while len(data) <= maximum_bytes:
            block = os.read(descriptor, maximum_bytes + 1 - len(data))
            if not block:
                break
            data.extend(block)
        if len(data) > maximum_bytes:
            raise TerminalError("terminal instance configuration is too large")
        return bytes(data)
    except OSError as error:
        raise TerminalError("terminal instance configuration is unavailable") from error
    finally:
        os.close(descriptor)


def load_descriptor_ca_pem(path: Path) -> str:
    """Read public PEM trust material placed in the signed `.qsm` envelope.

    This is public certificate data, but it still must be root-owned and not
    writable by a non-root account.  PVE returns it only after its existing
    TLS/authentication and ``VM.Console`` boundary; the native client uses it
    for the one described terminal endpoint rather than trusting a redirect
    or a certificate supplied by a remote media route.
    """
    data = _read_root_local_file(path, private=False)
    try:
        pem = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise TerminalError("terminal descriptor CA is invalid") from error
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----\r?\n(?:[A-Za-z0-9+/=\r\n]+)-----END CERTIFICATE-----\r?\n?",
        pem,
        flags=re.ASCII,
    )
    if not blocks or "".join("".join(blocks).split()) != "".join(pem.split()):
        # Preserve only canonical PEM certificate bundles.  The comparison
        # rejects comments, private keys and unrelated unstructured content.
        raise TerminalError("terminal descriptor CA is invalid")
    try:
        for block in blocks:
            ssl.PEM_cert_to_DER_cert(block)
    except ValueError as error:
        raise TerminalError("terminal descriptor CA is invalid") from error
    return pem


def load_instance_environment(directory: Path, vmid: int) -> dict[str, str]:
    """Load the exact root-owned policy file for one validated VMID.

    This is purposefully not a shell parser: substitutions, command expansion
    and ``source`` semantics have no place in a remote-desktop authorization
    boundary.  A file contains only ``KEY=VALUE`` lines, with comments and
    blank lines allowed.
    """
    if not _valid_vmid(vmid):
        raise TerminalError("terminal VM selection is invalid")
    if not directory.is_absolute() or not directory.is_dir():
        raise TerminalError("terminal instance directory is unavailable")
    path = directory / f"{vmid}.conf"
    data = _read_root_local_file(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TerminalError("terminal instance configuration is invalid") from error
    environment: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise TerminalError("terminal instance configuration is invalid")
        key, value = line.split("=", 1)
        key = key.strip()
        # Values intentionally retain leading/trailing spaces only when an
        # administrator put them there.  All supported values are paths,
        # numbers or enum values and do not need shell quoting.
        value = value.strip()
        if (not _ENVIRONMENT_KEY_PATTERN.fullmatch(key) or
                key not in _INSTANCE_ENVIRONMENT_KEYS or key in environment or
                "\x00" in value or "\r" in value or "\n" in value or len(value) > 4096):
            raise TerminalError("terminal instance configuration is invalid")
        environment[key] = value
    return environment


def _environment_port(environment: Mapping[str, str], name: str) -> int:
    raw = environment.get(name, "")
    if not raw.isascii() or not raw.isdecimal():
        raise TerminalError("terminal transport port is not configured")
    value = int(raw, 10)
    if not _valid_port(value):
        raise TerminalError("terminal transport port is invalid")
    return value


def _optional_lease_config(environment: Mapping[str, str], audience: str) -> LeaseIssuerConfig:
    """Build a complete per-VM native GameStream lease authority.

    Native media is not an optional compatibility fallback in the terminal
    service.  A VM becomes remotely available only when its Sunshine transport
    can enforce a matching, VM-scoped system-auth lease.
    """
    values = (
        environment.get("QSUNSHINE_GAMESTREAM_LEASE_ISSUER", ""),
        environment.get("QSUNSHINE_GAMESTREAM_LEASE_CA_CERT", ""),
        environment.get("QSUNSHINE_GAMESTREAM_LEASE_CA_KEY", ""),
        environment.get("QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_CERT", ""),
        environment.get("QSUNSHINE_GAMESTREAM_LEASE_TTL_SECONDS", ""),
    )
    if any(not value for value in values):
        raise TerminalError("terminal VM has no complete native media lease configuration")
    ttl_text = values[4]
    if not ttl_text.isascii() or not ttl_text.isdecimal():
        raise TerminalError("terminal VM has invalid native media lease configuration")
    config = LeaseIssuerConfig(
        issuer=_require_absolute_path(values[0], "lease issuer"),
        ca_certificate=_require_absolute_path(values[1], "lease CA certificate"),
        ca_private_key=_require_absolute_path(values[2], "lease CA key"),
        sunshine_server_certificate=_require_absolute_path(values[3], "Sunshine server certificate"),
        audience=audience,
        ttl_seconds=int(ttl_text, 10),
        require_root_owner=os.geteuid() == 0,
    )
    try:
        validate_config(config)
    except AuthError as error:
        raise TerminalError("terminal VM has invalid native media lease configuration") from error
    return config


def _validate_preprovisioned_sunshine_server_material(
        environment: Mapping[str, str]) -> tuple[Path, Path]:
    """Require a stable GameStream certificate/key before a worker starts.

    Sunshine can generate its own pairing certificate on first launch, but a
    lease response must already return that public certificate before a native
    client may start GameStream. Requiring an explicit root-provisioned pair
    removes that first-launch race and keeps native lease mode independent of
    legacy pairing/PIN state.
    """
    certificate = _require_absolute_path(
        environment.get("QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_CERT", ""),
        "Sunshine GameStream server certificate")
    private_key = _require_absolute_path(
        environment.get("QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_KEY", ""),
        "Sunshine GameStream server key")
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        load_tls_server_cert_chain(
            context, certificate, private_key,
            require_root_owner=os.geteuid() == 0)
    except (AuthError, OSError, ssl.SSLError) as error:
        raise TerminalError("terminal VM has invalid pre-provisioned Sunshine server material") from error
    return certificate, private_key


@dataclass(frozen=True)
class NodeEndpoint:
    """One administrator-approved native broker address for a PVE node."""

    host: str
    port: int
    server_name: str


def load_node_endpoints(path: Path) -> dict[str, NodeEndpoint]:
    """Load the shared public node-to-broker map without accepting redirects.

    The file is intentionally PVE-cluster-readable but must remain
    root-owned/non-writable.  It contains only public endpoint names, yet it
    is an authorization-relevant routing policy: PVE must never return an
    arbitrary hostname for native descriptor redemption.
    """
    data = _read_root_local_file(path, private=False)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TerminalError("terminal node endpoint policy is invalid") from error
    if not isinstance(document, dict) or set(document) != {"version", "nodes"} or \
            document.get("version") != PROTOCOL_VERSION or not isinstance(document.get("nodes"), dict):
        raise TerminalError("terminal node endpoint policy is invalid")
    nodes = document["nodes"]
    if not nodes or len(nodes) > 256:
        raise TerminalError("terminal node endpoint policy is invalid")
    endpoints: dict[str, NodeEndpoint] = {}
    for node, raw_endpoint in nodes.items():
        if (not isinstance(node, str) or not _PVE_NODE_PATTERN.fullmatch(node) or
                not isinstance(raw_endpoint, dict) or
                set(raw_endpoint) != {"host", "port", "server_name"}):
            raise TerminalError("terminal node endpoint policy is invalid")
        host = raw_endpoint.get("host")
        port = raw_endpoint.get("port")
        server_name = raw_endpoint.get("server_name")
        normalised_host = _normalise_host(host)
        normalised_server_name = _normalise_host(server_name)
        if normalised_host is None or not _valid_port(port) or normalised_server_name is None:
            raise TerminalError("terminal node endpoint policy is invalid")
        endpoints[node] = NodeEndpoint(
            host=normalised_host, port=port, server_name=normalised_server_name)
    return endpoints


@dataclass(frozen=True)
class WorkerRoute:
    """The server-authoritative routes returned only after PVE authorization."""

    media_host: str
    media_port: int
    qsf_host: str
    qsf_port: int
    lease_host: str
    lease_port: int

    def to_wire(self) -> dict[str, dict[str, object]]:
        return {
            "media": {"host": self.media_host, "port": self.media_port},
            "qsf": {"host": self.qsf_host, "port": self.qsf_port},
            "lease": {"host": self.lease_host, "port": self.lease_port},
        }


@dataclass(frozen=True)
class WorkerSpec:
    vmid: int
    audience: str
    environment: Mapping[str, str]
    route: WorkerRoute
    lease_config: LeaseIssuerConfig


@dataclass(frozen=True)
class AuthorizedPveVm:
    """One VM identity after PVE authorized ``VM.Console`` locally."""

    subject: str
    node: str
    vmid: int
    guest_type: str = "qemu"
    state: str = "running"

    @property
    def audience(self) -> str:
        return f"vm-{self.vmid}"


@dataclass(frozen=True)
class PveAclLaunchRequest:
    """An already-authorized PVE API request received over the root socket.

    The PVE API endpoint performs its ordinary authenticated ``VM.Console``
    permission check before it writes this request.  The terminal broker does
    not receive a PVE cookie, password, CSRF value, or bearer artifact: its
    only authority boundary is the root-only local Unix socket.
    """

    subject: str
    node: str
    vmid: int

    @property
    def audience(self) -> str:
        return f"vm-{self.vmid}"


class WorkerManagerProtocol(Protocol):
    def ensure_started(self, route: Any) -> WorkerSpec:
        ...

    def stop_all(self) -> None:
        ...


class TerminalSessionRegistry:
    """Map a current bearer ticket hash to its VM-only lease authority.

    The actual ticket is never kept by the service after it is written to the
    TLS response.  The hash lets a later Moonlight CSR request select a VM
    lease issuer without accepting an audience selector from the client.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[bytes, tuple[int, LeaseIssuerConfig]] = {}

    @staticmethod
    def _digest(ticket: str) -> bytes:
        return hashlib.sha256(ticket.encode("ascii", "strict")).digest()

    def remember(self, ticket: str, expires_at: int, lease_config: LeaseIssuerConfig) -> None:
        with self._lock:
            self._prune_locked(int(time.time()))
            self._sessions[self._digest(ticket)] = (expires_at, lease_config)

    def lease_config_for(self, ticket: str) -> LeaseIssuerConfig:
        try:
            digest = self._digest(ticket)
        except (UnicodeEncodeError, AttributeError) as error:
            raise TerminalError("authentication failed") from error
        with self._lock:
            self._prune_locked(int(time.time()))
            selected = self._sessions.get(digest)
            if selected is None:
                raise TerminalError("authentication failed")
            return selected[1]

    def _prune_locked(self, now: int) -> None:
        for digest, (expires_at, _config) in tuple(self._sessions.items()):
            if expires_at <= now:
                self._sessions.pop(digest, None)


class TerminalWorkerManager:
    """Own transient per-VM transport children within one systemd service."""

    def __init__(self, *, instance_directory: Path, runtime_directory: Path,
                 vm_runtime_directory: Path, state_directory: Path, ticket_key_path: Path,
                 terminal_certificate: Path, terminal_key: Path,
                 lease_host: str, lease_port: int, transport_bind_host: str,
                 advertised_host: str, local_node: str | None) -> None:
        self._instance_directory = instance_directory
        self._runtime_directory = runtime_directory
        self._vm_runtime_directory = vm_runtime_directory
        self._state_directory = state_directory
        self._ticket_key_path = ticket_key_path
        self._terminal_certificate = terminal_certificate
        self._terminal_key = terminal_key
        self._lease_host = lease_host
        self._lease_port = lease_port
        self._transport_bind_host = transport_bind_host
        self._advertised_host = advertised_host
        self._local_node = local_node
        self._lock = threading.RLock()
        self._workers: dict[int, tuple[WorkerSpec, list[subprocess.Popen[bytes]]]] = {}
        self._dbus_buses: dict[int, subprocess.Popen[bytes]] = {}

    @staticmethod
    def _route_attribute(route: Any, name: str) -> Any:
        value = getattr(route, name, None)
        if value is None and isinstance(route, Mapping):
            value = route.get(name)
        return value

    def _validate_pve_route(self, route: Any) -> tuple[int, str]:
        vmid = self._route_attribute(route, "vmid")
        audience = self._route_attribute(route, "audience")
        guest_type = self._route_attribute(route, "guest_type")
        state = self._route_attribute(route, "state")
        node = self._route_attribute(route, "node")
        if not _valid_vmid(vmid) or not isinstance(audience, str) or audience != f"vm-{vmid}" or \
                not valid_audience(audience) or guest_type != "qemu" or state != "running" or \
                not isinstance(node, str) or not node:
            raise TerminalError("requested VM is not available for terminal access")
        if self._local_node is not None and node != self._local_node:
            raise TerminalError("requested VM belongs to another Proxmox node")
        return vmid, audience

    def _vm_runtime_path(self, vmid: int, leaf: str) -> Path:
        if not _valid_vmid(vmid) or leaf not in {"qemu-display1.bus", "qsf-agent.sock"}:
            raise TerminalError("terminal VM runtime policy is invalid")
        return self._vm_runtime_directory / str(vmid) / leaf

    def _dbus_socket_path(self, environment: Mapping[str, str], vmid: int) -> Path:
        address = environment.get("SUNSHINE_QEMU_DBUS_ADDRESS", "")
        expected = self._vm_runtime_path(vmid, "qemu-display1.bus")
        if address != f"unix:path={expected}":
            raise TerminalError("terminal VM has an invalid private QEMU D-Bus address")
        return expected

    def _qsf_agent_socket_path(self, environment: Mapping[str, str], vmid: int) -> Path:
        configured = environment.get("QSUNSHINE_QSF_AGENT_SOCKET", "")
        expected = self._vm_runtime_path(vmid, "qsf-agent.sock")
        if configured != str(expected):
            raise TerminalError("terminal VM has an invalid private QSF guest-agent socket")
        return expected

    def _prepare_vm_runtime_directory_locked(self, vmid: int) -> Path:
        root = self._vm_runtime_directory
        try:
            root.mkdir(mode=0o750, parents=True, exist_ok=True)
            root_metadata = os.stat(root)
            if (not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_mode & 0o027 or
                    (os.geteuid() == 0 and root_metadata.st_uid != 0)):
                raise TerminalError("terminal VM runtime directory is unsafe")
            if os.geteuid() == 0:
                os.chown(root, 0, 0)
            os.chmod(root, 0o750)
            directory = root / str(vmid)
            directory.mkdir(mode=0o750, exist_ok=True)
            metadata = os.stat(directory)
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o027 or
                    (os.geteuid() == 0 and metadata.st_uid != 0)):
                raise TerminalError("terminal VM runtime directory is unsafe")
            if os.geteuid() == 0:
                os.chown(directory, 0, 0)
            os.chmod(directory, 0o750)
            return directory
        except OSError as error:
            raise TerminalError("cannot prepare terminal VM runtime directory") from error

    @staticmethod
    def _safe_stale_socket_removal(path: Path) -> None:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return
        except OSError as error:
            raise TerminalError("cannot inspect terminal VM D-Bus socket") from error
        if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_mode & 0o077 or
                (os.geteuid() == 0 and metadata.st_uid != 0)):
            raise TerminalError("terminal VM D-Bus socket is unsafe")
        try:
            path.unlink()
        except OSError as error:
            raise TerminalError("cannot remove stale terminal VM D-Bus socket") from error

    def _ensure_dbus_bus_locked(self, vmid: int, environment: Mapping[str, str]) -> None:
        """Start the VM's private session bus before QEMU needs Display1.

        The config address is constrained to `/run/q-sunshine/<vmid>/`; this
        process owns the daemon and never falls back to a host session bus.
        """
        socket_path = self._dbus_socket_path(environment, vmid)
        self._prepare_vm_runtime_directory_locked(vmid)
        previous = self._dbus_buses.get(vmid)
        if previous is not None and previous.poll() is None:
            try:
                metadata = os.lstat(socket_path)
                if stat.S_ISSOCK(metadata.st_mode):
                    os.chmod(socket_path, 0o700)
                    metadata = os.lstat(socket_path)
                    if (not metadata.st_mode & 0o077 and
                            (os.geteuid() != 0 or metadata.st_uid == 0)):
                        return
            except OSError:
                pass
            self._terminate([previous])
            self._dbus_buses.pop(vmid, None)
        elif previous is not None:
            self._dbus_buses.pop(vmid, None)
        self._safe_stale_socket_removal(socket_path)
        environment_for_bus = self._base_child_environment()
        try:
            process = subprocess.Popen(
                ["/usr/bin/dbus-daemon", "--session", "--nofork", "--nopidfile",
                 f"--address=unix:path={socket_path}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment_for_bus,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise TerminalError("cannot start terminal VM D-Bus daemon") from error
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise TerminalError("terminal VM D-Bus daemon failed during startup")
            try:
                metadata = os.lstat(socket_path)
                if stat.S_ISSOCK(metadata.st_mode):
                    # dbus-daemon's session configuration initially creates
                    # its Unix socket as 0777 regardless of umask.  The
                    # enclosing root-only VM directory already prevents
                    # traversal; chmod closes the short-lived filesystem
                    # permission gap before this manager reports readiness.
                    os.chmod(socket_path, 0o700)
                    metadata = os.lstat(socket_path)
                    if (not metadata.st_mode & 0o077 and
                            (os.geteuid() != 0 or metadata.st_uid == 0)):
                        self._dbus_buses[vmid] = process
                        return
            except OSError:
                pass
            time.sleep(0.05)
        self._terminate([process])
        raise TerminalError("terminal VM D-Bus daemon did not become ready")

    def prepare_configured_buses(self) -> None:
        """Prepare every declared Display1 bus before PVE starts its guests."""
        try:
            entries = tuple(self._instance_directory.iterdir())
        except OSError as error:
            raise TerminalError("terminal instance directory is unavailable") from error
        vmids: list[int] = []
        for entry in entries:
            match = re.fullmatch(r"([1-9][0-9]{1,8})\.conf", entry.name)
            if match is None:
                continue
            vmid = int(match.group(1), 10)
            if _valid_vmid(vmid):
                vmids.append(vmid)
        with self._lock:
            for vmid in sorted(set(vmids)):
                environment = load_instance_environment(self._instance_directory, vmid)
                self._dbus_socket_path(environment, vmid)
                self._qsf_agent_socket_path(environment, vmid)
                self._ensure_dbus_bus_locked(vmid, environment)

    def _worker_spec(self, route: Any) -> WorkerSpec:
        vmid, audience = self._validate_pve_route(route)
        environment = load_instance_environment(self._instance_directory, vmid)
        configured_node = environment.get("QSUNSHINE_PVE_NODE", "")
        node = self._route_attribute(route, "node")
        if configured_node and configured_node != node:
            raise TerminalError("terminal VM policy belongs to another Proxmox node")
        media_port = _environment_port(environment, "QSUNSHINE_MEDIA_PORT")
        qsf_port = _environment_port(environment, "QSUNSHINE_QSF_PORT")
        self._dbus_socket_path(environment, vmid)
        self._qsf_agent_socket_path(environment, vmid)
        _validate_preprovisioned_sunshine_server_material(environment)
        host = environment.get("QSUNSHINE_ADVERTISE_HOST", self._advertised_host)
        if not _valid_host(host):
            raise TerminalError("terminal VM has invalid advertised host")
        return WorkerSpec(
            vmid=vmid,
            audience=audience,
            environment=environment,
            route=WorkerRoute(media_host=host, media_port=media_port,
                              qsf_host=host, qsf_port=qsf_port,
                              lease_host=self._lease_host, lease_port=self._lease_port),
            lease_config=_optional_lease_config(environment, audience),
        )

    @staticmethod
    def _base_child_environment() -> dict[str, str]:
        # Do not pass terminal control-plane material or service-manager
        # ambient variables to children.  PVE credentials and authorization
        # artifacts never enter a worker in this design.
        return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}

    def _make_child_environment(self, spec: WorkerSpec) -> dict[str, str]:
        environment = self._base_child_environment()
        environment.update(spec.environment)
        # The lease issuer and CA private key belong solely to the terminal
        # parent. Sunshine needs only the CA public certificate/audience and
        # its pre-provisioned server pair; QSF needs none of this material.
        environment.pop("QSUNSHINE_GAMESTREAM_LEASE_ISSUER", None)
        environment.pop("QSUNSHINE_GAMESTREAM_LEASE_CA_KEY", None)
        environment.pop("QSUNSHINE_GAMESTREAM_LEASE_TTL_SECONDS", None)
        environment.update({
            "QSUNSHINE_BIND_ADDRESS": self._transport_bind_host,
            "QSUNSHINE_PORT": str(spec.route.media_port),
            "QSUNSHINE_AUTH_AUDIENCE": spec.audience,
            "XDG_CONFIG_HOME": str(self._state_directory / str(spec.vmid)),
        })
        return environment

    @staticmethod
    def _spawn(arguments: list[str], environment: Mapping[str, str], *,
               journal_output: bool = False) -> subprocess.Popen[bytes]:
        try:
            return subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                # The QSF children emit only bounded non-secret readiness and
                # profile-transition evidence. Keep those in the terminal
                # service journal for an end-to-end operator trace; Sunshine
                # itself remains quiet because it has no such narrow audit
                # format and may log unrelated upstream Web UI details.
                stdout=None if journal_output else subprocess.DEVNULL,
                stderr=None if journal_output else subprocess.DEVNULL,
                env=dict(environment),
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise TerminalError("cannot start terminal transport worker") from error

    @staticmethod
    def _terminate(processes: list[subprocess.Popen[bytes]]) -> None:
        for process in reversed(processes):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
        deadline = time.monotonic() + 3.0
        for process in reversed(processes):
            if process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        pass
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass

    @staticmethod
    def _wait_for_qsf_control(control_socket: Path, token_file: Path,
                              processes: list[subprocess.Popen[bytes]]) -> None:
        """Wait for the private local capability boundary before TCP exists."""
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                raise TerminalError("terminal transport worker failed during startup")
            try:
                socket_metadata = os.stat(control_socket)
                token_metadata = os.stat(token_file)
                if stat.S_ISSOCK(socket_metadata.st_mode) and \
                        stat.S_ISREG(token_metadata.st_mode) and \
                        (token_metadata.st_mode & 0o077) == 0:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        raise TerminalError("terminal QSF control worker did not become ready")

    def _start_locked(self, spec: WorkerSpec) -> list[subprocess.Popen[bytes]]:
        worker_directory = self._runtime_directory / f"vm-{spec.vmid}"
        try:
            worker_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(worker_directory, 0o700)
            (self._state_directory / str(spec.vmid)).mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._state_directory / str(spec.vmid), 0o700)
        except OSError as error:
            raise TerminalError("cannot prepare terminal VM runtime directory") from error
        self._ensure_dbus_bus_locked(spec.vmid, spec.environment)
        sunshine_environment = self._make_child_environment(spec)
        qsf_environment = dict(sunshine_environment)
        for name in (
                "QSUNSHINE_GAMESTREAM_LEASE_CA_CERT",
                "QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_CERT",
                "QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_KEY"):
            qsf_environment.pop(name, None)
        control_socket = worker_directory / "control.sock"
        token_file = worker_directory / "control.token"
        # Every client-visible control-plane TLS endpoint uses the same
        # descriptor-pinned terminal certificate.  Per-VM QSF TLS overrides
        # would require a second client trust decision and defeat the purpose
        # of the PVE-issued one-time launch envelope.
        qsf_certificate = str(self._terminal_certificate)
        qsf_key = str(self._terminal_key)
        processes: list[subprocess.Popen[bytes]] = []
        try:
            processes.append(self._spawn(
                ["/usr/bin/q-sunshine", "capture=qemu_dbus", "system_tray=false",
                 f"bind_address={self._transport_bind_host}", f"port={spec.route.media_port}"],
                sunshine_environment))
            # qsf-control is deliberately local only.  The global terminal
            # broker later starts its ticket gateway with an audience fixed by
            # the already-authorized VM, not the remote request.
            processes.append(self._spawn(
                ["/usr/bin/q-sunshine-qsf-control",
                 f"--agent-socket={spec.environment['QSUNSHINE_QSF_AGENT_SOCKET']}",
                 f"--control-socket={control_socket}", f"--token-file={token_file}"],
                qsf_environment, journal_output=True))
            self._wait_for_qsf_control(control_socket, token_file, processes)
            qsf_environment["QSUNSHINE_AUTH_TICKET_KEY"] = str(self._ticket_key_path)
            qsf_environment["QSUNSHINE_AUTH_AUDIENCE"] = spec.audience
            processes.append(self._spawn(
                ["/usr/bin/q-sunshine-qsf-terminal-gateway",
                 f"--control-socket={control_socket}", f"--token-file={token_file}",
                 f"--server-cert={qsf_certificate}", f"--server-key={qsf_key}",
                 f"--listen-host={self._transport_bind_host}",
                 f"--listen-port={spec.route.qsf_port}",
                 "--max-concurrent-requests=16"],
                qsf_environment, journal_output=True))
            # Popen succeeding does not prove the executable is healthy, but
            # a fast exit catches missing paths, invalid config and listener
            # collisions before a signed route is returned to the caller.
            time.sleep(0.08)
            if any(process.poll() is not None for process in processes):
                raise TerminalError("terminal transport worker failed during startup")
            return processes
        except Exception:
            self._terminate(processes)
            raise

    def ensure_started(self, route: Any) -> WorkerSpec:
        spec = self._worker_spec(route)
        with self._lock:
            previous = self._workers.get(spec.vmid)
            if previous is not None:
                previous_spec, processes = previous
                if all(process.poll() is None for process in processes):
                    # PVE topology and root-local config can change only
                    # through administrative action.  Never silently return a
                    # previous worker if its authoritative route changed.
                    if previous_spec != spec:
                        raise TerminalError("terminal VM policy changed; retry after worker restart")
                    return previous_spec
                self._terminate(processes)
                self._workers.pop(spec.vmid, None)
            processes = self._start_locked(spec)
            self._workers[spec.vmid] = (spec, processes)
            return spec

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
            buses = list(self._dbus_buses.values())
            self._dbus_buses.clear()
        for _spec, processes in workers:
            self._terminate(processes)
        self._terminate(buses)


@dataclass(frozen=True)
class PendingDescriptor:
    """A one-use PVE-to-native-client handoff, retained only in RAM."""

    expires_at: int
    subject: str
    worker: WorkerSpec
    endpoint: NodeEndpoint


class DescriptorRegistry:
    """Issue opaque, short-lived launch descriptors without retaining them."""

    def __init__(self, ttl_seconds: int) -> None:
        if not 1 <= ttl_seconds <= MAX_DESCRIPTOR_TTL_SECONDS:
            raise TerminalError("terminal descriptor lifetime is invalid")
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._descriptors: dict[bytes, PendingDescriptor] = {}

    @staticmethod
    def _digest(descriptor: str) -> bytes:
        return hashlib.sha256(descriptor.encode("ascii", "strict")).digest()

    def issue(self, subject: str, worker: WorkerSpec,
              endpoint: NodeEndpoint) -> tuple[str, int]:
        if not valid_subject(subject):
            raise TerminalError("authentication failed")
        descriptor = "qsd1." + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
        expires_at = int(time.time()) + self._ttl_seconds
        with self._lock:
            self._prune_locked(int(time.time()))
            self._descriptors[self._digest(descriptor)] = PendingDescriptor(
                expires_at=expires_at, subject=subject, worker=worker, endpoint=endpoint)
        return descriptor, expires_at

    def redeem(self, descriptor: str) -> PendingDescriptor:
        if not isinstance(descriptor, str) or not _DESCRIPTOR_PATTERN.fullmatch(descriptor):
            raise TerminalError("authentication failed")
        with self._lock:
            self._prune_locked(int(time.time()))
            pending = self._descriptors.pop(self._digest(descriptor), None)
            if pending is None or pending.expires_at <= int(time.time()):
                raise TerminalError("authentication failed")
            return pending

    def _prune_locked(self, now: int) -> None:
        for digest, pending in tuple(self._descriptors.items()):
            if pending.expires_at <= now:
                self._descriptors.pop(digest, None)


class TerminalBroker:
    """Turn one PVE ACL-authorized VM handoff into native transport admission."""

    def __init__(self, *, worker_manager: WorkerManagerProtocol, ticket_key: bytes,
                 node_endpoints: Mapping[str, NodeEndpoint], endpoint_ca_pem: str,
                 local_node: str,
                 ticket_ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
                 descriptor_ttl_seconds: int = DEFAULT_DESCRIPTOR_TTL_SECONDS) -> None:
        if (len(ticket_key) != 32 or
                not TICKET_TTL_MIN_SECONDS <= ticket_ttl_seconds <= TICKET_TTL_MAX_SECONDS or
                not isinstance(local_node, str) or not _PVE_NODE_PATTERN.fullmatch(local_node) or
                not node_endpoints or not endpoint_ca_pem):
            raise TerminalError("terminal broker configuration is invalid")
        checked_endpoints: dict[str, NodeEndpoint] = {}
        for node, endpoint in node_endpoints.items():
            normalised_host = _normalise_host(getattr(endpoint, "host", None))
            normalised_server_name = _normalise_host(getattr(endpoint, "server_name", None))
            if (not isinstance(node, str) or not _PVE_NODE_PATTERN.fullmatch(node) or
                    not isinstance(endpoint, NodeEndpoint) or normalised_host is None or
                    not _valid_port(endpoint.port) or normalised_server_name is None):
                raise TerminalError("terminal broker configuration is invalid")
            checked_endpoints[node] = NodeEndpoint(
                host=normalised_host, port=endpoint.port,
                server_name=normalised_server_name)
        self._worker_manager = worker_manager
        self._ticket_key = ticket_key
        self._ticket_ttl_seconds = ticket_ttl_seconds
        self._node_endpoints = checked_endpoints
        self._endpoint_ca_pem = endpoint_ca_pem
        self._local_node = local_node
        self._sessions = TerminalSessionRegistry()
        self._descriptors = DescriptorRegistry(descriptor_ttl_seconds)

    @staticmethod
    def parse_pve_acl_launch(payload: Any) -> PveAclLaunchRequest:
        """Accept only the fixed root-local PVE API handoff schema.

        This parser deliberately has no PVE credential field.  The caller is the
        PVE API endpoint after its own authentication and ``VM.Console`` ACL
        check; the Unix peer-credential gate makes that assertion local to
        the PVE/terminal service boundary rather than a network protocol.
        """
        version = payload.get("version") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or
                set(payload) != {"version", "op", "node", "vmid", "subject"} or
                type(version) is not int or version != PROTOCOL_VERSION or
                payload.get("op") != PVE_ACL_LAUNCH_OPERATION):
            raise TerminalError("authentication failed")
        subject = payload.get("subject")
        node = payload.get("node")
        vmid = payload.get("vmid")
        if (not _safe_ascii(subject, maximum=MAX_PVE_SUBJECT_BYTES) or
                not valid_subject(subject) or subject.count("@") != 1 or
                not isinstance(node, str) or not _PVE_NODE_PATTERN.fullmatch(node) or
                not _valid_vmid(vmid)):
            raise TerminalError("authentication failed")
        return PveAclLaunchRequest(subject=subject, node=node, vmid=vmid)

    def _endpoint_for_node(self, node: str) -> NodeEndpoint:
        endpoint = self._node_endpoints.get(node)
        if endpoint is None:
            raise TerminalError("authentication failed")
        return endpoint

    def _issue_descriptor(self, claim: AuthorizedPveVm) -> dict[str, Any]:
        endpoint = self._endpoint_for_node(claim.node)
        worker = self._worker_manager.ensure_started(claim)
        descriptor, expires_at = self._descriptors.issue(claim.subject, worker, endpoint)
        return {
            "version": PROTOCOL_VERSION,
            "kind": LAUNCH_DESCRIPTOR_KIND,
            "endpoint": {
                "host": endpoint.host,
                "port": endpoint.port,
                "server_name": endpoint.server_name,
                "ca_pem": self._endpoint_ca_pem,
            },
            "claim": descriptor,
            "expires_at_utc_ms": expires_at * 1000,
        }

    def launch_from_pve_acl(self, payload: Any) -> dict[str, Any]:
        """Issue a descriptor for PVE's already ACL-authorized local request.

        No network listener calls this method.  ``PveAclLaunchRequest`` is
        accepted only by :class:`PveAclLaunchServer`, which requires a Linux
        ``SO_PEERCRED`` peer UID of zero.  Retaining the map and local-node
        checks prevents the node-level terminal service from becoming a
        cluster-wide descriptor oracle if a local PVE handler is misrouted.
        """
        try:
            request = self.parse_pve_acl_launch(payload)
            self._endpoint_for_node(request.node)
            if request.node != self._local_node:
                raise TerminalError("authentication failed")
            return self._issue_descriptor(AuthorizedPveVm(
                subject=request.subject, node=request.node, vmid=request.vmid))
        except (AuthError, TerminalError, OSError, ValueError) as error:
            raise TerminalError("authentication failed") from error

    def redeem_descriptor(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"claim", "op", "version"} or \
                payload.get("version") != PROTOCOL_VERSION or \
                payload.get("op") != DESCRIPTOR_REDEEM_OPERATION:
            raise TerminalError("authentication failed")
        descriptor = payload.get("claim")
        if not isinstance(descriptor, str):
            raise TerminalError("authentication failed")
        pending = self._descriptors.redeem(descriptor)
        try:
            ticket, expires_at = issue_ticket(
                self._ticket_key, subject=pending.subject, audience=pending.worker.audience,
                ttl_seconds=self._ticket_ttl_seconds)
            self._sessions.remember(ticket, expires_at, pending.worker.lease_config)
            return {
                "version": PROTOCOL_VERSION,
                "session_token": ticket,
                "subject": pending.subject,
                "audience": pending.worker.audience,
                "expires_at_utc_ms": expires_at * 1000,
                "routes": pending.worker.route.to_wire(),
                # This is public trust material authenticated by the first
                # descriptor TLS connection. The native client keeps it only
                # in a mode-0600 ephemeral file while its patched Moonlight
                # child needs the existing CA-file contract.
                "server_name": pending.endpoint.server_name,
                "ca_pem": self._endpoint_ca_pem,
            }
        except AuthError as error:
            raise TerminalError("authentication failed") from error

    def issue_gamestream_lease(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("op") != GAMESTREAM_LEASE_OPERATION:
            raise TerminalError("authentication failed")
        token = payload.get("session_token")
        if not isinstance(token, str):
            raise TerminalError("authentication failed")
        config = self._sessions.lease_config_for(token)
        try:
            return issue_lease(self._ticket_key, payload, config)
        except AuthError as error:
            raise TerminalError("authentication failed") from error


def _parse_pve_acl_json(raw: bytes) -> dict[str, Any]:
    """Decode one internal JSON object without duplicate-key ambiguity."""
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("invalid JSON constant")

    try:
        payload = json.loads(
            raw.decode("ascii"), object_pairs_hook=unique_object,
            parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise TerminalError("authentication failed") from error
    if not isinstance(payload, dict):
        raise TerminalError("authentication failed")
    return payload


_UNIX_PEERCRED = struct.Struct("3i")


def _require_root_unix_peer(connection: socket.socket) -> None:
    """Fail closed unless Linux attached a root ``SO_PEERCRED`` identity."""
    peer_credential_option = getattr(socket, "SO_PEERCRED", None)
    if peer_credential_option is None:
        raise TerminalError("terminal PVE ACL socket is unavailable")
    try:
        credentials = connection.getsockopt(
            socket.SOL_SOCKET, peer_credential_option, _UNIX_PEERCRED.size)
        pid, uid, gid = _UNIX_PEERCRED.unpack(credentials)
    except (OSError, struct.error, TypeError) as error:
        raise TerminalError("authentication failed") from error
    # A credential record with a non-positive PID or negative gid is not a
    # valid Linux ``struct ucred`` either.  It makes fake/truncated values
    # fail closed in addition to enforcing the actual root-UID boundary.
    if pid <= 0 or uid != 0 or gid < 0:
        raise TerminalError("authentication failed")


def _pve_acl_socket_path(value: object) -> Path:
    """Return one short, lexical absolute pathname for the AF_UNIX listener."""
    path = _require_absolute_path(value, "terminal PVE ACL socket")
    try:
        encoded_path = os.fsencode(str(path))
    except UnicodeEncodeError as error:
        raise TerminalError("terminal PVE ACL socket is invalid") from error
    if (path == path.parent or any(part == ".." for part in path.parts) or
            len(encoded_path) >= 104):
        # Keep clear of the kernel's short ``sockaddr_un.sun_path`` limit and
        # never make cleanup resolve a caller-supplied parent traversal.
        raise TerminalError("terminal PVE ACL socket is invalid")
    return path


def _validate_pve_acl_socket_parent(path: Path) -> None:
    """Require an existing root-local directory; never create arbitrary paths."""
    try:
        metadata = os.lstat(path.parent)
    except OSError as error:
        raise TerminalError("terminal PVE ACL socket directory is unavailable") from error
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o022 or
            (os.geteuid() == 0 and metadata.st_uid != 0)):
        raise TerminalError("terminal PVE ACL socket directory is unsafe")


def _remove_stale_pve_acl_socket(path: Path) -> None:
    """Remove only a prior private socket, never a caller-selected file."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise TerminalError("cannot inspect terminal PVE ACL socket") from error
    if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_mode & 0o077 or
            (os.geteuid() == 0 and metadata.st_uid != 0)):
        raise TerminalError("terminal PVE ACL socket is unsafe")
    try:
        path.unlink()
    except OSError as error:
        raise TerminalError("cannot remove stale terminal PVE ACL socket") from error


class PveAclLaunchRequestHandler(socketserver.BaseRequestHandler):
    """Serve only root-local, PVE ACL-authorized descriptor requests."""

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, PveAclLaunchServer)
        response: dict[str, Any] = {"ok": False, "error": "authentication failed"}
        try:
            self.request.settimeout(PVE_ACL_REQUEST_TIMEOUT_SECONDS)
            _require_root_unix_peer(self.request)
            payload = _parse_pve_acl_json(
                read_line(self.request, MAX_PVE_ACL_REQUEST_BYTES))
            # Deliberately return the raw launch envelope.  It contains no
            # PVE bearer artifact and is passed by PVE directly to its
            # Console action, without a broker-side HTTP launch path.
            response = server.broker.launch_from_pve_acl(payload)
        except (OSError, AuthError, TerminalError):
            pass
        try:
            self.request.sendall(encode_response(response))
        except OSError:
            pass


class PveAclLaunchServer(BoundedThreadingMixIn, socketserver.UnixStreamServer):
    """One private AF_UNIX listener from PVE's root API handler to the broker."""

    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, socket_path: Path, broker: TerminalBroker, *,
                 max_concurrent_requests: int) -> None:
        self._socket_path = _pve_acl_socket_path(str(socket_path))
        _validate_pve_acl_socket_parent(self._socket_path)
        _remove_stale_pve_acl_socket(self._socket_path)
        self.broker = broker
        self._owns_socket_path = False
        try:
            super().__init__(str(self._socket_path), PveAclLaunchRequestHandler,
                             max_concurrent_requests=max_concurrent_requests)
            self._validate_bound_socket()
        except BaseException:
            self._unlink_owned_socket()
            raise

    def server_bind(self) -> None:
        # The parent is already non-writable by other identities.  A narrow
        # umask protects the bind-to-chmod interval as well.
        previous_umask = os.umask(0o077)
        try:
            super().server_bind()
            # Mark ownership immediately after bind so an error in the
            # following hardening steps still cleans up this service's inode.
            self._owns_socket_path = True
            if os.geteuid() == 0:
                os.chown(self._socket_path, 0, 0)
            os.chmod(self._socket_path, 0o600)
        except OSError as error:
            raise TerminalError("cannot create terminal PVE ACL socket") from error
        finally:
            os.umask(previous_umask)

    def _validate_bound_socket(self) -> None:
        try:
            metadata = os.lstat(self._socket_path)
        except OSError as error:
            raise TerminalError("terminal PVE ACL socket is unavailable") from error
        if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_mode & 0o077 or
                (os.geteuid() == 0 and metadata.st_uid != 0)):
            raise TerminalError("terminal PVE ACL socket is unsafe")

    def _unlink_owned_socket(self) -> None:
        if not self._owns_socket_path:
            return
        try:
            metadata = os.lstat(self._socket_path)
            if (stat.S_ISSOCK(metadata.st_mode) and not metadata.st_mode & 0o077 and
                    (os.geteuid() != 0 or metadata.st_uid == 0)):
                self._socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Never turn shutdown into deletion of a file whose type or
            # ownership changed unexpectedly; the safe parent makes that a
            # service-administration failure rather than a data-loss risk.
            pass
        finally:
            self._owns_socket_path = False

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self._unlink_owned_socket()


class TerminalRequestHandler(socketserver.BaseRequestHandler):
    """Serve only native descriptor redemption and GameStream lease JSON."""

    @staticmethod
    def _handle_native(server: "TerminalServer", connection: socket.socket,
                       source: str, initial: bytes) -> None:
        response: dict[str, Any] = {"ok": False, "error": "authentication failed"}
        try:
            raw = read_line(connection, MAX_REQUEST_BYTES, initial=initial)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or not server.limiter.allow(source):
                raise TerminalError("authentication failed")
            if payload.get("op") == DESCRIPTOR_REDEEM_OPERATION:
                response = {"ok": True, "result": server.broker.redeem_descriptor(payload)}
            elif payload.get("op") == GAMESTREAM_LEASE_OPERATION:
                response = {"ok": True, "result": server.broker.issue_gamestream_lease(payload)}
            else:
                raise TerminalError("authentication failed")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AuthError, TerminalError):
            server.limiter.record_failure(source)
        try:
            connection.sendall(encode_response(response))
        except OSError:
            pass

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, TerminalServer)
        source = str(self.client_address[0])
        try:
            self.request.settimeout(TLS_HANDSHAKE_TIMEOUT_SECONDS)
            self.request.do_handshake()
            self.request.settimeout(TLS_REQUEST_TIMEOUT_SECONDS)
            initial = self.request.recv(1)
            if not initial:
                raise TerminalError("authentication failed")
            if initial == b"{":
                self._handle_native(server, self.request, source, initial)
            else:
                server.limiter.record_failure(source)
                self.request.sendall(encode_response({"ok": False, "error": "authentication failed"}))
        except (OSError, ssl.SSLError, TerminalError):
            # A malformed TLS request has no dependable wire protocol to
            # reply on.  It still participates in the bounded failure limit.
            server.limiter.record_failure(source)


class TerminalServer(BoundedThreadingMixIn, socketserver.TCPServer):
    """One TLS acceptor for native redemption and GameStream leases."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], context: ssl.SSLContext,
                 broker: TerminalBroker, *, max_concurrent_requests: int) -> None:
        self._context = context
        self.broker = broker
        self.limiter = AttemptLimiter()
        super().__init__(address, TerminalRequestHandler,
                         max_concurrent_requests=max_concurrent_requests)

    def get_request(self) -> tuple[ssl.SSLSocket, tuple[str, int]]:
        raw, address = self.socket.accept()
        try:
            connection = self._context.wrap_socket(
                raw, server_side=True, do_handshake_on_connect=False)
        except Exception:
            raw.close()
            raise
        return connection, address


def terminal_tls_context(certificate: Path, private_key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    if hasattr(context, "num_tickets"):
        context.num_tickets = 0
    load_tls_server_cert_chain(
        context, certificate, private_key, require_root_owner=os.geteuid() == 0,
        allow_root_group_readable_private_key=(
            os.geteuid() == 0 and is_pve_proxy_tls_material(certificate, private_key)))
    return context


def serve(arguments: argparse.Namespace) -> int:
    if (not _valid_host(arguments.listen_host) or not _valid_host(arguments.transport_bind_host) or
            not _valid_port(arguments.listen_port) or
            not 1 <= arguments.ticket_ttl_seconds <= TICKET_TTL_MAX_SECONDS or
            not 1 <= arguments.descriptor_ttl_seconds <= MAX_DESCRIPTOR_TTL_SECONDS or
            not 1 <= arguments.max_concurrent_requests <= MAX_CONCURRENT_REQUESTS):
        raise TerminalError("terminal service configuration is invalid")
    local_node = arguments.local_node.strip() if arguments.local_node else socket.gethostname().split(".", 1)[0]
    if not _PVE_NODE_PATTERN.fullmatch(local_node):
        raise TerminalError("terminal local Proxmox node is invalid")
    pve_acl_socket = _pve_acl_socket_path(arguments.pve_launch_socket)
    node_endpoints = load_node_endpoints(
        _require_absolute_path(arguments.node_endpoints_file, "terminal node endpoint policy"))
    local_endpoint = node_endpoints.get(local_node)
    if local_endpoint is None:
        raise TerminalError("terminal node endpoint policy has no local node")
    certificate = _require_absolute_path(arguments.server_cert, "terminal TLS certificate")
    private_key = _require_absolute_path(arguments.server_key, "terminal TLS key")
    ticket_key = load_ticket_key(_require_absolute_path(arguments.ticket_key, "terminal ticket key"),
                                 require_root_owner=os.geteuid() == 0)
    endpoint_ca_pem = load_descriptor_ca_pem(
        _require_absolute_path(arguments.descriptor_ca_file, "terminal descriptor CA"))
    manager = TerminalWorkerManager(
        instance_directory=_require_absolute_path(arguments.instance_directory, "instance directory"),
        runtime_directory=_require_absolute_path(arguments.runtime_directory, "runtime directory"),
        vm_runtime_directory=_require_absolute_path(
            arguments.vm_runtime_directory, "VM runtime directory"),
        state_directory=_require_absolute_path(arguments.state_directory, "state directory"),
        ticket_key_path=_require_absolute_path(arguments.ticket_key, "terminal ticket key"),
        terminal_certificate=certificate,
        terminal_key=private_key,
        lease_host=local_endpoint.host,
        lease_port=local_endpoint.port,
        transport_bind_host=arguments.transport_bind_host,
        advertised_host=local_endpoint.host,
        local_node=local_node,
    )
    # The private Display1 buses must exist before pve-guests starts QEMU.
    # This preparation launches no network listener and no Sunshine worker.
    manager.prepare_configured_buses()
    broker = TerminalBroker(
        worker_manager=manager,
        ticket_key=ticket_key,
        node_endpoints=node_endpoints,
        endpoint_ca_pem=endpoint_ca_pem,
        local_node=local_node,
        ticket_ttl_seconds=arguments.ticket_ttl_seconds,
        descriptor_ttl_seconds=arguments.descriptor_ttl_seconds,
    )
    tls_server: TerminalServer | None = None
    pve_acl_server: PveAclLaunchServer | None = None
    threads: list[threading.Thread] = []
    listener_threads: list[tuple[socketserver.BaseServer, threading.Thread]] = []
    stopping = threading.Event()
    previous_handlers: dict[int, Any] = {}
    try:
        pve_acl_server = PveAclLaunchServer(
            pve_acl_socket, broker,
            max_concurrent_requests=arguments.max_concurrent_requests)
        tls_server = TerminalServer(
            (arguments.listen_host, arguments.listen_port),
            terminal_tls_context(certificate, private_key), broker,
            max_concurrent_requests=arguments.max_concurrent_requests)

        def request_stop(_signum: int, _frame: Any) -> None:
            stopping.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        thread = threading.Thread(
            target=tls_server.serve_forever, kwargs={"poll_interval": 0.25},
            name="q-sunshine-terminal-tls", daemon=True)
        thread.start()
        threads.append(thread)
        listener_threads.append((tls_server, thread))
        pve_acl_thread = threading.Thread(
            target=pve_acl_server.serve_forever, kwargs={"poll_interval": 0.25},
            name="q-sunshine-terminal-pve-acl", daemon=True)
        pve_acl_thread.start()
        threads.append(pve_acl_thread)
        listener_threads.append((pve_acl_server, pve_acl_thread))
        host, port = tls_server.server_address[:2]
        print(f"Q_SUNSHINE_TERMINAL_READY host={host} port={port}", flush=True)
        print(f"Q_SUNSHINE_PVE_ACL_READY socket={pve_acl_socket}", flush=True)
        while not stopping.wait(0.25):
            if any(not thread.is_alive() for thread in threads):
                raise TerminalError("terminal listener stopped unexpectedly")
    finally:
        # ``BaseServer.shutdown()`` waits for ``serve_forever()``.  Do not
        # call it for a listener that was constructed but never threaded (for
        # example if creation of the second listener failed); ``server_close``
        # below is the safe cleanup path for that case.
        for listener, listener_thread in listener_threads:
            if listener_thread.is_alive():
                try:
                    listener.shutdown()
                except OSError:
                    pass
        for thread in threads:
            thread.join(timeout=2.0)
        if tls_server is not None:
            tls_server.server_close()
        if pve_acl_server is not None:
            pve_acl_server.server_close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        manager.stop_all()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-cert", required=True, help="root-owned PEM TLS certificate")
    parser.add_argument("--server-key", required=True, help="root-owned PEM TLS private key")
    parser.add_argument("--ticket-key", required=True, help="root-owned raw 32-byte ticket key")
    parser.add_argument("--descriptor-ca-file", required=True,
                        help="root-owned PEM CA placed in the PVE-issued .qsm descriptor")
    parser.add_argument("--instance-directory", default="/etc/q-sunshine/instances.d")
    parser.add_argument("--runtime-directory", default="/run/q-sunshine/terminal")
    parser.add_argument("--vm-runtime-directory", default="/run/q-sunshine")
    parser.add_argument("--state-directory", default="/var/lib/q-sunshine")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=48123)
    parser.add_argument("--pve-launch-socket", default=DEFAULT_PVE_ACL_LAUNCH_SOCKET,
                        help="root-only AF_UNIX handoff socket for PVE's authorized VM.Console API")
    parser.add_argument("--node-endpoints-file", required=True,
                        help="root-owned shared node-to-broker JSON policy")
    parser.add_argument("--transport-bind-host", default="127.0.0.1")
    parser.add_argument("--local-node", default="",
                        help="optional exact PVE node name served by this process")
    parser.add_argument("--ticket-ttl-seconds", type=int, default=DEFAULT_TICKET_TTL_SECONDS)
    parser.add_argument("--descriptor-ttl-seconds", type=int, default=DEFAULT_DESCRIPTOR_TTL_SECONDS)
    parser.add_argument("--max-concurrent-requests", type=int,
                        default=DEFAULT_MAX_CONCURRENT_REQUESTS)
    arguments = parser.parse_args()
    try:
        return serve(arguments)
    except (TerminalError, AuthError, OSError, ssl.SSLError) as error:
        print(f"q-sunshine-terminal: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
