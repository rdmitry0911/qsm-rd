#!/usr/bin/env python3
"""Local direct-media-to-WebRTC bridge for the PVE browser console.

The local producer is the per-VM QEMU Display1 media worker. It sends encoded
H.264 and Opus over two private Unix ``SOCK_SEQPACKET`` sockets; this module
packetizes those elementary streams for WebRTC without decoding or re-encoding
them. The PVE API / terminal-service adapter owns authorization and passes an
already-authorized SDP offer to :meth:`BrowserWebRtcBridge.answer_offer`.
No PVE cookie, password, CSRF value, GameStream ticket, QSF token, or terminal
signing key appears in this module's wire protocol or browser response.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import stat
import struct
import sys
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import av
from aiortc import (MediaStreamTrack, RTCPeerConnection, RTCSessionDescription,
                    RTCRtpSender)
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import SessionDescription


PACKET_MAGIC = 0x51534D50  # "QSMP", encoded as a network-order u32.
PACKET_HEADER = struct.Struct("!IIIII")
PACKET_FIRST = 0x00000001
PACKET_END = 0x00000002
PACKET_IDR = 0x00000004
PACKET_AUDIO = 0x00000008
PACKET_CONFIG = 0x00000010
INPUT_MAGIC = 0x51534D49  # "QSMI", encoded as a network-order u32.
INPUT_VERSION = 1
INPUT_HEADER = struct.Struct("!IBBH")
INPUT_MOUSE_POSITION = 1
INPUT_MOUSE_BUTTON = 2
INPUT_KEYBOARD = 3
INPUT_SCROLL = 4
INPUT_RESIZE = 5
MAX_FRAGMENT_BYTES = 256 * 1024
MAX_ACCESS_UNIT_BYTES = 4 * 1024 * 1024
MAX_SDP_BYTES = 128 * 1024
MAX_CONTROL_MESSAGE_BYTES = 1024
MAX_GUEST_CONTROL_MESSAGE_BYTES = 3 * 1024 * 1024
VIDEO_TIME_BASE = Fraction(1, 90_000)
AUDIO_TIME_BASE = Fraction(1, 48_000)
# This route is an interactive console, not a recorder.  A browser which is
# temporarily behind must receive the current encoded picture rather than a
# small backlog of already obsolete mouse/desktop updates.  Audio can retain a
# few 20 ms Opus packets for normal WebRTC jitter handling without becoming a
# visible lip-sync delay.
VIDEO_QUEUE_DEPTH = 1
AUDIO_QUEUE_DEPTH = 4


class BridgeError(RuntimeError):
    """A local bridge configuration or packet was invalid."""


@dataclass(frozen=True)
class EncodedUnit:
    """One fully reassembled encoded access unit."""

    data: bytes
    keyframe: bool
    duration: int


class _VideoAssembler:
    """Recover only complete ordered access units after local packet loss."""

    def __init__(self, fps: int) -> None:
        if not 10 <= fps <= 240:
            raise BridgeError("invalid video frame rate")
        self._nominal_duration = 90_000 // fps
        # Do not put the producer's update cadence on the RTP clock.  A D-Bus
        # Display1 surface is damage-driven: after an input event its
        # next frame can arrive after an arbitrary quiet period.  Giving that
        # gap to the receiver as an RTP timestamp makes Chromium schedule the
        # new frame behind the preceding picture.  The encoded stream is
        # configured for ``fps``, so each access unit belongs to that steady
        # media clock even when the desktop is idle between updates.
        self._frame: int | None = None
        self._fragment = 0
        self._keyframe = False
        self._parts: list[bytes] = []
        self._size = 0

    def _reset(self) -> None:
        self._frame = None
        self._fragment = 0
        self._keyframe = False
        self._parts = []
        self._size = 0

    def add(self, frame: int, fragment: int, flags: int, data: bytes) -> EncodedUnit | None:
        if flags & (PACKET_AUDIO | PACKET_CONFIG):
            raise BridgeError("video socket received a non-video packet")
        if flags & PACKET_FIRST:
            if fragment != 0:
                raise BridgeError("video access unit starts at a nonzero fragment")
            self._reset()
            self._frame = frame
        if self._frame is None:
            return None
        if frame != self._frame or fragment != self._fragment:
            # The Unix sender intentionally drops rather than blocks a
            # local encoder. Do not emit a corrupt partial frame; wait
            # for the next FIRST boundary instead.
            self._reset()
            return None
        self._size += len(data)
        if self._size > MAX_ACCESS_UNIT_BYTES:
            self._reset()
            raise BridgeError("encoded video access unit is too large")
        self._parts.append(data)
        self._fragment += 1
        self._keyframe = self._keyframe or bool(flags & PACKET_IDR)
        if not flags & PACKET_END:
            return None
        result = EncodedUnit(data=b"".join(self._parts), keyframe=self._keyframe,
                             duration=self._nominal_duration)
        self._reset()
        return result


class _AudioAssembler:
    """Validate Opus packet timing metadata and produce raw Opus packets."""

    def __init__(self) -> None:
        self._samples_per_frame: int | None = None
        self._last_packet: int | None = None

    @property
    def samples_per_frame(self) -> int | None:
        return self._samples_per_frame

    def add(self, frame: int, fragment: int, flags: int, data: bytes) -> EncodedUnit | None:
        expected = PACKET_FIRST | PACKET_END | PACKET_AUDIO
        if (flags & expected) != expected:
            raise BridgeError("malformed Opus tap packet")
        if flags & PACKET_CONFIG:
            if frame != 0 or data or not 120 <= fragment <= 2880:
                raise BridgeError("malformed Opus tap configuration")
            self._samples_per_frame = fragment
            return None
        if fragment != 0 or not data:
            raise BridgeError("malformed Opus media packet")
        if self._samples_per_frame is None:
            raise BridgeError("Opus packet arrived before negotiated duration")
        if self._last_packet is not None and frame < self._last_packet:
            raise BridgeError("Opus packet sequence moved backwards")
        self._last_packet = frame
        return EncodedUnit(data=data, keyframe=False, duration=self._samples_per_frame)


class _PacketTrack(MediaStreamTrack):
    """A bounded async source that keeps encoded packets encoded."""

    def __init__(self, kind: str, *, maximum_queue: int) -> None:
        super().__init__()
        self.kind = kind
        self._queue: asyncio.Queue[EncodedUnit | None] = asyncio.Queue(maxsize=maximum_queue)
        self._pts = 0
        self._closed = False

    def put_nowait(self, unit: EncodedUnit) -> None:
        if self._closed:
            return
        # Prefer current video/audio over latency growth.  The next IDR lets
        # the browser recover after a pressure drop; no local reader is ever
        # allowed to pause a capture/encoder thread.
        while self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(unit)

    def end_nowait(self) -> None:
        if self._closed:
            return
        self._closed = True
        while self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(None)

    async def recv(self) -> av.Packet:
        unit = await self._queue.get()
        if unit is None:
            raise MediaStreamError
        packet = av.Packet(unit.data)
        packet.pts = self._pts
        packet.dts = self._pts
        packet.time_base = VIDEO_TIME_BASE if self.kind == "video" else AUDIO_TIME_BASE
        packet.is_keyframe = unit.keyframe
        self._pts += unit.duration
        return packet


class _TrackFanout:
    """Publish one encoded elementary stream to each browser peer.

    A QEMU Display1 capture must have one producer, but PVE Console is a
    multi-viewer facility.  The ingress thread schedules this object on its
    owning asyncio loop, therefore subscription changes and packet fan-out
    cannot race an individual peer's queue teardown.
    """

    def __init__(self) -> None:
        self._tracks: set[_PacketTrack] = set()
        # H.264 parameter sets are emitted with each IDR by the worker. Keep
        # the current complete IDR so a browser which joins an already-live
        # VM stream never starts by decoding arbitrary P-frames without PPS.
        self._bootstrap: EncodedUnit | None = None
        self._closed = False

    def subscribe(self, *, kind: str, maximum_queue: int) -> _PacketTrack:
        if self._closed:
            raise BridgeError("shared browser media source is closed")
        track = _PacketTrack(kind, maximum_queue=maximum_queue)
        self._tracks.add(track)
        if self._bootstrap is not None:
            track.put_nowait(self._bootstrap)
        return track

    def unsubscribe(self, track: _PacketTrack) -> None:
        if track in self._tracks:
            self._tracks.remove(track)
            track.end_nowait()

    def put_nowait(self, unit: EncodedUnit) -> None:
        if not self._closed:
            if unit.keyframe:
                self._bootstrap = unit
            for track in tuple(self._tracks):
                track.put_nowait(unit)

    def end_nowait(self) -> None:
        if self._closed:
            return
        self._closed = True
        for track in tuple(self._tracks):
            track.end_nowait()
        self._tracks.clear()
        self._bootstrap = None


class UnixTapIngress:
    """Own two private Unix sequenced-packet taps and feed encoded tracks.

    The single allowed producer is the local direct media worker started by
    the terminal service. A new producer connection replaces neither media
    track nor its async queue, so a controlled capture reconnect has one
    stable WebRTC endpoint. Connections from another local UID are rejected
    before any packet bytes are read.
    """

    def __init__(self, runtime_directory: Path, loop: asyncio.AbstractEventLoop,
                 video_track: _PacketTrack, audio_track: _PacketTrack, *, fps: int,
                 expected_uid: int | None = None) -> None:
        self._runtime_directory = runtime_directory
        self._loop = loop
        self._video_track = video_track
        self._audio_track = audio_track
        self._video_assembler = _VideoAssembler(fps)
        self._audio_assembler = _AudioAssembler()
        self._expected_uid = os.geteuid() if expected_uid is None else expected_uid
        if not isinstance(self._expected_uid, int) or self._expected_uid < 0:
            raise BridgeError("invalid local producer identity")
        self.video_path = runtime_directory / "browser-video.sock"
        self.audio_path = runtime_directory / "browser-audio.sock"
        self._listeners: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._error_lock = threading.Lock()
        self._error: BridgeError | None = None

    @property
    def audio_samples_per_frame(self) -> int | None:
        return self._audio_assembler.samples_per_frame

    def _record_error(self, error: BridgeError) -> None:
        with self._error_lock:
            if self._error is None:
                self._error = error

    def raise_if_failed(self) -> None:
        with self._error_lock:
            if self._error is not None:
                raise self._error

    @staticmethod
    def _validate_runtime_directory(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise BridgeError("browser bridge runtime directory is unavailable") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or \
                metadata.st_mode & 0o077:
            raise BridgeError("browser bridge runtime directory is unsafe")

    @staticmethod
    def _validate_socket_path(path: Path) -> None:
        # Keep away from Linux sockaddr_un's pathname ceiling and never remove
        # an arbitrary pre-existing file as part of a bridge startup.
        if path.parent == path or ".." in path.parts or len(os.fsencode(path)) >= 104:
            raise BridgeError("browser bridge Unix socket path is invalid")
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise BridgeError("browser bridge Unix socket cannot be inspected") from error
        raise BridgeError("browser bridge Unix socket already exists")

    def start(self) -> None:
        self._validate_runtime_directory(self._runtime_directory)
        for path in (self.video_path, self.audio_path):
            self._validate_socket_path(path)
        try:
            for path in (self.video_path, self.audio_path):
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                listener.bind(os.fspath(path))
                os.chmod(path, 0o600)
                listener.listen(1)
                listener.settimeout(0.25)
                self._listeners.append(listener)
            self._threads = [
                threading.Thread(target=self._serve, args=(self._listeners[0], False),
                                 name="qsm-browser-video-tap", daemon=True),
                threading.Thread(target=self._serve, args=(self._listeners[1], True),
                                 name="qsm-browser-audio-tap", daemon=True),
            ]
            for thread in self._threads:
                thread.start()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _peer_uid(connection: socket.socket) -> int:
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
        except (AttributeError, OSError, struct.error) as error:
            raise BridgeError("cannot verify local media producer") from error
        return uid

    def _publish(self, track: _PacketTrack, unit: EncodedUnit) -> None:
        self._loop.call_soon_threadsafe(track.put_nowait, unit)

    def _serve(self, listener: socket.socket, is_audio: bool) -> None:
        assembler = self._audio_assembler if is_audio else self._video_assembler
        track = self._audio_track if is_audio else self._video_track
        while not self._stopping.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                if self._peer_uid(connection) != self._expected_uid:
                    raise BridgeError("unexpected local media producer identity")
                connection.settimeout(0.25)
                while not self._stopping.is_set():
                    try:
                        packet, _ancillary, flags, _address = connection.recvmsg(
                            PACKET_HEADER.size + MAX_FRAGMENT_BYTES, 0)
                    except TimeoutError:
                        continue
                    if not packet:
                        break
                    if flags & socket.MSG_TRUNC or len(packet) < PACKET_HEADER.size:
                        raise BridgeError("truncated local media packet")
                    magic, frame, fragment, packet_flags, length = PACKET_HEADER.unpack_from(packet)
                    if magic != PACKET_MAGIC or length > MAX_FRAGMENT_BYTES or \
                            len(packet) != PACKET_HEADER.size + length:
                        raise BridgeError("invalid local media packet")
                    if bool(packet_flags & PACKET_AUDIO) != is_audio:
                        raise BridgeError("local media packet arrived on the wrong socket")
                    unit = assembler.add(frame, fragment, packet_flags, packet[PACKET_HEADER.size:])
                    if unit is not None:
                        self._publish(track, unit)
            except BridgeError as error:
                self._record_error(error)
            finally:
                connection.close()

    def close(self) -> None:
        self._stopping.set()
        for listener in self._listeners:
            try:
                listener.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=1)
        self._loop.call_soon_threadsafe(self._video_track.end_nowait)
        self._loop.call_soon_threadsafe(self._audio_track.end_nowait)
        for path in (self.video_path, self.audio_path):
            try:
                metadata = path.lstat()
                if stat.S_ISSOCK(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


class UnixInputEgress:
    """Forward a strictly bounded browser control stream to QEMU input.

    The browser never obtains this local socket path.  The terminal launches
    the one authenticated receiver with it in its environment; the receiver
    must connect from the same service UID.  Browser data-channel JSON is
    parsed here and rewritten into a small fixed binary protocol, so neither
    a browser string nor an SDP field can become a QEMU D-Bus invocation.
    """

    def __init__(self, runtime_directory: Path, *, expected_uid: int | None = None) -> None:
        self._runtime_directory = runtime_directory
        self._expected_uid = os.geteuid() if expected_uid is None else expected_uid
        if not isinstance(self._expected_uid, int) or self._expected_uid < 0:
            raise BridgeError("invalid local receiver identity")
        self.path = runtime_directory / "browser-input.sock"
        self._listener: socket.socket | None = None
        self._connection: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._error: BridgeError | None = None

    def start(self) -> None:
        UnixTapIngress._validate_runtime_directory(self._runtime_directory)
        UnixTapIngress._validate_socket_path(self.path)
        listener: socket.socket | None = None
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            listener.bind(os.fspath(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(1)
            listener.settimeout(0.25)
            self._listener = listener
            self._thread = threading.Thread(target=self._accept,
                                            name="qsm-browser-input-sink", daemon=True)
            self._thread.start()
        except BaseException:
            if listener is not None:
                listener.close()
            self.close()
            raise

    def _record_error(self, error: BridgeError) -> None:
        with self._lock:
            if self._error is None:
                self._error = error

    def raise_if_failed(self) -> None:
        with self._lock:
            if self._error is not None:
                raise self._error

    def _accept(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                if UnixTapIngress._peer_uid(connection) != self._expected_uid:
                    raise BridgeError("unexpected local input receiver identity")
                with self._lock:
                    # A second connection is never a controlled reconnect.
                    # It would let a same-UID, unrelated process replace the
                    # QEMU input destination mid-session.
                    if self._connection is not None:
                        raise BridgeError("duplicate local input receiver")
                    self._connection = connection
                return
            except BridgeError as error:
                self._record_error(error)
                connection.close()
                return

    @staticmethod
    def _integer(value: object, minimum: int, maximum: int) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise BridgeError("invalid browser control message")
        return value

    @classmethod
    def encode_browser_message(cls, raw: object) -> bytes:
        """Validate one WebRTC JSON control message and create one packet."""
        if not isinstance(raw, str) or not raw.isascii() or not 1 <= len(raw) <= MAX_CONTROL_MESSAGE_BYTES:
            raise BridgeError("invalid browser control message")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise BridgeError("invalid browser control message") from error
        if not isinstance(value, dict) or set(value) - {"op", "x", "y", "width", "height", "button", "down", "key", "modifiers", "vertical", "horizontal", "fps"}:
            raise BridgeError("invalid browser control message")
        op = value.get("op")
        payload: bytes
        opcode: int
        if op == "mouse_position" and set(value) == {"op", "x", "y", "width", "height"}:
            x = cls._integer(value["x"], 0, 32767)
            y = cls._integer(value["y"], 0, 32767)
            width = cls._integer(value["width"], 1, 32767)
            height = cls._integer(value["height"], 1, 32767)
            if x >= width or y >= height:
                raise BridgeError("invalid browser control message")
            opcode = INPUT_MOUSE_POSITION
            payload = struct.pack("!hhhh", x, y, width, height)
        elif op == "mouse_button" and set(value) == {"op", "button", "down"}:
            button = cls._integer(value["button"], 1, 5)
            if type(value["down"]) is not bool:
                raise BridgeError("invalid browser control message")
            opcode = INPUT_MOUSE_BUTTON
            payload = struct.pack("!BB", button, int(value["down"]))
        elif op == "keyboard" and set(value) == {"op", "key", "down", "modifiers"}:
            key = cls._integer(value["key"], 0, 65535)
            modifiers = cls._integer(value["modifiers"], 0, 255)
            if type(value["down"]) is not bool:
                raise BridgeError("invalid browser control message")
            opcode = INPUT_KEYBOARD
            payload = struct.pack("!HBB", key, int(value["down"]), modifiers)
        elif op == "scroll" and set(value) == {"op", "vertical", "horizontal"}:
            vertical = cls._integer(value["vertical"], -32768, 32767)
            horizontal = cls._integer(value["horizontal"], -32768, 32767)
            opcode = INPUT_SCROLL
            payload = struct.pack("!hh", vertical, horizontal)
        elif op == "resize" and set(value) == {"op", "width", "height", "fps"}:
            width = cls._integer(value["width"], 64, 16384)
            height = cls._integer(value["height"], 64, 16384)
            fps = cls._integer(value["fps"], 10, 240)
            # The direct worker emits H.264 4:2:0, which requires even luma
            # dimensions. Reject here rather than silently changing the
            # visible browser viewport behind the user's back.
            if width % 2 or height % 2:
                raise BridgeError("invalid browser control message")
            opcode = INPUT_RESIZE
            payload = struct.pack("!IIH", width, height, fps)
        else:
            raise BridgeError("invalid browser control message")
        return INPUT_HEADER.pack(INPUT_MAGIC, INPUT_VERSION, opcode, len(payload)) + payload

    def send_browser_message(self, raw: object) -> None:
        packet = self.encode_browser_message(raw)
        self._send_packet(packet)

    def send_browser_pointer_message(self, raw: object) -> None:
        """Forward only a latest-state absolute pointer packet.

        Pointer samples travel on the browser's unordered, non-retransmitted
        channel.  Keeping them separate from keys, clicks and resize requests
        means a stale cursor coordinate can be discarded without delaying a
        subsequent click or keyboard shortcut.
        """
        packet = self.encode_browser_message(raw)
        if packet[5] != INPUT_MOUSE_POSITION:
            raise BridgeError("browser pointer channel received a non-pointer message")
        self._send_packet(packet)

    def _send_packet(self, packet: bytes) -> None:
        with self._lock:
            if self._error is not None:
                raise self._error
            connection = self._connection
            if connection is None:
                # Input emitted before the authenticated receiver is live is
                # not queued. Queuing a mouse/key state across that boundary
                # can create a stuck key or a click in a new session.
                return
            try:
                sent = connection.send(packet, socket.MSG_DONTWAIT | socket.MSG_NOSIGNAL)
            except (BlockingIOError, OSError) as error:
                self._connection = None
                connection.close()
                raise BridgeError("local input receiver is unavailable") from error
            if sent != len(packet):
                self._connection = None
                connection.close()
                raise BridgeError("local input receiver is unavailable")

    def close(self) -> None:
        self._stopping.set()
        with self._lock:
            connection = self._connection
            self._connection = None
            listener = self._listener
            self._listener = None
        for descriptor in (connection, listener):
            if descriptor is not None:
                try:
                    descriptor.close()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=1)
        try:
            metadata = self.path.lstat()
            if stat.S_ISSOCK(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class SharedMediaIngress:
    """One QEMU-worker media ingress shared by several WebRTC peers.

    Each subscriber gets fresh bounded tracks, so a stalled browser loses only
    its own old packets and cannot block capture, encoding, or another
    viewer.  A worker is still authenticated by :class:`UnixTapIngress` and
    connects to exactly one private video/audio socket pair.
    """

    def __init__(self, runtime_directory: Path, loop: asyncio.AbstractEventLoop,
                 *, fps: int, expected_producer_uid: int | None = None) -> None:
        self._video = _TrackFanout()
        self._audio = _TrackFanout()
        self._ingress = UnixTapIngress(
            runtime_directory, loop, self._video, self._audio, fps=fps,
            expected_uid=expected_producer_uid)
        self._started = False
        self._closed = False

    @property
    def video_path(self) -> Path:
        return self._ingress.video_path

    @property
    def audio_path(self) -> Path:
        return self._ingress.audio_path

    def start(self) -> None:
        if self._closed:
            raise BridgeError("shared browser media source is closed")
        if not self._started:
            self._ingress.start()
            self._started = True

    def raise_if_failed(self) -> None:
        if not self._started:
            raise BridgeError("shared browser media source is not started")
        self._ingress.raise_if_failed()

    def subscribe(self) -> tuple[_PacketTrack, _PacketTrack]:
        if not self._started:
            raise BridgeError("shared browser media source is not started")
        return (
            self._video.subscribe(kind="video", maximum_queue=VIDEO_QUEUE_DEPTH),
            self._audio.subscribe(kind="audio", maximum_queue=AUDIO_QUEUE_DEPTH),
        )

    def unsubscribe(self, video_track: _PacketTrack, audio_track: _PacketTrack) -> None:
        self._video.unsubscribe(video_track)
        self._audio.unsubscribe(audio_track)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ingress.close()


class BrowserWebRtcBridge:
    """One browser console's encrypted WebRTC media endpoint.

    Signalling is intentionally a one-off PVE API request.  ICE candidates are
    embedded in the returned SDP and the resulting DTLS/SRTP transport is
    direct between the browser and this node.  TURN configuration, when a
    deployment needs it, belongs to PVE node policy rather than an extension.
    """

    def __init__(self, runtime_directory: Path, *, fps: int = 60,
                 expected_producer_uid: int | None = None,
                 shared_media: SharedMediaIngress | None = None,
                 shared_input: UnixInputEgress | None = None,
                 guest_dispatch: Callable[[Any], dict[str, Any]] | None = None) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError as error:
            raise BridgeError("BrowserWebRtcBridge must be created in an event loop") from error
        self._pc = RTCPeerConnection()
        self._shared_media = shared_media
        self._owns_media = shared_media is None
        if shared_media is None:
            self.video_track = _PacketTrack("video", maximum_queue=VIDEO_QUEUE_DEPTH)
            self.audio_track = _PacketTrack("audio", maximum_queue=AUDIO_QUEUE_DEPTH)
            self.ingress = UnixTapIngress(runtime_directory, self._loop, self.video_track,
                                           self.audio_track, fps=fps,
                                           expected_uid=expected_producer_uid)
        else:
            self.video_track, self.audio_track = shared_media.subscribe()
            self.ingress = None
        self._owns_input = shared_input is None
        self.input = shared_input or UnixInputEgress(
            runtime_directory, expected_uid=expected_producer_uid)
        self._guest_dispatch = guest_dispatch
        self._closed = False
        # A PVE console session has exactly one SDP offer and one pair of
        # tracks.  In particular, do not let a caller append another sender
        # to an already-authorized peer connection by replaying signalling.
        self._taps_started = False
        self._offer_consumed = False
        self._control_channel_seen = False
        self._pointer_channel_seen = False
        self._control_channel: object | None = None
        self._pc.on("datachannel", self._on_datachannel)

        @self._pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            # This is deliberately diagnostic-only.  It contains neither the
            # SDP nor a peer address, but tells the PVE operator whether a
            # blank console is caused before or after the DTLS/SRTP browser
            # connection becomes usable.
            print(
                f"qsm-direct-terminal: WebRTC connection state={self._pc.connectionState}",
                file=sys.stderr,
                flush=True,
            )

    @property
    def input_context(self) -> str:
        return f"unix:{self.input.path}"

    def _send_control(self, payload: dict[str, Any]) -> None:
        """Send a bounded, server-originated guest-side-channel event."""
        channel = self._control_channel
        if self._closed or channel is None or getattr(channel, "readyState", None) != "open":
            return
        try:
            channel.send(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
        except Exception:
            # SCTP closure is ordinary browser lifecycle, not a media failure.
            pass

    def notify_guest_clipboard(self, text: str) -> None:
        """Publish a validated guest clipboard update to this browser only."""
        if not isinstance(text, str):
            return
        encoded = text.encode("utf-8")
        if len(encoded) > 1024 * 1024:
            return
        self._loop.call_soon_threadsafe(
            self._send_control,
            {"op": "qsm_guest_clipboard", "text_b64": base64.b64encode(encoded).decode("ascii")},
        )

    @staticmethod
    def _guest_request(message: object) -> tuple[str, dict[str, Any]] | None:
        """Recognise one guest command without widening the input protocol."""
        if isinstance(message, bytes):
            if len(message) > MAX_GUEST_CONTROL_MESSAGE_BYTES:
                raise BridgeError("browser guest message is too large")
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError as error:
                raise BridgeError("browser guest message is invalid") from error
        if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_GUEST_CONTROL_MESSAGE_BYTES:
            raise BridgeError("browser guest message is invalid")
        try:
            value = json.loads(message)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, dict) or not isinstance(value.get("op"), str) or \
                not value["op"].startswith("qsm_guest_"):
            return None
        request_id = value.pop("request_id", None)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64 or \
                not request_id.isascii() or not request_id.replace("-", "").isalnum():
            raise BridgeError("browser guest message is invalid")
        operations = {
            "qsm_guest_clipboard_set": "clipboard_set",
            "qsm_guest_clipboard_get": "clipboard_get",
            "qsm_guest_file_upload": "file_upload",
            "qsm_guest_file_download": "file_download",
            "qsm_guest_status": "status",
        }
        operation = operations.get(value.pop("op"))
        if operation is None:
            raise BridgeError("browser guest message is invalid")
        value["op"] = operation
        return request_id, value

    async def _dispatch_guest_request(self, channel: object, request_id: str,
                                      request: dict[str, Any]) -> None:
        try:
            if self._guest_dispatch is None:
                raise BridgeError("guest tools are unavailable")
            result = await asyncio.to_thread(self._guest_dispatch, request)
            response: dict[str, Any] = {
                "op": "qsm_guest_result", "request_id": request_id, "ok": True, "result": result,
            }
        except Exception:
            # The guest-side detail can include a transient agent state. Keep
            # the PVE browser response useful but deliberately non-sensitive.
            response = {"op": "qsm_guest_result", "request_id": request_id, "ok": False,
                        "error": "Guest clipboard or file operation failed."}
        if not self._closed and channel is self._control_channel:
            self._send_control(response)

    def _on_datachannel(self, channel: object) -> None:
        # There are exactly two browser-to-guest channels.  qsm-control is
        # reliable/ordered for stateful keyboard, button and resize messages;
        # qsm-pointer carries only replaceable cursor coordinates and must not
        # queue behind a lost packet.  Reject every other SCTP data channel.
        label = getattr(channel, "label", None)
        control = (label == "qsm-control" and not self._control_channel_seen and
                   getattr(channel, "ordered", None) is True and
                   getattr(channel, "maxRetransmits", None) is None and
                   getattr(channel, "maxPacketLifeTime", None) is None)
        pointer = (label == "qsm-pointer" and not self._pointer_channel_seen and
                   getattr(channel, "ordered", None) is False and
                   getattr(channel, "maxRetransmits", None) == 0 and
                   getattr(channel, "maxPacketLifeTime", None) is None)
        if not control and not pointer:
            close = getattr(channel, "close", None)
            if callable(close):
                close()
            return
        if control:
            self._control_channel_seen = True
            self._control_channel = channel
        else:
            self._pointer_channel_seen = True

        @channel.on("message")
        def on_message(message: object) -> None:
            try:
                if control:
                    guest = self._guest_request(message)
                    if guest is None:
                        self.input.send_browser_message(message)
                    else:
                        asyncio.create_task(self._dispatch_guest_request(channel, *guest))
                else:
                    self.input.send_browser_pointer_message(message)
            except BridgeError:
                # A malformed browser command must not tear down encrypted
                # video. Close this data channel; a fresh PVE Console launch
                # is required for another input authority.
                close = getattr(channel, "close", None)
                if callable(close):
                    close()

    def start_taps(self) -> tuple[str, str]:
        if self._closed:
            raise BridgeError("browser bridge is closed")
        if self._taps_started:
            raise BridgeError("browser bridge taps are already running")
        if self._owns_media:
            assert self.ingress is not None
            self.ingress.start()
        else:
            assert self._shared_media is not None
            self._shared_media.raise_if_failed()
        try:
            if self._owns_input:
                self.input.start()
            else:
                self.input.raise_if_failed()
        except BaseException:
            if self._owns_media:
                assert self.ingress is not None
                self.ingress.close()
            raise
        self._taps_started = True
        if self._owns_media:
            assert self.ingress is not None
            return (f"unix:{self.ingress.video_path}", f"unix:{self.ingress.audio_path}")
        return ("", "")

    @staticmethod
    def _h264_codecs() -> list[object]:
        return [
            codec for codec in RTCRtpSender.getCapabilities("video").codecs
            if codec.mimeType.lower() == "video/h264"
        ]

    @staticmethod
    def _validate_offer_codecs(sdp: str) -> None:
        """Reject an incompatible browser before mutating the peer connection.

        The direct route's broadly deployable browser codec is H.264. Some Linux
        Chromium distributions deliberately omit it, even though a normal
        Chrome/Edge/Safari build advertises it.  Failing at this boundary lets
        the PVE UI present a useful capability error and, critically, avoids
        adding senders to a session which can never be negotiated.
        """
        try:
            description = SessionDescription.parse(sdp)
        except (TypeError, ValueError) as error:
            raise BridgeError("invalid browser WebRTC offer") from error
        h264 = any(
            media.kind == "video" and any(
                codec.mimeType.lower() == "video/h264" for codec in media.rtp.codecs)
            for media in description.media
        )
        opus = any(
            media.kind == "audio" and any(
                codec.mimeType.lower() == "audio/opus" for codec in media.rtp.codecs)
            for media in description.media
        )
        if not h264:
            raise BridgeError("browser does not offer WebRTC H.264")
        if not opus:
            raise BridgeError("browser does not offer WebRTC Opus")

    async def answer_offer(self, sdp: str, sdp_type: str = "offer") -> dict[str, str]:
        if self._closed or not isinstance(sdp, str) or not isinstance(sdp_type, str) or \
                sdp_type != "offer" or not sdp.isascii() or "\x00" in sdp or \
                not 1 <= len(sdp.encode("ascii")) <= MAX_SDP_BYTES:
            raise BridgeError("invalid browser WebRTC offer")
        if not self._taps_started:
            raise BridgeError("browser bridge taps are not running")
        self._validate_offer_codecs(sdp)
        if self._offer_consumed:
            raise BridgeError("browser bridge already consumed its offer")
        # Set before the first await.  An offer is a single-use authorization
        # artifact even if DTLS/ICE negotiation subsequently fails; the PVE
        # adapter must create a fresh bridge/session for a retry.
        self._offer_consumed = True
        if self._owns_media:
            assert self.ingress is not None
            self.ingress.raise_if_failed()
        else:
            assert self._shared_media is not None
            self._shared_media.raise_if_failed()
        self.input.raise_if_failed()
        video_sender = self._pc.addTrack(self.video_track)
        audio_sender = self._pc.addTrack(self.audio_track)
        h264 = self._h264_codecs()
        if not h264:
            raise BridgeError("WebRTC H.264 packetizer is unavailable")
        for transceiver in self._pc.getTransceivers():
            if transceiver.sender == video_sender:
                transceiver.setCodecPreferences(h264)
            elif transceiver.sender == audio_sender:
                # Keep aiortc's built-in Opus capability only.  It packetizes
                # an av.Packet unchanged rather than re-encoding it.
                opus = [codec for codec in RTCRtpSender.getCapabilities("audio").codecs
                        if codec.mimeType.lower() == "audio/opus"]
                if not opus:
                    raise BridgeError("WebRTC Opus packetizer is unavailable")
                transceiver.setCodecPreferences(opus)
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        if self._pc.localDescription is None:
            raise BridgeError("cannot create browser WebRTC answer")
        return {"type": self._pc.localDescription.type, "sdp": self._pc.localDescription.sdp}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_media:
            assert self.ingress is not None
            self.ingress.close()
        else:
            assert self._shared_media is not None
            self._shared_media.unsubscribe(self.video_track, self.audio_track)
        if self._owns_input:
            self.input.close()
        await self._pc.close()


__all__ = [
    "AUDIO_TIME_BASE",
    "BrowserWebRtcBridge",
    "BridgeError",
    "EncodedUnit",
    "MAX_FRAGMENT_BYTES",
    "MAX_CONTROL_MESSAGE_BYTES",
    "INPUT_HEADER",
    "INPUT_KEYBOARD",
    "INPUT_MAGIC",
    "INPUT_MOUSE_BUTTON",
    "INPUT_MOUSE_POSITION",
    "INPUT_SCROLL",
    "INPUT_RESIZE",
    "INPUT_VERSION",
    "PACKET_AUDIO",
    "PACKET_CONFIG",
    "PACKET_END",
    "PACKET_FIRST",
    "PACKET_HEADER",
    "PACKET_IDR",
    "PACKET_MAGIC",
    "SharedMediaIngress",
    "UnixTapIngress",
    "UnixInputEgress",
    "VIDEO_TIME_BASE",
    "_AudioAssembler",
    "_PacketTrack",
    "_VideoAssembler",
]
