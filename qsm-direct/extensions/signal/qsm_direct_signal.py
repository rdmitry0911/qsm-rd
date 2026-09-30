#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Node-local HTTPS signalling service for the QSM Direct browser console.

This is the *structural* alternative to registering a private ``PVE::API2``
route inside pveproxy/pvedaemon.  PVE has no supported route plug-in ABI, so
the earlier design shipped package-owned launchers that verified pinned PVE
file checksums before loading a module.  This service needs none of that: it
listens on its own TLS port with the node's own certificate and authorises
every request by relaying the browser's PVE ticket to the node's own
``/access/ticket`` endpoint, which is a stable, documented PVE API across 7,
8 and 9.  PVE remains the sole authority for authentication and the
``VM.Console`` / ``VM.Config.Options`` ACL; this service mints nothing.

Trust model:

* The browser sends its PVE ticket (the ``PVEAuthCookie`` value, which the
  PVE UI itself reads from JavaScript) and its user id *in the request body*,
  never as an ambient cookie.  A cross-site page cannot read that cookie, so
  the endpoints are inherently CSRF-safe and no ``Allow-Credentials`` CORS is
  used.
* Authorisation is delegated: the service asks the local pveproxy to verify
  ``username`` + ``ticket`` and the required privilege on ``/vms/<vmid>``.
  Only the username PVE confirms is passed to the terminal as the subject.
* The service reaches the unchanged ``qsm-pve-direct-terminal`` over its
  existing root-only Unix socket, exactly as the retired PVE route did.

