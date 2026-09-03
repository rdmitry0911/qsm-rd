#!/usr/bin/env python3
"""Exercise the real Qt E2E driver through the PVE .qsm receiver path.

This is intentionally a bootstrap smoke, not a synthetic media result.  The
disposable broker proves the exact one-use redeem protocol and the real driver
then starts the same lease-aware Moonlight controller used by the full visual
and QSF harness.  It stops before QSF activation because no guest is involved.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
        self._listener.settimeout(10)
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
            self._thread.join(timeout=12)

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
                    connection.sendall(
                        json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        except BaseException as error:  # fixture errors are asserted by the caller
            self.error = error


class DescriptorRealDriverTest(unittest.TestCase):
    real_driver: Path

    @classmethod
    def setUpClass(cls) -> None:
        if not cls.real_driver.is_file() or not os.access(cls.real_driver, os.X_OK):
            raise unittest.SkipTest("real Qt descriptor E2E driver is unavailable")
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("openssl is required")

    def _openssl(self, *arguments: str) -> None:
        subprocess.run(["openssl", *arguments], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_descriptor_mode_redeems_then_starts_and_scrubs_the_child_ca_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="q-sunshine-qt-real-descriptor-") as temporary:
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
            launch_file.chmod(0o644)
            phase_directory = path / "phases"
            phase_directory.mkdir(mode=0o700)
            child_directory = path / "child"
            child_directory.mkdir(mode=0o700)
            fake_moonlight = path / "moonlight-fake"
            fake_moonlight.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                ": \"${QSUNSHINE_DESCRIPTOR_CHILD_DIR:?}\"\n"
                "printf '%s\\n' \"$@\" > \"$QSUNSHINE_DESCRIPTOR_CHILD_DIR/argv\"\n"
                "test -n \"${QSM_GAMESTREAM_AUTH_CA_FILE:-}\"\n"
                "printf '%s\\n' \"$QSM_GAMESTREAM_AUTH_CA_FILE\" > \"$QSUNSHINE_DESCRIPTOR_CHILD_DIR/ca-path\"\n"
                "stat -c '%a' \"$QSM_GAMESTREAM_AUTH_CA_FILE\" > \"$QSUNSHINE_DESCRIPTOR_CHILD_DIR/ca-mode\"\n"
                "test -r \"$QSM_GAMESTREAM_AUTH_CA_FILE\"\n"
                "trap 'exit 0' TERM INT\n"
                "while :; do sleep 1; done\n",
                encoding="utf-8")
            fake_moonlight.chmod(0o700)
            environment = os.environ.copy()
            environment.update({
                "QT_QPA_PLATFORM": "offscreen",
                "XDG_CONFIG_HOME": str(path / "config"),
                "XDG_DATA_HOME": str(path / "data"),
                "XDG_CACHE_HOME": str(path / "cache"),
                "QSUNSHINE_DESCRIPTOR_CHILD_DIR": str(child_directory),
            })
            broker.start()
            try:
                completed = subprocess.run(
                    [str(self.real_driver), "--launch-file", str(launch_file),
                     "--descriptor-bootstrap-only", "--moonlight-binary", str(fake_moonlight),
                     "--initial-resolution", "1280x720", "--phase-dir", str(phase_directory),
                     "--timeout-ms", "60000"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=30, env=environment)
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
            self.assertIn("QSUNSHINE_QT_DESCRIPTOR_BOOTSTRAP_OK", completed.stdout)
            self.assertNotIn(EXPECTED_CLAIM, completed.stdout + completed.stderr)
            self.assertNotIn(EXPECTED_TICKET, completed.stdout + completed.stderr)
            self.assertEqual((child_directory / "ca-mode").read_text(encoding="ascii").strip(), "600")
            ca_path = Path((child_directory / "ca-path").read_text(encoding="utf-8").strip())
            self.assertFalse(ca_path.exists(), "Moonlight CA file survived receiver teardown")
            child_argv = (child_directory / "argv").read_text(encoding="utf-8").splitlines()
            self.assertIn("stream", child_argv)
            self.assertIn("--qsm-system-auth", child_argv)
            self.assertIn("--display-mode", child_argv)
            self.assertIn("windowed", child_argv)
            self.assertIn("--resolution", child_argv)
            self.assertIn("1280x720", child_argv)
            self.assertNotIn(EXPECTED_CLAIM, child_argv)
            self.assertNotIn(EXPECTED_TICKET, child_argv)
            self.assertTrue((phase_directory / "driver-ready").is_file())
            self.assertTrue((phase_directory / "descriptor-redeem-started").is_file())
            self.assertTrue((phase_directory / "descriptor-redeemed").is_file())
            self.assertTrue((phase_directory / "windowed-process-started").is_file())
            self.assertTrue((phase_directory / "descriptor-bootstrap-verified").is_file())
            self.assertTrue((phase_directory / "complete").is_file())

    def test_descriptor_mode_rejects_legacy_login_and_application_ingress_before_reading_a_file(self) -> None:
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        for option, value in (("--system-auth-username", "alice"), ("--app", "Desktop")):
            with self.subTest(option=option):
                completed = subprocess.run(
                    [str(self.real_driver), "--launch-file", "/nonexistent/vm-100.qsm",
                     option, value],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                    env=environment)
                self.assertEqual(completed.returncode, 2)
                self.assertIn(f"{option} is not accepted with --launch-file", completed.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-driver", required=True, type=Path)
    arguments, remaining = parser.parse_known_args()
    DescriptorRealDriverTest.real_driver = arguments.real_driver
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
