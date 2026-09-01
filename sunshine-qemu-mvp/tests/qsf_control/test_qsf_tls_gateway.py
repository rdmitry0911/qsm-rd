#!/usr/bin/env python3
"""End-to-end mTLS companion test through the real local QSF broker."""

from __future__ import annotations

import importlib.util
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


def load_tls_client_module() -> object:
    specification = importlib.util.spec_from_file_location("qsf_tls_client_test", CLIENT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


TLS_CLIENT_MODULE = load_tls_client_module()


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
        control_environment = os.environ.copy()
        control_environment.update({
            "QSUNSHINE_QSF_HOST_MAX_WIDTH": "2560",
            "QSUNSHINE_QSF_HOST_MAX_HEIGHT": "1440",
            "QSUNSHINE_QSF_HOST_MAX_FPS": "60",
            "QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS": "30000",
            "QSUNSHINE_QSF_HOST_ENCODER_CODECS": "H264",
        })
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
            env=control_environment,
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

    def _client(self, *arguments: str, input_data: bytes = b"",
                timeout: float | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                sys.executable, str(CLIENT), "--host", "127.0.0.1",
                "--port", str(self.port), "--server-name", "localhost",
                "--ca-file", str(self.path / "ca.crt"),
                "--cert-file", str(self.path / "client.crt"),
                "--key-file", str(self.path / "client.key"), *arguments,
            ],
            input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
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

        optimized = self._client("optimize-connection", "--resolution", "2560x1440",
                                 "--max-fps", "60", "--decoder-codecs", "H264")
        self.assertEqual(optimized.returncode, 0, optimized.stderr.decode())
        self.assertIn(b'"width": 2560', optimized.stdout)
        self.assertIn(b'"video_codec": "H.264"', optimized.stdout)
        self.assertTrue(self.agent.optimization_requested)
        self.assertEqual(self.agent.received_pair_capabilities, (2560, 1440, 60, 28000, "H264"))

        context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(self.path / "client.crt", self.path / "client.key")
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                connection.sendall(b'{"op":"status","token":"never-forwarded"}\n')
                self.assertIn(b'"ok":false', connection.recv(4096).lower())

    def test_gateway_keeps_profile_request_open_for_guest_scanout_ack(self) -> None:
        """The mTLS hop must not preempt the broker's bounded apply window."""
        # This deliberately exceeds the short TLS request-read limit.  It
        # models a real Weston/seatd restart while the broker is waiting for
        # the guest's authoritative connection-profile acknowledgement.
        self.agent.profile_apply_delay_seconds = 13.0
        started = time.monotonic()
        optimized = self._client("optimize-connection", "--resolution", "1280x720",
                                 "--max-fps", "60", "--decoder-codecs", "H264")
        elapsed = time.monotonic() - started
        self.assertEqual(optimized.returncode, 0, optimized.stderr.decode())
        self.assertGreaterEqual(elapsed, 12.5)
        self.assertLess(elapsed, 30.0)
        self.assertIn(b'"width": 1280', optimized.stdout)

    def test_silent_tls_peer_cannot_block_another_client_handshake(self) -> None:
        """TLS handshake work belongs to a bounded worker, not accept()."""
        slow_peer = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            # The server's select loop polls at 250 ms.  Let it accept this
            # plain TCP peer, which intentionally never sends a TLS ClientHello.
            time.sleep(0.4)
            started = time.monotonic()
            try:
                status = self._client("status", timeout=5)
            except subprocess.TimeoutExpired as error:
                self.fail(f"a silent TLS peer blocked the accept loop: {error}")
            elapsed = time.monotonic() - started
            self.assertEqual(status.returncode, 0, status.stderr.decode())
            self.assertLess(elapsed, 4.0)
        finally:
            slow_peer.close()


class QsfTlsClientDeadlineTest(unittest.TestCase):
    def test_dribbled_reply_uses_one_absolute_deadline(self) -> None:
        """Each recv must consume the same client operation budget."""
        class DribblingConnection:
            def __init__(self) -> None:
                self.timeouts: list[float] = []

            def settimeout(self, value: float) -> None:
                self.timeouts.append(value)

            def recv(self, _size: int) -> bytes:
                time.sleep(0.04)
                return b"x"

        connection = DribblingConnection()
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            TLS_CLIENT_MODULE.read_line(connection, time.monotonic() + 0.07)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(len(connection.timeouts), 2)
        self.assertGreater(connection.timeouts[0], connection.timeouts[-1])
        self.assertLess(elapsed, 0.25)


def shutil_which(name: str) -> str | None:
    # Kept local to avoid pulling a GUI or non-stdlib Python dependency.
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    unittest.main()
