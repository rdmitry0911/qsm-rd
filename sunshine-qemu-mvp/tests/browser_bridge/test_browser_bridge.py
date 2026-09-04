#!/usr/bin/env python3
"""Local protocol tests for the encoded GameStream-to-WebRTC bridge."""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path

from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.codecs.h264 import H264Encoder

from extensions.browser_bridge.qsm_browser_bridge import (
    AUDIO_TIME_BASE,
    BrowserWebRtcBridge,
    BridgeError,
    EncodedUnit,
    INPUT_HEADER,
    INPUT_KEYBOARD,
    INPUT_MAGIC,
    INPUT_MOUSE_BUTTON,
    INPUT_MOUSE_POSITION,
    INPUT_RESIZE,
    INPUT_SCROLL,
    INPUT_VERSION,
    PACKET_AUDIO,
    PACKET_CONFIG,
    PACKET_END,
    PACKET_FIRST,
    PACKET_HEADER,
    PACKET_IDR,
    PACKET_MAGIC,
    SharedMediaIngress,
    VIDEO_TIME_BASE,
    _AudioAssembler,
    _PacketTrack,
    _VideoAssembler,
)


class BrowserBridgeAssemblerTests(unittest.TestCase):
    def test_guest_file_manifest_is_a_narrow_browser_command(self) -> None:
        self.assertEqual(BrowserWebRtcBridge._guest_request(
            '{"op":"qsm_guest_file_list","request_id":"files-1","area":"outgoing"}'),
            ("files-1", {"op": "file_list", "area": "outgoing"}),
        )

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
            '{"op":"mouse_button","button":6,"down":true}',
            '{"op":"keyboard","key":30,"down":1,"modifiers":0}',
            '{"op":"scroll","vertical":0,"horizontal":0,"extra":1}',
            '{"op":"resize","width":1919,"height":1080,"fps":60}',
            '{"op":"unknown"}',
        ):
            with self.assertRaises(BridgeError):
                UnixInputEgress.encode_browser_message(raw)


class BrowserBridgeAsyncTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_guest_files_are_chunked_below_browser_sctp_message_limits(self) -> None:
        calls: list[dict[str, object]] = []
        replies: list[dict[str, object]] = []
        download = bytes(index % 251 for index in range(100_003))

        def guest_dispatch(request: dict[str, object]) -> dict[str, object]:
            calls.append(request)
            if request["op"] == "file_upload":
                return {"name": request["name"], "bytes": len(base64.b64decode(request["data_b64"]))}
            return {
                "name": "guest.bin", "bytes": len(download),
                "data_b64": base64.b64encode(download).decode("ascii"),
            }

        with tempfile.TemporaryDirectory(prefix="qsm-browser-guest-chunks.") as directory:
            bridge = BrowserWebRtcBridge(Path(directory), fps=60, guest_dispatch=guest_dispatch)
            channel = object()
            bridge._control_channel = channel
            bridge._send_control = replies.append  # type: ignore[method-assign]
            upload = bytes(index % 241 for index in range(100_001))
            request_id = "upload-1"
            try:
                for offset in range(0, len(upload), 32 * 1024):
                    chunk = upload[offset:offset + 32 * 1024]
                    bridge._accept_guest_upload_chunk(channel, request_id, {
                        "op": "file_upload_chunk", "transfer_id": "transfer-1", "name": "client.bin",
                        "size": len(upload), "offset": offset,
                        "data_b64": base64.b64encode(chunk).decode("ascii"),
                    })
                deadline = time.monotonic() + 2
                while not calls and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertEqual(len(calls), 1)
                self.assertEqual(base64.b64decode(calls[0]["data_b64"]), upload)
                self.assertEqual(replies.pop(), {
                    "op": "qsm_guest_result", "request_id": request_id, "ok": True,
                    "result": {"name": "client.bin", "bytes": len(upload)},
                })

                await bridge._dispatch_guest_request(channel, "download-1", {"op": "file_download"})
                self.assertGreater(len(replies), 1)
                self.assertTrue(all(reply["op"] == "qsm_guest_file_download_chunk" for reply in replies))
                self.assertTrue(all(len(reply["data_b64"]) < 64 * 1024 for reply in replies))
                restored = b"".join(base64.b64decode(reply["data_b64"]) for reply in replies)
                self.assertEqual(restored, download)
            finally:
                await bridge.close()

    async def test_offer_answer_is_h264_opus_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-browser-sdp.") as directory:
            browser = RTCPeerConnection()
            browser.addTransceiver("video", direction="recvonly")
            browser.addTransceiver("audio", direction="recvonly")
            try:
                offer = await browser.createOffer()
                await browser.setLocalDescription(offer)
                bridge = BrowserWebRtcBridge(Path(directory), fps=60)
                bridge.start_taps()
                answer = await bridge.answer_offer(browser.localDescription.sdp)
                self.assertEqual(answer["type"], "answer")
                self.assertIn("H264/90000", answer["sdp"])
                self.assertIn("opus/48000", answer["sdp"])
                await browser.setRemoteDescription(RTCSessionDescription(**answer))
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
