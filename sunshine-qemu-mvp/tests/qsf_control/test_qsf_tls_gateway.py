#!/usr/bin/env python3
"""End-to-end mTLS companion test through the real local QSF broker."""

from __future__ import annotations

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

from test_qsf_control import FakeAgent


ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "extensions" / "qsf_control" / "qsf_control.py"
GATEWAY = ROOT / "extensions" / "qsf_control" / "qsf_tls_gateway.py"
CLIENT = ROOT / "extensions" / "qsf_control" / "qsf_tls_client.py"


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class QsfTlsGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil_which("openssl"):
            self.skipTest("openssl is required")
        self.directory = tempfile.TemporaryDirectory(prefix="qsf-tls-test-")
        self.path = Path(self.directory.name)
        self.agent = FakeAgent(self.path / "agent.sock")
        self.control_socket = self.path / "control.sock"
        self.token_file = self.path / "token"
        self.control = subprocess.Popen(
            [
                sys.executable, str(CONTROL),
                "--agent-socket", str(self.path / "agent.sock"),
                "--control-socket", str(self.control_socket),
                "--token-file", str(self.token_file),
                "--no-qemu-resize",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._wait(lambda: self.control_socket.exists() and self.token_file.exists(),
                   "local QSF control")
        self._create_certificates()
        self.port = free_loopback_port()
        self.gateway = subprocess.Popen(
            [
                sys.executable, str(GATEWAY),
                "--control-socket", str(self.control_socket),
                "--token-file", str(self.token_file),
                "--server-cert", str(self.path / "server.crt"),
                "--server-key", str(self.path / "server.key"),
                "--client-ca", str(self.path / "ca.crt"),
                "--listen-host", "127.0.0.1", "--listen-port", str(self.port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.gateway.stdout is not None
        readable, _, _ = select.select([self.gateway.stdout], [], [], 5)
        if not readable:
            self._fail_process("TLS gateway did not announce readiness", self.gateway)
        self.assertIn(f"port={self.port}", self.gateway.stdout.readline())

    def tearDown(self) -> None:
        for process in (getattr(self, "gateway", None), getattr(self, "control", None)):
            if process is None:
                continue
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
        if hasattr(self, "agent"):
            self.agent.close()
        if hasattr(self, "directory"):
            self.directory.cleanup()

    def _wait(self, predicate: object, label: str) -> None:
        assert callable(predicate)
        for _ in range(100):
            if predicate():
                return
            if self.control.poll() is not None:
                self._fail_process(f"{label} exited", self.control)
            time.sleep(0.02)
        self.fail(f"timed out waiting for {label}")

    def _fail_process(self, label: str, process: subprocess.Popen[str]) -> None:
        stdout, stderr = process.communicate(timeout=1)
        self.fail(f"{label}: stdout={stdout} stderr={stderr}")

    def _openssl(self, *arguments: str) -> None:
        subprocess.run(["openssl", *arguments], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _create_certificates(self) -> None:
        extensions = self.path / "extensions.cnf"
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:localhost\n",
            encoding="ascii",
        )
        client_extensions = self.path / "client-extensions.cnf"
        client_extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=clientAuth\n",
            encoding="ascii",
        )
        self._openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=qsf-test-ca", "-keyout", str(self.path / "ca.key"),
            "-out", str(self.path / "ca.crt"),
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign",
        )
        for name, subject, extension_file in (
            ("server", "/CN=localhost", extensions),
            ("client", "/CN=qsf-test-client", client_extensions),
        ):
            self._openssl(
                "req", "-newkey", "rsa:2048", "-nodes", "-subj", subject,
                "-keyout", str(self.path / f"{name}.key"),
                "-out", str(self.path / f"{name}.csr"),
            )
            self._openssl(
                "x509", "-req", "-days", "1", "-sha256",
                "-in", str(self.path / f"{name}.csr"),
                "-CA", str(self.path / "ca.crt"),
                "-CAkey", str(self.path / "ca.key"), "-CAcreateserial",
                "-out", str(self.path / f"{name}.crt"),
                "-extfile", str(extension_file),
            )

    def _client(self, *arguments: str, input_data: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                sys.executable, str(CLIENT), "--host", "127.0.0.1",
                "--port", str(self.port), "--server-name", "localhost",
                "--ca-file", str(self.path / "ca.crt"),
                "--cert-file", str(self.path / "client.crt"),
                "--key-file", str(self.path / "client.key"), *arguments,
            ],
            input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_remote_clipboard_files_resize_and_hidden_local_token(self) -> None:
        status = self._client("status")
        self.assertEqual(status.returncode, 0, status.stderr.decode())
        self.assertIn(b'"agent": "ready"', status.stdout)

        copied = "remote client → guest\nПривет".encode("utf-8")
        clipboard_set = self._client("clipboard-set", input_data=copied)
        self.assertEqual(clipboard_set.returncode, 0, clipboard_set.stderr.decode())
        self.assertEqual(self.agent.clipboard, copied)
        clipboard_get = self._client("clipboard-get")
        self.assertEqual(clipboard_get.returncode, 0, clipboard_get.stderr.decode())
        self.assertEqual(clipboard_get.stdout, copied)

        source = self.path / "client.bin"
        source.write_bytes(b"\x00remote-file\xff")
        upload = self._client("upload", str(source), "--name", "remote.bin")
        self.assertEqual(upload.returncode, 0, upload.stderr.decode())
        self.assertEqual(self.agent.incoming["remote.bin"], source.read_bytes())
        destination = self.path / "download.bin"
        download = self._client("download", "guest.txt", str(destination))
        self.assertEqual(download.returncode, 0, download.stderr.decode())
        self.assertEqual(destination.read_bytes(), b"guest-to-client\n")

        resize = self._client("resize", "1280", "720")
        self.assertEqual(resize.returncode, 0, resize.stderr.decode())
        self.assertEqual(self.agent.received_resize, (1280, 720))

        context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(self.path / "client.crt", self.path / "client.key")
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                connection.sendall(b'{"op":"status","token":"never-forwarded"}\n')
                self.assertIn(b'"ok":false', connection.recv(4096).lower())


def shutil_which(name: str) -> str | None:
    # Kept local to avoid pulling a GUI or non-stdlib Python dependency.
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    unittest.main()
