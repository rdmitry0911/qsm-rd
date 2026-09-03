#!/usr/bin/env python3
"""Exercise the cryptographic core of a PIN-free GameStream lease.

This is deliberately a small interoperability proof, not a deployed login
service.  It creates an instance CA, a client-generated RSA key/CSR, and a
short-lived client-auth leaf.  It then proves that a real TLS server accepts
the lease only when the caller possesses that private key, and rejects an
untrusted leaf.  The audience URI is checked separately because OpenSSL's
generic TLS verifier validates the issuer, validity period, and EKU but does
not know q-sunshine's per-VM policy.

Run directly with ``python3 prototypes/gamestream_system_auth/test_mtls_lease.py``.
No files are written outside a temporary directory.
"""

from __future__ import annotations

import datetime as dt
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path


AUDIENCE = "vm-100"
AUDIENCE_URI = f"urn:q-sunshine:aud:{AUDIENCE}"


def run(*arguments: str, cwd: Path) -> str:
    """Run one OpenSSL command and return its UTF-8 standard output."""
    result = subprocess.run(
        ["openssl", *arguments], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout


def must_fail(*arguments: str, cwd: Path) -> None:
    """Assert that OpenSSL rejects the supplied input."""
    result = subprocess.run(
        ["openssl", *arguments], cwd=cwd, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode == 0:
        raise AssertionError(f"unexpected OpenSSL success: {arguments!r}")


def issue_client_lease(directory: Path, *, certificate_name: str,
                       audience: str, days: int = 1,
                       extended_key_usage: str = "clientAuth") -> None:
    """Sign a client-owned CSR with the instance CA and strict leaf extensions."""
    extension_file = directory / f"{certificate_name}.ext"
    extension_file.write_text(
        "[qsm_client_lease]\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        f"extendedKeyUsage=critical,{extended_key_usage}\n"
        f"subjectAltName=critical,URI:urn:q-sunshine:aud:{audience}\n",
        encoding="ascii",
    )
    run(
        "x509", "-req", "-in", f"{certificate_name}.csr",
        "-CA", "lease-ca.pem", "-CAkey", "lease-ca.key", "-CAcreateserial",
        "-out", f"{certificate_name}.pem", "-days", str(days), "-sha256",
        "-extfile", extension_file.name, "-extensions", "qsm_client_lease",
        cwd=directory,
    )


def certificate_has_expected_audience(directory: Path, certificate_name: str,
                                      audience: str) -> bool:
    """Return whether the leaf carries the exact VM audience URI."""
    extension = run(
        "x509", "-in", certificate_name, "-noout", "-ext", "subjectAltName",
        cwd=directory,
    )
    return f"URI:urn:q-sunshine:aud:{audience}" in extension


def tls_handshake(directory: Path, *, use_client_certificate: bool) -> bool:
    """Perform one localhost mTLS handshake against a fresh certificate-required server."""
    ready = threading.Event()
    result: list[bool] = []
    errors: list[BaseException] = []

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_3
    server_context.load_cert_chain(directory / "server.pem", directory / "server.key")
    server_context.load_verify_locations(cafile=directory / "lease-ca.pem")
    server_context.verify_mode = ssl.CERT_REQUIRED

    def serve_once() -> None:
        ready.set()
        try:
            connection, _ = listener.accept()
            with connection, server_context.wrap_socket(connection, server_side=True) as tls:
                tls.recv(1)
                result.append(True)
        except ssl.SSLError:
            result.append(False)
        except BaseException as error:  # surface fixture errors in the caller
            errors.append(error)
        finally:
            listener.close()

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    if not ready.wait(5):
        raise AssertionError("TLS fixture did not become ready")

    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.minimum_version = ssl.TLSVersion.TLSv1_3
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    if use_client_certificate:
        client_context.load_cert_chain(directory / "client.pem", directory / "client.key")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            with client_context.wrap_socket(connection, server_hostname="q-sunshine") as tls:
                tls.sendall(b"x")
    except ssl.SSLError:
        pass
    thread.join(5)
    if thread.is_alive():
        raise AssertionError("TLS fixture did not finish")
    if errors:
        raise errors[0]
    return result == [True]


def main() -> None:
    """Create and verify a leaf that is suitable for the proposed lease flow."""
    with tempfile.TemporaryDirectory(prefix="qsm-mtls-lease-") as temporary:
        directory = Path(temporary)
        run(
            "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256",
            "-keyout", "lease-ca.key", "-out", "lease-ca.pem", "-days", "30",
            "-subj", "/CN=q-sunshine vm-100 client lease CA",
            cwd=directory,
        )
        run(
            "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
            "-keyout", "client.key", "-out", "client.csr",
            "-subj", "/CN=q-sunshine user alice",
            cwd=directory,
        )
        issue_client_lease(directory, certificate_name="client", audience=AUDIENCE)
        run(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
            "-keyout", "server.key", "-out", "server.pem", "-days", "1",
            "-subj", "/CN=q-sunshine GameStream",
            cwd=directory,
        )

        verified = run(
            "verify", "-CAfile", "lease-ca.pem", "-purpose", "sslclient", "client.pem",
            cwd=directory,
        )
        if "client.pem: OK" not in verified:
            raise AssertionError("lease does not validate as an SSL client certificate")
        if not certificate_has_expected_audience(directory, "client.pem", AUDIENCE):
            raise AssertionError("lease is missing its expected VM audience URI")
        if certificate_has_expected_audience(directory, "client.pem", "vm-101"):
            raise AssertionError("audience matcher accepted a different VM")
        if not tls_handshake(directory, use_client_certificate=True):
            raise AssertionError("TLS server rejected the signed client lease")
        if tls_handshake(directory, use_client_certificate=False):
            raise AssertionError("TLS server accepted a connection without proof of the lease key")

        run(
            "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
            "-keyout", "wrong-purpose.key", "-out", "wrong-purpose.csr",
            "-subj", "/CN=q-sunshine user alice",
            cwd=directory,
        )
        issue_client_lease(
            directory, certificate_name="wrong-purpose", audience=AUDIENCE,
            extended_key_usage="serverAuth",
        )
        must_fail(
            "verify", "-CAfile", "lease-ca.pem", "-purpose", "sslclient",
            "wrong-purpose.pem", cwd=directory,
        )

        run(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
            "-keyout", "forged.key", "-out", "forged.pem", "-days", "1",
            "-subj", "/CN=q-sunshine user alice",
            cwd=directory,
        )
        must_fail(
            "verify", "-CAfile", "lease-ca.pem", "-purpose", "sslclient", "forged.pem",
            cwd=directory,
        )

        # A 24-hour fixture leaf must fail at a future verification time.  The
        # product path tightens this further to a 60--900 second maximum.
        future = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).timestamp())
        must_fail(
            "verify", "-attime", str(future), "-CAfile", "lease-ca.pem",
            "-purpose", "sslclient", "client.pem", cwd=directory,
        )

    print("QSM_GAMESTREAM_MTLS_LEASE_PROOF_OK")


if __name__ == "__main__":
    main()
