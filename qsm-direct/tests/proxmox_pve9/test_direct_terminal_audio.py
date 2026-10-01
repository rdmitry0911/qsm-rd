#!/usr/bin/env python3
"""Console sound and microphone plumbing of the terminal service and bridge."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
import unittest
from pathlib import Path

import av
from aiortc.mediastreams import MediaStreamError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_LIBRARY = os.environ.get("QSM_DIRECT_PACKAGE_LIBRARY")
IMPORT_ROOT = Path(PACKAGE_LIBRARY) if PACKAGE_LIBRARY else PROJECT_ROOT
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

if PACKAGE_LIBRARY:
    from browser_bridge.qsm_browser_bridge import BrowserWebRtcBridge
    from direct_terminal.qsm_direct_terminal import (PCM_DESKTOP_SOUND, PCM_FRAME_BYTES, PCM_MICROPHONE,
                                                     ContainerAudioRelay, MicrophoneRoute, PcmLink)
else:
    from extensions.browser_bridge.qsm_browser_bridge import BrowserWebRtcBridge
    from extensions.direct_terminal.qsm_direct_terminal import (PCM_DESKTOP_SOUND, PCM_FRAME_BYTES,
                                                                PCM_MICROPHONE, ContainerAudioRelay,
                                                                MicrophoneRoute, PcmLink)


class PcmLinkTests(unittest.TestCase):
    def test_records_carry_kind_and_pcm_and_a_full_socket_drops(self) -> None:
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        link = PcmLink(ours)
        link.send(PCM_MICROPHONE, b"\x01\x02\x03\x04")
        self.assertEqual(theirs.recv(100), bytes((PCM_MICROPHONE,)) + b"\x01\x02\x03\x04")
        for _ in range(2000):  # nobody reads: must neither block nor raise
            link.send(PCM_MICROPHONE, b"\x00" * PCM_FRAME_BYTES)
        theirs.close()
        link.send(PCM_MICROPHONE, b"\x00" * 4)  # a gone worker is not an error
        link.close()


class MicrophoneRouteTests(unittest.TestCase):
    def test_the_last_speaker_keeps_the_microphone_until_silent(self) -> None:
        delivered: list[bytes] = []
        route = MicrophoneRoute(delivered.append)
        alice, bob = object(), object()
        route(alice, b"a1")
        route(bob, b"b1")  # alice spoke a moment ago: bob waits
        route(alice, b"a2")
        self.assertEqual(delivered, [b"a1", b"a2"])
        route._last -= MicrophoneRoute.HANDOVER_SECONDS + 0.1  # alice fell silent
        route(bob, b"b2")
        route(alice, b"a3")
        self.assertEqual(delivered, [b"a1", b"a2", b"b2"])


class ContainerAudioRelayTests(unittest.TestCase):
    def test_session_sound_becomes_records_and_microphone_reaches_the_session(self) -> None:
        ours, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        agent_host, agent = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        connections = [agent_host]

        def connector(_timeout: float) -> socket.socket:
            if not connections:
                raise OSError("no session")
            return connections.pop()

        relay = ContainerAudioRelay(connector, PcmLink(ours))
        try:
            agent.sendall(b"\x11" * (PCM_FRAME_BYTES + 100))  # 1 record + a remainder
            worker.settimeout(3)
            record = worker.recv(PCM_FRAME_BYTES + 10)
            self.assertEqual(record, bytes((PCM_DESKTOP_SOUND,)) + b"\x11" * PCM_FRAME_BYTES)
            deadline = time.monotonic() + 3
            while relay._connection is None and time.monotonic() < deadline:
                time.sleep(0.01)
            relay.microphone(b"\x22" * 8)
            agent.settimeout(3)
            self.assertEqual(agent.recv(100), b"\x22" * 8)
        finally:
            relay.close()
            agent.close()
            worker.close()


class _Track:
    kind = "audio"

    def __init__(self, frames: list[av.AudioFrame]) -> None:
        self._frames = frames

    async def recv(self) -> av.AudioFrame:
        if not self._frames:
            raise MediaStreamError
        return self._frames.pop(0)


def _frame(layout: str, rate: int, samples: int) -> av.AudioFrame:
    frame = av.AudioFrame(format="s16", layout=layout, samples=samples)
    frame.sample_rate = rate
    for plane in frame.planes:
        plane.update(b"\x10\x00" * (plane.buffer_size // 2))
    return frame


class BridgeMicrophoneTests(unittest.TestCase):
    def test_decoded_frames_reach_the_callback_as_48k_stereo(self) -> None:
        received: list[bytes] = []

        async def scenario() -> None:
            bridge = BrowserWebRtcBridge.__new__(BrowserWebRtcBridge)
            bridge._closed = False
            bridge._on_microphone = lambda _peer, data: received.append(data)
            await bridge._pump_microphone(_Track([_frame("stereo", 48000, 960),
                                                  _frame("mono", 44100, 4410)]))
        asyncio.run(scenario())
        self.assertEqual(len(received[0]), 960 * 4)
        self.assertTrue(all(len(chunk) % 4 == 0 for chunk in received))
        resampled = sum(len(chunk) for chunk in received[1:]) // 4
        self.assertGreater(resampled, 4200)  # ~100 ms at 48 kHz, minus resampler delay
        self.assertLessEqual(resampled, 4800)


if __name__ == "__main__":
    unittest.main()
