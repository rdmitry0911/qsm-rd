#!/usr/bin/env python3
"""Bounded, headless H.264 encoder selection for the direct browser path.

This module deliberately answers one narrow question: *which encoder can this
node initialize right now?*  It does not infer an answer from PCI IDs, device
nodes, CPU count, or a previously running Sunshine instance.  The answer is
obtained by running a tiny local FFmpeg encode and is therefore usable on
NVIDIA, Intel, AMD, and CPU-only nodes alike.

The browser transport currently has H.264 as its universally required video
baseline.  Keeping selection here at that boundary also prevents a guest
VirGL/display capability from being mistaken for an encoder capability.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import json
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


class EncoderProbeError(RuntimeError):
    """No permitted local H.264 encoder could be initialized."""


_RENDER_NODE = re.compile(r"/dev/dri/renderD[0-9]+\Z")
_MAX_PROBE_SECONDS = 8.0
# NVENC's documented minimum encode surface is larger than an icon-sized
# fixture.  640x360 is still negligible, yet exercises the same initialization
# path as an interactive browser session on all supported backends.
_PROBE_WIDTH = 640
_PROBE_HEIGHT = 360


@dataclass(frozen=True)
class EncoderSelection:
    """A verified encoder implementation, not a hardware guess."""

    name: str
    ffmpeg_encoder: str
    hardware: bool

    def wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ffmpeg_encoder": self.ffmpeg_encoder,
            "hardware": self.hardware,
            "codec": "H264",
        }


_SELECTIONS: dict[str, EncoderSelection] = {
    "nvenc": EncoderSelection("nvenc", "h264_nvenc", True),
    "qsv": EncoderSelection("qsv", "h264_qsv", True),
    "vaapi": EncoderSelection("vaapi", "h264_vaapi", True),
    "software": EncoderSelection("software", "libx264", False),
}
_AUTO_ORDER = ("nvenc", "qsv", "vaapi", "software")


def configured_encoder_policy(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return a root-policy-controlled candidate order.

    ``QSUNSHINE_DIRECT_ENCODER=auto`` is the default.  An administrator may
    choose one exact backend to make fleet policy reproducible.  It is never a
    free-form FFmpeg argument, executable path, or device path.
    """
    values = os.environ if environment is None else environment
    raw = values.get("QSUNSHINE_DIRECT_ENCODER", "auto")
    if not isinstance(raw, str) or not raw.isascii():
        raise EncoderProbeError("QSUNSHINE_DIRECT_ENCODER is invalid")
    if raw == "auto":
        return _AUTO_ORDER
    if raw in _SELECTIONS:
        return (raw,)
    raise EncoderProbeError(
        "QSUNSHINE_DIRECT_ENCODER must be auto, nvenc, qsv, vaapi, or software")


def _render_node(environment: Mapping[str, str]) -> str | None:
    value = environment.get("QSUNSHINE_DIRECT_VAAPI_RENDER_NODE", "")
    if value:
        if not value.isascii() or not _RENDER_NODE.fullmatch(value):
            raise EncoderProbeError("QSUNSHINE_DIRECT_VAAPI_RENDER_NODE is invalid")
        return value if os.path.exists(value) else None
    for candidate in sorted(("/dev/dri/renderD128", "/dev/dri/renderD129",
                             "/dev/dri/renderD130", "/dev/dri/renderD131")):
        if os.path.exists(candidate):
            return candidate
    return None


def _command(selection: EncoderSelection, *, ffmpeg: str,
             environment: Mapping[str, str]) -> list[str] | None:
    """Build a fixed local init test; no caller text reaches argv."""
    source = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black:s={_PROBE_WIDTH}x{_PROBE_HEIGHT}:r=30",
        "-frames:v", "2", "-an",
    ]
    if selection.name == "nvenc":
        return source + ["-c:v", selection.ffmpeg_encoder, "-preset", "p1",
                         "-tune", "ll", "-f", "h264", "pipe:1"]
    if selection.name == "qsv":
        return source + ["-vf", "format=nv12", "-c:v", selection.ffmpeg_encoder,
                         "-f", "h264", "pipe:1"]
    if selection.name == "vaapi":
        render_node = _render_node(environment)
        if render_node is None:
            return None
        return [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-vaapi_device", render_node,
            "-f", "lavfi", "-i",
            f"color=c=black:s={_PROBE_WIDTH}x{_PROBE_HEIGHT}:r=30",
            "-frames:v", "2", "-an", "-vf", "format=nv12,hwupload",
            "-c:v", selection.ffmpeg_encoder, "-f", "h264", "pipe:1",
        ]
    if selection.name == "software":
        return source + ["-c:v", selection.ffmpeg_encoder, "-preset", "ultrafast",
                         "-tune", "zerolatency", "-f", "h264", "pipe:1"]
    raise AssertionError("unknown fixed encoder selection")


Run = Callable[..., subprocess.CompletedProcess[bytes]]


def select_h264_encoder(*, environment: Mapping[str, str] | None = None,
                        run: Run = subprocess.run,
                        which: Callable[[str], str | None] = shutil.which,
                        timeout_seconds: float | None = None) -> EncoderSelection:
    """Return the first policy-permitted encoder which emits a real H.264 AU.

    A failing accelerator is normal on a heterogeneous cluster: it simply
    falls through to the next permitted candidate.  The last CPU fallback is
    still a real encoder, not a degraded protocol or a different client path.
    """
    values = os.environ if environment is None else environment
    if timeout_seconds is not None and timeout_seconds <= 0.0:
        raise EncoderProbeError("direct H.264 encoder probe timed out")
    timeout = _MAX_PROBE_SECONDS if timeout_seconds is None else min(
        _MAX_PROBE_SECONDS, timeout_seconds)
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        raise EncoderProbeError("FFmpeg is unavailable for direct H.264 encoding")
    failures: list[str] = []
    for name in configured_encoder_policy(values):
        selection = _SELECTIONS[name]
        command = _command(selection, ffmpeg=ffmpeg, environment=values)
        if command is None:
            failures.append(f"{name}: no permitted render node")
            continue
        try:
            completed = run(command, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            failures.append(f"{name}: {type(error).__name__}")
            continue
        if completed.returncode == 0 and completed.stdout:
            return selection
        failures.append(f"{name}: initialization failed")
    raise EncoderProbeError("no permitted direct H.264 encoder initialized (" +
                            "; ".join(failures) + ")")


def main() -> int:
    """Small operator-facing preflight entry point; it accepts no input."""
    try:
        print(json.dumps(select_h264_encoder().wire(), sort_keys=True,
                         separators=(",", ":")))
    except EncoderProbeError as error:
        print(f"q-sunshine encoder probe: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
