#!/usr/bin/env python3
"""TLS/PAM-to-CSR e2e test for the GameStream lease endpoint.

This owns no GameStream protocol implementation: it proves the authd boundary
only.  A real TLS client signs in through the stdin-only PAM fixture, keeps
the returned qsa1 ticket in memory, and exchanges it plus a client-owned CSR
for a CA-validated VM-scoped client-auth leaf.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUTH_GATEWAY = ROOT / "extensions" / "system_auth" / "q_sunshine_auth_gateway.py"
ISSUER_SOURCE = ROOT / "extensions" / "gamestream_auth" / "q_sunshine_lease_issuer.c"
SYSTEM_AUTH_DIRECTORY = ROOT / "extensions" / "system_auth"
sys.path.insert(0, str(SYSTEM_AUTH_DIRECTORY))

from q_sunshine_auth import issue_ticket  # noqa: E402


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


class AuthdLeaseEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("openssl") is None or shutil.which("cc") is None:
            raise unittest.SkipTest("openssl and cc are required")
        cls.build_directory = tempfile.TemporaryDirectory(
            prefix="q-sunshine-authd-lease-build-")
        cls.issuer = Path(cls.build_directory.name) / "q-sunshine-lease-issuer"
        completed = subprocess.run(
            [
                "cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-O2",
                "-D_FORTIFY_SOURCE=2", "-fstack-protector-strong", "-fPIE",
                "-Wall", "-Wextra", "-Werror", "-Wformat=2",
                "-Werror=format-security", str(ISSUER_SOURCE), "-o", str(cls.issuer),
                "-pie", "-Wl,-z,relro,-z,now", "-lssl", "-lcrypto",
            ],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"cannot compile lease issuer: {completed.stderr}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build_directory.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="q-sunshine-authd-lease-")
        self.path = Path(self.directory.name)
        self._create_material()
        self.port = free_loopback_port()
        self.gateway = subprocess.Popen(
            [
                sys.executable, str(AUTH_GATEWAY),
                "--server-cert", str(self.path / "auth-server.crt"),
                "--server-key", str(self.path / "auth-server.key"),
                "--ticket-key", str(self.path / "ticket.key"),
                "--audience", "vm-100", "--pam-helper", str(self.path / "fake-pam.py"),
                "--pam-service", "q-sunshine-test", "--allow-user", "alice",
                "--gamestream-lease-issuer", str(self.issuer),
                "--gamestream-lease-ca-cert", str(self.path / "lease-ca.crt"),
                "--gamestream-lease-ca-key", str(self.path / "lease-ca.key"),
                "--gamestream-lease-sunshine-server-cert", str(self.path / "sunshine.crt"),
                "--gamestream-lease-ttl-seconds", "120",
                "--listen-host", "127.0.0.1", "--listen-port", str(self.port),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self._wait_ready()

    def tearDown(self) -> None:
        terminate(getattr(self, "gateway", None))
        self.directory.cleanup()

    def _openssl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["openssl", *arguments], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True,
        )

    def _create_material(self) -> None:
        # TLS server certificate for PAM login and lease transport.
        self._openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-keyout", str(self.path / "auth-server.key"),
            "-out", str(self.path / "auth-server.crt"),
            "-addext", "subjectAltName=DNS:localhost",
        )
        (self.path / "auth-server.key").chmod(0o600)
        self._openssl(
            "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "1",
            "-subj", "/CN=q-sunshine lease CA vm-100",
            "-keyout", str(self.path / "lease-ca.key"),
            "-out", str(self.path / "lease-ca.crt"),
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        )
        (self.path / "lease-ca.key").chmod(0o600)
        # This is deliberately distinct from the authd TLS server certificate:
        # returned Bootstrap trust is for the future Sunshine GameStream peer.
        self._openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=sunshine-vm-100", "-keyout", str(self.path / "sunshine.key"),
            "-out", str(self.path / "sunshine.crt"),
        )
        (self.path / "sunshine.key").chmod(0o600)
        self._openssl(
            "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=owned-by-client",
            "-keyout", str(self.path / "client.key"), "-out", str(self.path / "client.csr"),
        )
        (self.path / "client.key").chmod(0o600)
        (self.path / "ticket.key").write_bytes(os.urandom(32))
        (self.path / "ticket.key").chmod(0o600)
        helper = self.path / "fake-pam.py"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "packet = sys.stdin.buffer.read()\n"
            "valid = False\n"
            "if len(packet) >= 4:\n"
            "  n = int.from_bytes(packet[:2], 'big'); p = 2 + n\n"
            "  if p + 2 <= len(packet):\n"
            "    user = packet[2:p]; m = int.from_bytes(packet[p:p+2], 'big')\n"
            "    password = packet[p+2:p+2+m]\n"
            "    valid = p + 2 + m == len(packet) and user == b'alice' and password == b'correct horse'\n"
            "raise SystemExit(0 if valid else 1)\n",
            encoding="ascii",
        )
        helper.chmod(0o700)

    def _wait_ready(self) -> None:
        assert self.gateway.stdout is not None
        readable, _, _ = select.select([self.gateway.stdout], [], [], 6)
        if not readable:
            stdout, stderr = self.gateway.communicate(timeout=1)
            self.fail(f"authd did not start: stdout={stdout} stderr={stderr}")
        self.assertIn("Q_SUNSHINE_SYSTEM_AUTH_READY", self.gateway.stdout.readline())

    def _request(self, payload: dict[str, object]) -> dict[str, object]:
        context = ssl.create_default_context(cafile=str(self.path / "auth-server.crt"))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        with socket.create_connection(("127.0.0.1", self.port), timeout=4) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                connection.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
                response = bytearray()
                while b"\n" not in response:
                    block = connection.recv(4096)
                    if not block:
                        break
                    response.extend(block)
        self.assertIn(b"\n", response)
        decoded = json.loads(bytes(response).split(b"\n", 1)[0].decode("ascii"))
        self.assertIsInstance(decoded, dict)
        return decoded

    def _login(self) -> str:
        response = self._request({
            "op": "login", "version": 1, "username": "alice", "password": "correct horse",
        })
        self.assertEqual(response.get("ok"), True, response)
        result = response.get("result")
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        ticket = result.get("session_token")
        self.assertIsInstance(ticket, str)
        return ticket

    def _lease_request(self, ticket: str) -> dict[str, object]:
        return self._request({
            "op": "gamestream_lease", "version": 1, "session_token": ticket,
            "csr_pem": (self.path / "client.csr").read_text(encoding="ascii"),
        })

    def test_tls_pam_login_ticket_csr_and_vm_lease(self) -> None:
        ticket = self._login()
        response = self._lease_request(ticket)
        self.assertEqual(response.get("ok"), True, response)
        result = response.get("result")
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(set(result), {
            "version", "audience", "subject", "expires_at_unix_ms",
            "client_certificate_pem", "sunshine_server_certificate_pem",
        })
        self.assertEqual(result["audience"], "vm-100")
        self.assertEqual(result["subject"], "alice")
        self.assertGreater(int(result["expires_at_unix_ms"]), int(time.time()) * 1000)
        self.assertEqual(result["sunshine_server_certificate_pem"],
                         (self.path / "sunshine.crt").read_text(encoding="ascii"))
        leaf = result["client_certificate_pem"]
        self.assertIsInstance(leaf, str)
        assert isinstance(leaf, str)
        self.assertNotIn("PRIVATE KEY", leaf)
        self.assertNotIn(ticket, repr(response))
        (self.path / "lease.crt").write_text(leaf, encoding="ascii")
        verified = self._openssl(
            "verify", "-purpose", "sslclient", "-CAfile", str(self.path / "lease-ca.crt"),
            str(self.path / "lease.crt"))
        self.assertIn("lease.crt: OK", verified.stdout)
        extensions = self._openssl(
            "x509", "-in", str(self.path / "lease.crt"), "-noout", "-text").stdout
        self.assertIn("URI:urn:q-sunshine:aud:vm-100", extensions)
        self.assertIn("CA:FALSE", extensions)
        self.assertIn("TLS Web Client Authentication", extensions)

    def test_cross_vm_ticket_is_rejected_at_tls_endpoint(self) -> None:
        ticket, _ = issue_ticket(
            (self.path / "ticket.key").read_bytes(), subject="alice", audience="vm-101",
            ttl_seconds=300,
        )
        response = self._lease_request(ticket)
        self.assertEqual(response, {"ok": False, "error": "authentication failed"})

    def test_lease_request_with_unknown_field_is_rejected_at_tls_endpoint(self) -> None:
        ticket = self._login()
        response = self._request({
            "op": "gamestream_lease", "version": 1, "session_token": ticket,
            "csr_pem": (self.path / "client.csr").read_text(encoding="ascii"),
            "unexpected": True,
        })
        self.assertEqual(response, {"ok": False, "error": "authentication failed"})

    def test_partial_lease_configuration_fails_closed_before_ready(self) -> None:
        """A direct authd launch must not accidentally enable a partial lease."""
        partial = subprocess.run(
            [
                sys.executable, str(AUTH_GATEWAY),
                "--server-cert", str(self.path / "auth-server.crt"),
                "--server-key", str(self.path / "auth-server.key"),
                "--ticket-key", str(self.path / "ticket.key"),
                "--audience", "vm-100", "--pam-helper", str(self.path / "fake-pam.py"),
                "--pam-service", "q-sunshine-test", "--allow-user", "alice",
                "--gamestream-lease-issuer", str(self.issuer),
                "--listen-host", "127.0.0.1", "--listen-port", "0",
            ],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=8, check=False,
        )
        self.assertEqual(partial.returncode, 2, partial.stderr)
        self.assertNotIn("Q_SUNSHINE_SYSTEM_AUTH_READY", partial.stdout)
        self.assertIn("GameStream lease configuration", partial.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
