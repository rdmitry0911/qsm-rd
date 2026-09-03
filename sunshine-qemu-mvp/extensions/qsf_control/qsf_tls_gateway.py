#!/usr/bin/env python3
"""Mutual-TLS gateway from a remote QSF companion to a local control socket.

The local qsf-control token never crosses the network.  The gateway reads it
from its 0600 host-only file, and accepts a single JSON request per TLS
connection only after validating a client certificate issued by the configured
client CA.  It is deliberately a session companion, not an extension to the
GameStream wire protocol.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import ssl
import sys
import threading
from pathlib import Path
from typing import Any

from qsf_control import (ControlError, MAX_CONTROL_LINE,
                         PROFILE_TRANSACTION_TIMEOUT_SECONDS,
                         read_existing_token)

# Keep the ticket primitive in a deliberately separate system-auth component.
# During an installed run the sibling directory is /usr/lib/q-sunshine/
# system_auth; during source tests it is extensions/system_auth.
_SYSTEM_AUTH_MODULE_DIRECTORY = Path(__file__).resolve().parents[1] / "system_auth"
if str(_SYSTEM_AUTH_MODULE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_AUTH_MODULE_DIRECTORY))
from q_sunshine_auth import AuthError as SystemAuthError  # noqa: E402
from q_sunshine_auth import (is_pve_proxy_tls_material, load_ticket_key, load_tls_server_cert_chain,
                             valid_audience, verify_ticket)  # noqa: E402


# A TLS peer has a short bounded period to complete the transport setup and
# send its one request.  Once accepted, connection_optimize may legitimately
# wait for the guest compositor to restart and prove a new VirGL scanout.
# Keep this ingress bound separate from the profile-transaction budget.
TLS_REQUEST_READ_TIMEOUT_SECONDS = 12.0
TLS_HANDSHAKE_TIMEOUT_SECONDS = 12.0
# The local hop must cover the whole transaction plus a small scheduling
# margin.  In particular, it must not time out while the guest is still within
# its documented profile-apply window.
LOCAL_CONTROL_RESPONSE_TIMEOUT_SECONDS = max(
    80.0, PROFILE_TRANSACTION_TIMEOUT_SECONDS + 5.0)
# Every accepted peer receives a worker because TLS handshakes must not block
# the accept loop. Bound those workers so incomplete handshakes and long but
# legitimate profile transactions cannot be amplified into unbounded threads.
DEFAULT_MAX_CONCURRENT_REQUESTS = 16
# The packaged QSF units use TasksMax=48. A 16-worker cap leaves room for the
# broker/main process and avoids advertising a command-line value systemd
# cannot actually sustain during slow TLS or long profile transactions.
MAX_CONCURRENT_REQUESTS = 16


def read_line(connection: socket.socket, limit: int) -> bytes:
    payload = bytearray()
    while b"\n" not in payload:
        chunk = connection.recv(min(65536, limit + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > limit:
            raise ControlError("remote QSF request exceeds the size limit")
    if not payload or b"\n" not in payload:
        raise ControlError("remote QSF request is not newline terminated")
    return bytes(payload).split(b"\n", 1)[0]


def encode_response(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


class LocalControlForwarder:
    """Inject host-local capability material into exactly one broker request."""

    def __init__(self, control_socket: Path, token_file: Path) -> None:
        self._control_socket = control_socket
        self._token_file = token_file

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "token" in payload:
            raise ControlError("remote clients may not supply a local control token")
        token = read_existing_token(self._token_file)
        local_payload = dict(payload)
        local_payload["token"] = token
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as local:
            # This must outlive the broker's bounded guest profile-apply
            # window.  Otherwise a healthy Weston restart gets reported to
            # the Qt client as a transport timeout before the authoritative
            # connection-profile acknowledgement can arrive.
            local.settimeout(LOCAL_CONTROL_RESPONSE_TIMEOUT_SECONDS)
            local.connect(str(self._control_socket))
            local.sendall(encode_response(local_payload))
            response = read_line(local, MAX_CONTROL_LINE)
        try:
            decoded = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControlError("local QSF control returned malformed JSON") from error
        if not isinstance(decoded, dict):
            raise ControlError("local QSF control returned a non-object response")
        return decoded


class RemoteAuthenticator:
    """Select one explicit remote-authentication boundary for this gateway.

    The existing client-certificate route stays available for deployments
    which pre-provision mTLS credentials.  The system-ticket route is for a
    user who has just completed the TLS/PAM login flow and therefore has no
    long-lived client certificate.  It is intentionally an either/or choice:
    accepting both with an accidental optional client certificate would make
    configuration review unnecessarily ambiguous.
    """

    def __init__(self, client_ca: str | None, ticket_key_file: str | None,
                 audience: str | None) -> None:
        has_mtls = bool(client_ca)
        has_ticket = bool(ticket_key_file or audience)
        if has_mtls == has_ticket:
            raise ControlError(
                "configure exactly one remote authentication mode: --client-ca or "
                "--system-auth-ticket-key with --system-auth-audience")
        self._client_ca = client_ca
        self._ticket_key: bytes | None = None
        self._audience: str | None = None
        if has_ticket:
            if not ticket_key_file or not audience or not valid_audience(audience):
                raise ControlError("system-auth ticket mode requires a valid key and audience")
            try:
                self._ticket_key = load_ticket_key(
                    Path(ticket_key_file), require_root_owner=os.geteuid() == 0)
            except SystemAuthError as error:
                raise ControlError("cannot load system-auth ticket key") from error
            self._audience = audience

    @property
    def client_ca(self) -> str | None:
        return self._client_ca

    def validate_and_strip(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate auth and never forward its bearer material locally."""
        if self._ticket_key is None:
            # mTLS certificate verification happened inside the TLS handshake.
            # A bearer credential is not meaningful in this mode and rejecting
            # it avoids accidentally teaching callers that it is accepted.
            if "authorization" in payload:
                raise ControlError("remote authorization does not match this gateway mode")
            return payload
        authorization = payload.get("authorization")
        if (not isinstance(authorization, dict) or
                set(authorization) != {"scheme", "token"} or
                authorization.get("scheme") != "Bearer" or
                not isinstance(authorization.get("token"), str)):
            raise ControlError("authentication required")
        try:
            verify_ticket(self._ticket_key, authorization["token"], audience=self._audience or "")
        except SystemAuthError as error:
            raise ControlError("authentication required") from error
        sanitized = dict(payload)
        sanitized.pop("authorization", None)
        return sanitized


