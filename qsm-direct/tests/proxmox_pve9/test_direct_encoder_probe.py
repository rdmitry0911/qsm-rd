#!/usr/bin/env python3
"""Unit coverage for direct browser encoder selection."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "qsm_direct_encoder_probe_under_test",
    ROOT / "extensions" / "direct_terminal" / "qsm_direct_encoder_probe.py")
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def success(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(command, 0, b"\x00\x00\x00\x01\x65", b"")


class DirectEncoderProbeTests(unittest.TestCase):
    def test_auto_prefers_verified_nvenc_with_worker_low_latency_options(self) -> None:
        calls: list[list[str]] = []

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            del kwargs
            calls.append(command)
            return success(command)

        selected = probe.select_auto_h264_encoder(
            run=run, which=lambda _: "/usr/bin/ffmpeg",
            candidates=(probe.DirectEncoderSelection("h264_nvenc"),
                        probe.DirectEncoderSelection("libx264")))
        self.assertEqual(selected, probe.DirectEncoderSelection("h264_nvenc"))
        self.assertIn("-zerolatency", calls[0])
        self.assertIn("-rc-lookahead", calls[0])
        self.assertEqual(calls[0][calls[0].index("-preset") + 1], "p1")

    def test_auto_falls_back_to_verified_cpu_encoder(self) -> None:
        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            del kwargs
            encoder = command[command.index("-c:v") + 1]
            if encoder == "h264_nvenc":
                return subprocess.CompletedProcess(command, 1, b"", b"unavailable")
            return success(command)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            selected = probe.select_auto_h264_encoder(
                run=run, which=lambda _: "/usr/bin/ffmpeg",
                candidates=(probe.DirectEncoderSelection("h264_nvenc"),
                            probe.DirectEncoderSelection("libx264")))
        self.assertEqual(selected, probe.DirectEncoderSelection("libx264"))
        self.assertIn("no hardware H.264 encoder initialized, using libx264", stderr.getvalue())
        self.assertIn("h264_nvenc: exit 1 (unavailable)", stderr.getvalue())

    def test_cpu_fallback_names_each_failed_accelerator_with_its_ffmpeg_error(self) -> None:
        # hz-main 2026-11-18: no VA driver was installed, so h264_vaapi on the
        # render node failed with FFmpeg's own device error while the journal
        # showed only encoder=libx264.
        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            del kwargs
            encoder = command[command.index("-c:v") + 1]
            if encoder == "h264_vaapi":
                return subprocess.CompletedProcess(
                    command, 251, b"",
                    b"[AVHWDeviceContext @ 0x1] Failed to initialise VAAPI connection: -1 (unknown libva error).\n"
                    b"Device creation failed: -5.\n")
            if encoder == "libx264":
                return success(command)
            return subprocess.CompletedProcess(command, 1, b"", b"")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            selected = probe.select_auto_h264_encoder(
                run=run, which=lambda _: "/usr/bin/ffmpeg",
                candidates=(probe.DirectEncoderSelection("h264_nvenc"),
                            probe.DirectEncoderSelection("h264_vaapi", "/dev/dri/renderD128"),
                            probe.DirectEncoderSelection("libx264")))
        self.assertEqual(selected, probe.DirectEncoderSelection("libx264"))
        message = stderr.getvalue()
        self.assertIn("h264_nvenc: exit 1;", message)
        self.assertIn("h264_vaapi@/dev/dri/renderD128: exit 251 (Device creation failed: -5.)", message)

    def test_hardware_selection_is_silent(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            selected = probe.select_auto_h264_encoder(
                run=success, which=lambda _: "/usr/bin/ffmpeg",
                candidates=(probe.DirectEncoderSelection("h264_vaapi", "/dev/dri/renderD128"),
                            probe.DirectEncoderSelection("libx264")))
        self.assertEqual(selected, probe.DirectEncoderSelection("h264_vaapi", "/dev/dri/renderD128"))
        self.assertEqual(stderr.getvalue(), "")

    def test_auto_reports_a_node_without_any_usable_encoder(self) -> None:
        with self.assertRaisesRegex(probe.DirectEncoderProbeError, "no direct H.264 encoder"):
            probe.select_auto_h264_encoder(
                run=lambda command, **_: subprocess.CompletedProcess(command, 1, b"", b"failed"),
                which=lambda _: "/usr/bin/ffmpeg",
                candidates=(probe.DirectEncoderSelection("libx264"),))

    def test_hardware_mode_never_selects_libx264(self) -> None:
        original = probe._candidates
        try:
            probe._candidates = lambda: (  # type: ignore[assignment]
                probe.DirectEncoderSelection("h264_nvenc"),
                probe.DirectEncoderSelection("libx264"),
            )
            selected = probe.select_hardware_h264_encoder(
                run=lambda command, **_: subprocess.CompletedProcess(command, 1, b"", b"unavailable"),
                which=lambda _: "/usr/bin/ffmpeg")
        except probe.DirectEncoderProbeError:
            selected = None
        finally:
            probe._candidates = original  # type: ignore[assignment]
        self.assertIsNone(selected, "hardware-only policy must fail rather than fall back to CPU")

    def test_hevc_is_advertisable_only_after_a_hardware_probe(self) -> None:
        calls: list[list[str]] = []

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            del kwargs
            calls.append(command)
            return success(command)

        selected = probe.select_hardware_hevc_encoder(
            run=run, which=lambda _: "/usr/bin/ffmpeg",
            candidates=(probe.DirectEncoderSelection("hevc_nvenc"),))
        self.assertEqual(selected, probe.DirectEncoderSelection("hevc_nvenc"))
        self.assertEqual(calls[0][calls[0].index("-c:v") + 1], "hevc_nvenc")
        self.assertIn("-zerolatency", calls[0])
        self.assertEqual(calls[0][-2], "hevc")


if __name__ == "__main__":
    unittest.main()
