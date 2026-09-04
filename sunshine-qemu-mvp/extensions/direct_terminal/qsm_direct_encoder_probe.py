"""Choose a verified low-latency H.264 encoder for QSM Direct.

The QEMU Display1 scanout says nothing about the node which encodes it.  In
particular, a VirGL guest can run on a node with NVENC, Intel QSV, VA-API, or
no usable accelerator at all.  This module therefore makes no PCI/device-name
inference: it initializes each permitted FFmpeg encoder with the same bounded
low-latency envelope used by the direct media worker and keeps the first one
which emits H.264.

An administrator's explicit per-VM encoder policy is handled by the terminal
service and is never silently substituted.  This probe is only the portable
``auto`` policy.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


_RENDER_NODE = re.compile(r"/dev/dri/renderD[0-9]{1,4}\Z")
_PROBE_WIDTH = 640
_PROBE_HEIGHT = 360
_PROBE_TIMEOUT_SECONDS = 8.0


class DirectEncoderProbeError(RuntimeError):
    """No usable H.264 encoder could be initialized."""


@dataclass(frozen=True)
class DirectEncoderSelection:
    """A concrete worker setting, verified rather than guessed."""

    encoder: str
    vaapi_device: str | None = None


Run = Callable[..., subprocess.CompletedProcess[bytes]]
Which = Callable[[str], str | None]


def _usable_render_nodes() -> tuple[str, ...]:
    """Return only actual DRM render character devices in stable order."""
    result: list[str] = []
    for candidate in sorted(Path("/dev/dri").glob("renderD*")):
        path = os.fspath(candidate)
        if not _RENDER_NODE.fullmatch(path):
            continue
        try:
            metadata = candidate.stat()
        except OSError:
            continue
        if stat.S_ISCHR(metadata.st_mode):
            result.append(path)
    return tuple(result)


def _base(ffmpeg: str) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black:s={_PROBE_WIDTH}x{_PROBE_HEIGHT}:r=30",
        "-frames:v", "2", "-an",
    ]


def _command(selection: DirectEncoderSelection, *, ffmpeg: str) -> list[str]:
    """Build fixed argv matching the direct worker's low-latency encoder mode."""
    if selection.encoder == "h264_nvenc":
        return _base(ffmpeg) + [
            "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
            "-forced-idr", "1", "-zerolatency", "1", "-delay", "0",
            "-rc-lookahead", "0", "-rc", "cbr_ld_hq", "-b:v", "20M",
            "-maxrate", "20M", "-bufsize", "333k", "-g", "30", "-bf", "0",
            "-f", "h264", "pipe:1",
        ]
    if selection.encoder == "h264_qsv":
        return _base(ffmpeg) + [
            "-vf", "format=nv12", "-c:v", "h264_qsv", "-f", "h264", "pipe:1",
        ]
    if selection.encoder == "h264_vaapi" and selection.vaapi_device is not None:
        return [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-vaapi_device", selection.vaapi_device,
            "-f", "lavfi", "-i",
            f"color=c=black:s={_PROBE_WIDTH}x{_PROBE_HEIGHT}:r=30",
            "-frames:v", "2", "-an", "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-f", "h264", "pipe:1",
        ]
    if selection.encoder == "libx264":
        return _base(ffmpeg) + [
            "-c:v", "libx264", "-threads", "2", "-thread_type", "slice",
            "-slices", "2", "-preset", "ultrafast", "-tune", "zerolatency",
            "-x264-params", "aud=1:keyint=30:min-keyint=30:scenecut=0:bframes=0:repeat-headers=1",
            "-f", "h264", "pipe:1",
        ]
    raise AssertionError("invalid direct encoder selection")


def _candidates() -> tuple[DirectEncoderSelection, ...]:
    vaapi = tuple(DirectEncoderSelection("h264_vaapi", node) for node in _usable_render_nodes())
    return (
        DirectEncoderSelection("h264_nvenc"),
        DirectEncoderSelection("h264_qsv"),
        *vaapi,
        DirectEncoderSelection("libx264"),
    )


def select_auto_h264_encoder(*, run: Run = subprocess.run,
                             which: Which = shutil.which,
                             timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
                             candidates: Sequence[DirectEncoderSelection] | None = None
                             ) -> DirectEncoderSelection:
    """Return the first encoder that produces a nonempty H.264 access unit.

    Failure of an accelerator is expected on a heterogeneous PVE cluster; it
    is deliberately a local fallback, not a terminal-service failure.  CPU
    fallback is also initialized rather than assumed available, so a broken
    FFmpeg installation produces a clear startup error.
    """
    if not 0.0 < timeout_seconds <= _PROBE_TIMEOUT_SECONDS:
        raise DirectEncoderProbeError("direct encoder probe timeout is invalid")
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        raise DirectEncoderProbeError("FFmpeg is unavailable for direct H.264 encoding")
    failures: list[str] = []
    for selection in candidates if candidates is not None else _candidates():
        command = _command(selection, ffmpeg=ffmpeg)
        try:
            completed = run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False, timeout=timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as error:
            failures.append(f"{selection.encoder}: {type(error).__name__}")
            continue
        if completed.returncode == 0 and completed.stdout:
            return selection
        failures.append(f"{selection.encoder}: exit {completed.returncode}")
    raise DirectEncoderProbeError("no direct H.264 encoder initialized (" + "; ".join(failures) + ")")
