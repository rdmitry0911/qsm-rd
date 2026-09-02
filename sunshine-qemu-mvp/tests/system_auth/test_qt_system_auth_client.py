#!/usr/bin/env python3
"""Exercise Qt TLS/PAM login through the real ticket-mode QSF gateway."""

from __future__ import annotations

import argparse
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUTH_GATEWAY = ROOT / "extensions" / "system_auth" / "q_sunshine_auth_gateway.py"
QSF_GATEWAY = ROOT / "extensions" / "qsf_control" / "qsf_tls_gateway.py"
QSF_CONTROL = ROOT / "extensions" / "qsf_control" / "qsf_control.py"
sys.path.insert(0, str(ROOT / "tests" / "qsf_control"))

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


class QtSystemAuthClientTest(unittest.TestCase):
    qt_client: Path

    @classmethod
    def setUpClass(cls) -> None:
        if not cls.qt_client.is_file() or not os.access(cls.qt_client, os.X_OK):
            raise unittest.SkipTest("Qt system-auth test executable is unavailable")
        if not shutil_which("openssl"):
            raise unittest.SkipTest("openssl is required")

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="q-sunshine-qt-system-auth-")
        self.path = Path(self.directory.name)
        self._create_certificates()
        self.ticket_key = self.path / "ticket.key"
        self.ticket_key.write_bytes(os.urandom(32))
        self.ticket_key.chmod(0o600)
        helper = self.path / "fake-pam-helper.py"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "data = sys.stdin.buffer.read()\n"
            "ok = False\n"
            "if len(data) >= 4:\n"
            " n = int.from_bytes(data[:2], 'big'); p = 2 + n\n"
            " if p + 2 <= len(data):\n"
            "  u = data[2:p]; m = int.from_bytes(data[p:p+2], 'big'); w = data[p+2:p+2+m]\n"
            "  ok = p + 2 + m == len(data) and u == b'alice' and w == b'correct horse'\n"
            "raise SystemExit(0 if ok else 1)\n", encoding="ascii")
        helper.chmod(0o700)
        self.auth_port = free_loopback_port()
        self.auth_gateway = subprocess.Popen(
            [
                sys.executable, str(AUTH_GATEWAY),
                "--server-cert", str(self.path / "server.crt"),
                "--server-key", str(self.path / "server.key"),
                "--ticket-key", str(self.ticket_key), "--audience", "vm-100",
                "--pam-helper", str(helper), "--pam-service", "q-sunshine-test",
                "--allow-user", "alice", "--listen-host", "127.0.0.1",
                "--listen-port", str(self.auth_port),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self._wait_ready(self.auth_gateway, "Q_SUNSHINE_SYSTEM_AUTH_READY")
        self._start_ticket_mode_qsf_gateway()

    def _wait_ready(self, process: subprocess.Popen[str], marker: str) -> None:
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 5)
        if not readable:
            stdout, stderr = process.communicate(timeout=1)
            self.fail(f"gateway did not start: stdout={stdout} stderr={stderr}")
        self.assertIn(marker, process.stdout.readline())

    def _start_ticket_mode_qsf_gateway(self) -> None:
        self.agent = FakeAgent(self.path / "agent.sock")
        self.control_socket = self.path / "control.sock"
        self.control_token = self.path / "control.token"
        environment = os.environ.copy()
        environment.update({
            "QSUNSHINE_QSF_HOST_MAX_WIDTH": "1920",
            "QSUNSHINE_QSF_HOST_MAX_HEIGHT": "1080",
            "QSUNSHINE_QSF_HOST_MAX_FPS": "60",
            "QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS": "12000",
            "QSUNSHINE_QSF_HOST_ENCODER_CODECS": "H264",
        })
        self.control = subprocess.Popen(
            [
                sys.executable, str(QSF_CONTROL),
                "--agent-socket", str(self.path / "agent.sock"),
                "--control-socket", str(self.control_socket),
                "--token-file", str(self.control_token), "--no-qemu-resize",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
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
        self.qsf_port = free_loopback_port()
        self.qsf_gateway = subprocess.Popen(
            [
                sys.executable, str(QSF_GATEWAY),
                "--control-socket", str(self.control_socket),
                "--token-file", str(self.control_token),
                "--server-cert", str(self.path / "server.crt"),
                "--server-key", str(self.path / "server.key"),
                "--system-auth-ticket-key", str(self.ticket_key),
                "--system-auth-audience", "vm-100",
                "--listen-host", "127.0.0.1", "--listen-port", str(self.qsf_port),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self._wait_ready(self.qsf_gateway, "QSF_TLS_GATEWAY_READY")

    def tearDown(self) -> None:
        terminate(getattr(self, "qsf_gateway", None))
        terminate(getattr(self, "control", None))
        if hasattr(self, "agent"):
            self.agent.close()
        terminate(getattr(self, "auth_gateway", None))
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
            "-subj", "/CN=q-sunshine-qt-system-auth-ca",
            "-keyout", str(self.path / "ca.key"), "-out", str(self.path / "ca.crt"),
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign")
        self._openssl(
            "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
            "-keyout", str(self.path / "server.key"), "-out", str(self.path / "server.csr"))
        self._openssl(
            "x509", "-req", "-days", "1", "-sha256", "-in", str(self.path / "server.csr"),
            "-CA", str(self.path / "ca.crt"), "-CAkey", str(self.path / "ca.key"),
            "-CAcreateserial", "-out", str(self.path / "server.crt"), "-extfile", str(extension))

    def _run_qt_client(self, expected_audience: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment["XDG_CONFIG_HOME"] = str(self.path / "config")
        return subprocess.run(
            [
                str(self.qt_client),
                "--auth-host", "127.0.0.1", "--auth-port", str(self.auth_port),
                "--auth-server-name", "localhost", "--auth-ca-file", str(self.path / "ca.crt"),
                "--qsf-host", "127.0.0.1", "--qsf-port", str(self.qsf_port),
                "--qsf-server-name", "localhost", "--qsf-ca-file", str(self.path / "ca.crt"),
                "--profile-id", "vm-100", "--expected-audience", expected_audience,
                "--username", "alice", "--password-stdin",
            ], input="correct horse\n", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, env=environment,
        )

    def test_tls_pam_login_qsf_ticket_gateway_logout_expiry_and_secret_boundary(self) -> None:
        completed = self._run_qt_client("vm-100")
        self.assertEqual(completed.returncode, 0,
                         f"stdout={completed.stdout}\nstderr={completed.stderr}")
        self.assertIn("QSUNSHINE_SYSTEM_AUTH_QT_QSF_E2E_OK", completed.stdout)
        self.assertIn("QSUNSHINE_SYSTEM_AUTH_EXPIRY_GUARD_OK", completed.stdout)
        self.assertIn("QSUNSHINE_SYSTEM_AUTH_QT_CLIENT_OK", completed.stdout)

    def test_ticket_for_another_vm_audience_never_opens_client_admission(self) -> None:
        completed = self._run_qt_client("vm-101")
        self.assertNotEqual(completed.returncode, 0,
                            f"unexpected stdout={completed.stdout}\nstderr={completed.stderr}")
        self.assertIn("QSUNSHINE_SYSTEM_AUTH_QT_TEST_FAILED=System-authentication response is invalid",
                      completed.stderr)
        self.assertNotIn("QSUNSHINE_SYSTEM_AUTH_QT_QSF_E2E_OK", completed.stdout)


def shutil_which(program: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / program
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qt-client", required=True, type=Path)
    arguments, remaining = parser.parse_known_args()
    QtSystemAuthClientTest.qt_client = arguments.qt_client
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
    return 0


if __name__ == "__main__":
    main()
