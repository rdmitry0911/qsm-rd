#!/usr/bin/env python3
"""Local protocol tests for the encoded Display1-to-WebRTC bridge."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import time
import re
import unittest
import av
from fractions import Fraction
from pathlib import Path

from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.codecs.h264 import H264Encoder
from aiortc.rtp import RTCP_PSFB_PLI, RtcpPsfbPacket

from extensions.browser_bridge.qsm_browser_bridge import (
    AUDIO_TIME_BASE,
    BrowserWebRtcBridge,
    BridgeError,
    CURSOR_HAS_SHAPE,
    CURSOR_HEADER,
    CURSOR_VERSION,
    CURSOR_VISIBLE,
    EncodedUnit,
    GuestCursor,
    INPUT_HEADER,
    INPUT_KEYFRAME_REQUEST,
    INPUT_KEYBOARD,
    INPUT_MAGIC,
    LOCAL_MEDIA_SOCKET_BUFFER_BYTES,
    INPUT_MOUSE_BUTTON,
    INPUT_MOUSE_POSITION,
    INPUT_RESIZE,
    INPUT_SCROLL,
    INPUT_VERSION,
    MAX_FRAGMENT_BYTES,
    PACKET_AUDIO,
    PACKET_CONFIG,
    PACKET_CURSOR,
    PACKET_END,
    PACKET_FIRST,
    PACKET_HEADER,
    PACKET_IDR,
    PACKET_MAGIC,
    SharedMediaIngress,
    VIDEO_TIME_BASE,
    _AudioAssembler,
    _PacketTrack,
    _HevcPacketizer,
    _h265_offer_details,
    _VideoAssembler,
)


class BrowserBridgeAssemblerTests(unittest.TestCase):
    def test_hevc_rfc7798_packetizer_keeps_nal_headers_and_fu_boundaries(self) -> None:
        packetizer = _HevcPacketizer()
        packet = av.Packet(
            b"\x00\x00\x00\x01\x40\x01vps"
            b"\x00\x00\x01\x26\x01" + b"x" * 1600)
        packet.pts = 0
        packet.time_base = Fraction(1, 90_000)
        payloads, timestamp = packetizer.pack(packet)
        self.assertEqual(timestamp, 0)
        self.assertEqual(payloads[0], b"\x40\x01vps")
        self.assertGreaterEqual(len(payloads), 3)
        # NAL type 19 becomes FU type 49; S/E delimit the fragmented slice.
        self.assertEqual(payloads[1][:2], b"\x62\x01")
        self.assertEqual(payloads[1][2], 0x80 | 19)
        self.assertEqual(payloads[-1][2], 0x40 | 19)

    def test_local_media_socket_buffer_is_large_enough_for_fragmented_idr(self) -> None:
        # The producer and receiver intentionally reserve space for several
        # 256 KiB private records, so a busy asyncio turn cannot cut a large
        # IDR in half before WebRTC packetization starts.
        self.assertGreaterEqual(LOCAL_MEDIA_SOCKET_BUFFER_BYTES, 2 * MAX_FRAGMENT_BYTES)

    def test_terminal_callback_is_idempotent(self) -> None:
        """A closed browser must not retain duplicate media subscriptions."""
        notified: list[str] = []

        async def exercise() -> None:
            with tempfile.TemporaryDirectory(prefix="qsm-browser-terminal.") as directory:
                bridge = BrowserWebRtcBridge(Path(directory), fps=60,
                                              on_terminal=lambda: notified.append("closed"))
                try:
                    bridge._notify_terminal()
                    bridge._notify_terminal()
                    self.assertTrue(bridge.terminal)
                    self.assertEqual(notified, ["closed"])
                finally:
                    await bridge.close()

        asyncio.run(exercise())

    def test_guest_channel_accepts_only_clipboard_operations(self) -> None:
        self.assertEqual(BrowserWebRtcBridge._guest_request(
            '{"op":"qsm_guest_clipboard_get","request_id":"clipboard-1"}'),
            ("clipboard-1", {"op": "clipboard_get"}),
        )
        with self.assertRaises(BridgeError):
            BrowserWebRtcBridge._guest_request(
                '{"op":"qsm_guest_file_list","request_id":"files-1","area":"outgoing"}')

    def test_video_requires_complete_ordered_access_unit_and_recovers_at_first_boundary(self) -> None:
        assembler = _VideoAssembler(60)
        self.assertIsNone(assembler.add(4, 1, PACKET_END, b"late"))
        self.assertIsNone(assembler.add(5, 0, PACKET_FIRST | PACKET_IDR, b"one"))
        unit = assembler.add(5, 1, PACKET_END | PACKET_IDR, b"two")
        self.assertEqual(unit, EncodedUnit(b"onetwo", True, 1500))
        self.assertIsNone(assembler.add(6, 0, PACKET_FIRST, b"a"))
        self.assertIsNone(assembler.add(7, 1, PACKET_END, b"corrupt"))
        self.assertIsNone(assembler.add(8, 0, PACKET_FIRST, b"fresh"))
        self.assertEqual(assembler.add(8, 1, PACKET_END, b"frame"),
                         EncodedUnit(b"freshframe", False, 1500))

    def test_audio_requires_configured_duration_and_monotonic_sequence(self) -> None:
        assembler = _AudioAssembler()
        with self.assertRaises(BridgeError):
            assembler.add(0, 0, PACKET_FIRST | PACKET_END | PACKET_AUDIO, b"opus")
        self.assertIsNone(assembler.add(
            0, 240, PACKET_FIRST | PACKET_END | PACKET_AUDIO | PACKET_CONFIG, b""))
        self.assertEqual(assembler.samples_per_frame, 240)
        self.assertEqual(assembler.add(3, 0, PACKET_FIRST | PACKET_END | PACKET_AUDIO, b"opus"),
                         EncodedUnit(b"opus", False, 240))
        with self.assertRaises(BridgeError):
            assembler.add(2, 0, PACKET_FIRST | PACKET_END | PACKET_AUDIO, b"opus")

    def test_browser_control_schema_is_rewritten_into_fixed_packets(self) -> None:
        from extensions.browser_bridge.qsm_browser_bridge import UnixInputEgress

        packet = UnixInputEgress.encode_browser_message(
            '{"op":"mouse_position","x":20,"y":10,"width":1920,"height":1080}')
        magic, version, opcode, length = INPUT_HEADER.unpack_from(packet)
        self.assertEqual((magic, version, opcode, length),
                         (INPUT_MAGIC, INPUT_VERSION, INPUT_MOUSE_POSITION, 8))
        self.assertEqual(packet[INPUT_HEADER.size:], b"\x00\x14\x00\x0a\x07\x80\x04\x38")

        sequenced_pointer = UnixInputEgress.encode_browser_message(
            '{"op":"mouse_position","x":20,"y":10,"width":1920,"height":1080,"sequence":4294967295}')
        self.assertEqual(INPUT_HEADER.unpack_from(sequenced_pointer),
                         (INPUT_MAGIC, INPUT_VERSION, INPUT_MOUSE_POSITION, 12))
        self.assertEqual(sequenced_pointer[INPUT_HEADER.size:],
                         b"\x00\x14\x00\x0a\x07\x80\x04\x38\xff\xff\xff\xff")

        button = UnixInputEgress.encode_browser_message(
            '{"op":"mouse_button","button":1,"down":true}')
        self.assertEqual(INPUT_HEADER.unpack_from(button)[2], INPUT_MOUSE_BUTTON)
        keyboard = UnixInputEgress.encode_browser_message(
            '{"op":"keyboard","key":30,"down":false,"modifiers":2}')
        self.assertEqual(INPUT_HEADER.unpack_from(keyboard)[2], INPUT_KEYBOARD)
        scroll = UnixInputEgress.encode_browser_message(
            '{"op":"scroll","vertical":-120,"horizontal":0}')
        self.assertEqual(INPUT_HEADER.unpack_from(scroll)[2], INPUT_SCROLL)
        resize = UnixInputEgress.encode_browser_message(
            '{"op":"resize","width":1920,"height":1080,"fps":60}')
        self.assertEqual(INPUT_HEADER.unpack_from(resize),
                         (INPUT_MAGIC, INPUT_VERSION, INPUT_RESIZE, 10))
        self.assertEqual(resize[INPUT_HEADER.size:],
                         b"\x00\x00\x07\x80\x00\x00\x04\x38\x00\x3c")

        for raw in (
            '{"op":"mouse_position","x":1920,"y":0,"width":1920,"height":1080}',
            '{"op":"mouse_position","x":20,"y":10,"width":1920,"height":1080,"sequence":4294967296}',
            '{"op":"mouse_button","button":6,"down":true}',
            '{"op":"keyboard","key":30,"down":1,"modifiers":0}',
            '{"op":"scroll","vertical":0,"horizontal":0,"extra":1}',
            '{"op":"resize","width":1919,"height":1080,"fps":60}',
            '{"op":"unknown"}',
        ):
            with self.assertRaises(BridgeError):
                UnixInputEgress.encode_browser_message(raw)


class BrowserBridgeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_hevc_answer_repeats_the_browsers_profile_tier_level(self) -> None:
        """libwebrtc matches H265 by profile, tier and level: the answer must echo them."""
        with tempfile.TemporaryDirectory(prefix="qsm-browser-hevc-fmtp.") as directory:
            bridge = BrowserWebRtcBridge(Path(directory), fps=60, video_codec="hevc")
            browser = RTCPeerConnection()
            try:
                capabilities = RTCRtpSender.getCapabilities("video").codecs
                hevc = [codec for codec in capabilities if codec.mimeType.lower() == "video/h265"]
                video = browser.addTransceiver("video", direction="recvonly")
                video.setCodecPreferences(hevc)
                browser.addTransceiver("audio", direction="recvonly")
                browser.createDataChannel("qsm-control", ordered=True)
                browser.createDataChannel("qsm-pointer", ordered=False, maxRetransmits=0)
                await browser.setLocalDescription(await browser.createOffer())
                sdp = browser.localDescription.sdp
                # Replace the offer's video codec list with Chrome 151's layout:
                # H.264 on 102 with RTX 103 (apt=102), H265 Main on payload type
                # 49 with RTX 50 - below the 96..127 dynamic range - level 6,
                # Main tier, SRST.  103 is therefore a decoy that an answer must
                # not reuse for H265.
                head, video = sdp.split("m=video", 1)
                video, tail = (video.split("\r\nm=", 1) + [""])[:2]
                video_lines = [line for line in video.split("\r\n")
                               if not re.match(r"a=(rtpmap|fmtp|rtcp-fb):", line)]
                video_lines[0] = re.sub(r"^( \d+ [^ ]+ ).*$", r"\g<1>102 103 49 50", video_lines[0])
                video_lines += [
                    "a=rtpmap:102 H264/90000",
                    "a=fmtp:102 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
                    "a=rtpmap:103 rtx/90000", "a=fmtp:103 apt=102",
                    "a=rtpmap:49 H265/90000", "a=rtcp-fb:49 nack", "a=rtcp-fb:49 nack pli",
                    "a=fmtp:49 level-id=180;profile-id=1;tier-flag=0;tx-mode=SRST",
                    "a=rtpmap:50 rtx/90000", "a=fmtp:50 apt=49",
                ]
                sdp = head + "m=video" + "\r\n".join(video_lines) + "\r\n" + ("m=" + tail if tail else "")
                bridge.start_taps()
                answer = await bridge.answer_offer(sdp)
                self.assertRegex(answer["sdp"], r"m=video \d+ UDP/TLS/RTP/SAVPF 49 50\r\n")
                self.assertIn("a=rtpmap:49 H265/90000\r\n", answer["sdp"])
                self.assertIn("a=rtpmap:50 rtx/90000\r\na=fmtp:50 apt=49\r\n", answer["sdp"])
                self.assertNotIn("a=rtpmap:103", answer["sdp"])
                fmtp = re.search(r"a=fmtp:49 ([^\r\n]*)", answer["sdp"])
                self.assertIsNotNone(fmtp, "the H265 answer must carry an fmtp line")
                parameters = dict(item.split("=", 1) for item in fmtp.group(1).split(";"))
                self.assertEqual(parameters, {"level-id": "180", "profile-id": "1",
                                              "tier-flag": "0", "tx-mode": "SRST"})
            finally:
                await browser.close()
                await bridge.close()
        # An offer without parameters is answered with the encoder's own
        # Main / tier 0 / level 4 description rather than nothing.
        self.assertEqual(_h265_offer_details(
            "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
            "m=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\na=rtpmap:96 H265/90000\r\n"),
            ({"profile-id": "1", "tier-flag": "0", "level-id": "120", "tx-mode": "SRST"}, 96))

    async def test_hevc_offer_answer_uses_h265_only_when_offered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-browser-hevc-sdp.") as directory:
            bridge = BrowserWebRtcBridge(Path(directory), fps=60, video_codec="hevc")
            browser = RTCPeerConnection()
            try:
                hevc = [codec for codec in RTCRtpSender.getCapabilities("video").codecs
                        if codec.mimeType.lower() == "video/h265"]
                self.assertTrue(hevc)
                video = browser.addTransceiver("video", direction="recvonly")
                video.setCodecPreferences(hevc)
                browser.addTransceiver("audio", direction="recvonly")
                browser.createDataChannel("qsm-control", ordered=True)
                browser.createDataChannel("qsm-pointer", ordered=False, maxRetransmits=0)
                offer = await browser.createOffer()
                await browser.setLocalDescription(offer)
                self.assertEqual(BrowserWebRtcBridge.offered_video_codecs(browser.localDescription.sdp),
                                 frozenset({"hevc"}))
                bridge.start_taps()
                answer = await bridge.answer_offer(browser.localDescription.sdp)
                self.assertIn("H265/90000", answer["sdp"])
                self.assertNotIn("H264/90000", answer["sdp"])
            finally:
                await bridge.close()
                await browser.close()

    async def test_rtcp_pli_requests_an_external_worker_idr(self) -> None:
        """Pre-encoded H.264 needs an explicit PLI-to-worker handoff."""
        class Sender:
            def __init__(self) -> None:
                self.feedback: list[object] = []

            async def _handle_rtcp_packet(self, packet: object) -> None:
                self.feedback.append(packet)

        with tempfile.TemporaryDirectory(prefix="qsm-browser-pli.") as directory:
            bridge = BrowserWebRtcBridge(Path(directory), fps=60)
            requested: list[str] = []
            bridge.input.request_keyframe = lambda: requested.append("idr")  # type: ignore[method-assign]
            sender = Sender()
            try:
                bridge._attach_video_recovery(sender)
                pli = RtcpPsfbPacket(fmt=RTCP_PSFB_PLI, ssrc=1, media_ssrc=2)
                await sender._handle_rtcp_packet(pli)
                self.assertEqual(requested, ["idr"])
                self.assertEqual(sender.feedback, [pli])

                non_pli = RtcpPsfbPacket(fmt=15, ssrc=1, media_ssrc=2)
                await sender._handle_rtcp_packet(non_pli)
                self.assertEqual(requested, ["idr"])
                self.assertEqual(sender.feedback, [pli, non_pli])
            finally:
                await bridge.close()

    @staticmethod
    def _send(path: str, frame: int, fragment: int, flags: int, body: bytes) -> None:
        message = PACKET_HEADER.pack(PACKET_MAGIC, frame, fragment, flags, len(body)) + body
        producer = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            producer.connect(path)
            producer.sendall(message)
        finally:
            producer.close()

    async def test_private_socket_reassembles_encoded_units_without_transcoding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-browser-bridge.") as directory:
            bridge = BrowserWebRtcBridge(Path(directory), fps=60)
            video_context, audio_context = bridge.start_taps()
            video_path = video_context.removeprefix("unix:")
            audio_path = audio_context.removeprefix("unix:")
            self._send(video_path, 1, 0, PACKET_FIRST | PACKET_IDR,
                       b"\x00\x00\x00\x01\x67\x42")
            self._send(video_path, 1, 1, PACKET_END | PACKET_IDR,
                       b"\x00\x00\x00\x01\x65\x88")
            self._send(audio_path, 0, 240,
                       PACKET_FIRST | PACKET_END | PACKET_AUDIO | PACKET_CONFIG, b"")
            self._send(audio_path, 0, 0, PACKET_FIRST | PACKET_END | PACKET_AUDIO,
                       b"\xf8\xff\xfe")
            video = await asyncio.wait_for(bridge.video_track.recv(), 2)
            audio = await asyncio.wait_for(bridge.audio_track.recv(), 2)
            self.assertEqual(bytes(video), b"\x00\x00\x00\x01\x67\x42\x00\x00\x00\x01\x65\x88")
            self.assertEqual((video.pts, video.dts, video.time_base), (0, 0, VIDEO_TIME_BASE))
            self.assertTrue(video.is_keyframe)
            self.assertEqual(bytes(audio), b"\xf8\xff\xfe")
            self.assertEqual((audio.pts, audio.dts, audio.time_base), (0, 0, AUDIO_TIME_BASE))
            self.assertEqual(bridge.ingress.audio_samples_per_frame, 240)
            bridge.ingress.raise_if_failed()

            # aiortc's H.264 pack path receives the av.Packet directly. Its
            # result contains the source NAL; no decoded VideoFrame is ever
            # supplied to an encoder.
            payloads, timestamp = H264Encoder().pack(video)
            self.assertGreaterEqual(len(payloads), 1)
            self.assertEqual(timestamp, 0)
            self.assertTrue(any(payload.endswith(b"\x65\x88") for payload in payloads))
            await bridge.close()

    async def test_private_video_tap_publishes_display1_cursor_without_video_frame(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-browser-cursor.") as directory:
            bridge = BrowserWebRtcBridge(Path(directory), fps=60)
            try:
                video_context, _audio_context = bridge.start_taps()
                video_path = video_context.removeprefix("unix:")
                # QEMU Display1 uses BGRA bytes for its native little-endian
                # pixman ARGB cursor. This must not be mistaken for an H.264
                # access unit or wait for one to reach the bridge.
                pixels = bytes((0x11, 0x22, 0x33, 0xff) * 4)
                shape = CURSOR_HEADER.pack(
                    CURSOR_VERSION, CURSOR_VISIBLE | CURSOR_HAS_SHAPE,
                    7, 0x1234, 120, 80, 2, 2, 0, 1) + pixels
                self._send(video_path, 1, 0, PACKET_FIRST | PACKET_END | PACKET_CURSOR, shape)
                deadline = time.monotonic() + 2
                while bridge._latest_cursor is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                cursor = bridge._latest_cursor
                self.assertIsNotNone(cursor)
                assert cursor is not None
                self.assertEqual((cursor.sequence, cursor.shape_id, cursor.x, cursor.y),
                                 (7, 0x1234, 120, 80))
                self.assertTrue(cursor.visible)
                self.assertEqual((cursor.width, cursor.height, cursor.hotspot_x, cursor.hotspot_y),
                                 (2, 2, 0, 1))
                self.assertEqual(cursor.bgra, pixels)

                movement = CURSOR_HEADER.pack(
                    CURSOR_VERSION, CURSOR_VISIBLE, 8, 0x1234, 121, 81, 2, 2, 0, 1)
                self._send(video_path, 2, 0, PACKET_FIRST | PACKET_END | PACKET_CURSOR, movement)
                deadline = time.monotonic() + 2
                while (bridge._latest_cursor is None or bridge._latest_cursor.sequence != 8) and \
                        time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(bridge._latest_cursor)
                self.assertEqual((bridge._latest_cursor.sequence, bridge._latest_cursor.x,
                                  bridge._latest_cursor.y), (8, 121, 81))
                bridge.ingress.raise_if_failed()
            finally:
                await bridge.close()

    async def test_one_worker_media_ingress_fans_out_to_two_browser_tracks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-browser-fanout.") as directory:
            source = SharedMediaIngress(Path(directory), asyncio.get_running_loop(), fps=60)
            source.start()
            first_video, _first_audio = source.subscribe()
            try:
                self._send(os.fspath(source.video_path), 1, 0, PACKET_FIRST | PACKET_IDR,
                           b"\x00\x00\x00\x01\x67\x42")
                self._send(os.fspath(source.video_path), 1, 1, PACKET_END | PACKET_IDR,
                           b"\x00\x00\x00\x01\x65\x88")
                first = await asyncio.wait_for(first_video.recv(), 2)
                # A viewer can join long after the worker. It must receive a
                # complete IDR with the repeated SPS/PPS immediately, not
                # wait for a later keyframe after undecodable P-frames.
                second_video, _second_audio = source.subscribe()
                second = await asyncio.wait_for(second_video.recv(), 2)
                expected = b"\x00\x00\x00\x01\x67\x42\x00\x00\x00\x01\x65\x88"
                self.assertEqual(bytes(first), expected)
                self.assertEqual(bytes(second), expected)
                self.assertTrue(first.is_keyframe)
                self.assertTrue(second.is_keyframe)
                source.raise_if_failed()
            finally:
                source.close()

    async def test_cached_shared_cursor_is_safe_during_bridge_construction(self) -> None:
        """A new viewer must accept the cursor published before it connected.

        ``_CursorFanout.subscribe`` deliberately replays latest state
        synchronously.  This covers the production path where a running VM
        has already emitted Display1 cursor data before PVE creates the
        browser WebRTC bridge.
        """
        with tempfile.TemporaryDirectory(prefix="qsm-browser-cached-cursor.") as directory:
            source = SharedMediaIngress(Path(directory), asyncio.get_running_loop(), fps=60)
            source.start()
            bridge: BrowserWebRtcBridge | None = None
            try:
                cached = GuestCursor(
                    sequence=9, shape_id=0x1234, visible=True, x=320, y=240,
                    width=1, height=1, hotspot_x=0, hotspot_y=0,
                    bgra=b"\x00\x00\x00\xff",
                )
                source._cursors.put_nowait(cached)  # cached source state, as from Display1
                bridge = BrowserWebRtcBridge(Path(directory) / "viewer", fps=60,
                                              shared_media=source)
                self.assertEqual(bridge._latest_cursor, cached)
                self.assertFalse(bridge._closed)
            finally:
                if bridge is not None:
                    await bridge.close()
                source.close()

    async def test_private_input_socket_accepts_one_same_uid_receiver(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-browser-input.") as directory:
            bridge = BrowserWebRtcBridge(Path(directory), fps=60)
            bridge.start_taps()
            receiver = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                receiver.connect(bridge.input_context.removeprefix("unix:"))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    bridge.input.raise_if_failed()
                    bridge.input.send_browser_message(
                        '{"op":"mouse_button","button":1,"down":true}')
                    receiver.settimeout(0.1)
                    try:
                        packet = receiver.recv(64)
                        break
                    except TimeoutError:
                        await asyncio.sleep(0.01)
                else:
                    self.fail("browser input receiver did not receive a packet")
                self.assertEqual(INPUT_HEADER.unpack_from(packet),
                                 (INPUT_MAGIC, INPUT_VERSION, INPUT_MOUSE_BUTTON, 2))
                self.assertEqual(packet[INPUT_HEADER.size:], b"\x01\x01")
                bridge.input.request_keyframe()
                packet = receiver.recv(64)
                self.assertEqual(INPUT_HEADER.unpack_from(packet),
                                 (INPUT_MAGIC, INPUT_VERSION, INPUT_KEYFRAME_REQUEST, 0))
                bridge.input.send_browser_pointer_message(
                    '{"op":"mouse_position","x":20,"y":10,"width":1920,"height":1080}')
                packet = receiver.recv(64)
                self.assertEqual(INPUT_HEADER.unpack_from(packet),
                                 (INPUT_MAGIC, INPUT_VERSION, INPUT_MOUSE_POSITION, 8))
                with self.assertRaises(BridgeError):
                    bridge.input.send_browser_pointer_message(
                        '{"op":"mouse_button","button":1,"down":true}')
            finally:
                receiver.close()
                await bridge.close()

    async def test_offer_answer_is_h264_opus_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-browser-sdp.") as directory:
            browser = RTCPeerConnection()
            browser.addTransceiver("video", direction="recvonly")
            browser.addTransceiver("audio", direction="recvonly")
            browser.createDataChannel("qsm-control", ordered=True)
            browser.createDataChannel("qsm-pointer", ordered=False, maxRetransmits=0)
            server_channels: list[str] = []

            @browser.on("datachannel")
            def on_datachannel(channel: object) -> None:
                server_channels.append(str(getattr(channel, "label", "")))
            try:
                offer = await browser.createOffer()
                await browser.setLocalDescription(offer)
                bridge = BrowserWebRtcBridge(Path(directory), fps=60)
                bridge.start_taps()
                answer = await bridge.answer_offer(browser.localDescription.sdp)
                self.assertEqual(answer["type"], "answer")
                self.assertIn("H264/90000", answer["sdp"])
                # Keep RTP retransmission alongside H.264.  A browser NACK
                # must be repaired as a fresh RTX packet rather than waiting
                # for a full IDR after every individual UDP loss.
                self.assertIn("rtx/90000", answer["sdp"])
                self.assertRegex(answer["sdp"], r"a=fmtp:\d+ apt=\d+")
                self.assertIn("opus/48000", answer["sdp"])
                await browser.setRemoteDescription(RTCSessionDescription(**answer))
                deadline = time.monotonic() + 3
                while "qsm-guest-cursor" not in server_channels and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertIn("qsm-guest-cursor", server_channels)
                with self.assertRaisesRegex(BridgeError, "already consumed"):
                    await bridge.answer_offer(browser.localDescription.sdp)
                await bridge.close()
            finally:
                await browser.close()

    async def test_offer_is_bounded_and_not_a_browser_authority_channel(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-browser-invalid.") as directory:
            bridge = BrowserWebRtcBridge(Path(directory), fps=60)
            bridge.start_taps()
            with self.assertRaises(BridgeError):
                await bridge.answer_offer("x\x00y")
            with self.assertRaises(BridgeError):
                await bridge.answer_offer("x" * (128 * 1024 + 1))
            await bridge.close()

    async def test_h264_capability_is_checked_before_a_session_is_consumed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-browser-codec.") as directory:
            incompatible = RTCPeerConnection()
            video_only = RTCPeerConnection()
            compatible = RTCPeerConnection()
            bridge = BrowserWebRtcBridge(Path(directory), fps=60)
            bridge.start_taps()
            try:
                video = incompatible.addTransceiver("video", direction="recvonly")
                vp8 = [codec for codec in RTCRtpSender.getCapabilities("video").codecs
                       if codec.mimeType.lower() == "video/vp8"]
                self.assertTrue(vp8)
                video.setCodecPreferences(vp8)
                rejected_offer = await incompatible.createOffer()
                await incompatible.setLocalDescription(rejected_offer)
                with self.assertRaisesRegex(BridgeError, "does not offer WebRTC H.264"):
                    await bridge.answer_offer(incompatible.localDescription.sdp)

                video_only.addTransceiver("video", direction="recvonly")
                no_audio_offer = await video_only.createOffer()
                await video_only.setLocalDescription(no_audio_offer)
                with self.assertRaisesRegex(BridgeError, "does not offer WebRTC Opus"):
                    await bridge.answer_offer(video_only.localDescription.sdp)

                compatible.addTransceiver("video", direction="recvonly")
                compatible.addTransceiver("audio", direction="recvonly")
                accepted_offer = await compatible.createOffer()
                await compatible.setLocalDescription(accepted_offer)
                answer = await bridge.answer_offer(compatible.localDescription.sdp)
                self.assertEqual(answer["type"], "answer")
                await compatible.setRemoteDescription(RTCSessionDescription(**answer))
            finally:
                await bridge.close()
                await incompatible.close()
                await video_only.close()
                await compatible.close()


class PacketTrackTests(unittest.IsolatedAsyncioTestCase):
    async def test_packet_track_keeps_packets_encoded_and_uses_media_clock(self) -> None:
        track = _PacketTrack("video", maximum_queue=1)
        track.put_nowait(EncodedUnit(b"h264", True, 3000))
        packet = await track.recv()
        self.assertEqual(bytes(packet), b"h264")
        self.assertEqual(packet.time_base, Fraction(1, 90000))
        self.assertTrue(packet.is_keyframe)
        track.end_nowait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
