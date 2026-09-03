#!/usr/bin/env python3
"""Hardware-neutral tests for the direct H.264 encoder policy."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "q_sunshine_encoder_probe_under_test",
    ROOT / "extensions" / "qsf_control" / "q_sunshine_encoder_probe.py")
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def _success(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(command, 0, b"\x00\x00\x00\x01\x65", b"")


class EncoderProbeTest(unittest.TestCase):
    def test_auto_prefers_working_acceleration(self) -> None:
        calls: list[list[str]] = []

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            del kwargs
            calls.append(command)
            return _success(command)

        result = probe.select_h264_encoder(environment={}, run=run,
                                           which=lambda _: "/usr/bin/ffmpeg")
        self.assertEqual(result.name, "nvenc")
        self.assertTrue(result.hardware)
        self.assertEqual(calls[0][-3:], ["-f", "h264", "pipe:1"])

    def test_auto_falls_back_from_failed_accelerators_to_cpu(self) -> None:
        seen: list[str] = []

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            del kwargs
            encoder = command[command.index("-c:v") + 1]
            seen.append(encoder)
            if encoder == "libx264":
                return _success(command)
            return subprocess.CompletedProcess(command, 1, b"", b"unavailable")

        result = probe.select_h264_encoder(environment={}, run=run,
                                           which=lambda _: "/usr/bin/ffmpeg")
        self.assertEqual(result.name, "software")
        # The current fixture does expose a render node, so its VAAPI attempt
        # is observable too.  It still falls through rather than assuming the
        # node implies a usable driver.
        self.assertEqual(seen, ["h264_nvenc", "h264_qsv", "h264_vaapi", "libx264"])

    def test_explicit_policy_never_silently_changes_backend(self) -> None:
        with self.assertRaisesRegex(probe.EncoderProbeError, "no permitted"):
            probe.select_h264_encoder(
                environment={"QSUNSHINE_DIRECT_ENCODER": "nvenc"},
                run=lambda command, **kwargs: subprocess.CompletedProcess(command, 1, b"", b""),
                which=lambda _: "/usr/bin/ffmpeg")

    def test_vaapi_has_a_fixed_render_node_boundary(self) -> None:
        with self.assertRaisesRegex(probe.EncoderProbeError, "RENDER_NODE"):
            probe.select_h264_encoder(
                environment={"QSUNSHINE_DIRECT_ENCODER": "vaapi",
                             "QSUNSHINE_DIRECT_VAAPI_RENDER_NODE": "/tmp/not-a-node"},
                which=lambda _: "/usr/bin/ffmpeg")

    def test_invalid_policy_is_rejected_before_a_process_is_started(self) -> None:
        with self.assertRaisesRegex(probe.EncoderProbeError, "must be"):
            probe.select_h264_encoder(
                environment={"QSUNSHINE_DIRECT_ENCODER": "nvenc;bad"},
                which=lambda _: "/usr/bin/ffmpeg")


if __name__ == "__main__":
    unittest.main()
