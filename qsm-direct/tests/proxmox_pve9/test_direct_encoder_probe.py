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
    if command[0].endswith("ffprobe"):
        # A conforming encoder: the decoder presents the probed picture.
        return subprocess.CompletedProcess(
            command, 0, f"{probe._PROBE_WIDTH},{probe._PROBE_HEIGHT}\n".encode(), b"")
    return subprocess.CompletedProcess(command, 0, b"\x00\x00\x00\x01\x65", b"")


def which(name: str) -> str:
    return f"/usr/bin/{name}"


def misdeclared(encoder: str, width: int, height: int):
    """FFmpeg exits 0, but the stream declares another picture size."""
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[0].endswith("ffprobe"):
            stream = kwargs["input"]
            if stream == encoder.encode():
                return subprocess.CompletedProcess(command, 0, f"{width},{height}\n".encode(), b"")
            return success(command)
        used = command[command.index("-c:v") + 1]
        return subprocess.CompletedProcess(command, 0, used.encode(), b"")
    return run


class DirectEncoderProbeTests(unittest.TestCase):
    def test_auto_prefers_verified_nvenc_with_worker_low_latency_options(self) -> None:
        calls: list[list[str]] = []

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            del kwargs
            calls.append(command)
            return success(command)

        selected = probe.select_auto_h264_encoder(
            run=run, which=which,
            candidates=(probe.DirectEncoderSelection("h264_nvenc"),
                        probe.DirectEncoderSelection("libx264")))
        self.assertEqual(selected, probe.DirectEncoderSelection("h264_nvenc"))
        self.assertIn("-zerolatency", calls[0])
        self.assertIn("-rc-lookahead", calls[0])
        self.assertEqual(calls[0][calls[0].index("-preset") + 1], "p1")

    def test_auto_falls_back_to_verified_cpu_encoder(self) -> None:
        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            del kwargs
            if command[0].endswith("ffprobe"):
                return success(command)
            encoder = command[command.index("-c:v") + 1]
            if encoder == "h264_nvenc":
                return subprocess.CompletedProcess(command, 1, b"", b"unavailable")
            return success(command)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            selected = probe.select_auto_h264_encoder(
                run=run, which=which,
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
            if command[0].endswith("ffprobe"):
                return success(command)
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
                run=run, which=which,
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
                run=success, which=which,
                candidates=(probe.DirectEncoderSelection("h264_vaapi", "/dev/dri/renderD128"),
                            probe.DirectEncoderSelection("libx264")))
        self.assertEqual(selected, probe.DirectEncoderSelection("h264_vaapi", "/dev/dri/renderD128"))
        self.assertEqual(stderr.getvalue(), "")

    def test_auto_reports_a_node_without_any_usable_encoder(self) -> None:
        with self.assertRaisesRegex(probe.DirectEncoderProbeError, "no direct H.264 encoder"):
            probe.select_auto_h264_encoder(
                run=lambda command, **_: subprocess.CompletedProcess(command, 1, b"", b"failed"),
                which=which,
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
                which=which)
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
            run=run, which=which,
            candidates=(probe.DirectEncoderSelection("hevc_nvenc"),))
        self.assertEqual(selected, probe.DirectEncoderSelection("hevc_nvenc"))
        self.assertEqual(calls[0][calls[0].index("-c:v") + 1], "hevc_nvenc")
        self.assertIn("-zerolatency", calls[0])
        self.assertEqual(calls[0][-2], "hevc")

    def test_probe_size_exposes_padded_coded_pictures(self) -> None:
        # Not a multiple of 64 wide nor of 8 high, and even for 4:2:0.
        self.assertNotEqual(probe._PROBE_WIDTH % 64, 0)
        self.assertNotEqual(probe._PROBE_HEIGHT % 8, 0)
        self.assertEqual((probe._PROBE_WIDTH % 2, probe._PROBE_HEIGHT % 2), (0, 0))
        command = probe._command(probe.DirectEncoderSelection("hevc_vaapi", "/dev/dri/renderD128"),
                                 ffmpeg="/usr/bin/ffmpeg")
        self.assertIn(f"color=c=black:s={probe._PROBE_WIDTH}x{probe._PROBE_HEIGHT}:r=30", command)

    def test_hevc_encoder_declaring_another_picture_size_is_not_advertised(self) -> None:
        # hz-main, AMD Raphael, Mesa 25.0.7, FFmpeg 7.1.5: hevc_vaapi pads the
        # coded picture to 1472x688 but crops it to 1466x686.  The browser then
        # shows 1466x686 for a 1410x686 guest, so a resized Console re-sent the
        # same size until it gave up and never presented the guest's geometry.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(probe.DirectEncoderProbeError) as raised:
            probe.select_hardware_hevc_encoder(
                run=misdeclared("hevc_vaapi", 1466, 686), which=which,
                candidates=(probe.DirectEncoderSelection("hevc_vaapi", "/dev/dri/renderD128"),))
        self.assertIn("hevc_vaapi@/dev/dri/renderD128: stream geometry 1466x686, not 1410x686",
                      str(raised.exception))

    def test_a_conforming_hevc_encoder_after_a_misdeclaring_one_is_selected(self) -> None:
        selected = probe.select_hardware_hevc_encoder(
            run=misdeclared("hevc_vaapi", 1466, 686), which=which,
            candidates=(probe.DirectEncoderSelection("hevc_vaapi", "/dev/dri/renderD128"),
                        probe.DirectEncoderSelection("hevc_nvenc")))
        self.assertEqual(selected, probe.DirectEncoderSelection("hevc_nvenc"))

    def test_h264_geometry_is_verified_too_and_names_the_fallback(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            selected = probe.select_auto_h264_encoder(
                run=misdeclared("h264_vaapi", 1424, 688), which=which,
                candidates=(probe.DirectEncoderSelection("h264_vaapi", "/dev/dri/renderD128"),
                            probe.DirectEncoderSelection("libx264")))
        self.assertEqual(selected, probe.DirectEncoderSelection("libx264"))
        self.assertIn("stream geometry 1424x688, not 1410x686", stderr.getvalue())

    def test_unreadable_geometry_is_not_accepted(self) -> None:
        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            del kwargs
            if command[0].endswith("ffprobe"):
                return subprocess.CompletedProcess(command, 1, b"", b"Invalid data")
            return success(command)

        with self.assertRaises(probe.DirectEncoderProbeError) as raised:
            probe.select_hardware_hevc_encoder(
                run=run, which=which, candidates=(probe.DirectEncoderSelection("hevc_nvenc"),))
        self.assertIn("hevc_nvenc: stream geometry unreadable", str(raised.exception))

    def test_missing_ffprobe_is_a_clear_probe_error(self) -> None:
        with self.assertRaises(probe.DirectEncoderProbeError) as raised:
            probe.select_auto_h264_encoder(
                run=success, which=lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
                candidates=(probe.DirectEncoderSelection("libx264"),))
        self.assertIn("FFprobe is unavailable", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
