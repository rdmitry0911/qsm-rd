#!/usr/bin/env python3
"""Media/input E2E gate for the standalone QSM Display1 worker.

This deliberately uses the production worker, FFmpeg's H.264 encoder, and a
private D-Bus QEMU Display1 peer.  Unix packet taps replace only the browser's
WebRTC decoder, which makes this gate usable in minimal LXC laboratories where
Chrome's own network sandbox cannot open ICE UDP sockets.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import select
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path


PACKET_MAGIC = 0x51534D50
PACKET_HEADER = struct.Struct("!IIIII")
PACKET_FIRST = 0x01
PACKET_END = 0x02
PACKET_IDR = 0x04
PACKET_AUDIO = 0x08
PACKET_CONFIG = 0x10
PACKET_CURSOR = 0x20
CURSOR_HEADER = struct.Struct("!BBQQiiHHHH")
CURSOR_VISIBLE = 0x01
CURSOR_HAS_SHAPE = 0x02
INPUT_MAGIC = 0x51534D49
INPUT_HEADER = struct.Struct("!IBBH")
INPUT_RESIZE = 5
INPUT_MOUSE_POSITION = 1
INPUT_MOUSE_BUTTON = 2
INPUT_KEYBOARD = 3


class QualificationError(RuntimeError):
    """The independently running direct-media path did not complete."""


def terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def wait_socket(path: Path, process: subprocess.Popen[bytes], label: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise QualificationError(f"{label} exited before its socket appeared")
        if path.exists():
            return
        time.sleep(0.02)
    raise QualificationError(f"{label} did not create its socket")


def listener(path: Path) -> socket.socket:
    result = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    result.bind(os.fspath(path))
    result.listen(1)
    result.settimeout(5)
    return result


def accept(listener_socket: socket.socket, label: str) -> socket.socket:
    try:
        connection, _address = listener_socket.accept()
    except TimeoutError as error:
        raise QualificationError(f"worker did not connect the {label} socket") from error
    return connection


def packet(opcode: int, payload: bytes) -> bytes:
    return INPUT_HEADER.pack(INPUT_MAGIC, 1, opcode, len(payload)) + payload


def read_record(connection: socket.socket) -> tuple[int, int, int, bytes]:
    data = connection.recv(256 * 1024 + PACKET_HEADER.size)
    if len(data) < PACKET_HEADER.size:
        raise QualificationError("worker sent a truncated media record")
    magic, frame, fragment, flags, length = PACKET_HEADER.unpack_from(data)
    body = data[PACKET_HEADER.size:]
    if magic != PACKET_MAGIC or length != len(body):
        raise QualificationError("worker sent an invalid media record")
    return frame, fragment, flags, body


def qualify(worker_binary: Path, fake_qemu_binary: Path, *, encoder: str,
            width: int, height: int, frames: int, fps: int, source_fps: int) -> str:
    if not worker_binary.is_file() or not os.access(worker_binary, os.X_OK):
        raise QualificationError("direct worker binary is unavailable")
    if not fake_qemu_binary.is_file() or not os.access(fake_qemu_binary, os.X_OK):
        raise QualificationError("fake QEMU binary is unavailable")
    with tempfile.TemporaryDirectory(prefix="qsm-direct-media-e2e.") as temporary:
        root = Path(temporary)
        bus = root / "display.bus"
        address = f"unix:path={bus}"
        daemon = subprocess.Popen(
            ["/usr/bin/dbus-daemon", "--session", "--nofork", "--nopidfile", f"--address={address}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        fake: subprocess.Popen[bytes] | None = None
        worker: subprocess.Popen[bytes] | None = None
        listeners: list[socket.socket] = []
        connections: list[socket.socket] = []
        try:
            wait_socket(bus, daemon, "private D-Bus daemon")
            video_listener = listener(root / "video.sock")
            audio_listener = listener(root / "audio.sock")
            input_listener = listener(root / "input.sock")
            listeners = [video_listener, audio_listener, input_listener]
            fake = subprocess.Popen(
                [os.fspath(fake_qemu_binary), "--bus-address", address, "--width", str(width), "--height", str(height),
                 "--frames", str(frames), "--fps", str(source_fps), "--inline"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(0.15)
            if fake.poll() is not None:
                raise QualificationError("fake QEMU did not start")
            worker = subprocess.Popen(
                [os.fspath(worker_binary), "--dbus-address", address,
                 "--video-socket", f"unix:{root / 'video.sock'}",
                 "--audio-socket", f"unix:{root / 'audio.sock'}",
                 "--input-socket", f"unix:{root / 'input.sock'}", "--encoder", encoder,
                 "--fps", str(fps), "--initial-size", f"{width}x{height}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(0.1)
            if worker.poll() is not None:
                _stdout, stderr = worker.communicate()
                raise QualificationError(
                    f"direct worker exited during startup: {stderr.decode('utf-8', 'replace').strip()}")
            video = accept(video_listener, "video")
            audio = accept(audio_listener, "audio")
            input_connection = accept(input_listener, "input")
            connections = [video, audio, input_connection]
            if worker.poll() is not None:
                raise QualificationError("direct worker exited during startup")

            input_connection.sendall(packet(INPUT_RESIZE, struct.pack("!IIH", width, height, fps)))
            # The product pointer channel is unordered/non-retransmitted.
            # Feed a deliberate stale sample after a newer coordinate and a
            # serial wrap: Display1 must retain the newest position rather
            # than visibly snapping a held guest window backwards.
            input_connection.sendall(packet(INPUT_MOUSE_POSITION,
                                            struct.pack("!hhhhI", 20, 10, width, height, 0xFFFF_FFFE)))
            input_connection.sendall(packet(INPUT_MOUSE_POSITION,
                                            struct.pack("!hhhhI", 25, 14, width, height, 0xFFFF_FFFF)))
            input_connection.sendall(packet(INPUT_MOUSE_POSITION,
                                            struct.pack("!hhhhI", 1, 1, width, height, 0xFFFF_FFFE)))
            input_connection.sendall(packet(INPUT_MOUSE_POSITION,
                                            struct.pack("!hhhhI", 30, 16, width, height, 0)))
            input_connection.sendall(packet(INPUT_MOUSE_BUTTON, b"\x01\x01"))
            input_connection.sendall(packet(INPUT_MOUSE_BUTTON, b"\x01\x00"))
            input_connection.sendall(packet(INPUT_KEYBOARD, b"\x00\x1e\x01\x00"))
            input_connection.sendall(packet(INPUT_KEYBOARD, b"\x00\x1e\x00\x00"))
            # Browser full-screen/close can interrupt DOM before mouseup or
            # keyup.  Close the real worker input peer with both states held;
            # its QEMU Display1 receiver must synthesize one matching release
            # for each, otherwise every later Console starts with a stuck
            # drag/click or modifier.
            input_connection.sendall(packet(INPUT_MOUSE_BUTTON, b"\x01\x01"))
            input_connection.sendall(packet(INPUT_KEYBOARD, b"\x00\x1e\x01\x00"))
            input_connection.close()
            connections.remove(input_connection)

            video.setblocking(False)
            audio.setblocking(False)
            video_frames: dict[int, tuple[bool, bool]] = {}
            video_records = 0
            idr_frames: set[int] = set()
            audio_configuration = False
            cursor_shape_records = 0
            cursor_shape_ids: set[int] = set()
            cursor_movement = False
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if fake.poll() is not None:
                    break
                ready, _unused, _exceptional = select.select([video, audio], [], [], 0.1)
                for connection in ready:
                    frame, _fragment, flags, body = read_record(connection)
                    if connection is audio:
                        if flags == (PACKET_FIRST | PACKET_END | PACKET_AUDIO | PACKET_CONFIG) and not body:
                            audio_configuration = True
                        continue
                    if flags & PACKET_AUDIO:
                        raise QualificationError("video tap received an audio record")
                    if flags & PACKET_CURSOR:
                        if flags != (PACKET_FIRST | PACKET_END | PACKET_CURSOR) or \
                                len(body) < CURSOR_HEADER.size:
                            raise QualificationError("worker sent a malformed guest cursor record")
                        version, cursor_flags, sequence, shape_id, x, y, width_cursor, height_cursor, hot_x, hot_y = \
                            CURSOR_HEADER.unpack_from(body)
                        if version != 1 or shape_id == 0 or \
                                not 1 <= width_cursor <= 64 or not 1 <= height_cursor <= 64 or \
                                hot_x >= width_cursor or hot_y >= height_cursor:
                            raise QualificationError(
                                "worker changed the Display1 guest cursor: "
                                f"v={version} flags={cursor_flags} shape={shape_id} "
                                f"size={width_cursor}x{height_cursor} hotspot={hot_x}x{hot_y}")
                        if cursor_flags & CURSOR_HAS_SHAPE:
                            if len(body) != CURSOR_HEADER.size + width_cursor * height_cursor * 4:
                                raise QualificationError("worker sent an incomplete guest cursor shape")
                            cursor_shape_records += 1
                            cursor_shape_ids.add(shape_id)
                        elif len(body) != CURSOR_HEADER.size:
                            raise QualificationError("worker sent unexpected guest cursor bytes")
                        cursor_movement = cursor_movement or (sequence >= 2 and
                                                               bool(cursor_flags & CURSOR_VISIBLE) and x > 0 and y > 0)
                        continue
                    first, last = video_frames.get(frame, (False, False))
                    video_frames[frame] = (first or bool(flags & PACKET_FIRST), last or bool(flags & PACKET_END))
                    if flags & PACKET_IDR:
                        idr_frames.add(frame)
                    video_records += 1
            try:
                fake_stdout, fake_stderr = fake.communicate(timeout=3)
            except subprocess.TimeoutExpired as error:
                raise QualificationError("fake QEMU did not finish its Display1 stream") from error
            if fake.returncode != 0:
                raise QualificationError(f"fake QEMU failed: {fake_stderr.decode('utf-8', 'replace')}")
            complete_frames = sum(first and last for first, last in video_frames.values())
            if complete_frames < 10 or not idr_frames or video_records < complete_frames:
                raise QualificationError("FFmpeg did not produce complete H.264 access units")
            if source_fps > fps:
                # Display1 damage may arrive much faster than the negotiated
                # media rate. The worker must coalesce it before H.264/RTP;
                # otherwise each extra AU receives a nominal 1/fps timestamp
                # and Chrome eventually jitters between old and new desktop
                # pictures. Permit a small start/stop scheduling margin only.
                maximum_frames = math.ceil(frames * fps / source_fps) + 6
                if complete_frames > maximum_frames:
                    raise QualificationError(
                        "worker emitted Display1 damage faster than its negotiated media clock: "
                        f"complete={complete_frames} maximum={maximum_frames} "
                        f"source_fps={source_fps} fps={fps}")
            if not audio_configuration:
                raise QualificationError("worker did not publish its Opus configuration")
            if cursor_shape_records != 1 or cursor_shape_ids != {1} or not cursor_movement:
                raise QualificationError("worker did not publish an out-of-band Display1 guest cursor")
            trace = fake_stdout.decode("utf-8", "replace").strip()
            if not all(marker in trace for marker in (
                    "FAKE_QEMU_RESULT", f"frames={frames}", f"requested={width}x{height}",
                    "keyboard=4", "keyboard_press=2", "keyboard_release=2",
                    "mouse=6", "button_press=2", "button_release=2",
                    # The mock deliberately exposes relative mode. The
                    # retained +5,+2 delta proves the stale FFFE packet was
                    # discarded and that the FFFF -> 0 serial wrap advanced.
                    "relative=1:5x2",
                    "listener=1", "peer_completed=1")):
                raise QualificationError(f"direct input/resize did not reach Display1: {trace}")
            return trace
        finally:
            for connection in connections:
                connection.close()
            for item in listeners:
                item.close()
            terminate(worker)
            terminate(fake)
            terminate(daemon)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--fake-qemu", type=Path, required=True)
    parser.add_argument("--encoder", default="libx264")
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--source-fps", type=int,
                        help="optional fake Display1 damage rate; defaults to --fps")
    arguments = parser.parse_args()
    try:
        source_fps = arguments.source_fps if arguments.source_fps is not None else arguments.fps
        if (arguments.width < 64 or arguments.height < 64 or arguments.frames < 10 or
                not 10 <= arguments.fps <= 240 or not 10 <= source_fps <= 240 or
                arguments.width % 2 or arguments.height % 2):
            raise QualificationError("invalid media qualification dimensions")
        trace = qualify(arguments.worker, arguments.fake_qemu, encoder=arguments.encoder,
                        width=arguments.width, height=arguments.height,
                        frames=arguments.frames, fps=arguments.fps, source_fps=source_fps)
    except (OSError, QualificationError, subprocess.SubprocessError) as error:
        print(f"QSM_DIRECT_WORKER_MEDIA_E2E_FAILED: {error}")
        return 1
    print(f"QSM_DIRECT_WORKER_MEDIA_E2E_OK {trace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