class GatewayRequestHandler(socketserver.BaseRequestHandler):
    """Serve one mTLS-authenticated request then close its connection."""

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, GatewayServer)
        try:
            # GatewayServer deliberately accepts a plain TCP connection and
            # lets this worker perform the TLS handshake.  Calling
            # SSLContext.wrap_socket() on the listening socket instead would
            # make TCPServer.accept() synchronously wait on a peer that never
            # speaks TLS, starving every other QSF companion request.
            self.request.settimeout(TLS_HANDSHAKE_TIMEOUT_SECONDS)
            self.request.do_handshake()
            self.request.settimeout(TLS_REQUEST_READ_TIMEOUT_SECONDS)
            raw = read_line(self.request, MAX_CONTROL_LINE)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ControlError("remote QSF request must be a JSON object")
            response = server.forwarder.request(server.authenticator.validate_and_strip(payload))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ControlError) as error:
            response = {"ok": False, "error": str(error)}
        try:
            self.request.sendall(encode_response(response))
        except OSError:
            pass


class BoundedThreadingMixIn(socketserver.ThreadingMixIn):
    """Reject excess accepted sockets instead of creating unbounded workers."""

    daemon_threads = True
    # A stop must not wait for a malicious peer holding an incomplete TLS
    # handshake. Normal request ingress and local profile-operation deadlines
    # remain bounded separately.
    block_on_close = False

    def __init__(self, *arguments: Any, max_concurrent_requests: int,
                 **keyword_arguments: Any) -> None:
        if not 1 <= max_concurrent_requests <= MAX_CONCURRENT_REQUESTS:
            raise ValueError("invalid maximum concurrent request count")
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        super().__init__(*arguments, **keyword_arguments)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            # Do not start a handshake or forwarder operation for a peer that
            # exceeds the bounded service capacity.
            # Do not call TCPServer.shutdown_request on an unhandshaken
            # SSLSocket: its SHUT_WR path can enter SSL shutdown behavior.
            # Closing the accepted descriptor is the bounded rejection path.
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


