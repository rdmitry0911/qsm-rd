#!/usr/bin/env python3
"""Regression for cancelling a real Qt profile handoff after remote dispatch.

The fake guest intentionally holds AWAIT_CONNECTION_PROFILE.  The driver is
allowed to call cancel only after this test has observed CONNECTION_OPTIMIZE at
the guest boundary, which rules out a merely local queued-request test.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_qsf_tls_gateway import QsfTlsGatewayTest


arguments = argparse.Namespace(qt_client=None)


def configure_arguments(argv: list[str]) -> list[str]:
    """Parse the CMake-supplied driver path without breaking unittest."""
    global arguments
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--qt-client", required=True, type=Path)
    arguments, unittest_arguments = parser.parse_known_args(argv)
    return unittest_arguments


class QsfQtProfileCancellationTest(QsfTlsGatewayTest):
    def test_cancel_after_remote_connection_optimize_waits_for_terminal_reply(self) -> None:
        client = arguments.qt_client
        if client is None:
            self.skipTest("the Qt profile cancellation driver is supplied by CMake")
        self.assertTrue(client.is_file(), f"Qt profile cancellation driver is missing: {client}")
        self.assertTrue(os.access(client, os.X_OK), f"Qt profile cancellation driver is not executable: {client}")

        # Leave a substantial interval between guest receipt and terminal ACK:
        # the C++ driver must stay busy throughout it. This uses the existing
        # broker/gateway and therefore exercises its genuine worker lifetime.
        self.agent.profile_apply_delay_seconds = 3.0
        cancel_marker = self.path / "cancel-after-remote-dispatch"
        environment = os.environ.copy()
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")
        process = subprocess.Popen(
            [
                str(client),
                "--host", "127.0.0.1", "--port", str(self.port),
                "--server-name", "localhost",
                "--ca-file", str(self.path / "ca.crt"),
                "--cert-file", str(self.path / "client.crt"),
                "--key-file", str(self.path / "client.key"),
                "--cancel-marker", str(cancel_marker),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            deadline = time.monotonic() + 10.0
            while not self.agent.optimization_requested and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            self.assertTrue(self.agent.optimization_requested,
                            "driver never delivered connection_optimize to the remote guest")
            self.assertIsNone(process.poll(), "driver exited before cancellation could be issued")
            cancel_marker.write_text("remote dispatch observed\n", encoding="ascii")
            stdout, stderr = process.communicate(timeout=15)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            raise

        self.assertEqual(process.returncode, 0,
                         f"Qt cancellation driver failed: stdout={stdout} stderr={stderr}")
        self.assertIn("PROFILE_CANCEL_REMOTE_DISPATCH_CONFIRMED", stdout)
        self.assertIn("PROFILE_CANCEL_START_GATE_OK", stdout)
        self.assertIn("PROFILE_CANCEL_BUSY_GUARD_OK", stdout)
        self.assertIn("PROFILE_CANCEL_TERMINAL_RELEASE_OK", stdout)


def load_tests(_loader: unittest.TestLoader, _tests: unittest.TestSuite,
               _pattern: str | None) -> unittest.TestSuite:
    """Keep this focused regression independent of its shared TLS fixture's tests."""
    suite = unittest.TestSuite()
    suite.addTest(QsfQtProfileCancellationTest(
        "test_cancel_after_remote_connection_optimize_waits_for_terminal_reply"))
    return suite


if __name__ == "__main__":
    sys.argv = [sys.argv[0], *configure_arguments(sys.argv[1:])]
    unittest.main()
