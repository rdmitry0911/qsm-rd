#!/usr/bin/env python3
"""Exercise the production Qt QSF client through the real mTLS gateway.

The client executable is deliberately a small headless driver, but it links
the exact QsfClient class used by qsunshine-client.  This keeps the mTLS,
clipboard, file and resize assertions independent of a GUI compositor.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from test_qsf_tls_gateway import QsfTlsGatewayTest


arguments = argparse.Namespace(qt_client=None)


def configure_arguments(argv: list[str]) -> list[str]:
    """Parse the standalone runner option without breaking unittest discovery."""
    global arguments
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--qt-client", required=True, type=Path)
    arguments, unittest_arguments = parser.parse_known_args(argv)
    return unittest_arguments


class QsfQtClientE2ETest(QsfTlsGatewayTest):
    def test_qt_client_mtls_clipboard_files_and_resize(self) -> None:
        client = arguments.qt_client
        if client is None:
            self.skipTest("Qt QSF driver is supplied only by the CMake E2E runner")
        self.assertTrue(client.is_file(), f"Qt QSF client is missing: {client}")
        self.assertTrue(os.access(client, os.X_OK), f"Qt QSF client is not executable: {client}")

        guest_clipboard = "guest → Qt\nПривет".encode("utf-8")
        client_clipboard = "Qt → guest\nЗдравствуйте".encode("utf-8")
        self.agent.clipboard = guest_clipboard

        upload_source = self.path / "qt-client-upload.bin"
        upload_bytes = b"\x00Qt-client-file\xff\n"
        upload_source.write_bytes(upload_bytes)
        download_destination = self.path / "qt-client-download.bin"

        environment = os.environ.copy()
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")
        result = subprocess.run(
            [
                str(client),
                "--host", "127.0.0.1", "--port", str(self.port),
                "--server-name", "localhost",
                "--ca-file", str(self.path / "ca.crt"),
                "--cert-file", str(self.path / "client.crt"),
                "--key-file", str(self.path / "client.key"),
                "--expected-guest-clipboard", guest_clipboard.decode("utf-8"),
                "--client-clipboard", client_clipboard.decode("utf-8"),
                "--upload-source", str(upload_source), "--upload-name", "qt-client.bin",
                "--download-name", "guest.txt",
                "--download-destination", str(download_destination),
                "--optimized-resolution", "2560x1440",
                "--optimized-fps", "60",
                "--optimized-bitrate", "28000",
                "--optimized-codec", "H.264",
                "--resize", "1280x720",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            timeout=110,
        )
        self.assertEqual(
            result.returncode, 0,
            f"Qt QSF driver failed: stdout={result.stdout} stderr={result.stderr}",
        )
        self.assertIn("QSF_QT_CLIENT_HOST_OPTIMIZATION_OK", result.stdout)
        self.assertIn("QSF_QT_CLIENT_E2E_OK", result.stdout)
        self.assertEqual(self.agent.clipboard, client_clipboard)
        self.assertEqual(self.agent.incoming["qt-client.bin"], upload_bytes)
        self.assertEqual(download_destination.read_bytes(), b"guest-to-client\n")
        self.assertEqual(self.agent.received_resize, (1280, 720))
        self.assertTrue(self.agent.optimization_requested)
        self.assertEqual(self.agent.received_pair_capabilities, (2560, 1440, 60, 28000, "H264"))


if __name__ == "__main__":
    import unittest

    sys.argv = [sys.argv[0], *configure_arguments(sys.argv[1:])]
    unittest.main()
