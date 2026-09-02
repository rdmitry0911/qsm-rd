#!/usr/bin/env python3
"""Integration tests for q-sunshine's PIN-free GameStream certificate lease.

The test calls the issuer through the same Python API that the TLS/PAM authd
will use.  It therefore exercises ticket audience verification, the proof of
possession contained in the CSR, and the X.509 profile with actual OpenSSL.
No private client key is supplied to q-sunshine or appears in its response.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SYSTEM_AUTH_DIRECTORY = ROOT / "extensions" / "system_auth"
GAMESTREAM_AUTH_DIRECTORY = ROOT / "extensions" / "gamestream_auth"
ISSUER_SOURCE = GAMESTREAM_AUTH_DIRECTORY / "q_sunshine_lease_issuer.c"
sys.path.insert(0, str(SYSTEM_AUTH_DIRECTORY))
sys.path.insert(0, str(GAMESTREAM_AUTH_DIRECTORY))

from q_sunshine_auth import AuthError, issue_ticket  # noqa: E402
from q_sunshine_gamestream_lease import (  # noqa: E402
    LEASE_OPERATION,
    LEASE_PROTOCOL_VERSION,
    LeaseIssuerConfig,
    issue_lease,
)


class GameStreamLeaseTest(unittest.TestCase):
    """Use a genuine CA and client key, while keeping all state temporary."""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("openssl") is None or shutil.which("cc") is None:
            raise unittest.SkipTest("openssl and cc are required")
        cls.build_directory = tempfile.TemporaryDirectory(
            prefix="q-sunshine-gamestream-lease-build-")
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
        self.directory = tempfile.TemporaryDirectory(prefix="q-sunshine-gamestream-lease-")
        self.path = Path(self.directory.name)
        self.ca_key = self.path / "lease-ca.key"
        self.ca_certificate = self.path / "lease-ca.crt"
        self.server_key = self.path / "sunshine-server.key"
        self.server_certificate = self.path / "sunshine-server.crt"
        self.client_key = self.path / "client.key"
        self.client_csr = self.path / "client.csr"
        self.ca_serial = self.path / "lease-ca.srl"
        self._create_certificates()
        self.ticket_key = os.urandom(32)
        self.now = int(time.time())
        self.config = LeaseIssuerConfig(
            issuer=self.issuer,
            ca_certificate=self.ca_certificate,
            ca_private_key=self.ca_key,
            sunshine_server_certificate=self.server_certificate,
            audience="vm-100",
            ttl_seconds=120,
            # Fixture files belong to the test user. Installed config uses True.
            require_root_owner=False,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _openssl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["openssl", *arguments], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True,
        )

    def _create_certificates(self) -> None:
        self._openssl(
            "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "1",
            "-subj", "/CN=q-sunshine-lease-test-ca",
            "-keyout", str(self.ca_key), "-out", str(self.ca_certificate),
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        )
        self.ca_key.chmod(0o600)
        self._openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=sunshine-vm-100",
            "-keyout", str(self.server_key), "-out", str(self.server_certificate),
        )
        self.server_key.chmod(0o600)
        self._openssl(
            "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=client-key-proof",
            "-keyout", str(self.client_key), "-out", str(self.client_csr),
            # The CA request extension is intentional: the issuer must ignore it.
            "-addext", "basicConstraints=critical,CA:TRUE",
        )
        self.client_key.chmod(0o600)

    def _request(self, ticket: str, csr: Path | None = None) -> dict[str, object]:
        return {
            "op": LEASE_OPERATION,
            "version": LEASE_PROTOCOL_VERSION,
            "session_token": ticket,
            "csr_pem": (csr or self.client_csr).read_text(encoding="ascii"),
        }

    def _ticket(self, *, audience: str = "vm-100", ttl: int = 300) -> str:
        ticket, _ = issue_ticket(
            self.ticket_key, subject="alice", audience=audience, ttl_seconds=ttl,
            now=self.now,
        )
        return ticket

    def test_issues_a_vm_scoped_client_auth_lease_without_private_key_persistence(self) -> None:
        ticket = self._ticket()
        response = issue_lease(self.ticket_key, self._request(ticket), self.config,
                               now=self.now)
        self.assertEqual(response["version"], 1)
        self.assertEqual(response["audience"], "vm-100")
        self.assertEqual(response["subject"], "alice")
        self.assertGreater(int(response["expires_at_unix_ms"]), self.now * 1000)
        self.assertLessEqual(int(response["expires_at_unix_ms"]), (self.now + 119) * 1000)
        self.assertEqual(response["sunshine_server_certificate_pem"],
                         self.server_certificate.read_text(encoding="ascii"))
        leaf = response["client_certificate_pem"]
        self.assertIsInstance(leaf, str)
        assert isinstance(leaf, str)
        self.assertNotIn("PRIVATE KEY", leaf)
        self.assertNotIn("PRIVATE KEY", repr(response))
        self.assertNotIn(ticket, repr(response))
        lease = self.path / "lease.crt"
        lease.write_text(leaf, encoding="ascii")
        verify = self._openssl(
            "verify", "-purpose", "sslclient", "-CAfile", str(self.ca_certificate), str(lease))
        self.assertIn(f"{lease}: OK", verify.stdout)
        description = self._openssl("x509", "-in", str(lease), "-noout", "-text").stdout
        self.assertIn("CA:FALSE", description)
        self.assertIn("TLS Web Client Authentication", description)
        self.assertIn("URI:urn:q-sunshine:aud:vm-100", description)
        self.assertIn("CN = q-sunshine user alice", description)
        # The only private key remains the pre-existing client fixture key.
        self.assertEqual(sorted(item.name for item in self.path.glob("*.key")),
                         ["client.key", "lease-ca.key", "sunshine-server.key"])

    def test_rejects_wrong_vm_ticket_tampered_ticket_bad_csr_and_ec_key(self) -> None:
        with self.assertRaises(AuthError):
            issue_lease(self.ticket_key, self._request(self._ticket(audience="vm-101")),
                        self.config, now=self.now)
        with self.assertRaises(AuthError):
            issue_lease(self.ticket_key, self._request(self._ticket() + "x"), self.config,
                        now=self.now)
        malformed = self._request(self._ticket())
        malformed["csr_pem"] = "not a csr"
        with self.assertRaises(AuthError):
            issue_lease(self.ticket_key, malformed, self.config, now=self.now)
        ec_key = self.path / "ec-client.key"
        ec_csr = self.path / "ec-client.csr"
        self._openssl(
            "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-nodes", "-subj", "/CN=ec-client", "-keyout", str(ec_key), "-out", str(ec_csr))
        ec_key.chmod(0o600)
        with self.assertRaises(AuthError):
            issue_lease(self.ticket_key, self._request(self._ticket(), ec_csr), self.config,
                        now=self.now)
        low_exponent_key = self.path / "low-exponent.key"
        low_exponent_csr = self.path / "low-exponent.csr"
        self._openssl(
            "genpkey", "-algorithm", "RSA", "-out", str(low_exponent_key),
            "-pkeyopt", "rsa_keygen_bits:2048", "-pkeyopt", "rsa_keygen_pubexp:3")
        low_exponent_key.chmod(0o600)
        self._openssl(
            "req", "-new", "-key", str(low_exponent_key), "-subj", "/CN=low-exponent",
            "-out", str(low_exponent_csr))
        with self.assertRaises(AuthError):
            issue_lease(self.ticket_key, self._request(self._ticket(), low_exponent_csr),
                        self.config, now=self.now)

    def test_short_or_expired_remaining_ticket_cannot_extend_access(self) -> None:
        too_short = self._ticket(ttl=60)
        with self.assertRaises(AuthError):
            issue_lease(self.ticket_key, self._request(too_short), self.config,
                        now=self.now + 1)
        with self.assertRaises(AuthError):
            issue_lease(self.ticket_key, self._request(self._ticket()), self.config,
                        now=self.now + 300)

    def test_rejects_unsafe_authority_files_and_a_symlinked_issuer(self) -> None:
        ticket = self._ticket()
        self.ca_key.chmod(0o644)
        try:
            with self.assertRaises(AuthError):
                issue_lease(self.ticket_key, self._request(ticket), self.config, now=self.now)
        finally:
            self.ca_key.chmod(0o600)
        self.server_certificate.chmod(0o664)
        try:
            with self.assertRaises(AuthError):
                issue_lease(self.ticket_key, self._request(ticket), self.config, now=self.now)
        finally:
            self.server_certificate.chmod(0o644)
        issuer_link = self.path / "issuer-link"
        issuer_link.symlink_to(self.issuer)
        symlinked = LeaseIssuerConfig(
            issuer=issuer_link,
            ca_certificate=self.ca_certificate,
            ca_private_key=self.ca_key,
            sunshine_server_certificate=self.server_certificate,
            audience="vm-100",
            ttl_seconds=120,
            require_root_owner=False,
        )
        with self.assertRaises(AuthError):
            issue_lease(self.ticket_key, self._request(ticket), symlinked, now=self.now)

    def test_rejects_relative_helper_path_before_any_issuance(self) -> None:
        relative = LeaseIssuerConfig(
            issuer=Path("q-sunshine-lease-issuer"),
            ca_certificate=self.ca_certificate,
            ca_private_key=self.ca_key,
            sunshine_server_certificate=self.server_certificate,
            audience="vm-100",
            ttl_seconds=120,
            require_root_owner=False,
        )
        with self.assertRaises(AuthError):
            issue_lease(self.ticket_key, self._request(self._ticket()), relative, now=self.now)


if __name__ == "__main__":
    unittest.main(verbosity=2)
