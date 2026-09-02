#!/usr/bin/env python3
"""TLS 1.3 PAM login gateway for short-lived q-sunshine session tickets.

This program intentionally authenticates only q-sunshine protocols which
verify the ticket themselves.  It does not and cannot alter stock
Sunshine/Moonlight GameStream pairing: that protocol authenticates an already
paired client X.509 certificate, not a username/password.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from q_sunshine_auth import (AuthError, TICKET_TTL_MAX_SECONDS,
                             TICKET_TTL_MIN_SECONDS, issue_ticket,
                             load_ticket_key, load_tls_server_cert_chain,
                             valid_audience, valid_subject)

# In a source checkout the lease module is a sibling extension. The Debian
# package deliberately co-installs it beside this script in system_auth/, which
# Python already puts on sys.path for direct script execution.
_GAMESTREAM_AUTH_MODULE_DIRECTORY = Path(__file__).resolve().parents[1] / "gamestream_auth"
if _GAMESTREAM_AUTH_MODULE_DIRECTORY.is_dir() and \
        str(_GAMESTREAM_AUTH_MODULE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_GAMESTREAM_AUTH_MODULE_DIRECTORY))
from q_sunshine_gamestream_lease import (LEASE_OPERATION, LeaseIssuerConfig,  # noqa: E402
                                         issue_lease, validate_config)


# A lease request can contain a 16-KiB public PEM CSR plus JSON escaping and
# framing. Keep the whole TLS request bounded while allowing the documented
# maximum CSR; normal login requests remain far smaller.
MAX_REQUEST_BYTES = 20 * 1024
MAX_USERNAME_BYTES = 64
MAX_PASSWORD_BYTES = 4096
TLS_HANDSHAKE_TIMEOUT_SECONDS = 12.0
TLS_REQUEST_TIMEOUT_SECONDS = 12.0
PAM_HELPER_TIMEOUT_SECONDS = 8.0
MAX_FAILURES_PER_SOURCE = 5
FAILURE_WINDOW_SECONDS = 300.0
LOCKOUT_SECONDS = 60.0
# A slow TCP/TLS peer consumes a worker until its short ingress deadline. Keep
# that work bounded so a connection flood cannot create unbounded Python
# threads or concurrent PAM helper processes. The listen backlog is separate
# and intentionally small below.
DEFAULT_MAX_CONCURRENT_REQUESTS = 16
# The packaged auth service has TasksMax=48. Each accepted auth request can
# have one Python worker plus one short-lived PAM helper, so a 16-worker cap
# leaves headroom for the main process and systemd without advertising an
# unattainable listener capacity.
MAX_CONCURRENT_REQUESTS = 16
_PAM_SERVICE_PATTERN = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")


def encode_response(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii") + b"\n"


def read_line(connection: socket.socket, maximum: int) -> bytes:
    payload = bytearray()
    while b"\n" not in payload:
        block = connection.recv(min(65536, maximum + 1 - len(payload)))
        if not block:
            break
        payload.extend(block)
        if len(payload) > maximum:
            raise AuthError("authentication failed")
    if not payload or b"\n" not in payload:
        raise AuthError("authentication failed")
    line, remainder = bytes(payload).split(b"\n", 1)
    if remainder:
        raise AuthError("authentication failed")
    return line


def valid_pam_service(value: str) -> bool:
    return bool(_PAM_SERVICE_PATTERN.fullmatch(value))


def valid_remote_host(value: str) -> bool:
    """Accept only the numeric kernel peer address passed to PAM_RHOST."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def parse_login(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict) or set(payload) != {"op", "password", "username", "version"}:
        raise AuthError("authentication failed")
    username = payload.get("username")
    password = payload.get("password")
    if (payload.get("version") != 1 or payload.get("op") != "login" or
            not isinstance(username, str) or
            not isinstance(password, str) or not valid_subject(username)):
        raise AuthError("authentication failed")
    # Authenticate the UTF-8 byte sequence PAM will actually receive.  A
    # system username must fit the conservative ASCII profile above, whereas
    # passwords can retain arbitrary Unicode excluding embedded NUL.
    try:
        encoded_password = password.encode("utf-8")
    except UnicodeEncodeError as error:
        raise AuthError("authentication failed") from error
    if (not password or "\x00" in password or len(username.encode("ascii")) > MAX_USERNAME_BYTES or
            len(encoded_password) > MAX_PASSWORD_BYTES):
        raise AuthError("authentication failed")
    return username, password