It never logs a ticket, SDP, cookie or guest pixel; the journal carries only
fixed diagnostic labels.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socket
import ssl
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
PVE_OPERATION = "pve_acl_webrtc"
MAX_BODY_BYTES = 128 * 1024
MAX_TICKET_BYTES = 8192
REQUEST_TIMEOUT_SECONDS = 30.0
_NODE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.-]{0,62}\Z")
_VMID = re.compile(r"\A[1-9][0-9]{1,8}\Z")
_USER = re.compile(r"\A[^\s@/:\\\x00-\x1f]{1,64}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_CODEC = re.compile(r"\A(?:auto|h264|hevc)\Z")
_ENCODER = re.compile(r"\A(?:auto|hardware|software)\Z")
_ROUTE = re.compile(
    r"\A/nodes/(?P<node>[^/]+)/(?P<kind>qemu|lxc)/(?P<vmid>[0-9]+)/(?P<action>qsm-direct|qsm-direct-settings)\Z")


class SignalError(Exception):
    """A request could not be served; the status is deliberately generic."""

    def __init__(self, status: int, code: str = "request failed") -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class PveAuthority:
    """Delegates authentication and ACL checks to the node's own pveproxy."""

    def __init__(self, pve_url: str, ca_file: Path) -> None:
        self._ticket_url = pve_url.rstrip("/") + "/api2/json/access/ticket"
        # Verify the loopback pveproxy by CA signature.  Hostname verification
        # is disabled on purpose: the connection never leaves the host, so the
        # CA signature is the meaningful check.  pveproxy may serve EITHER the
        # self-signed node certificate (signed by the PVE cluster CA) OR a
        # custom/ACME certificate (e.g. Let's Encrypt, when the node is reached
        # by domain name) — that one is signed by a public CA, not the cluster
        # CA.  Trust BOTH the system CA store and the cluster CA so ticket
        # verification keeps working after an administrator installs a domain
        # certificate; otherwise every console offer failed with 502.
        context = ssl.create_default_context()
        try:
            context.load_verify_locations(cafile=str(ca_file))
        except OSError:
            # A missing cluster CA is non-fatal: the system store still verifies
            # a custom/ACME pveproxy certificate.
            pass
        context.check_hostname = False
        self._context = context

    def authorize(self, username: str, ticket: str, vmid: int, privilege: str) -> str:
        """Return the confirmed username, or raise SignalError(401)."""
        if not _USER.fullmatch(username) or not (1 <= len(ticket) <= MAX_TICKET_BYTES):
            raise SignalError(401, "authentication failure")
        form = urllib.parse.urlencode({
            "username": username,
            "password": ticket,
            "path": f"/vms/{vmid}",
            "privs": privilege,
        }).encode("ascii")
        request = urllib.request.Request(self._ticket_url, data=form, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS,
                                        context=self._context) as response:
                body = json.loads(response.read(MAX_BODY_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as error:
            # 401 from PVE means the ticket is invalid or lacks the privilege.
            raise SignalError(401, "authentication failure") from error
        except (OSError, ValueError) as error:
            raise SignalError(502, "authority unavailable") from error
        data = body.get("data") if isinstance(body, dict) else None
        confirmed = data.get("username") if isinstance(data, dict) else None
        if not isinstance(confirmed, str) or not _USER.fullmatch(confirmed):
            raise SignalError(401, "authentication failure")
        return confirmed


class TerminalClient:
    """Speaks the unchanged qsm-pve-direct-terminal Unix-socket protocol."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    def create_transport(self, node: str, vmid: int, subject: str, sdp: str,
                         width: int, height: int, fps: int) -> dict[str, str]:
        request = {
            "version": PROTOCOL_VERSION, "op": PVE_OPERATION, "node": node, "vmid": vmid,
            "subject": subject, "sdp": sdp, "sdp_type": "offer",
            "width": width, "height": height, "fps": fps,
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(REQUEST_TIMEOUT_SECONDS)
                connection.connect(str(self._socket_path))
                connection.sendall(json.dumps(request, separators=(",", ":")).encode("ascii") + b"\n")
                payload = _read_line(connection)
        except OSError as error:
            raise SignalError(503, "console terminal unavailable") from error
        try:
            response = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise SignalError(502, "console terminal error") from error
        if not isinstance(response, dict) or not response.get("ok"):
            raise SignalError(502, "console terminal error")
        result = response.get("result")
        if not isinstance(result, dict) or set(result) != {"type", "sdp"} or \
                result.get("type") != "answer" or not isinstance(result.get("sdp"), str):
            raise SignalError(502, "console terminal error")
        return result


class VmPolicyStore:
    """Reads and writes the per-VM codec policy the terminal already consumes."""

    _DEFAULTS = {"codec": "auto", "encoder": "auto"}

    def __init__(self, instance_directory: Path) -> None:
        self._directory = instance_directory

    def _path(self, vmid: int) -> Path:
        return self._directory / f"{vmid}.conf"

    def read(self, vmid: int) -> dict[str, str]:
        path = self._path(vmid)
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return dict(self._DEFAULTS)
        except OSError as error:
            raise SignalError(503, "policy unavailable") from error
        if not path.is_file() or stat.st_uid not in (0, os.geteuid()) or \
                (stat.st_mode & 0o022) or stat.st_size > 16384:
            raise SignalError(503, "policy unavailable")
        values: dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line or line.startswith("#"):
                    continue
                match = re.fullmatch(r"([A-Z0-9_]+)=([^\r\n\x00]*)", line)
                if not match or match.group(1) in values:
                    raise SignalError(503, "policy unavailable")
                values[match.group(1)] = match.group(2)
        except OSError as error:
            raise SignalError(503, "policy unavailable") from error
        codec = values.get("QSM_DIRECT_CODEC", "auto")
        encoder = values.get("QSM_DIRECT_ENCODER_MODE", "auto")
        if not _CODEC.fullmatch(codec) or not _ENCODER.fullmatch(encoder) or \
                (codec == "hevc" and encoder == "software"):
            raise SignalError(503, "policy unavailable")
        return {"codec": codec, "encoder": encoder}

    def write(self, vmid: int, codec: str, encoder: str) -> dict[str, str]:
        if not _CODEC.fullmatch(codec) or not _ENCODER.fullmatch(encoder) or \
                (codec == "hevc" and encoder == "software"):
            raise SignalError(400, "invalid policy")
        if not self._directory.is_dir() or self._directory.stat().st_uid not in (0, os.geteuid()):
            raise SignalError(503, "policy unavailable")
        existing = self.read(vmid)
        address = self._read_dbus_address(vmid)
        temporary = self._directory / f".{vmid}.conf.tmp"
        lines = [f"QSM_DIRECT_QEMU_DBUS_ADDRESS={address}\n"] if address else []
        lines += [f"QSM_DIRECT_CODEC={codec}\n", f"QSM_DIRECT_ENCODER_MODE={encoder}\n"]
        # Preserve a pinned explicit encoder if one was set out of band.
        if existing.get("encoder_binary"):
            lines.append(f"QSM_DIRECT_ENCODER={existing['encoder_binary']}\n")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("".join(lines))
            os.replace(temporary, self._path(vmid))
        except OSError as error:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise SignalError(503, "policy unavailable") from error
        return {"codec": codec, "encoder": encoder}

    def _read_dbus_address(self, vmid: int) -> str | None:
        try:
            for line in self._path(vmid).read_text(encoding="utf-8").splitlines():
                if line.startswith("QSM_DIRECT_QEMU_DBUS_ADDRESS="):
                    return line.split("=", 1)[1]
        except OSError:
            return None
        return None


class ContainerPolicyStore(VmPolicyStore):
    """Codec policy of an LXC console, kept in its own root-owned namespace.

    The file's presence is what enables the container for the QSM console
    (written by the operator/setup tool), so settings are only readable and
    writable for an enabled container, and its session-user line is kept.
    """

    _PRESERVED = ("QSM_DIRECT_LXC_UID", "QSM_DIRECT_LXC_DISPLAY", "QSM_DIRECT_ENCODER")

    def read(self, vmid: int) -> dict[str, str]:
        if not self._path(vmid).exists():
            raise SignalError(404, "container console not enabled")
        return super().read(vmid)

    def write(self, vmid: int, codec: str, encoder: str) -> dict[str, str]:
        if not _CODEC.fullmatch(codec) or not _ENCODER.fullmatch(encoder) or \
                (codec == "hevc" and encoder == "software"):
            raise SignalError(400, "invalid policy")
        path = self._path(vmid)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as error:
            raise SignalError(404, "container console not enabled") from error
        except OSError as error:
            raise SignalError(503, "policy unavailable") from error
        kept = [line for line in lines
                if line.split("=", 1)[0].strip() in self._PRESERVED]
        body = "".join(f"{line}\n" for line in kept)
        body += f"QSM_DIRECT_CODEC={codec}\nQSM_DIRECT_ENCODER_MODE={encoder}\n"
        temporary = self._directory / f".{vmid}.conf.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(temporary, path)
        except OSError as error:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise SignalError(503, "policy unavailable") from error
        return {"codec": codec, "encoder": encoder}


class SignalConfig:
    def __init__(self, authority: PveAuthority, terminal: TerminalClient,
                 policy: VmPolicyStore, local_node: str | None,
                 container_policy: VmPolicyStore | None = None) -> None:
        self.authority = authority
        self.terminal = terminal
        self.policy = policy
        self.local_node = local_node
        self.container_policy = container_policy or policy


def _read_line(connection: socket.socket) -> bytes:
    payload = bytearray()
    while len(payload) <= MAX_BODY_BYTES:
        block = connection.recv(min(65536, MAX_BODY_BYTES + 1 - len(payload)))
        if not block:
            break
        payload.extend(block)
        newline = payload.find(b"\n")
        if newline >= 0:
            return bytes(payload[:newline])
    raise SignalError(502, "console terminal error")


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SignalError(400, "invalid request")
    return value


def handle_request(config: SignalConfig, method: str, path: str,
                   body: dict[str, Any]) -> dict[str, Any]:
    """Pure request core, shared by the HTTP handler and the tests."""
    match = _ROUTE.fullmatch(path)
    if not match:
        raise SignalError(404, "not found")
    node = match.group("node")
    if not _NODE.fullmatch(node) or not _VMID.fullmatch(match.group("vmid")):
        raise SignalError(400, "invalid request")
    if config.local_node is not None and node != config.local_node:
        raise SignalError(404, "not found")
    vmid = int(match.group("vmid"))
    action = match.group("action")
    # PVE's ACL path is /vms/<vmid> for both QEMU VMs and LXC containers, and
    # the terminal tells them apart from the node's own configs.  The kind only
    # selects where the per-guest codec policy lives.
    policy = config.container_policy if match.group("kind") == "lxc" else config.policy
    if not isinstance(body, dict):
        raise SignalError(400, "invalid request")
    username = body.get("user")
    ticket = body.get("ticket")
    if not isinstance(username, str) or not isinstance(ticket, str):
        raise SignalError(401, "authentication failure")

    if action == "qsm-direct" and method == "POST":
        subject = config.authority.authorize(username, ticket, vmid, "VM.Console")
        sdp = body.get("sdp")
        if not isinstance(sdp, str) or not sdp.isascii() or not 1 <= len(sdp) <= MAX_BODY_BYTES:
            raise SignalError(400, "invalid request")
        width = _integer(body.get("width"), 64, 16384)
        height = _integer(body.get("height"), 64, 16384)
        fps = _integer(body.get("fps"), 10, 240)
        if width % 2 or height % 2:
            raise SignalError(400, "invalid request")
        result = config.terminal.create_transport(node, vmid, subject, sdp, width, height, fps)
        return {"data": {"type": result["type"], "sdp": result["sdp"]}}

    if action == "qsm-direct-settings" and method == "GET":
        config.authority.authorize(username, ticket, vmid, "VM.Console")
        return {"data": policy.read(vmid)}

    if action == "qsm-direct-settings" and method == "PUT":
        config.authority.authorize(username, ticket, vmid, "VM.Config.Options")
        codec = body.get("codec")
        encoder = body.get("encoder")
        if not isinstance(codec, str) or not isinstance(encoder, str):
            raise SignalError(400, "invalid request")
        return {"data": policy.write(vmid, codec, encoder)}

    raise SignalError(405, "method not allowed")


class SignalHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "qsm-direct-signal"
    sys_version = ""

    @property
    def _config(self) -> SignalConfig:
        return self.server.signal_config  # type: ignore[attr-defined]

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        # Reflect only a syntactically valid https origin; credentials are in
        # the body, so no Allow-Credentials is emitted.
        if re.fullmatch(r"https://[A-Za-z0-9.\-:\[\]]{1,255}", origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-QSM-User")
        self.send_header("Access-Control-Max-Age", "600")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _read_body(self) -> dict[str, Any]:
        length = self.headers.get("Content-Length")
        if length is None:
            return {}
        try:
            size = int(length)
        except ValueError:
            raise SignalError(400, "invalid request")
        if size < 0 or size > MAX_BODY_BYTES:
            raise SignalError(413, "request too large")
        raw = self.rfile.read(size) if size else b""
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise SignalError(400, "invalid request") from error
        if not isinstance(value, dict):
            raise SignalError(400, "invalid request")
        return value

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            body = self._read_body()
            # Credentials travel in headers, never in the URL or the logged
            # body: Authorization: Bearer <PVE ticket>, X-QSM-User: <user>.
            authorization = self.headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                body["ticket"] = authorization[len("Bearer "):]
            user = self.headers.get("X-QSM-User")
            if user is not None:
                body["user"] = user
            response = handle_request(self._config, method, parsed.path, body)
            self._send(200, response)
        except SignalError as error:
            self._send(error.status, {"errors": error.code})
        except Exception:  # noqa: BLE001 - never leak an internal error to the client
            self._send(500, {"errors": "internal error"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def log_message(self, *_args: Any) -> None:
        # The default handler writes client requests (paths, query strings) to
        # stderr; those can carry a ticket. Stay silent; failures are logged
        # with fixed labels by the dispatch path instead.
        return


class SignalServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: SignalConfig,
                 ssl_context: ssl.SSLContext) -> None:
        super().__init__(address, SignalHandler)
        self.signal_config = config
        self.socket = ssl_context.wrap_socket(self.socket, server_side=True)


def _build_ssl_context(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8007)
    parser.add_argument("--cert", type=Path, default=None)
    parser.add_argument("--key", type=Path, default=None)
    parser.add_argument("--ca", type=Path, default=Path("/etc/pve/pve-root-ca.pem"))
    parser.add_argument("--pve-url", default="https://127.0.0.1:8006")
    parser.add_argument("--terminal-socket", type=Path,
                        default=Path("/run/qsm-pve-direct-terminal/pve-webrtc.sock"))
    parser.add_argument("--instance-directory", type=Path,
                        default=Path("/etc/qsm-pve-direct/instances.d"))
    parser.add_argument("--container-instance-directory", type=Path,
                        default=Path("/etc/qsm-pve-direct/containers.d"))
    parser.add_argument("--local-node", default=None)
    arguments = parser.parse_args(argv)

    # Serve the SAME TLS certificate pveproxy serves so the :8007 signalling
    # origin is valid by whatever name reaches :8006.  pveproxy prefers the
    # custom/ACME pveproxy-ssl.pem when present, else the self-signed
    # pve-ssl.pem; mirror that unless an explicit --cert/--key was given.  A
    # self-signed cert only lists the node name and IPs in its SAN, so reaching
    # the GUI by domain name left the browser unable to POST its offer to :8007.
    cert = arguments.cert
    key = arguments.key
    if cert is None or key is None:
        custom_cert = Path("/etc/pve/local/pveproxy-ssl.pem")
        custom_key = Path("/etc/pve/local/pveproxy-ssl.key")
        use_custom = custom_cert.exists() and custom_key.exists()
        if cert is None:
            cert = custom_cert if use_custom else Path("/etc/pve/local/pve-ssl.pem")
        if key is None:
            key = custom_key if use_custom else Path("/etc/pve/local/pve-ssl.key")

    config = SignalConfig(
        authority=PveAuthority(arguments.pve_url, arguments.ca),
        terminal=TerminalClient(arguments.terminal_socket),
        policy=VmPolicyStore(arguments.instance_directory),
        local_node=arguments.local_node,
        container_policy=ContainerPolicyStore(arguments.container_instance_directory),
    )
    context = _build_ssl_context(cert, key)
    server = SignalServer((arguments.listen, arguments.port), config, context)
    print(f"QSM_DIRECT_SIGNAL_READY listen={arguments.listen}:{arguments.port}",
          file=sys.stderr, flush=True)
    threading.current_thread().name = "qsm-direct-signal"
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
