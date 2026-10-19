#!/usr/bin/env python3
"""Local direct-media-to-WebRTC bridge for the PVE browser console.

The local producer is the per-VM QEMU Display1 media worker. It sends encoded
H.264 or HEVC and Opus over two private Unix ``SOCK_SEQPACKET`` sockets; this module
packetizes those elementary streams for WebRTC without decoding or re-encoding
them. The PVE API / terminal-service adapter owns authorization and passes an
already-authorized SDP offer to :meth:`BrowserWebRtcBridge.answer_offer`.
No PVE cookie, password, CSRF value, external transport ticket, QSF token, or terminal
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
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import av
from aiortc import (MediaStreamTrack, RTCPeerConnection, RTCSessionDescription,
                    RTCRtpSender)
from aiortc import codecs as aiortc_codecs
from aiortc import clock as aiortc_clock
import aiortc.rtcrtpsender as aiortc_rtcrtpsender
from aiortc.codecs.h264 import H264Encoder
from aiortc.mediastreams import convert_timebase
from aiortc.mediastreams import MediaStreamError
from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecParameters
from aiortc.rtp import (RTCP_PSFB_PLI, RTCP_PSFB_APP, RtcpPsfbPacket,
                        RtcpRrPacket, RtcpSrPacket, RtpPacket, unpack_remb_fci)
from aiortc.sdp import SessionDescription


PACKET_MAGIC = 0x51534D50  # "QSMP", encoded as a network-order u32.
PACKET_HEADER = struct.Struct("!IIIII")
PACKET_FIRST = 0x00000001
PACKET_END = 0x00000002
PACKET_IDR = 0x00000004
PACKET_AUDIO = 0x00000008
PACKET_CONFIG = 0x00000010
PACKET_CURSOR = 0x00000020
CURSOR_HEADER = struct.Struct("!BBQQiiHHHH")
CURSOR_VERSION = 1
CURSOR_VISIBLE = 0x01
CURSOR_HAS_SHAPE = 0x02
INPUT_MAGIC = 0x51534D49  # "QSMI", encoded as a network-order u32.
INPUT_VERSION = 1
INPUT_HEADER = struct.Struct("!IBBH")
INPUT_MOUSE_POSITION = 1
INPUT_MOUSE_BUTTON = 2
INPUT_KEYBOARD = 3
INPUT_SCROLL = 4
INPUT_RESIZE = 5
INPUT_KEYFRAME_REQUEST = 6
INPUT_VIDEO_BITRATE = 7
MAX_FRAGMENT_BYTES = 256 * 1024
MAX_ACCESS_UNIT_BYTES = 4 * 1024 * 1024
MAX_CURSOR_EDGE = 64
MAX_CURSOR_BYTES = MAX_CURSOR_EDGE * MAX_CURSOR_EDGE * 4
MAX_SDP_BYTES = 128 * 1024
MAX_CONTROL_MESSAGE_BYTES = 1024
# The optional guest channel accepts up to 1 MiB of UTF-8 clipboard text. Its
# JSON/base64 envelope is larger, while interactive input remains constrained
# by MAX_CONTROL_MESSAGE_BYTES below.
MAX_GUEST_CONTROL_MESSAGE_BYTES = 2 * 1024 * 1024
# Payload budget leaves room for RTP extensions, SRTP authentication, RTX,
# UDP and IPv6 even on a VPN path whose MTU is only 1280–1300 bytes.
VIDEO_RTP_PAYLOAD_MAX = 1150
# Default encoder ceiling is 8 Mbps. Allow RTP/SRTP/IP overhead and recovery,
# but never flush a whole IDR at LAN speed into a routed VPN's small queue.
VIDEO_PACING_BURST_BYTES = 2400
VIDEO_TIME_BASE = Fraction(1, 90_000)
AUDIO_TIME_BASE = Fraction(1, 48_000)
# The matching worker requests the same node-local socket capacity.  It holds
# a burst of an H.264 IDR while the ingress thread queues its previous record
# into asyncio; it is never exposed as a browser presentation queue.
LOCAL_MEDIA_SOCKET_BUFFER_BYTES = 2 * 1024 * 1024
# A short burst must retain encoded reference frames: arbitrary P-frame
# replacement corrupts all dependent frames until the next IDR. Bound this
# FIFO to four frames (about 67 ms at 60 fps); a genuine overrun resets the
# decoder chain and requests a new keyframe instead of forwarding broken data.
VIDEO_QUEUE_DEPTH = 4
AUDIO_QUEUE_DEPTH = 4
VIDEO_CODECS = frozenset({"h264", "hevc"})
# RFC 7798 section 7.1 fmtp keys a browser may put on its H265 offer and that
# the answer must repeat for the browser to recognise the same codec.  Values
# are bounded ASCII tokens; anything else in the offer is ignored.
H265_FMTP_KEYS = ("profile-space", "profile-id", "tier-flag", "level-id",
                  "interop-constraints", "profile-compatibility-indicator", "tx-mode")
H265_DEFAULT_PARAMETERS = {"profile-id": "1", "tier-flag": "0", "level-id": "120", "tx-mode": "SRST"}
_H265_FMTP_VALUE = re.compile(r"\A[A-Za-z0-9]{1,32}\Z")
_HEVC_CODEC_LOCK = threading.Lock()
_HEVC_PACKETIZER_INSTALLED = False
_H264_PACKETIZER_INSTALLED = False


class _VideoPacer:
    """Per-peer token bucket, with backpressure and no extra media queue.

    A late event-loop wake releases only a bounded burst, never all the
    accumulated idle-time credit. RTP and RTX share the budget. Audio, RTCP
    and SCTP never acquire this lock.
    """

    def __init__(self, video_bitrate_kbps: int = 8000, *, hardware: bool = False) -> None:
        if type(video_bitrate_kbps) is not int or not 500 <= video_bitrate_kbps <= 100000:
            raise ValueError("video bitrate must be 500..100000 kbit/s")
        self._hardware = hardware
        self.set_bitrate(video_bitrate_kbps)
        self._lock = asyncio.Lock()
        self._tokens = float(self._burst)
        self._updated = time.monotonic()
        self.sent_packets = 0
        self.sent_bytes = 0

    def set_bitrate(self, video_bitrate_kbps: int) -> None:
        if type(video_bitrate_kbps) is not int or not 500 <= video_bitrate_kbps <= 100000:
            raise ValueError("video bitrate must be 500..100000 kbit/s")
        # At low targets hardware frames can exceed their target, including
        # NVENC and Intel H.264 QSV/VA-API. Do not turn that driver
        # limitation into continuous local FIFO loss. This is pacing credit,
        # not an extra encoder target; receiver loss/RTT still governs rate.
        headroom = max(0., 800 - video_bitrate_kbps / 10) if self._hardware else 0
        self._rate = (video_bitrate_kbps + headroom) * 1000 / 8 * 1.25
        # Retain two-packet pacing at the default; at higher rates permit
        # two milliseconds of credit so timer granularity does not cap LAN use.
        self._burst = max(VIDEO_PACING_BURST_BYTES, int(self._rate * .002))
        self._tokens = min(getattr(self, "_tokens", 0.), self._burst)

    async def send(self, data: bytes, sender: object,
                   forward: Callable[..., Any]) -> None:
        if (len(data) < 12 or data[0] >> 6 != 2 or 192 <= data[1] <= 223 or
                struct.unpack_from("!I", data, 8)[0] not in
                (sender._ssrc, sender._rtx_ssrc)):
            await forward(data)
            return
        # IPv6 + UDP + the maximum SRTP tag; accounting is conservative on IPv4.
        cost = len(data) + 64
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._burst,
                                   self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    break
                await asyncio.sleep((cost - self._tokens) / self._rate)
            # abs-send-time must describe transmission, not entry into the
            # pacer, otherwise receiver bandwidth estimates include our wait.
            extension_map = sender._RTCRtpSender__rtp_header_extensions_map
            packet = RtpPacket.parse(data, extension_map)
            packet.extensions.abs_send_time = (aiortc_clock.current_ntp_time() >> 14) & 0x00FFFFFF
            await forward(packet.serialize(extension_map))
            self.sent_packets += 1
            self.sent_bytes += cost


@dataclass
class _ReceiverFeedback:
    updated: float
    loss: float = 0.
    loss_time: float = float("-inf")
    rtt: float = 0.
    base_rtt: float = float("inf")
    remb: int = 0
    remb_time: float = float("-inf")
    remb_changed: float = float("-inf")
    remb_decreased: float = float("-inf")
    remb_reference: int = 0
    sequence: int | None = None
    lost: int = 0
    reduced_sequence: int | None = None
    queue_since: float | None = None
    rtt_samples: list[tuple[float, float]] = field(default_factory=list)
    sent_bytes: int | None = None
    sent_time: float = 0.
    sending_kbps: float = 0.


class AdaptiveVideoRate:
    """Hysteretic bitrate control for one shared encoded producer.

    RTCP REMB estimates, receiver-report loss and RTT inflation constrain the
    encoder *and* every RTP/RTX pacer. The slowest fresh receiver wins, because
    all viewers consume the same predictive stream. Fresh feedback is required
    for growth; a closed or silent peer cannot leave a permanent low ceiling.
    This is a conservative application controller, not a GCC implementation.
    """

    def __init__(self, maximum_kbps: int, apply: Callable[[int], None], *,
                 clock: Callable[[], float] = time.monotonic, enabled: bool = True,
                 hardware: bool = False) -> None:
        if type(maximum_kbps) is not int or not 500 <= maximum_kbps <= 100000:
            raise ValueError("video bitrate must be 500..100000 kbit/s")
        self.enabled = enabled
        self.hardware = hardware
        self.maximum_kbps = maximum_kbps
        self.current_kbps = maximum_kbps
        self._apply = apply
        self._clock = clock
        self._changed = clock()
        self._healthy_since = clock()
        self._receivers: dict[object, _ReceiverFeedback] = {}
        self._listeners: dict[object, Callable[[int], None]] = {}

    def add(self, peer: object, listener: Callable[[int], None]) -> None:
        self._listeners[peer] = listener
        listener(self.current_kbps)

    def remove(self, peer: object) -> None:
        self._listeners.pop(peer, None)
        self._receivers.pop(peer, None)

    def receiver_report(self, peer: object, report: object, rtt: float | None,
                        bytes_sent: int | None = None) -> None:
        now = self._clock()
        state = self._receivers.setdefault(peer, _ReceiverFeedback(now))
        sequence = report.highest_sequence
        # Ignore duplicate or out-of-order reports, including their old loss.
        if state.sequence is not None and sequence <= state.sequence:
            return
        loss = report.fraction_lost / 256
        if state.sequence is not None:
            delta = sequence - state.sequence
            loss = max(0., min(1., (report.packets_lost - state.lost) / delta))
        state.sequence, state.lost = sequence, report.packets_lost
        state.loss, state.loss_time, state.updated = loss, now, now
        if rtt is not None and 0 < rtt < 30:
            state.rtt = rtt
            state.rtt_samples = [(t, value) for t, value in state.rtt_samples if now - t < 60]
            state.rtt_samples.append((now, rtt))
            state.base_rtt = min(value for _, value in state.rtt_samples)
            # A single VPN RTT spike is not evidence of congestion. Require
            # a substantial, sustained queue, observed in new receiver reports.
            if rtt > state.base_rtt + max(.08, state.base_rtt * .75):
                if state.queue_since is None:
                    state.queue_since = now
            else:
                state.queue_since = None
        else:
            state.queue_since = None
        if type(bytes_sent) is int and bytes_sent >= 0:
            if state.sent_bytes is not None and bytes_sent >= state.sent_bytes and now > state.sent_time:
                state.sending_kbps = (bytes_sent - state.sent_bytes) * 8 / (now - state.sent_time) / 1000
            state.sent_bytes, state.sent_time = bytes_sent, now
        self._update(now)

    def receiver_estimate(self, peer: object, bitrate_bps: int) -> None:
        if type(bitrate_bps) is not int or bitrate_bps <= 0:
            return
        now = self._clock()
        state = self._receivers.setdefault(peer, _ReceiverFeedback(now))
        if not state.remb_reference or abs(bitrate_bps - state.remb_reference) > state.remb_reference * .05:
            if not state.remb_reference or bitrate_bps < state.remb_reference * .9:
                state.remb_decreased = now
            state.remb_changed, state.remb_reference = now, bitrate_bps
        state.remb, state.remb_time, state.updated = bitrate_bps, now, now
        self._update(now)

    def _update(self, now: float) -> None:
        if not self.enabled:
            return
        # REMB can be app-limited on an idle desktop. Require a fresh RR and
        # actual sender demand; otherwise neither delay noise nor a low REMB
        # should continually reconfigure NVENC (which forces an IDR in FFmpeg).
        fresh = [s for s in self._receivers.values() if now - s.loss_time < 6]
        bad = [s for s in fresh if s.loss >= .03 or
               (s.queue_since is not None and now - s.queue_since >= 3 and
                s.sending_kbps >= self.current_kbps * .5)]
        if bad:
            self._healthy_since = now
        pending = [s for s in bad if s.sequence != s.reduced_sequence]
        if not fresh or now - self._changed < (1 if pending else 10):
            return
        target = self.current_kbps
        ceiling = self.maximum_kbps
        loss = max((s.loss for s in bad), default=0.)
        queued = any(s.queue_since is not None and now - s.queue_since >= 3 for s in bad)
        if pending:
            # Consume each congestion report once. Repeated REMB packets must
            # not apply an old loss/RTT sample again while the path is draining.
            factor = .5 if max(s.loss for s in pending) >= .12 else .75
            for state in fresh:
                if now - state.remb_time < 5:
                    ceiling = min(ceiling, max(500, int(state.remb * .8 / 1000) - 128))
            target = min(ceiling, int(target * factor))
        elif not bad and now - self._healthy_since >= 10 and any(
                s.sending_kbps >= self.current_kbps * .7 for s in fresh):
            # Infrequent, material quality steps only when the video is using
            # the current allowance. Static desktops do not need upward probes.
            target = min(ceiling, target + max(250, int(target * .25)))
        target = max(500, min(self.maximum_kbps, target))
        if target == self.current_kbps:
            return
        try:
            self._apply(target)
        except BridgeError:
            return
        if target < self.current_kbps:
            for state in pending:
                state.reduced_sequence = state.sequence
        self.current_kbps, self._changed = target, now
        for listener in tuple(self._listeners.values()):
            listener(target)
        print(f"qsm-direct-terminal: adaptive video bitrate_kbps={target} "
              f"maximum_kbps={self.maximum_kbps} receivers={len(fresh)} "
              f"loss={loss:.4f} queued={int(queued)} remb_ceiling_kbps={ceiling}",
              file=sys.stderr, flush=True)


class _H264Packetizer(H264Encoder):
    """RFC 6184 single-NAL/FU-A payloads with QSM's VPN-safe size budget.

    Keep aiortc's encoder and Annex-B parser, but scope packet sizing to this
    sender instead of changing the third-party module's global PACKET_MAX.
    Small NALs travel individually; aggregation is optional in mode 1.
    """

    @classmethod
    def _packetize(cls, packages: Any) -> list[bytes]:
        payloads: list[bytes] = []
        for nal in packages:
            if len(nal) <= VIDEO_RTP_PAYLOAD_MAX:
                payloads.append(nal)
                continue
            indicator = (nal[0] & 0xE0) | 28
            nal_type = nal[0] & 0x1F
            available = VIDEO_RTP_PAYLOAD_MAX - 2
            for offset in range(1, len(nal), available):
                end = min(len(nal), offset + available)
                flags = nal_type | (0x80 if offset == 1 else 0) | (0x40 if end == len(nal) else 0)
                payloads.append(bytes([indicator, flags]) + nal[offset:end])
        return payloads


def _install_h264_packetizer() -> None:
    global _H264_PACKETIZER_INSTALLED
    with _HEVC_CODEC_LOCK:
        if _H264_PACKETIZER_INSTALLED:
            return
        original = aiortc_rtcrtpsender.get_encoder
        def get_encoder(codec: RTCRtpCodecParameters) -> object:
            if codec.mimeType.lower() == "video/h264":
                return _H264Packetizer()
            return original(codec)
        aiortc_rtcrtpsender.get_encoder = get_encoder
        _H264_PACKETIZER_INSTALLED = True


class _HevcPacketizer:
    """RFC 7798 packetizer for an already encoded Annex-B HEVC access unit.

    QSM never asks aiortc to encode HEVC.  The node's verified hardware
    encoder writes an Annex-B access unit and this small adapter supplies the
    part aiortc intentionally does not yet implement: single-NAL and
    fragmentation-unit RTP payloads.  Keeping it here preserves the
    no-decode/no-reencode path used by H.264.
    """

    @staticmethod
    def _split_annex_b(data: bytes) -> list[bytes]:
        result: list[bytes] = []
        start = 0
        while True:
            marker = data.find(b"\x00\x00\x01", start)
            if marker < 0:
                break
            nal_start = marker + 3
            # A four-byte Annex-B marker is the same three-byte marker with
            # one preceding zero.  Exclude it from the previous NAL rather
            # than exposing a spurious trailing byte to RFC 7798.
            if marker > 0 and data[marker - 1] == 0:
                marker -= 1
                nal_start = marker + 4
            next_marker = data.find(b"\x00\x00\x01", nal_start)
            if next_marker < 0:
                nal = data[nal_start:]
                if nal:
                    result.append(nal)
                break
            nal_end = next_marker - 1 if next_marker > 0 and data[next_marker - 1] == 0 else next_marker
            nal = data[nal_start:nal_end]
            if nal:
                result.append(nal)
            start = next_marker
        return result

    @staticmethod
    def _packetize_nal(nal: bytes) -> list[bytes]:
        if len(nal) < 2:
            raise BridgeError("malformed HEVC access unit")
        if len(nal) <= VIDEO_RTP_PAYLOAD_MAX:
            return [nal]
        # RFC 7798 section 4.4.3: FU PayloadHdr retains F and LayerId/TID,
        # replaces the NAL type with 49, then carries S/E and the original
        # six-bit type in a one-byte FU header.
        fu_indicator = bytes([(nal[0] & 0x81) | (49 << 1), nal[1]])
        original_type = (nal[0] >> 1) & 0x3f
        available = VIDEO_RTP_PAYLOAD_MAX - 3
        payload = memoryview(nal)[2:]
        result: list[bytes] = []
        offset = 0
        while offset < len(payload):
            end = min(len(payload), offset + available)
            flags = original_type
            if offset == 0:
                flags |= 0x80
            if end == len(payload):
                flags |= 0x40
            result.append(fu_indicator + bytes([flags]) + payload[offset:end].tobytes())
            offset = end
        return result

    def encode(self, _frame: object, force_keyframe: bool = False) -> tuple[list[bytes], int]:
        del force_keyframe
        raise BridgeError("HEVC packetizer accepts only encoded access units")

    def pack(self, packet: av.Packet) -> tuple[list[bytes], int]:
        if not isinstance(packet, av.Packet):
            raise BridgeError("HEVC packetizer received an invalid access unit")
        nals = self._split_annex_b(bytes(packet))
        if not nals:
            raise BridgeError("HEVC access unit has no Annex-B NAL")
        payloads: list[bytes] = []
        for nal in nals:
            payloads.extend(self._packetize_nal(nal))
        return payloads, convert_timebase(packet.pts, packet.time_base, VIDEO_TIME_BASE)


def _install_hevc_packetizer() -> None:
    """Add a narrowly scoped H.265 sender capability to aiortc once.

    aiortc's built-in codecs intentionally omit HEVC.  Its SDP machinery is
    generic for non-H.264 video codecs, however, and its RTP sender accepts an
    externally encoded :class:`av.Packet`.  Registering the RFC 7798
    capability and packetizer at process startup lets normal offer/answer
    intersection decide whether the *actual browser* supports it.
    """
    global _HEVC_PACKETIZER_INSTALLED
    with _HEVC_CODEC_LOCK:
        if _HEVC_PACKETIZER_INSTALLED:
            return
        codecs = aiortc_codecs.CODECS["video"]
        if not any(codec.mimeType.lower() == "video/h265" for codec in codecs):
            # 103/104 follow aiortc's built-in 97..102 dynamic assignments.
            # During offer/answer aiortc replaces those values with the
            # browser's offered payload types, just as it does for H.264.
            codecs.extend([
                RTCRtpCodecParameters(
                    mimeType="video/H265", clockRate=90_000, payloadType=103,
                    rtcpFeedback=[
                        RTCRtcpFeedback(type="nack"),
                        RTCRtcpFeedback(type="nack", parameter="pli"),
                        RTCRtcpFeedback(type="goog-remb"),
                    ],
                    # RFC 7798 section 7.1 parameters describing what the
                    # verified NVENC/QSV/VA-API lane actually emits: Main
                    # profile, Main tier, level 4 for 1280x800 at 60 fps,
                    # single RTP stream.  libwebrtc treats an H265 codec
                    # without fmtp as Main/tier 0/level 3.1 and requires
                    # profile, tier *and* level to be equal before it counts
                    # the answer as the codec it offered; an answer without
                    # these parameters left Chrome with a negotiated codec it
                    # never decoded.  answer_offer() replaces them with the
                    # browser's own offered values.
                    parameters=dict(H265_DEFAULT_PARAMETERS),
                ),
                RTCRtpCodecParameters(
                    mimeType="video/rtx", clockRate=90_000, payloadType=104,
                    parameters={"apt": 103},
                ),
            ])
        original = aiortc_rtcrtpsender.get_encoder
        if not getattr(original, "_qsm_hevc_packetizer", False):
            def get_encoder(codec: RTCRtpCodecParameters) -> object:
                if codec.mimeType.lower() == "video/h265":
                    return _HevcPacketizer()
                return original(codec)
            setattr(get_encoder, "_qsm_hevc_packetizer", True)
            aiortc_rtcrtpsender.get_encoder = get_encoder
        _HEVC_PACKETIZER_INSTALLED = True


def _h265_offer_details(sdp: str) -> tuple[dict[str, str], int | None]:
    """Return the fmtp parameters and payload type of the browser's H265 line.

    A browser identifies its H265 codec by profile, tier and level, and it
    binds its receiver to the payload type *it* offered.  Chrome allocates
    H265 below the 96..127 dynamic range (49 with RTX 50, Main 10 on 51) and
    reuses 103 for an H.264 RTX stream; aiortc only adopts an offered payload
    type from the dynamic range, so an answer had to be pinned to the
    browser's number explicitly or every RTP packet was discarded as an
    unknown type (packetsReceived climbing, framesReceived 0, no PLI).
    Values are copied verbatim within a bounded token alphabet; an offer
    without parameters keeps the defaults describing the encoder's stream.
    """
    try:
        description = SessionDescription.parse(sdp)
    except (TypeError, ValueError) as error:
        raise BridgeError("invalid browser WebRTC offer") from error
    candidates: list[tuple[dict[str, str], int]] = []
    for media in description.media:
        if media.kind != "video":
            continue
        for codec in media.rtp.codecs:
            if codec.mimeType.lower() not in {"video/h265", "video/hevc"}:
                continue
            parameters = {
                key: str(value) for key, value in (codec.parameters or {}).items()
                if key in H265_FMTP_KEYS and _H265_FMTP_VALUE.fullmatch(str(value))
            }
            payload_type = codec.payloadType
            if type(payload_type) is not int or not 0 <= payload_type <= 127:
                continue
            candidates.append((parameters, payload_type))
    for parameters, payload_type in candidates:
        if parameters.get("profile-id", "1") == "1":
            return {**H265_DEFAULT_PARAMETERS, **parameters}, payload_type
    return dict(H265_DEFAULT_PARAMETERS), None


def _apply_h265_offer(parameters: dict[str, str], payload_type: int | None) -> None:
    """Make the registered H265 capability answer with the browser's values.

    aiortc keeps one process-wide codec list, so the H265 entry and its RTX
    companion are rewritten under the lock right before negotiation.  A
    payload type outside aiortc's dynamic range would otherwise never be
    adopted from the offer.
    """
    with _HEVC_CODEC_LOCK:
        hevc = None
        for codec in aiortc_codecs.CODECS["video"]:
            if codec.mimeType.lower() == "video/h265":
                codec.parameters = dict(parameters)
                if payload_type is not None:
                    codec.payloadType = payload_type
                hevc = codec
        if hevc is None:
            return
        for codec in aiortc_codecs.CODECS["video"]:
            if codec.mimeType.lower() == "video/rtx" and codec.parameters.get("apt") in {103, hevc.payloadType}:
                codec.parameters = {"apt": hevc.payloadType}


class BridgeError(RuntimeError):
    """A local bridge configuration or packet was invalid."""


@dataclass(frozen=True)
class EncodedUnit:
    """One fully reassembled encoded access unit."""

    data: bytes
    keyframe: bool
    duration: int


@dataclass(frozen=True)
class GuestCursor:
    """One complete QEMU Display1 guest cursor state.

    ``bgra`` is QEMU's native little-endian ARGB pixel storage. It is kept
    out of the video stream and converted to canvas RGBA only by the browser.
    """

    sequence: int
    shape_id: int
    visible: bool
    x: int
    y: int
    width: int
    height: int
    hotspot_x: int
    hotspot_y: int
    bgra: bytes


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
        self._queue: asyncio.Queue[tuple[EncodedUnit, int] | None] = asyncio.Queue(maxsize=maximum_queue)
        self._pts = 0
        self._closed = False
        self._waiting_for_keyframe = kind == "video"
        self._last_recovery_request = float("-inf")
        self.on_keyframe_required: Callable[[], None] | None = None
        self.dropped_units = 0
        self.overflows = 0
        self.delivered_units = 0
        self.recoveries = 0

    def _clear_queue(self) -> None:
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    self.dropped_units += 1
            except asyncio.QueueEmpty:
                break

    def _request_recovery(self) -> None:
        # A request may arrive before the worker connects, be throttled by a
        # concurrent PLI, or produce an IDR while the sender is still starting.
        # Retry only while waiting: recovery must not rely on periodic IDRs.
        now = time.monotonic()
        if self.on_keyframe_required is not None and now - self._last_recovery_request >= 1.0:
            self.on_keyframe_required()
            self._last_recovery_request = now

    def put_nowait(self, unit: EncodedUnit) -> None:
        if self._closed:
            return
        # Account for every produced sample, including ones discarded under
        # pressure. Compressing the RTP clock on a drop causes A/V drift.
        pts = self._pts
        self._pts += unit.duration
        if self._queue.full():
            self.overflows += 1
            if self.kind == "video":
                self._clear_queue()
                self._waiting_for_keyframe = True
                self._last_recovery_request = float("-inf")
            else:
                # Opus packets do not have a video reference chain. Keep the
                # existing bounded audio FIFO and discard only its oldest unit.
                self._queue.get_nowait()
                self.dropped_units += 1
        if self.kind == "video" and self._waiting_for_keyframe:
            if not unit.keyframe:
                self.dropped_units += 1
                self._request_recovery()
                return
            self._waiting_for_keyframe = False
            self._last_recovery_request = float("-inf")
            self.recoveries += 1
        self._queue.put_nowait((unit, pts))

    def end_nowait(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.on_keyframe_required = None
        self._clear_queue()
        self._queue.put_nowait(None)

    async def recv(self) -> av.Packet:
        item = await self._queue.get()
        if item is None:
            raise MediaStreamError
        unit, pts = item
        self.delivered_units += 1
        packet = av.Packet(unit.data)
        packet.pts = pts
        packet.dts = pts
        packet.time_base = VIDEO_TIME_BASE if self.kind == "video" else AUDIO_TIME_BASE
        packet.is_keyframe = unit.keyframe
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
            # A cached IDR followed by a recent P-frame is not a valid
            # bootstrap: that P-frame also needs all intervening references.
            self._bootstrap = unit if unit.keyframe else None
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


class _CursorFanout:
    """Fan out the latest complete guest cursor without retaining a queue.

    Cursor coordinates are replaceable state. A newly opened PVE Console gets
    the last complete shape/position immediately, while a stalled viewer can
    never make Display1, H.264, or another viewer wait behind it.
    """

    def __init__(self) -> None:
        self._listeners: set[Callable[[GuestCursor], None]] = set()
        self._latest: GuestCursor | None = None
        self._closed = False

    def subscribe(self, listener: Callable[[GuestCursor], None]) -> None:
        if self._closed:
            raise BridgeError("shared browser media source is closed")
        self._listeners.add(listener)
        if self._latest is not None:
            listener(self._latest)

    def unsubscribe(self, listener: Callable[[GuestCursor], None]) -> None:
        self._listeners.discard(listener)

    def put_nowait(self, cursor: GuestCursor) -> None:
        if self._closed:
            return
        self._latest = cursor
        for listener in tuple(self._listeners):
            listener(cursor)

    def close(self) -> None:
        self._closed = True
        self._listeners.clear()
        self._latest = None


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
                 expected_uid: int | None = None,
                 on_cursor: Callable[[GuestCursor], None] | None = None) -> None:
        self._runtime_directory = runtime_directory
        self._loop = loop
        self._video_track = video_track
        self._audio_track = audio_track
        self._video_assembler = _VideoAssembler(fps)
        self._audio_assembler = _AudioAssembler()
        self._on_cursor = on_cursor
        self._cursor_shapes: dict[int, GuestCursor] = {}
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
                # A large IDR can span multiple private packet records.  The
                # worker must not drop the tail merely because this ingress
                # thread is scheduling the preceding record into asyncio.
                # This best-effort request remains within PVE's normal 4 MiB
                # system cap and preserves the old bounded behaviour on an
                # unusually restrictive host.
                try:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                        LOCAL_MEDIA_SOCKET_BUFFER_BYTES)
                except OSError:
                    pass
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

    def _publish_cursor(self, cursor: GuestCursor) -> None:
        if self._on_cursor is not None:
            self._loop.call_soon_threadsafe(self._on_cursor, cursor)

    def _decode_cursor(self, frame: int, fragment: int, flags: int,
                       data: bytes) -> GuestCursor | None:
        expected = PACKET_CURSOR | PACKET_FIRST | PACKET_END
        if flags != expected or fragment != 0 or len(data) < CURSOR_HEADER.size:
            raise BridgeError("malformed guest cursor packet")
        version, cursor_flags, sequence, shape_id, x, y, width, height, hotspot_x, hotspot_y = \
            CURSOR_HEADER.unpack_from(data)
        if version != CURSOR_VERSION or cursor_flags & ~(CURSOR_VISIBLE | CURSOR_HAS_SHAPE):
            raise BridgeError("unsupported guest cursor packet")
        visible = bool(cursor_flags & CURSOR_VISIBLE)
        has_shape = bool(cursor_flags & CURSOR_HAS_SHAPE)
        if width > MAX_CURSOR_EDGE or height > MAX_CURSOR_EDGE or \
                (width == 0) != (height == 0) or \
                (width and (hotspot_x >= width or hotspot_y >= height)):
            raise BridgeError("invalid guest cursor geometry")
        pixels = data[CURSOR_HEADER.size:]
        if has_shape:
            expected_bytes = width * height * 4
            if shape_id == 0 or not width or len(pixels) != expected_bytes or \
                    expected_bytes > MAX_CURSOR_BYTES:
                raise BridgeError("invalid guest cursor shape")
            cursor = GuestCursor(sequence, shape_id, visible, x, y, width, height,
                                 hotspot_x, hotspot_y, pixels)
            self._cursor_shapes = {shape_id: cursor}
            return cursor
        if pixels:
            raise BridgeError("unexpected guest cursor pixels")
        shape = self._cursor_shapes.get(shape_id)
        if shape is None:
            # A Unix SOCK_SEQPACKET record is local and reliable. Still, a
            # producer reconnect can make a movement arrive before its new
            # CursorDefine; wait for that definition rather than paint a
            # potentially wrong host pointer.
            return None
        if (shape.width, shape.height, shape.hotspot_x, shape.hotspot_y) != \
                (width, height, hotspot_x, hotspot_y):
            raise BridgeError("guest cursor shape identity changed")
        return GuestCursor(sequence, shape_id, visible, x, y, width, height,
                           hotspot_x, hotspot_y, shape.bgra)

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
                try:
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                          LOCAL_MEDIA_SOCKET_BUFFER_BYTES)
                except OSError:
                    pass
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
                    if not is_audio and packet_flags & PACKET_CURSOR:
                        cursor = self._decode_cursor(
                            frame, fragment, packet_flags, packet[PACKET_HEADER.size:])
                        if cursor is not None:
                            self._publish_cursor(cursor)
                        continue
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


class PointerSequenceFilter:
    """Drop late pointer samples of ONE browser peer (wrapping u32 serial).

    RFC 1982-style: half the serial space counts as older, so one delayed
    packet can never move the guest pointer backwards.
    """

    def __init__(self) -> None:
        self._last: int | None = None

    def accept(self, sequence: int) -> bool:
        if self._last is not None and (sequence == self._last or
                                       (sequence - self._last) & 0xFFFF_FFFF >= 0x8000_0000):
            return False
        self._last = sequence
        return True


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
        if not isinstance(value, dict) or set(value) - {"op", "x", "y", "width", "height", "sequence", "button", "down", "key", "modifiers", "vertical", "horizontal", "fps"}:
            raise BridgeError("invalid browser control message")
        op = value.get("op")
        payload: bytes
        opcode: int
        if op == "mouse_position" and set(value) in (
                {"op", "x", "y", "width", "height"},
                {"op", "x", "y", "width", "height", "sequence"}):
            x = cls._integer(value["x"], 0, 32767)
            y = cls._integer(value["y"], 0, 32767)
            width = cls._integer(value["width"], 1, 32767)
            height = cls._integer(value["height"], 1, 32767)
            if x >= width or y >= height:
                raise BridgeError("invalid browser control message")
            opcode = INPUT_MOUSE_POSITION
            if "sequence" in value:
                # qsm-pointer is unordered/unreliable by design.  Include a
                # browser-local serial so the media worker can discard a
                # late coordinate instead of visibly moving a dragged guest
                # window backwards.  The legacy eight-byte shape remains
                # accepted for a rolling package upgrade.
                sequence = cls._integer(value["sequence"], 0, 0xFFFF_FFFF)
                payload = struct.pack("!hhhhI", x, y, width, height, sequence)
            else:
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

    def send_browser_pointer_message(self, raw: object,
                                     order: PointerSequenceFilter | None = None) -> None:
        """Forward only a latest-state absolute pointer packet.

        Pointer samples travel on the browser's unordered, non-retransmitted
        channel.  Keeping them separate from keys, clicks and resize requests
        means a stale cursor coordinate can be discarded without delaying a
        subsequent click or keyboard shortcut.

        The browser's serial is local to one Console, while one worker can
        outlive a Console or serve several at once: a worker-wide "newest
        serial" made every pointer sample of a reopened Console look stale
        until its counter overtook the previous one.  With ``order`` the
        serial is judged per browser peer here and stripped, so the ordered
        Unix socket hands the worker only in-order latest-state samples.
        """
        packet = self.encode_browser_message(raw)
        if packet[5] != INPUT_MOUSE_POSITION:
            raise BridgeError("browser pointer channel received a non-pointer message")
        if order is not None and len(packet) == INPUT_HEADER.size + 12:
            sequence = struct.unpack_from("!I", packet, INPUT_HEADER.size + 8)[0]
            if not order.accept(sequence):
                return
            packet = (INPUT_HEADER.pack(INPUT_MAGIC, INPUT_VERSION, INPUT_MOUSE_POSITION, 8) +
                      packet[INPUT_HEADER.size:INPUT_HEADER.size + 8])
        self._send_packet(packet)

    def request_keyframe(self) -> None:
        """Ask the local worker for a fresh IDR after authenticated RTCP PLI."""
        self._send_packet(INPUT_HEADER.pack(
            INPUT_MAGIC, INPUT_VERSION, INPUT_KEYFRAME_REQUEST, 0))

    def set_video_bitrate(self, bitrate_kbps: int) -> None:
        """Private producer control; never accepted as a browser input opcode."""
        if type(bitrate_kbps) is not int or not 500 <= bitrate_kbps <= 100000:
            raise BridgeError("video bitrate must be 500..100000 kbit/s")
        self._send_packet(INPUT_HEADER.pack(
            INPUT_MAGIC, INPUT_VERSION, INPUT_VIDEO_BITRATE, 4) + struct.pack("!I", bitrate_kbps))

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
        self._cursors = _CursorFanout()
        self._ingress = UnixTapIngress(
            runtime_directory, loop, self._video, self._audio, fps=fps,
            expected_uid=expected_producer_uid, on_cursor=self._cursors.put_nowait)
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

    def subscribe(self, on_cursor: Callable[[GuestCursor], None] | None = None) \
            -> tuple[_PacketTrack, _PacketTrack]:
        if not self._started:
            raise BridgeError("shared browser media source is not started")
        tracks = (
            self._video.subscribe(kind="video", maximum_queue=VIDEO_QUEUE_DEPTH),
            self._audio.subscribe(kind="audio", maximum_queue=AUDIO_QUEUE_DEPTH),
        )
        if on_cursor is not None:
            self._cursors.subscribe(on_cursor)
        return tracks

    def unsubscribe(self, video_track: _PacketTrack, audio_track: _PacketTrack,
                    on_cursor: Callable[[GuestCursor], None] | None = None) -> None:
        self._video.unsubscribe(video_track)
        self._audio.unsubscribe(audio_track)
        if on_cursor is not None:
            self._cursors.unsubscribe(on_cursor)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ingress.close()
        self._cursors.close()


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
                 guest_dispatch: Callable[[Any], dict[str, Any]] | None = None,
                 on_terminal: Callable[[], None] | None = None,
                 video_codec: str = "h264", video_bitrate_kbps: int = 8000,
                 video_rate: AdaptiveVideoRate | None = None,
                 on_microphone: Callable[[object, bytes], None] | None = None) -> None:
        if video_codec not in VIDEO_CODECS:
            raise BridgeError("invalid direct browser video codec")
        _install_h264_packetizer()
        if video_codec == "hevc":
            _install_hevc_packetizer()
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError as error:
            raise BridgeError("BrowserWebRtcBridge must be created in an event loop") from error
        self._pc = RTCPeerConnection()
        self.video_codec = video_codec
        self._shared_media = shared_media
        self._owns_media = shared_media is None
        # ``SharedMediaIngress.subscribe`` synchronously replays a cached
        # Display1 cursor to a new viewer.  Every field used by that callback
        # must therefore exist before subscribing: a VM which already has a
        # cursor must be just as connectable as a VM with no prior damage.
        self._closed = False
        self._on_terminal = on_terminal
        self._terminal_notified = False
        self._latest_cursor: GuestCursor | None = None
        self._sent_cursor_shape_id: int | None = None
        self._cursor_channel: object | None = None
        self._control_channel: object | None = None
        self._cursor_listener = self._receive_cursor
        if shared_media is None:
            self.video_track = _PacketTrack("video", maximum_queue=VIDEO_QUEUE_DEPTH)
            self.audio_track = _PacketTrack("audio", maximum_queue=AUDIO_QUEUE_DEPTH)
            self.ingress = UnixTapIngress(runtime_directory, self._loop, self.video_track,
                                           self.audio_track, fps=fps,
                                           expected_uid=expected_producer_uid,
                                           on_cursor=self._cursor_listener)
        else:
            self.video_track, self.audio_track = shared_media.subscribe(self._cursor_listener)
            self.ingress = None
        self._owns_input = shared_input is None
        self._pointer_order = PointerSequenceFilter()
        self.input = shared_input or UnixInputEgress(
            runtime_directory, expected_uid=expected_producer_uid)
        self._last_keyframe_request = float("-inf")
        self._video_rate = video_rate or AdaptiveVideoRate(video_bitrate_kbps, self.input.set_video_bitrate)
        self._video_pacer = _VideoPacer(video_bitrate_kbps, hardware=self._video_rate.hardware)
        self._video_rate_sequence = 0
        self._video_rate.add(self, self._video_rate_changed)
        self.video_track.on_keyframe_required = self._request_video_keyframe
        self._guest_dispatch = guest_dispatch
        # The terminal service owns the lifetime of the shared producer.  A
        # browser closing its tab used to leave this bridge subscribed until
        # the coarse ten-minute lease expired.  Besides leaking a WebRTC peer,
        # that made every new encoded frame fan out to old RTP senders.  Keep
        # the notification local and idempotent; it conveys no browser data.
        # A PVE console session has exactly one SDP offer and one pair of
        # tracks.  In particular, do not let a caller append another sender
        # to an already-authorized peer connection by replaying signalling.
        self._taps_started = False
        self._offer_consumed = False
        self._control_channel_seen = False
        self._pointer_channel_seen = False
        self._pc.on("datachannel", self._on_datachannel)
        # The browser's microphone: its audio m-line is sendrecv and it puts
        # a track on it only while the user has the microphone switched on.
        self._on_microphone = on_microphone
        self._microphone_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._pc.on("track", self._on_track)

        @self._pc.on("iceconnectionstatechange")
        def on_iceconnectionstatechange() -> None:
            print(f"qsm-direct-terminal: WebRTC ICE state={self._pc.iceConnectionState}",
                  file=sys.stderr, flush=True)

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
            if self._pc.connectionState == "connected" and self._health_task is None:
                self._health_task = asyncio.create_task(self._report_media_health())
            if self._pc.connectionState in {"closed", "failed"}:
                self._notify_terminal()

    async def _report_media_health(self) -> None:
        # Numeric transport counters only: no media, credentials or addresses.
        previous_time = time.monotonic()
        previous_frames = self.video_track.delivered_units
        while not self._closed:
            await asyncio.sleep(10)
            if self._closed or self._pc.connectionState != "connected":
                return
            self._publish_video_rate()
            now = time.monotonic()
            frames = self.video_track.delivered_units
            report = await self._pc.getStats()
            remote = next((r for r in report.values()
                           if r.type == "remote-inbound-rtp" and r.kind == "video"), None)
            loss = getattr(remote, "packetsLost", 0)
            fraction = getattr(remote, "fractionLost", 0)
            rtt = getattr(remote, "roundTripTime", None)
            rtt_ms = round(rtt * 1000) if rtt is not None else -1
            print(f"qsm-direct-terminal: media health video_fps={(frames - previous_frames) / max(0.001, now - previous_time):.1f} "
                  f"queue={self.video_track._queue.qsize()} drops={self.video_track.dropped_units} "
                  f"overflows={self.video_track.overflows} waiting_idr={int(self.video_track._waiting_for_keyframe)} "
                  f"remote_lost={loss} loss_fraction={fraction} rtt_ms={rtt_ms} "
                  f"bitrate_kbps={self._video_rate.current_kbps} maximum_kbps={self._video_rate.maximum_kbps} "
                  f"paced_packets={self._video_pacer.sent_packets} paced_bytes={self._video_pacer.sent_bytes}",
                  file=sys.stderr, flush=True)
            previous_time, previous_frames = now, frames

    @property
    def terminal(self) -> bool:
        """Whether this browser peer has reached an unrecoverable terminal state."""
        return self._terminal_notified

    @property
    def connection_state(self) -> str:
        """Current aiortc connection state for the terminal's bounded watchdog."""
        return str(self._pc.connectionState)

    def _notify_terminal(self) -> None:
        """Ask the terminal owner to release this dead browser peer once."""
        if self._terminal_notified:
            return
        self._terminal_notified = True
        if self._on_terminal is not None:
            try:
                self._on_terminal()
            except Exception:
                # Browser lifecycle cleanup is best-effort.  A callback must
                # never turn an ordinary DTLS close into a bridge crash.
                pass

    @property
    def input_context(self) -> str:
        return f"unix:{self.input.path}"

    def _send_control(self, payload: dict[str, Any]) -> bool:
        """Send a bounded, server-originated guest-side-channel event."""
        channel = self._control_channel
        if self._closed or channel is None or getattr(channel, "readyState", None) != "open":
            return False
        try:
            channel.send(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
            return True
        except Exception:
            # SCTP closure is ordinary browser lifecycle, not a media failure.
            return False

    def _receive_cursor(self, cursor: GuestCursor) -> None:
        """Accept an ingress-loop cursor update and publish latest state.

        The private tap has already fully validated the QEMU packet. All
        browser peers still receive only a bounded JSON envelope.
        """
        if self._closed:
            return
        self._latest_cursor = cursor
        self._publish_cursor()

    def _publish_cursor(self) -> None:
        cursor = self._latest_cursor
        if self._closed or cursor is None:
            return
        # Cursor definitions are stateful and travel over qsm-control once;
        # movements then use an unordered/unreliable server-created channel.
        # Thus an H.264 decoder or an SCTP retransmit cannot
        # make a new host mouse location wait behind an obsolete one.
        if cursor.shape_id and cursor.shape_id != self._sent_cursor_shape_id:
            if self._send_control({
                    "op": "qsm_guest_cursor_shape", "shape_id": cursor.shape_id,
                    "width": cursor.width, "height": cursor.height,
                    "hotspot_x": cursor.hotspot_x, "hotspot_y": cursor.hotspot_y,
                    "bgra_b64": base64.b64encode(cursor.bgra).decode("ascii"),
            }):
                self._sent_cursor_shape_id = cursor.shape_id
        channel = self._cursor_channel
        if channel is None or getattr(channel, "readyState", None) != "open":
            return
        try:
            channel.send(json.dumps({
                "op": "qsm_guest_cursor", "sequence": cursor.sequence,
                "shape_id": cursor.shape_id, "visible": cursor.visible,
                "x": cursor.x, "y": cursor.y,
            }, separators=(",", ":"), ensure_ascii=True))
        except Exception:
            # This channel deliberately carries replaceable state. A later
            # MouseSet will supersede a dropped/closing record.
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
            "qsm_guest_status": "status",
        }
        operation = operations.get(value.pop("op"))
        if operation is None:
            raise BridgeError("browser guest message is invalid")
        value["op"] = operation
        return request_id, value

    def _guest_result(self, channel: object, request_id: str, *,
                      result: dict[str, Any] | None = None) -> None:
        """Return a deliberately generic result for one browser guest request."""
        if self._closed or channel is not self._control_channel:
            return
        if result is None:
            self._send_control({
                "op": "qsm_guest_result", "request_id": request_id, "ok": False,
                "error": "Guest clipboard operation failed.",
            })
            return
        self._send_control({
            "op": "qsm_guest_result", "request_id": request_id, "ok": True, "result": result,
        })

    async def _dispatch_guest_request(self, channel: object, request_id: str,
                                      request: dict[str, Any]) -> None:
        try:
            if self._guest_dispatch is None:
                raise BridgeError("guest tools are unavailable")
            result = await asyncio.to_thread(self._guest_dispatch, request)
        except Exception:
            # The guest-side detail can include a transient agent state. Keep
            # the PVE browser response useful but deliberately non-sensitive.
            self._guest_result(channel, request_id)
        else:
            self._guest_result(channel, request_id, result=result)

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

            @channel.on("open")
            def on_control_open() -> None:
                self._sent_cursor_shape_id = None
                self._publish_cursor()
                self._video_rate_changed(self._video_rate.current_kbps)
            if getattr(channel, "readyState", None) == "open":
                on_control_open()
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
                    self.input.send_browser_pointer_message(message, self._pointer_order)
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
    def _video_codecs(codec_name: str) -> list[object]:
        """Return one negotiated video codec and its RTX capability.

        ``setCodecPreferences`` does more than select a video format.  Giving
        it one video codec alone suppresses aiortc's otherwise available RTX entries,
        leaving NACK recovery to retransmit an old packet with its original
        sequence number.  Chromium can discard that late packet as already
        past the jitter-buffer window and then requests a PLI, visibly
        corrupting a predictive frame chain until the next IDR.

        A generic RTX capability is deliberately paired with every selected
        H.264 or H.265 profile by aiortc when it builds SDP.  It makes a NACK
        carry a fresh RTX sequence number / SSRC and lets a browser recover
        individual UDP losses before it needs an IDR.
        """
        if codec_name not in VIDEO_CODECS:
            raise BridgeError("invalid direct browser video codec")
        capabilities = RTCRtpSender.getCapabilities("video").codecs
        mime_type = "video/h264" if codec_name == "h264" else "video/h265"
        selected = [
            codec for codec in capabilities
            if codec.mimeType.lower() == mime_type
        ]
        return selected + [
            codec for codec in capabilities
            if codec.mimeType.lower() == "video/rtx"
        ]

    @staticmethod
    def _describe_video_answer(sdp: str) -> str:
        """Fixed-format summary of the answered video codec, never the SDP."""
        try:
            description = SessionDescription.parse(sdp)
        except (TypeError, ValueError):
            return "codec=unparsed"
        for media in description.media:
            if media.kind != "video":
                continue
            for codec in media.rtp.codecs:
                if codec.mimeType.lower() == "video/rtx":
                    continue
                parameters = ";".join(
                    f"{key}={value}" for key, value in sorted((codec.parameters or {}).items())
                    if _H265_FMTP_VALUE.fullmatch(str(key).replace("-", "")) and _H265_FMTP_VALUE.fullmatch(str(value)))
                return f"codec={codec.mimeType} pt={codec.payloadType} fmtp={parameters or 'none'}"
        return "codec=none"

    def _video_rate_changed(self, bitrate_kbps: int) -> None:
        self._video_pacer.set_bitrate(bitrate_kbps)
        self._publish_video_rate()

    def _publish_video_rate(self) -> None:
        # Latest-state telemetry must not queue behind a lost reliable SCTP
        # message. Reuse the existing unordered server->browser cursor stream.
        # Serial numbers also reject a delayed initial reliable-channel copy.
        self._video_rate_sequence += 1
        payload = {"op": "qsm_video_rate", "sequence": self._video_rate_sequence,
                   "adaptive": self._video_rate.enabled,
                   "bitrate": self._video_rate.current_kbps,
                   "maximum": self._video_rate.maximum_kbps}
        channel = self._cursor_channel
        if channel is not None and getattr(channel, "readyState", None) == "open":
            try:
                channel.send(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
            except Exception:
                pass  # The next update/health tick replaces a lost sample.
        else:
            self._send_control(payload)

    def _request_video_keyframe(self) -> None:
        # Many losses can produce a PLI burst. Restarting an external encoder
        # for every report creates another freeze instead of repairing video.
        now = time.monotonic()
        if now - self._last_keyframe_request < 0.25:
            return
        try:
            self.input.request_keyframe()
            self._last_keyframe_request = now
        except BridgeError:
            pass  # The producer may already be retiring.

    def _attach_video_recovery(self, sender: object) -> None:
        """Bridge RTCP PLI to the external Display1 encoder.

        aiortc normally reacts to PLI by setting its internal force-keyframe
        flag. QSM gives it already encoded H.264 packets, so that flag cannot
        affect the external NVENC/QSV/VA-API/libx264 worker. Without this
        handoff, a damaged predictive chain survives until the next periodic
        IDR.
        """
        receive_rtcp = getattr(sender, "_handle_rtcp_packet", None)
        if not callable(receive_rtcp):
            raise BridgeError("WebRTC H.264 sender cannot process RTCP feedback")

        async def receive_with_recovery(packet: object) -> None:
            if isinstance(packet, RtcpPsfbPacket) and packet.fmt == RTCP_PSFB_PLI:
                self._request_video_keyframe()
            await receive_rtcp(packet)
            if isinstance(packet, RtcpPsfbPacket) and packet.fmt == RTCP_PSFB_APP:
                try:
                    bitrate, sources = unpack_remb_fci(packet.fci)
                except ValueError:
                    return
                if sender._ssrc in sources:
                    self._video_rate.receiver_estimate(self, bitrate)
            elif isinstance(packet, (RtcpRrPacket, RtcpSrPacket)):
                report = next((r for r in packet.reports if r.ssrc == sender._ssrc), None)
                if report is not None:
                    stats = await sender.getStats()
                    remote = next((r for r in stats.values()
                                   if r.type == "remote-inbound-rtp" and r.kind == "video"), None)
                    rtt = getattr(remote, "roundTripTime", None)
                    # aiortc's RTT EWMA weights its old value by 0.85. Use
                    # the matching raw SR/RR sample for congestion decisions:
                    # otherwise a short VPN queue can overflow before the
                    # smoothed statistic catches up. Keep aiortc's own stats.
                    if report.dlsr and report.lsr == getattr(sender, "_RTCRtpSender__lsr", None):
                        sent_at = getattr(sender, "_RTCRtpSender__lsr_time", None)
                        if sent_at is not None:
                            rtt = time.time() - sent_at - report.dlsr / 65536
                    outbound = next((r for r in stats.values()
                                     if r.type == "outbound-rtp" and r.kind == "video"), None)
                    self._video_rate.receiver_report(
                        self, report, rtt, getattr(outbound, "bytesSent", None))

        # Install before SDP application can start RTP/RTCP tasks. The hook is
        # per-video-sender, therefore it cannot pace or perturb audio/control.
        setattr(sender, "_handle_rtcp_packet", receive_with_recovery)

    @staticmethod
    def offered_video_codecs(sdp: str) -> frozenset[str]:
        """Return codecs expressly advertised by a syntactically valid offer."""
        try:
            description = SessionDescription.parse(sdp)
        except (TypeError, ValueError) as error:
            raise BridgeError("invalid browser WebRTC offer") from error
        result: set[str] = set()
        for media in description.media:
            if media.kind != "video":
                continue
            for codec in media.rtp.codecs:
                mime = codec.mimeType.lower()
                if mime == "video/h264":
                    result.add("h264")
                elif mime in {"video/h265", "video/hevc"}:
                    # The IANA/RFC 7798 RTP name is H265. Accept HEVC as an
                    # offer-side alias for older WebKit builds, while answers
                    # always use the standard H265 spelling.
                    result.add("hevc")
        return frozenset(result)

    @classmethod
    def offer_supports_codec(cls, sdp: str, codec_name: str) -> bool:
        if codec_name not in VIDEO_CODECS:
            return False
        return codec_name in cls.offered_video_codecs(sdp)

    @classmethod
    def _validate_offer_codecs(cls, sdp: str, video_codec: str) -> None:
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
        offered_video = cls.offered_video_codecs(sdp)
        opus = any(
            media.kind == "audio" and any(
                codec.mimeType.lower() == "audio/opus" for codec in media.rtp.codecs)
            for media in description.media
        )
        if video_codec not in offered_video:
            requested = "HEVC" if video_codec == "hevc" else "H.264"
            raise BridgeError(f"browser does not offer WebRTC {requested}")
        if not opus:
            raise BridgeError("browser does not offer WebRTC Opus")

    async def answer_offer(self, sdp: str, sdp_type: str = "offer") -> dict[str, str]:
        if self._closed or not isinstance(sdp, str) or not isinstance(sdp_type, str) or \
                sdp_type != "offer" or not sdp.isascii() or "\x00" in sdp or \
                not 1 <= len(sdp.encode("ascii")) <= MAX_SDP_BYTES:
            raise BridgeError("invalid browser WebRTC offer")
        if not self._taps_started:
            raise BridgeError("browser bridge taps are not running")
        self._validate_offer_codecs(sdp, self.video_codec)
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
        self._attach_video_recovery(video_sender)
        if self.video_codec == "hevc":
            # Answer with the browser's own payload type and its
            # profile/tier/level/tx-mode, so its receiver binds the packets
            # we send to the H265 decoder it offered.
            _apply_h265_offer(*_h265_offer_details(sdp))
        video_codecs = self._video_codecs(self.video_codec)
        if not video_codecs:
            requested = "HEVC" if self.video_codec == "hevc" else "H.264"
            raise BridgeError(f"WebRTC {requested} packetizer is unavailable")
        for transceiver in self._pc.getTransceivers():
            if transceiver.sender == video_sender:
                transceiver.setCodecPreferences(video_codecs)
            elif transceiver.sender == audio_sender:
                # Keep aiortc's built-in Opus capability only.  It packetizes
                # an av.Packet unchanged rather than re-encoding it.
                opus = [codec for codec in RTCRtpSender.getCapabilities("audio").codecs
                        if codec.mimeType.lower() == "audio/opus"]
                if not opus:
                    raise BridgeError("WebRTC Opus packetizer is unavailable")
                transceiver.setCodecPreferences(opus)
        candidate_lines = [line.split() for line in sdp.splitlines()
                           if line.startswith("a=candidate:")]
        mdns_candidates = sum(len(parts) > 4 and parts[4].endswith(".local")
                              for parts in candidate_lines)
        ice_offer = sdp
        if mdns_candidates:
            # mDNS names often cannot resolve across a routed LAN or VPN.
            # aioice prunes every ICE component when those candidates are
            # followed by end-of-candidates, so it gathers no local addresses
            # at all. Keep discovery open: the browser can contact our numeric
            # candidate and authenticated STUN learns its actual endpoint.
            # The terminal's negotiation lease still bounds an absent peer.
            ice_offer = "\r\n".join(line for line in sdp.splitlines()
                                    if line != "a=end-of-candidates") + "\r\n"
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=ice_offer, type=sdp_type))
        # BUNDLE may replace the sender's DTLS transport during the remote
        # description. Hook its final transport before the local answer can
        # start RTP. The SSRC check leaves bundled audio and RTCP unpaced;
        # SCTP uses DTLS's separate data path.
        transport = video_sender.transport
        send_rtp = transport._send_rtp

        async def send_paced(data: bytes) -> None:
            await self._video_pacer.send(data, video_sender, send_rtp)

        transport._send_rtp = send_paced
        # Diagnose unresolved mDNS offers without exposing SDP or addresses.
        ice_transports = {transceiver.receiver.transport.transport
                          for transceiver in self._pc.getTransceivers()}
        if self._pc.sctp is not None:
            ice_transports.add(self._pc.sctp.transport.transport)
        remote_candidates = sum(len(transport.getRemoteCandidates())
                                for transport in ice_transports)
        print(f"qsm-direct-terminal: WebRTC offer candidates={len(candidate_lines)} "
              f"mdns={mdns_candidates} resolved={remote_candidates}",
              file=sys.stderr, flush=True)
        # This is server-to-browser only. It is intentionally separate from
        # qsm-control so pointer feedback remains latest-state data rather
        # than sitting behind reliable keyboard traffic. The browser
        # receives it through RTCPeerConnection.ondatachannel.
        cursor_channel = self._pc.createDataChannel(
            "qsm-guest-cursor", ordered=False, maxRetransmits=0)
        self._cursor_channel = cursor_channel

        @cursor_channel.on("open")
        def on_cursor_open() -> None:
            self._publish_cursor()
            self._publish_video_rate()

        answer = await self._pc.createAnswer()
        # One fixed-format journal line per session naming the negotiated
        # video codec and its fmtp: a browser that decodes nothing is then
        # diagnosable from the node without the browser's own statistics.
        print(f"qsm-direct-terminal: WebRTC video answer {self._describe_video_answer(answer.sdp)}",
              file=sys.stderr, flush=True)
        await self._pc.setLocalDescription(answer)
        if self._pc.localDescription is None:
            raise BridgeError("cannot create browser WebRTC answer")
        return {"type": self._pc.localDescription.type, "sdp": self._pc.localDescription.sdp}

    def _on_track(self, track: MediaStreamTrack) -> None:
        if track.kind != "audio" or self._on_microphone is None or self._microphone_task is not None:
            return
        self._microphone_task = asyncio.ensure_future(self._pump_microphone(track))

    async def _pump_microphone(self, track: MediaStreamTrack) -> None:
        """Hand each decoded microphone frame on as 48 kHz stereo s16le."""
        resampler: av.AudioResampler | None = None
        while not self._closed:
            try:
                frame = await track.recv()
            except (MediaStreamError, asyncio.CancelledError):
                return
            if (frame.format.name, frame.layout.name, frame.sample_rate) == ("s16", "stereo", 48000):
                frames = [frame]
            else:
                if resampler is None:
                    resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
                frames = resampler.resample(frame)
            for converted in frames:
                data = bytes(converted.planes[0])[:converted.samples * 4]
                try:
                    self._on_microphone(self, data)  # type: ignore[misc]
                except Exception:
                    # A gone worker or relay must not end the browser peer.
                    pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._video_rate.remove(self)
        if self._health_task is not None:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)
        if self._microphone_task is not None:
            self._microphone_task.cancel()
        if self._owns_media:
            assert self.ingress is not None
            self.ingress.close()
        else:
            assert self._shared_media is not None
            self._shared_media.unsubscribe(self.video_track, self.audio_track,
                                           self._cursor_listener)
        if self._owns_input:
            self.input.close()
        await self._pc.close()


__all__ = [
    "AUDIO_TIME_BASE",
    "BrowserWebRtcBridge",
    "BridgeError",
    "CURSOR_HAS_SHAPE",
    "CURSOR_HEADER",
    "CURSOR_VERSION",
    "CURSOR_VISIBLE",
    "EncodedUnit",
    "GuestCursor",
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
    "PACKET_CURSOR",
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
