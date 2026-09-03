#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the patched Moonlight local encoded-stream bridge end to end.

This is intentionally a lab-only companion to run-descriptor-qt-virgl-qsf-e2e.sh.
It starts two owner-private AF_UNIX/SOCK_SEQPACKET listeners, makes the real
browser -> PVE -> descriptor -> Moonlight route run with the corresponding
QSM_BROWSER_* contexts, then verifies the framed H.264 and Opus tap.

It records counts and invariants only.  Neither GameStream/PVE credentials nor
media payload bytes are retained in the evidence directory.
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


PACKET_MAGIC = 0x51534D50  # "QSMP"
PACKET_HEADER = struct.Struct("!IIIII")
PACKET_FIRST = 0x00000001
PACKET_END = 0x00000002
PACKET_IDR = 0x00000004
PACKET_AUDIO = 0x00000008
PACKET_CONFIG = 0x00000010
MAX_FRAGMENT_BYTES = 256 * 1024


class TapError(RuntimeError):
    """A local tap packet violated the intentionally small protocol."""


@dataclass
class VideoState:
    connections: int = 0
    frame_number: int | None = None
    next_fragment: int = 0
    complete_frames: int = 0
    idr_frames: int = 0
    bytes: int = 0

    def receive(self, frame_number: int, fragment: int, flags: int, payload_size: int) -> None:
        if flags & PACKET_AUDIO:
            raise TapError("audio flag on video socket")
        if flags & PACKET_FIRST:
            if fragment != 0:
                raise TapError("first video fragment is not zero")
            self.frame_number = frame_number
            self.next_fragment = 0
        if self.frame_number is None:
            return  # A dropped packet is recovered at the next access-unit boundary.
        if frame_number != self.frame_number or fragment != self.next_fragment:
            self.frame_number = None
            self.next_fragment = 0
            return
        self.next_fragment += 1
        self.bytes += payload_size
        if flags & PACKET_END:
            self.complete_frames += 1
            if flags & PACKET_IDR:
                self.idr_frames += 1
            self.frame_number = None
            self.next_fragment = 0


@dataclass
class AudioState:
    connections: int = 0
    samples_per_frame: int | None = None
    packets: int = 0
    bytes: int = 0
    next_packet: int | None = None

    def receive(self, frame_number: int, fragment: int, flags: int, payload_size: int) -> None:
        expected = PACKET_FIRST | PACKET_END | PACKET_AUDIO
        if fragment != 0 or (flags & expected) != expected:
            # The one zero-byte configuration packet uses the fragment field
            # for the negotiated per-channel Opus sample count.
            if (flags & (expected | PACKET_CONFIG)) != (expected | PACKET_CONFIG) or \
                    frame_number != 0 or payload_size != 0 or not 120 <= fragment <= 2880:
                raise TapError("invalid Opus packet flags")
            self.samples_per_frame = fragment
            return
        if self.next_packet is not None and frame_number < self.next_packet:
            raise TapError("audio packet number moved backwards")
        self.next_packet = frame_number + 1
        self.packets += 1
        self.bytes += payload_size


def serve(path: Path, state: VideoState | AudioState, stop: threading.Event, errors: list[Exception]) -> None:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        listener.bind(os.fspath(path))
        os.chmod(path, 0o600)
        listener.listen(1)
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            state.connections += 1
            try:
                connection.settimeout(0.5)
                while not stop.is_set():
                    try:
                        packet = connection.recv(PACKET_HEADER.size + MAX_FRAGMENT_BYTES + 1)
                    except TimeoutError:
                        continue
                    if not packet:
                        break
                    if len(packet) < PACKET_HEADER.size:
                        raise TapError("short local bridge packet")
                    magic, frame_number, fragment, flags, payload_size = PACKET_HEADER.unpack_from(packet)
                    if magic != PACKET_MAGIC:
                        raise TapError("invalid local bridge packet magic")
                    if payload_size > MAX_FRAGMENT_BYTES or len(packet) != PACKET_HEADER.size + payload_size:
                        raise TapError("invalid local bridge packet length")
                    state.receive(frame_number, fragment, flags, payload_size)
            finally:
                connection.close()
    except Exception as error:  # preserve the worker error for the main process
        errors.append(error)
    finally:
        listener.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runner",
        type=Path,
        default=Path(__file__).with_name("run-descriptor-qt-virgl-qsf-e2e.sh"),
        help="the reviewed native acceptance runner",
    )
    parser.add_argument("runner_args", nargs=argparse.REMAINDER, help="arguments passed to the runner after --")
    args = parser.parse_args()
    runner = args.runner.resolve(strict=True)
    if not os.access(runner, os.X_OK):
        raise SystemExit(f"browser encoded tap: runner is not executable: {runner}")

    # /tmp keeps the pathname below sockaddr_un.sun_path's portable 108-byte
    # limit.  The directory is mode 0700 and socket endpoints are mode 0600.
    with tempfile.TemporaryDirectory(prefix="qsm-tap-", dir="/tmp") as runtime:
        runtime_path = Path(runtime)
        video_path = runtime_path / "video.sock"
        audio_path = runtime_path / "audio.sock"
        video = VideoState()
        audio = AudioState()
        errors: list[Exception] = []
        stop = threading.Event()
        workers = [
            threading.Thread(target=serve, args=(video_path, video, stop, errors), daemon=True),
            threading.Thread(target=serve, args=(audio_path, audio, stop, errors), daemon=True),
        ]
        for worker in workers:
            worker.start()

        environment = os.environ.copy()
        environment["QSM_BROWSER_VIDEO_RECORD_PATH"] = f"unix:{video_path}"
        environment["QSM_BROWSER_AUDIO_RECORD_PATH"] = f"unix:{audio_path}"
        command = [os.fspath(runner), *args.runner_args]
        try:
            completed = subprocess.run(command, env=environment, check=False)
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=2)

    if completed.returncode:
        return completed.returncode
    if errors:
        raise TapError(str(errors[0]))
    if video.complete_frames < 10 or video.idr_frames < 1 or video.bytes == 0:
        raise TapError("did not receive complete H.264 access units including an IDR")
    if audio.connections < 1 or audio.samples_per_frame is None:
        raise TapError("Moonlight did not attach and configure the local Opus sink")
    # VM-100 is a video/clipboard fixture and currently produces no guest
    # sound.  It nevertheless proves that Moonlight attached the Opus tap.
    # A non-silent QEMU audio fixture is qualified independently; do not make
    # absence of guest-generated audio look like a browser transport failure.
    print(
        "QSM_BROWSER_ENCODED_TAP_E2E_OK "
        f"h264_access_units={video.complete_frames} h264_idr={video.idr_frames} "
        f"h264_bytes={video.bytes} opus_connections={audio.connections} "
        f"opus_samples_per_frame={audio.samples_per_frame} "
        f"opus_packets={audio.packets} opus_bytes={audio.bytes}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TapError as error:
        print(f"browser encoded tap: {error}", file=sys.stderr)
        raise SystemExit(1)