def parse_allowed_users(repeated: list[str], comma_separated: str) -> frozenset[str]:
    """Build an explicit per-instance policy; an omitted list denies all."""
    candidates = list(repeated)
    if comma_separated:
        candidates.extend(comma_separated.split(","))
    allowed = frozenset(candidate.strip() for candidate in candidates if candidate.strip())
    if not allowed or any(not valid_subject(candidate) for candidate in allowed):
        raise AuthError("system-auth requires a non-empty valid per-instance user allowlist")
    return allowed


def optional_gamestream_lease_config(arguments: argparse.Namespace,
                                     audience: str) -> LeaseIssuerConfig | None:
    """Return a complete root-local lease configuration, or disable it.

    A partial configuration is a deployment error, never a best-effort
    endpoint. This preserves the generic remote failure while making a missing
    root-local authority evident during service startup.
    """
    path_values = (
        arguments.gamestream_lease_issuer,
        arguments.gamestream_lease_ca_cert,
        arguments.gamestream_lease_ca_key,
        arguments.gamestream_lease_sunshine_server_cert,
    )
    supplied = [value is not None and value.strip() != "" for value in path_values]
    ttl_supplied = arguments.gamestream_lease_ttl_seconds is not None
    if not any(supplied) and not ttl_supplied:
        return None
    if not all(supplied) or not ttl_supplied:
        raise AuthError("GameStream lease configuration must be all-or-nothing")
    config = LeaseIssuerConfig(
        issuer=Path(path_values[0].strip()),
        ca_certificate=Path(path_values[1].strip()),
        ca_private_key=Path(path_values[2].strip()),
        sunshine_server_certificate=Path(path_values[3].strip()),
        audience=audience,
        ttl_seconds=arguments.gamestream_lease_ttl_seconds,
        require_root_owner=os.geteuid() == 0,
    )
    try:
        validate_config(config)
    except AuthError as error:
        raise AuthError("GameStream lease configuration is invalid") from error
    return config


