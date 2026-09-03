#!/usr/bin/env python3
"""Protocol tests for the TLS/PAM system-auth and QSF ticket boundary."""

from __future__ import annotations

import json
import os
import select
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SYSTEM_AUTH_DIRECTORY = ROOT / "extensions" / "system_auth"
QSF_DIRECTORY = ROOT / "extensions" / "qsf_control"
AUTH_GATEWAY = SYSTEM_AUTH_DIRECTORY / "q_sunshine_auth_gateway.py"
QSF_GATEWAY = QSF_DIRECTORY / "qsf_tls_gateway.py"
QSF_CONTROL = QSF_DIRECTORY / "qsf_control.py"
sys.path.insert(0, str(SYSTEM_AUTH_DIRECTORY))
sys.path.insert(0, str(ROOT / "tests" / "qsf_control"))

from q_sunshine_auth import (AuthError, issue_ticket, load_tls_server_cert_chain,
                             verify_ticket)  # noqa: E402
from test_qsf_control import FakeAgent  # noqa: E402


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


class SystemAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil_which("openssl"):
            self.skipTest("openssl is required")
        self.directory = tempfile.TemporaryDirectory(prefix="q-sunshine-system-auth-")
        self.path = Path(self.directory.name)
        self._create_certificates()
        self.ticket_key = self.path / "ticket.key"
        self.ticket_key.write_bytes(os.urandom(32))
        self.ticket_key.chmod(0o600)
        self.helper_audit = self.path / "helper-audit.json"
        self.pam_helper = self.path / "fake-pam-helper.py"
        self.pam_helper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "data = sys.stdin.buffer.read()\n"
            "ok = False\n"
            "if len(data) >= 4:\n"
            "    n = int.from_bytes(data[:2], 'big')\n"
            "    p = 2 + n\n"
            "    if p + 2 <= len(data):\n"
            "        user = data[2:p]\n"
            "        m = int.from_bytes(data[p:p + 2], 'big')\n"
            "        password = data[p + 2:p + 2 + m]\n"
            "        ok = p + 2 + m == len(data) and user == b'alice' and password == b'correct horse'\n"
            "rhost = sys.argv[sys.argv.index('--rhost') + 1] if '--rhost' in sys.argv else None\n"
            "audit = {'argv_has_secret': any('correct horse' in value for value in sys.argv),\n"
            "         'env_has_secret': any('correct horse' in value for value in os.environ.values()),\n"
            "         'rhost': rhost}\n"
            "(pathlib.Path(__file__).with_name('helper-audit.json')).write_text(json.dumps(audit), encoding='ascii')\n"
            "raise SystemExit(0 if ok else 1)\n",
            encoding="utf-8",
        )
        self.pam_helper.chmod(0o700)
        self.auth_port = free_loopback_port()
        self.auth = subprocess.Popen(
            [
                sys.executable, str(AUTH_GATEWAY),
                "--server-cert", str(self.path / "server.crt"),
                "--server-key", str(self.path / "server.key"),
                "--ticket-key", str(self.ticket_key),
                "--audience", "vm-100",
                "--pam-helper", str(self.pam_helper),
                "--pam-service", "q-sunshine-test",
                "--allow-user", "alice",
                "--max-concurrent-requests", "2",
                "--listen-host", "127.0.0.1", "--listen-port", str(self.auth_port),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self._wait_ready(self.auth, "Q_SUNSHINE_SYSTEM_AUTH_READY")

    def tearDown(self) -> None:
        terminate(getattr(self, "qsf_gateway", None))
        terminate(getattr(self, "control", None))
        if hasattr(self, "agent"):
            self.agent.close()
        terminate(getattr(self, "auth", None))
        if hasattr(self, "directory"):
            self.directory.cleanup()

    def _openssl(self, *arguments: str) -> None:
        subprocess.run(["openssl", *arguments], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _create_certificates(self) -> None:
        extension = self.path / "server.ext"
        extension.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:localhost\n", encoding="ascii")
        self._openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=q-sunshine-system-auth-test-ca",
            "-keyout", str(self.path / "ca.key"), "-out", str(self.path / "ca.crt"),
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign",
        )
        self._openssl(
            "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
            "-keyout", str(self.path / "server.key"), "-out", str(self.path / "server.csr"),
        )
        self._openssl(
            "x509", "-req", "-days", "1", "-sha256", "-in", str(self.path / "server.csr"),
            "-CA", str(self.path / "ca.crt"), "-CAkey", str(self.path / "ca.key"),
            "-CAcreateserial", "-out", str(self.path / "server.crt"), "-extfile", str(extension),
        )

    def _wait_ready(self, process: subprocess.Popen[str], marker: str) -> None:
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 5)
        if not readable:
            stdout, stderr = process.communicate(timeout=1)
            self.fail(f"gateway did not announce readiness: stdout={stdout} stderr={stderr}")
        self.assertIn(marker, process.stdout.readline())

    def _tls_request(self, port: int, payload: dict[str, object]) -> dict[str, object]:
        context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        with socket.create_connection(("127.0.0.1", port), timeout=4) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                connection.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
                response = bytearray()
                while b"\n" not in response:
                    block = connection.recv(4096)
                    if not block:
                        break
                    response.extend(block)
        if b"\n" not in response:
            diagnostics = ""
            auth = getattr(self, "auth", None)
            if auth is not None and auth.stderr is not None:
                readable, _, _ = select.select([auth.stderr], [], [], 0.2)
                if readable:
                    diagnostics = os.read(auth.stderr.fileno(), 65536).decode("utf-8", "replace")
            self.fail(f"TLS gateway closed without a response: {diagnostics}")
        decoded = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
        self.assertIsInstance(decoded, dict)
        return decoded

    def login(self, username: str = "alice", password: str = "correct horse") -> dict[str, object]:
        return self._tls_request(self.auth_port, {
            "version": 1, "op": "login", "username": username, "password": password,
        })

    def test_tls_pam_login_issues_vm_scoped_ticket_without_argv_or_environment_secret(self) -> None:
        response = self.login()
        self.assertTrue(response.get("ok"), response)
        result = response.get("result")
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(result.get("subject"), "alice")
        self.assertEqual(result.get("audience"), "vm-100")
        ticket = result.get("session_token")
        self.assertIsInstance(ticket, str)
        assert isinstance(ticket, str)
        subject, expires_at = verify_ticket(self.ticket_key.read_bytes(), ticket, audience="vm-100")
        self.assertEqual(subject, "alice")
        self.assertGreater(expires_at, int(time.time()))
        audit = json.loads(self.helper_audit.read_text(encoding="ascii"))
        self.assertEqual(audit, {
            "argv_has_secret": False,
            "env_has_secret": False,
            "rhost": "127.0.0.1",
        })

    def test_bad_or_unallowed_login_has_one_generic_failure(self) -> None:
        bad_password = self.login(password="wrong")
        unallowed = self.login(username="mallory")
        malformed = self._tls_request(self.auth_port, {
            "version": 1, "op": "not-login", "username": "alice", "password": "correct horse",
        })
        for response in (bad_password, unallowed, malformed):
            self.assertEqual(response, {"ok": False, "error": "authentication failed"})

    def test_unallowed_user_never_reaches_pam_helper(self) -> None:
        response = self.login(username="mallory")
        self.assertEqual(response, {"ok": False, "error": "authentication failed"})
        self.assertFalse(self.helper_audit.exists(),
                         "per-VM allowlist rejection must precede PAM side effects")

    def test_success_does_not_reset_source_guess_budget(self) -> None:
        """A known valid account cannot reset four failed guesses at one IP."""
        for _ in range(4):
            response = self.login(password="wrong")
            self.assertEqual(response, {"ok": False, "error": "authentication failed"})
        accepted = self.login()
        self.assertTrue(accepted.get("ok"), accepted)
        # The fifth failure locks the source. A correct password then receives
        # the same generic failure until the bounded source lockout expires.
        fifth = self.login(password="wrong")
        locked = self.login()
        self.assertEqual(fifth, {"ok": False, "error": "authentication failed"})
        self.assertEqual(locked, {"ok": False, "error": "authentication failed"})

    def test_excess_incomplete_tls_peers_are_closed_at_the_worker_limit(self) -> None:
        """Two held handshakes cannot create an unbounded PAM/TLS worker pool."""
        held = [socket.create_connection(("127.0.0.1", self.auth_port), timeout=3)
                for _ in range(2)]
        try:
            # Let the accept loop assign both slots to peers which never send
            # a ClientHello. A third connection must be closed rather than
            # waiting for a third worker or reaching PAM.
            time.sleep(0.4)
            context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            started = time.monotonic()
            with socket.create_connection(("127.0.0.1", self.auth_port), timeout=2) as raw:
                raw.settimeout(2)
                with self.assertRaises((OSError, ssl.SSLError)):
                    context.wrap_socket(raw, server_hostname="localhost")
            self.assertLess(time.monotonic() - started, 2.5)
        finally:
            for connection in held:
                connection.close()

    def test_single_worker_slot_rejects_excess_tls_and_recovers_after_peer_close(self) -> None:
        """A max=1 listener rejects promptly and releases its slot on EOF."""
        port = free_loopback_port()
        limited = subprocess.Popen(
            [
                sys.executable, str(AUTH_GATEWAY),
                "--server-cert", str(self.path / "server.crt"),
                "--server-key", str(self.path / "server.key"),
                "--ticket-key", str(self.ticket_key), "--audience", "vm-100",
                "--pam-helper", str(self.pam_helper), "--pam-service", "q-sunshine-test",
                "--allow-user", "alice", "--max-concurrent-requests", "1",
                "--listen-host", "127.0.0.1", "--listen-port", str(port),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        held: socket.socket | None = None
        try:
            self._wait_ready(limited, "Q_SUNSHINE_SYSTEM_AUTH_READY")
            held = socket.create_connection(("127.0.0.1", port), timeout=3)
            time.sleep(0.4)
            context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            started = time.monotonic()
            with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
                raw.settimeout(2)
                with self.assertRaises((OSError, ssl.SSLError)):
                    context.wrap_socket(raw, server_hostname="localhost")
            self.assertLess(time.monotonic() - started, 2.5)
            held.close()
            held = None
            # The first worker sees EOF, releases its semaphore slot, and a
            # normal login succeeds through the same max=1 listener.
            time.sleep(0.2)
            response = self._tls_request(port, {
                "version": 1, "op": "login", "username": "alice", "password": "correct horse",
            })
            self.assertTrue(response.get("ok"), response)
        finally:
            if held is not None:
                held.close()
            terminate(limited)

    def test_ticket_is_strictly_signed_scoped_and_short_lived(self) -> None:
        key = self.ticket_key.read_bytes()
        ticket, expiry = issue_ticket(key, subject="alice", audience="vm-100", ttl_seconds=60,
                                      now=1000)
        self.assertEqual(verify_ticket(key, ticket, audience="vm-100", now=1001), ("alice", expiry))
        with self.assertRaises(AuthError):
            verify_ticket(key, ticket, audience="vm-101", now=1001)
        tampered = ticket[:-1] + ("A" if ticket[-1] != "A" else "B")
        with self.assertRaises(AuthError):
            verify_ticket(key, tampered, audience="vm-100", now=1001)
        with self.assertRaises(AuthError):
            verify_ticket(key, ticket, audience="vm-100", now=expiry)

    def test_tls_server_material_rejects_symlinks_and_unsafe_modes(self) -> None:
        certificate = self.path / "server.crt"
        private_key = self.path / "server.key"
        private_key.chmod(0o644)
        with self.assertRaises(AuthError):
            load_tls_server_cert_chain(
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), certificate, private_key,
                require_root_owner=False)
        # PVE's root-managed proxy key is 0640 so pveproxy can read it.  The
        # compatibility path must be an explicit opt-in; ordinary TLS callers
        # still reject a group-readable private key.
        private_key.chmod(0o640)
        with self.assertRaises(AuthError):
            load_tls_server_cert_chain(
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), certificate, private_key,
                require_root_owner=False)
        load_tls_server_cert_chain(
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), certificate, private_key,
            require_root_owner=False, allow_root_group_readable_private_key=True)
        private_key.chmod(0o600)
        linked_key = self.path / "server-key-link"
        linked_key.symlink_to(private_key)
        with self.assertRaises(AuthError):
            load_tls_server_cert_chain(
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), certificate, linked_key,
                require_root_owner=False)
        certificate.chmod(0o666)
        with self.assertRaises(AuthError):
            load_tls_server_cert_chain(
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), certificate, private_key,
                require_root_owner=False)

    def _start_qsf_ticket_gateway(self) -> int:
        self.agent = FakeAgent(self.path / "agent.sock")
        self.control_socket = self.path / "control.sock"
        self.control_token = self.path / "control.token"
        control_environment = os.environ.copy()
        control_environment.update({
            "QSUNSHINE_QSF_HOST_MAX_WIDTH": "1920",
            "QSUNSHINE_QSF_HOST_MAX_HEIGHT": "1080",
            "QSUNSHINE_QSF_HOST_MAX_FPS": "60",
            "QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS": "12000",
            "QSUNSHINE_QSF_HOST_ENCODER_CODECS": "H264",
        })
        self.control = subprocess.Popen(
            [sys.executable, str(QSF_CONTROL), "--agent-socket", str(self.path / "agent.sock"),
             "--control-socket", str(self.control_socket), "--token-file", str(self.control_token),
             "--no-qemu-resize"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=control_environment,
        )
        for _ in range(100):
            if self.control_socket.exists() and self.control_token.exists():
                break
            if self.control.poll() is not None:
                stdout, stderr = self.control.communicate(timeout=1)
                self.fail(f"qsf-control exited: stdout={stdout} stderr={stderr}")
            time.sleep(0.02)
        else:
            self.fail("timed out waiting for qsf-control")
        port = free_loopback_port()
        self.qsf_gateway = subprocess.Popen(
            [
                sys.executable, str(QSF_GATEWAY),
                "--control-socket", str(self.control_socket), "--token-file", str(self.control_token),
                "--server-cert", str(self.path / "server.crt"), "--server-key", str(self.path / "server.key"),
                "--system-auth-ticket-key", str(self.ticket_key),
                "--system-auth-audience", "vm-100",
                "--listen-host", "127.0.0.1", "--listen-port", str(port),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self._wait_ready(self.qsf_gateway, "QSF_TLS_GATEWAY_READY")
        return port

    def test_qsf_ticket_mode_has_no_client_cert_but_requires_a_valid_ticket(self) -> None:
        port = self._start_qsf_ticket_gateway()
        login = self.login()
        result = login["result"]
        assert isinstance(result, dict)
        ticket = result["session_token"]
        assert isinstance(ticket, str)
        accepted = self._tls_request(port, {
            "op": "status", "authorization": {"scheme": "Bearer", "token": ticket},
        })
        self.assertTrue(accepted.get("ok"), accepted)
        self.assertEqual(accepted.get("result", {}).get("agent"), "ready")
        missing = self._tls_request(port, {"op": "status"})
        self.assertEqual(missing, {"ok": False, "error": "authentication required"})
        # Ticket mode must not accept an mTLS-only configuration accidentally:
        # no client certificate was loaded for the accepted request above.


def shutil_which(program: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / program
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    unittest.main(verbosity=2)
