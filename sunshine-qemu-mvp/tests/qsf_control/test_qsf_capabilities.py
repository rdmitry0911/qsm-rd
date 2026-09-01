#!/usr/bin/env python3
"""Focused capability-contract tests for the QSF broker."""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "qsf_control_under_test", ROOT / "extensions" / "qsf_control" / "qsf_control.py")
assert SPEC is not None and SPEC.loader is not None
qsf_control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qsf_control)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False

    def read(self, size: int) -> bytes:
        return self.payload[:size]


class CapabilityContractTest(unittest.TestCase):
    def _environment(self, **extra: str) -> dict[str, str]:
        return {
            "QSUNSHINE_QSF_HOST_MAX_WIDTH": "2560",
            "QSUNSHINE_QSF_HOST_MAX_HEIGHT": "1440",
            "QSUNSHINE_QSF_HOST_MAX_FPS": "60",
            "QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS": "30000",
            "QSUNSHINE_QSF_HOST_ENCODER_CODECS": "H264,HEVC,AV1",
            "QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL": "",
            "QSUNSHINE_QSF_SUNSHINE_SERVERINFO_CA_FILE": "",
            **extra,
        }

    def test_serverinfo_intersects_only_codecs_with_explicit_tested_envelope(self) -> None:
        # H.264 (0x1), HEVC (0x100), and AV1 Main8 (0x10000).  Deliberately
        # absent are any imagined width/FPS/bitrate limits: serverinfo does
        # not publish a trustworthy throughput envelope.
        payload = b"<root><ServerCodecModeSupport>65793</ServerCodecModeSupport></root>"
        environment = self._environment(
            QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL="http://127.0.0.1:47990/serverinfo")
        with patch.dict(os.environ, environment, clear=False), \
                patch.object(qsf_control.urllib.request, "urlopen",
                             return_value=_Response(payload)) as urlopen:
            capabilities = qsf_control.detected_host_encoder_capabilities()
        self.assertEqual(capabilities["encoder_codecs"], ("H264", "HEVC", "AV1"))
        self.assertEqual(capabilities["max_width"], 2560)
        self.assertEqual(capabilities["max_height"], 1440)
        self.assertEqual(capabilities["max_fps"], 60)
        self.assertEqual(capabilities["max_bitrate_kbps"], 30000)
        urlopen.assert_called_once()

    def test_serverinfo_removes_unavailable_codecs_without_rewriting_geometry(self) -> None:
        environment = self._environment(
            QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL="http://localhost:47990/serverinfo")
        with patch.dict(os.environ, environment, clear=False), \
                patch.object(qsf_control.urllib.request, "urlopen",
                             return_value=_Response(
                                 b"<root><ServerCodecModeSupport>1</ServerCodecModeSupport></root>")):
            capabilities = qsf_control.detected_host_encoder_capabilities()
        self.assertEqual(capabilities["encoder_codecs"], ("H264",))
        self.assertEqual((capabilities["max_width"], capabilities["max_height"],
                          capabilities["max_fps"], capabilities["max_bitrate_kbps"]),
                         (2560, 1440, 60, 30000))

    def test_profile_deadline_caps_serverinfo_and_qemu_steps(self) -> None:
        """Slow local probes cannot silently extend a profile transaction."""
        environment = self._environment(
            QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL="http://127.0.0.1:47990/serverinfo")
        with patch.dict(os.environ, environment, clear=False), \
                patch.object(qsf_control.time, "monotonic", return_value=100.0), \
                patch.object(qsf_control.urllib.request, "urlopen", return_value=_Response(
                    b"<root><ServerCodecModeSupport>1</ServerCodecModeSupport></root>")) as urlopen:
            self.assertEqual(qsf_control._sunshine_serverinfo_encoder_codecs(deadline=101.25),
                             ("H264",))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 1.25)

        broker = qsf_control.Broker.__new__(qsf_control.Broker)
        broker._enable_qemu_resize = True

        class RunResult:
            returncode = 0
            stdout = ""

        with patch.object(qsf_control.time, "monotonic", return_value=100.0), \
                patch.object(qsf_control.subprocess, "run", return_value=RunResult()) as run:
            self.assertEqual(broker._set_qemu_ui_info(1920, 1080, deadline=101.25), "applied")
        self.assertEqual(run.call_args.kwargs["timeout"], 1.25)

    def test_expired_profile_deadline_does_not_start_serverinfo_probe(self) -> None:
        environment = self._environment(
            QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL="http://127.0.0.1:47990/serverinfo")
        with patch.dict(os.environ, environment, clear=False), \
                patch.object(qsf_control.time, "monotonic", return_value=100.0), \
                patch.object(qsf_control.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(qsf_control.ControlError,
                                        "connection profile transaction timed out"):
                qsf_control._sunshine_serverinfo_encoder_codecs(deadline=100.0)
        urlopen.assert_not_called()

    def test_resolution_is_client_aspect_preserving_when_pair_ceiling_is_lower(self) -> None:
        self.assertEqual(qsf_control._fit_resolution(3840, 2160, 2560, 1440),
                         (2560, 1440))
        self.assertEqual(qsf_control._fit_resolution(2560, 1600, 1920, 1080),
                         (1728, 1080))

    def test_serverinfo_url_cannot_be_an_arbitrary_network_target(self) -> None:
        with patch.dict(os.environ, self._environment(
                QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL="http://example.invalid/serverinfo"),
                clear=False):
            with self.assertRaises(qsf_control.ControlError):
                qsf_control.detected_host_encoder_capabilities()


if __name__ == "__main__":
    unittest.main()