class PamAuthenticator:
    """Call a narrow PAM conversation helper without leaking a password to argv."""

    def __init__(self, helper: Path, service: str) -> None:
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise AuthError("PAM authentication helper is unavailable")
        if not valid_pam_service(service):
            raise AuthError("PAM service name is invalid")
        self._helper = helper
        self._service = service

    def authenticate(self, username: str, password: str, remote_host: str) -> bool:
        if not valid_remote_host(remote_host):
            return False
        password_bytes = bytearray(password.encode("utf-8"))
        username_bytes = username.encode("ascii")
        packet = bytearray()
        try:
            packet.extend(len(username_bytes).to_bytes(2, "big"))
            packet.extend(username_bytes)
            packet.extend(len(password_bytes).to_bytes(2, "big"))
            packet.extend(password_bytes)
            # The environment intentionally excludes the instance file so no
            # unrelated secret is inherited by a PAM module. Password data is
            # sent only through this stdin pipe and is never a command-line
            # argument, environment value, log field, or QSettings entry. The
            # numeric source IP is passed as PAM_RHOST for host PAM policy.
            result = subprocess.run(
                [str(self._helper), "--service", self._service,
                 "--rhost", remote_host],
                input=packet, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, timeout=PAM_HELPER_TIMEOUT_SECONDS,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            password_bytes[:] = b"\0" * len(password_bytes)
            packet[:] = b"\0" * len(packet)


class AttemptLimiter:
    """Bound online guessing without retaining usernames or passwords."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    def allow(self, source: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            return self._locked_until.get(source, 0.0) <= now

    def record_failure(self, source: str) -> None:
        now = time.monotonic()
        with self._lock:
            failures = self._failures.setdefault(source, deque())
            while failures and failures[0] <= now - FAILURE_WINDOW_SECONDS:
                failures.popleft()
            failures.append(now)
            if len(failures) >= MAX_FAILURES_PER_SOURCE:
                self._locked_until[source] = now + LOCKOUT_SECONDS
                failures.clear()
            self._prune_locked(now)

    def _prune_locked(self, now: float) -> None:
        for source, until in tuple(self._locked_until.items()):
            if until <= now:
                self._locked_until.pop(source, None)
        for source, failures in tuple(self._failures.items()):
            while failures and failures[0] <= now - FAILURE_WINDOW_SECONDS:
                failures.popleft()
            if not failures:
                self._failures.pop(source, None)


class AuthGatewayRequestHandler(socketserver.BaseRequestHandler):
    """Handle one TLS login request and immediately close the connection."""

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, AuthGatewayServer)
        response: dict[str, Any] = {"ok": False, "error": "authentication failed"}
        source = str(self.client_address[0])
        try:
            self.request.settimeout(TLS_HANDSHAKE_TIMEOUT_SECONDS)
            self.request.do_handshake()
            self.request.settimeout(TLS_REQUEST_TIMEOUT_SECONDS)
            raw = read_line(self.request, MAX_REQUEST_BYTES)
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict) and payload.get("op") == LEASE_OPERATION:
                if server.gamestream_lease_config is None:
                    raise AuthError("authentication failed")
                response = {"ok": True, "result": issue_lease(
                    server.ticket_key, payload, server.gamestream_lease_config)}
            else:
                username, password = parse_login(payload)
                # The per-VM policy is an admission boundary of its own. Check it
                # before PAM so an unrelated local account cannot trigger PAM
                # lockouts, external-directory work, or side effects for a VM it
                # was never allowed to access. The wire response stays generic.
                if (not server.limiter.allow(source) or username not in server.allowed_users or
                        not server.authenticator.authenticate(username, password, source)):
                    server.limiter.record_failure(source)
                    raise AuthError("authentication failed")
                ticket, expires_at = issue_ticket(
                    server.ticket_key, subject=username, audience=server.audience,
                    ttl_seconds=server.ticket_ttl_seconds)
                response = {"ok": True, "result": {
                    "version": 1,
                    "session_token": ticket,
                    "subject": username,
                    "audience": server.audience,
                    "expires_at_unix_ms": expires_at * 1000,
                }}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AuthError):
            # Do not reveal whether a username exists, whether PAM is
            # temporarily unhealthy, or whether a source is rate limited.
            response = {"ok": False, "error": "authentication failed"}
        try:
            self.request.sendall(encode_response(response))
        except OSError:
            pass


class BoundedThreadingMixIn(socketserver.ThreadingMixIn):
    """Reject excess accepted sockets instead of creating unbounded workers."""

    daemon_threads = True
    # A service stop must not wait for a peer which deliberately holds an
    # incomplete TLS handshake. The process exits after the systemd stop
    # deadline; each normal connection still has its own bounded timeout.
    block_on_close = False

    def __init__(self, *arguments: Any, max_concurrent_requests: int,
                 **keyword_arguments: Any) -> None:
        if not 1 <= max_concurrent_requests <= MAX_CONCURRENT_REQUESTS:
            raise ValueError("invalid maximum concurrent request count")
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        super().__init__(*arguments, **keyword_arguments)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            # Do not start a handshake or a PAM helper for excess peers. A
            # close/reset is deliberate: revealing queue state is not useful
            # to a login client, and a normal client retries its sign-in.
            # Do not call TCPServer.shutdown_request here: its SHUT_WR path
            # can invoke SSL shutdown behavior on a peer that has never sent
            # a ClientHello. Closing the accepted descriptor is nonblocking.
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


class AuthGatewayServer(BoundedThreadingMixIn, socketserver.TCPServer):
    """A TCP acceptor which puts each bounded TLS handshake in a worker."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], context: ssl.SSLContext,
                 authenticator: PamAuthenticator, ticket_key: bytes,
                 audience: str, ticket_ttl_seconds: int,
                 allowed_users: frozenset[str],
                 gamestream_lease_config: LeaseIssuerConfig | None,
                 max_concurrent_requests: int) -> None:
        self._context = context
        self.authenticator = authenticator
        self.ticket_key = ticket_key
        self.audience = audience
        self.ticket_ttl_seconds = ticket_ttl_seconds
        self.allowed_users = allowed_users
        self.gamestream_lease_config = gamestream_lease_config
        self.limiter = AttemptLimiter()
        super().__init__(address, AuthGatewayRequestHandler,
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


def tls_server_context(arguments: argparse.Namespace) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    # Login is deliberately server-authenticated TLS. A first-time user has no
    # pre-provisioned client certificate; the returned ticket is short lived
    # and must travel only in a subsequent TLS-protected q-sunshine request.
    if hasattr(context, "num_tickets"):
        context.num_tickets = 0
    load_tls_server_cert_chain(
        context, Path(arguments.server_cert), Path(arguments.server_key),
        require_root_owner=os.geteuid() == 0)
    return context


def serve(arguments: argparse.Namespace) -> int:
    audience = arguments.audience.strip()
    if not valid_audience(audience):
        raise AuthError("system-auth audience is invalid")
    if not TICKET_TTL_MIN_SECONDS <= arguments.ticket_ttl_seconds <= TICKET_TTL_MAX_SECONDS:
        raise AuthError("system-auth ticket lifetime is invalid")
    if not 1 <= arguments.max_concurrent_requests <= MAX_CONCURRENT_REQUESTS:
        raise AuthError("system-auth maximum concurrent request count is invalid")
    authenticator = PamAuthenticator(Path(arguments.pam_helper), arguments.pam_service)
    # The packaged systemd unit is root, so production requires a root-owned
    # signing key.  An unprivileged source-tree fixture may use its own
    # temporary mode-0600 file without weakening the packaged deployment.
    ticket_key = load_ticket_key(Path(arguments.ticket_key),
                                 require_root_owner=os.geteuid() == 0)
    allowed_users = parse_allowed_users(arguments.allow_user, arguments.allowed_users)
    gamestream_lease_config = optional_gamestream_lease_config(arguments, audience)
    with AuthGatewayServer((arguments.listen_host, arguments.listen_port),
                           tls_server_context(arguments), authenticator, ticket_key,
                           audience, arguments.ticket_ttl_seconds,
                           allowed_users, gamestream_lease_config,
                           arguments.max_concurrent_requests) as server:
        host, port = server.server_address[:2]
        print(f"Q_SUNSHINE_SYSTEM_AUTH_READY host={host} port={port} audience={audience}",
              flush=True)
        server.serve_forever(poll_interval=0.25)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-cert", required=True, help="PEM TLS server certificate")
    parser.add_argument("--server-key", required=True, help="PEM TLS server private key")
    parser.add_argument("--ticket-key", required=True,
                        help="mode-0600 raw 32-byte per-instance ticket signing key")
    parser.add_argument("--audience", required=True,
                        help="per-instance ticket audience, for example vm-100")
    parser.add_argument("--pam-helper", default="/usr/lib/q-sunshine/bin/q-sunshine-pam-auth",
                        help="root-local PAM conversation helper")
    parser.add_argument("--pam-service", default="q-sunshine-remote",
                        help="PAM service name (default: q-sunshine-remote)")
    parser.add_argument("--allow-user", action="append", default=[], metavar="USERNAME",
                        help="allow this PAM user for this VM (repeatable; required)")
    parser.add_argument("--allowed-users", default="", metavar="USERNAME[,USERNAME...]",
                        help="comma-separated per-VM PAM allowlist (required if --allow-user is absent)")
    parser.add_argument("--ticket-ttl-seconds", type=int, default=600,
                        help="ticket lifetime in 60..900 seconds (default: 600)")
    parser.add_argument("--gamestream-lease-issuer",
                        help="root-local q-sunshine GameStream lease issuer (all lease options required)")
    parser.add_argument("--gamestream-lease-ca-cert",
                        help="root-local GameStream lease CA certificate (all lease options required)")
    parser.add_argument("--gamestream-lease-ca-key",
                        help="root-local GameStream lease CA private key (all lease options required)")
    parser.add_argument("--gamestream-lease-sunshine-server-cert",
                        help="root-local public Sunshine server certificate (all lease options required)")
    parser.add_argument("--gamestream-lease-ttl-seconds", type=int,
                        help="GameStream client-certificate lease lifetime in 60..900 seconds (all lease options required)")
    parser.add_argument("--max-concurrent-requests", type=int,
                        default=DEFAULT_MAX_CONCURRENT_REQUESTS,
                        help="bound incomplete TLS/PAM request workers to 1..16 (default: 16)")
    parser.add_argument("--listen-host", default="127.0.0.1",
                        help="bind address (default: loopback only)")
    parser.add_argument("--listen-port", type=int, default=48123,
                        help="TCP port (default: 48123; use 0 for a test port)")
    arguments = parser.parse_args()
    if not 0 <= arguments.listen_port <= 65535:
        parser.error("--listen-port must be between 0 and 65535")
    try:
        return serve(arguments)
    except (AuthError, OSError, ssl.SSLError) as error:
        print(f"q-sunshine-auth: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
