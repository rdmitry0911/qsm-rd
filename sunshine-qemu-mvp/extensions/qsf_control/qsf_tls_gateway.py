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
import socket
import socketserver
import ssl
import sys
from pathlib import Path
from typing import Any

from qsf_control import (ControlError, MAX_CONTROL_LINE,
                         PROFILE_TRANSACTION_TIMEOUT_SECONDS,
                         read_existing_token)


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
            response = server.forwarder.request(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ControlError) as error:
            response = {"ok": False, "error": str(error)}
        try:
            self.request.sendall(encode_response(response))
        except OSError:
            pass


class GatewayServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """TCP acceptor that defers each bounded TLS handshake to a worker."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], forwarder: LocalControlForwarder,
                 context: ssl.SSLContext) -> None:
        self.forwarder = forwarder
        self._context = context
        super().__init__(address, GatewayRequestHandler)

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


def tls_server_context(arguments: argparse.Namespace) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(arguments.server_cert, arguments.server_key)
    context.load_verify_locations(cafile=arguments.client_ca)
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
    with GatewayServer((arguments.listen_host, arguments.listen_port),
                       forwarder, tls_server_context(arguments)) as server:
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
    parser.add_argument("--client-ca", required=True,
                        help="PEM CA allowed to authenticate remote QSF clients")
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