class GatewayServer(BoundedThreadingMixIn, socketserver.TCPServer):
    """TCP acceptor that defers each bounded TLS handshake to a worker."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], forwarder: LocalControlForwarder,
                 context: ssl.SSLContext, authenticator: RemoteAuthenticator,
                 max_concurrent_requests: int) -> None:
        self.forwarder = forwarder
        self._context = context
        self.authenticator = authenticator
        super().__init__(address, GatewayRequestHandler,
                         max_concurrent_requests=max_concurrent_requests)

    def get_request(self) -> tuple[ssl.SSLSocket, tuple[str, int]]:
        """Accept quickly; never conduct a peer TLS handshake on this loop."""
        raw, address = self.socket.accept()
        try:
            # do_handshake_on_connect=False is essential: ThreadingMixIn only
            # creates the worker after get_request() returns.
            connection = self._context.wrap_socket(
                raw, server_side=True, do_handshake_on_connect=False)
        except Exception:
            raw.close()
            raise
        return connection, address


def tls_server_context(arguments: argparse.Namespace,
                       authenticator: RemoteAuthenticator) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        load_tls_server_cert_chain(
            context, Path(arguments.server_cert), Path(arguments.server_key),
            require_root_owner=os.geteuid() == 0,
            allow_root_group_readable_private_key=(
                os.geteuid() == 0 and is_pve_proxy_tls_material(
                    Path(arguments.server_cert), Path(arguments.server_key))))
    except SystemAuthError as error:
        raise ControlError("cannot load QSF TLS server material") from error
    if authenticator.client_ca:
        context.load_verify_locations(cafile=authenticator.client_ca)
        context.verify_mode = ssl.CERT_REQUIRED
    return context


def serve(arguments: argparse.Namespace) -> int:
    control_socket = Path(arguments.control_socket)
    token_file = Path(arguments.token_file)
    if not control_socket.is_socket():
        raise ControlError("QSF control socket does not exist")
    # Fail before opening a TCP listener if the local capability boundary is
    # not securely configured.
    read_existing_token(token_file)
    forwarder = LocalControlForwarder(control_socket, token_file)
    authenticator = RemoteAuthenticator(arguments.client_ca,
                                        arguments.system_auth_ticket_key,
                                        arguments.system_auth_audience)
    if not 1 <= arguments.max_concurrent_requests <= MAX_CONCURRENT_REQUESTS:
        raise ControlError("QSF maximum concurrent request count is invalid")
    with GatewayServer((arguments.listen_host, arguments.listen_port),
                       forwarder, tls_server_context(arguments, authenticator),
                       authenticator, arguments.max_concurrent_requests) as server:
        host, port = server.server_address[:2]
        print(f"QSF_TLS_GATEWAY_READY host={host} port={port}", flush=True)
        server.serve_forever(poll_interval=0.25)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-socket", required=True,
                        help="local qsf-control Unix socket")
    parser.add_argument("--token-file", required=True,
                        help="local 0600 qsf-control token file")
    parser.add_argument("--server-cert", required=True, help="PEM TLS server certificate")
    parser.add_argument("--server-key", required=True, help="PEM TLS server private key")
    parser.add_argument("--client-ca",
                        help="PEM CA allowed to authenticate remote QSF mTLS clients")
    parser.add_argument("--system-auth-ticket-key",
                        help="mode-0600 raw 32-byte key for TLS/PAM-issued session tickets")
    parser.add_argument("--system-auth-audience",
                        help="required per-VM audience for system-auth ticket mode")
    parser.add_argument("--max-concurrent-requests", type=int,
                        default=DEFAULT_MAX_CONCURRENT_REQUESTS,
                        help="bound incomplete TLS/profile request workers to 1..16 (default: 16)")
    parser.add_argument("--listen-host", default="127.0.0.1",
                        help="bind address (default: loopback only)")
    parser.add_argument("--listen-port", type=int, default=48122,
                        help="TCP port (default: 48122; use 0 for an ephemeral test port)")
    arguments = parser.parse_args()
    if not 0 <= arguments.listen_port <= 65535:
        parser.error("--listen-port must be between 0 and 65535")
    try:
        return serve(arguments)
    except (ControlError, OSError, ssl.SSLError) as error:
        print(f"qsf-tls-gateway: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
