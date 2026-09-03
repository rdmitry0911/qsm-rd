#!/usr/bin/env python3
"""Exercise the native one-use .qsm receiver against a disposable TLS broker."""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


EXPECTED_CLAIM = "qsd1." + "A" * 43
EXPECTED_TICKET = "qsa1.eyJhdWQiOiJ2bS0xMDAifQ." + "A" * 43


class OneUseBroker:
    def __init__(self, certificate: Path, private_key: Path, ca_pem: str) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = int(self._listener.getsockname()[1])
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(str(certificate), str(private_key))
        self._ca_pem = ca_pem
        self.request: dict[str, object] | None = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._serve_once, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        try:
            self._listener.close()
        finally:
            self._thread.join(timeout=3)

    def _serve_once(self) -> None:
        try:
            raw, _address = self._listener.accept()
            with raw:
                with self._context.wrap_socket(raw, server_side=True) as connection:
                    data = bytearray()
                    while b"\n" not in data:
                        block = connection.recv(8192)
                        if not block:
                            raise RuntimeError("receiver disconnected before redeem request")
                        data.extend(block)
                    self.request = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
                    response = {
                        "ok": True,
                        "result": {
                            "version": 1,
                            "session_token": EXPECTED_TICKET,
                            "subject": "alice@pam",
                            "audience": "vm-100",
                            "expires_at_utc_ms": int(time.time() * 1000) + 60_000,
                            "routes": {
                                "media": {"host": "192.0.2.44", "port": 47989},
                                "qsf": {"host": "192.0.2.44", "port": 48122},
                                "lease": {"host": "192.0.2.44", "port": 48123},
                            },
                            "server_name": "localhost",
                            "ca_pem": self._ca_pem,
                        },
                    }
                    connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        except BaseException as error:  # surface fixture errors in the assertion
            self.error = error


class QtLaunchDescriptorClientTest(unittest.TestCase):
    qt_client: Path

    @classmethod
    def setUpClass(cls) -> None:
        if not cls.qt_client.is_file() or not os.access(cls.qt_client, os.X_OK):
            raise unittest.SkipTest("Qt launch-descriptor test executable is unavailable")
        if not any((Path(directory) / "openssl").is_file()
                   for directory in os.environ.get("PATH", "").split(os.pathsep)):
            raise unittest.SkipTest("openssl is required")

    def _openssl(self, *arguments: str) -> None:
        subprocess.run(["openssl", *arguments], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_tls_redeem_installs_only_ephemeral_broker_routes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="q-sunshine-qt-launch-") as temporary:
            path = Path(temporary)
            certificate = path / "broker.crt"
            private_key = path / "broker.key"
            self._openssl(
                "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=localhost", "-keyout", str(private_key), "-out", str(certificate),
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment",
                "-addext", "subjectAltName=DNS:localhost")
            private_key.chmod(0o600)
            ca_pem = certificate.read_text(encoding="ascii")
            broker = OneUseBroker(certificate, private_key, ca_pem)
            broker.start()
            descriptor = {
                "version": 1,
                "kind": "q-sunshine-pve-launch",
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": broker.port,
                    "server_name": "localhost",
                    "ca_pem": ca_pem,
                },
                "claim": EXPECTED_CLAIM,
                "expires_at_utc_ms": int(time.time() * 1000) + 60_000,
            }
            launch_file = path / "vm-100.qsm"
            launch_file.write_text(json.dumps(descriptor, separators=(",", ":")), encoding="utf-8")
            # Browser downloads are normally readable (0644), but must not be
            # writable by another account. The native reader intentionally
            # accepts this instead of requiring a manual chmod step.
            launch_file.chmod(0o644)
            environment = os.environ.copy()
            environment.update({
                "QT_QPA_PLATFORM": "offscreen",
                "XDG_CONFIG_HOME": str(path / "config"),
            })
            try:
                completed = subprocess.run(
                    [str(self.qt_client), "--launch-file", str(launch_file)],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=25, env=environment)
            finally:
                broker.close()
            self.assertIsNone(broker.error, str(broker.error))
            self.assertEqual(broker.request, {
                "version": 1,
                "op": "redeem_launch",
                "claim": EXPECTED_CLAIM,
            })
            self.assertEqual(completed.returncode, 0,
                             f"stdout={completed.stdout}\nstderr={completed.stderr}")
            self.assertIn("QSUNSHINE_LAUNCH_DESCRIPTOR_TLS_E2E_OK", completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qt-client", required=True, type=Path)
    arguments, remaining = parser.parse_known_args()
    QtLaunchDescriptorClientTest.qt_client = arguments.qt_client
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
