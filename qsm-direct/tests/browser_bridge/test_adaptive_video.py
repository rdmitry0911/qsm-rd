#!/usr/bin/env python3
"""Deterministic controller and real private-worker protocol checks."""
import asyncio
import json
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiortc.rtp import (RTCP_PSFB_APP, RtcpPsfbPacket, RtcpRrPacket,
                        RtcpReceiverInfo, pack_remb_fci)
from extensions.browser_bridge.qsm_browser_bridge import (
    AdaptiveVideoRate, BrowserWebRtcBridge, BridgeError, UnixInputEgress,
    INPUT_HEADER, INPUT_MAGIC, INPUT_VERSION, INPUT_VIDEO_BITRATE, _VideoPacer)


class RateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.applied = []
        self.rate = AdaptiveVideoRate(20000, self.applied.append, clock=lambda:self.now)

    def rr(self, peer='a', loss=0., rtt=.08, sequence=None):
        state = self.rate._receivers.get(peer)
        seq = sequence if sequence is not None else (state.sequence or 0) + 1000 if state else 1000
        lost = (state.lost if state else 0) + int(1000 * loss)
        self.rate.receiver_report(peer, SimpleNamespace(highest_sequence=seq, packets_lost=lost,
                                                       fraction_lost=int(loss * 256)), rtt)

    def test_remb_drops_both_encoder_and_all_pacers_and_recovers_to_ceiling(self):
        first, second = _VideoPacer(20000), _VideoPacer(20000)
        self.rate.add('a', first.set_bitrate)
        self.rate.add('b', second.set_bitrate)
        self.now=2
        self.rate.receiver_estimate('a', 4_000_000)
        self.assertEqual(self.rate.current_kbps, 3072)
        self.assertEqual(self.applied, [3072])
        self.assertEqual(first._rate, second._rate)
        self.assertEqual(first._rate, 3072*1000/8*1.25)
        for t in range(3,90):
            self.now=t
            self.rate.receiver_estimate('a', 40_000_000)
            self.rr()
        self.assertEqual(self.rate.current_kbps, 20000)
        self.assertTrue(all(500 <= v <= 20000 for v in self.applied))

    def test_loss_decreases_fast_but_healthy_growth_waits(self):
        self.rr()
        self.now=2;self.rr(loss=.2)
        self.assertEqual(self.rate.current_kbps,10000)
        self.now=2.1;self.rr(loss=.2)
        self.assertEqual(self.rate.current_kbps,10000)
        self.now=3;self.rr()
        self.assertEqual(self.rate.current_kbps,10000)
        self.now=6;self.rr()
        self.assertEqual(self.rate.current_kbps,10800)

    def test_rtt_inflation_reduces_without_loss(self):
        self.rr(rtt=.08)
        self.now=2;self.rr(rtt=.3)
        self.assertEqual(self.rate.current_kbps,15000)

    def test_slowest_receiver_limits_shared_stream_until_removed(self):
        self.now=2;self.rate.receiver_estimate('slow',4_000_000)
        self.now=4;self.rate.receiver_estimate('fast',80_000_000)
        self.assertEqual(self.rate.current_kbps,3072)
        self.rate.remove('slow')
        self.now=6;self.rate.receiver_estimate('fast',80_000_000)
        self.assertGreater(self.rate.current_kbps,3072)

    def test_stale_estimate_expires_and_duplicate_report_cannot_reduce_twice(self):
        self.rr()
        self.now=2;self.rr(loss=.2)
        self.now=4;self.rr(loss=.2,sequence=2000)
        self.assertEqual(self.rate.current_kbps,10000)
        self.now=5;self.rate.receiver_estimate('slow',1_000_000)
        self.assertEqual(self.rate.current_kbps,672)
        for t in range(6,22):
            self.now=t;self.rr()
        self.assertGreater(self.rate.current_kbps,672)

    def test_unchanged_app_limited_remb_does_not_lock_low_rate_forever(self):
        self.now=2;self.rate.receiver_estimate('a',4_000_000)
        initial=self.rate.current_kbps
        for t in range(3,40):
            self.now=t;self.rate.receiver_estimate('a',4_000_000);self.rr()
        self.assertGreater(self.rate.current_kbps,initial*2)

    def test_rising_app_limited_estimate_cannot_reverse_rate_recovery(self):
        self.now=2;self.rate.receiver_estimate('a',4_000_000)
        minimum=self.rate.current_kbps
        previous=minimum
        estimate=4_000_000
        for t in range(6,50):
            self.now=t
            estimate=int(estimate*1.01)
            self.rate.receiver_estimate('a',estimate)
            self.rr()
            self.assertGreaterEqual(self.rate.current_kbps,previous)
            previous=self.rate.current_kbps
        self.assertGreater(self.rate.current_kbps,minimum*2)

    def test_nvenc_low_rate_headroom_does_not_change_encoder_target(self):
        gpu=_VideoPacer(8000,nvenc=True)
        self.assertEqual(gpu._rate,_VideoPacer(8000)._rate)
        gpu.set_bitrate(2000)
        self.assertEqual(gpu._rate,2600*1000/8*1.25)
        self.assertGreater(gpu._rate,2.58e6/8) # measured 2-Mbps NVENC output
        gpu.set_bitrate(500)
        self.assertGreater(gpu._rate,1.32e6/8)

    def test_floor_missing_feedback_fixed_mode_and_failed_worker(self):
        self.now=3;self.rate.receiver_estimate('a',1)
        self.assertEqual(self.rate.current_kbps,500)
        self.now=100
        self.assertEqual(self.rate.current_kbps,500) # no timer grows blindly
        fixed=AdaptiveVideoRate(8000,self.applied.append,clock=lambda:self.now,enabled=False)
        self.now=103;fixed.receiver_estimate('a',1000)
        self.assertEqual(fixed.current_kbps,8000)
        def failed(rate):raise BridgeError('gone')
        self.rate._apply=failed
        self.now=106;self.rate.receiver_estimate('a',80_000_000)
        self.assertEqual(self.rate.current_kbps,500)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_status_uses_latest_state_channel_with_increasing_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge=BrowserWebRtcBridge(Path(directory))
            reliable,latest=[],[]
            bridge._control_channel=SimpleNamespace(readyState='open',send=reliable.append)
            bridge._cursor_channel=SimpleNamespace(readyState='open',send=latest.append)
            try:
                bridge._video_rate.current_kbps=3500
                bridge._video_rate_changed(3500)
                bridge._publish_video_rate()
                self.assertFalse(reliable)
                messages=[json.loads(m) for m in latest]
                self.assertEqual([m['bitrate'] for m in messages],[3500,3500])
                self.assertLess(messages[0]['sequence'],messages[1]['sequence'])
            finally:await bridge.close()

    async def test_private_opcode_validated_and_browser_cannot_inject_it(self):
        with tempfile.TemporaryDirectory() as directory:
            egress=UnixInputEgress(Path(directory))
            a,b=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
            egress._connection=a
            try:
                egress.set_video_bitrate(3500)
                self.assertEqual(b.recv(100),INPUT_HEADER.pack(INPUT_MAGIC,INPUT_VERSION,INPUT_VIDEO_BITRATE,4)+struct.pack('!I',3500))
                for value in (True,499,100001,'8000'):
                    with self.assertRaises(BridgeError):egress.set_video_bitrate(value)
                with self.assertRaises(BridgeError):
                    egress.encode_browser_message(json.dumps({'op':'video_bitrate','bitrate':500}))
            finally:egress.close();b.close()

    async def test_rtcp_selects_video_ssrc_and_releases_shared_viewer(self):
        now=[0.]
        applied=[]
        rate=AdaptiveVideoRate(8000,applied.append,clock=lambda:now[0])
        with tempfile.TemporaryDirectory() as directory:
            bridge=BrowserWebRtcBridge(Path(directory),video_rate=rate)
            sender=SimpleNamespace(_ssrc=123,_handle_rtcp_packet=AsyncMock(),getStats=AsyncMock(return_value={}))
            bridge._attach_video_recovery(sender)
            now[0]=2
            await sender._handle_rtcp_packet(RtcpPsfbPacket(fmt=RTCP_PSFB_APP,ssrc=1,media_ssrc=0,fci=pack_remb_fci(2_000_000,[456])))
            self.assertFalse(applied)
            await sender._handle_rtcp_packet(RtcpPsfbPacket(fmt=RTCP_PSFB_APP,ssrc=1,media_ssrc=0,fci=pack_remb_fci(2_000_000,[123])))
            self.assertEqual(applied,[1472])
            report=RtcpReceiverInfo(ssrc=123,fraction_lost=0,packets_lost=0,highest_sequence=1000,jitter=0,lsr=0,dlsr=0)
            await sender._handle_rtcp_packet(RtcpRrPacket(ssrc=1,reports=[report]))
            self.assertIn(bridge,rate._receivers)
            await bridge.close()
            self.assertNotIn(bridge,rate._receivers)
            self.assertNotIn(bridge,rate._listeners)

if __name__=='__main__':unittest.main()
