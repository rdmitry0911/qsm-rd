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
        self.rate = AdaptiveVideoRate(20000, self.applied.append, clock=lambda: self.now)

    def rr(self, peer='a', loss=0., rtt=.08, sequence=None, sending_kbps=20000):
        state = self.rate._receivers.get(peer)
        seq = sequence if sequence is not None else ((state.sequence or 0) + 1000 if state else 1000)
        lost = (state.lost if state else 0) + int(1000 * loss)
        sent = (state.sent_bytes or 0) if state else 0
        elapsed = self.now - state.sent_time if state else 0
        sent += int(sending_kbps * 1000 / 8 * elapsed)
        self.rate.receiver_report(peer, SimpleNamespace(highest_sequence=seq, packets_lost=lost,
                                                       fraction_lost=int(loss * 256)), rtt, sent)

    def test_idle_desktop_with_low_remb_and_vpn_jitter_never_reconfigures(self):
        for t in range(180):
            self.now = t
            self.rate.receiver_estimate('a', 200000 + (t % 4) * 10000)
            self.rr(rtt=(.08, .111, .137, .112, .14)[t % 5], sending_kbps=64)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.rate.current_kbps, 20000)

    def test_busy_desktop_ignores_normal_rtt_jitter_and_one_large_spike(self):
        for t in range(180):
            self.now = t
            rtt = .4 if t == 22 else (.08, .111, .137, .112, .14)[t % 5]
            self.rr(rtt=rtt)
        self.assertEqual(self.applied, [])

    def test_sustained_large_queue_reduces_only_after_confirmation(self):
        self.rr()
        for t in (1, 2, 3):
            self.now = t
            self.rr(rtt=.3)
        self.assertFalse(self.applied)
        self.now = 4
        self.rr(rtt=.3)
        self.assertEqual(self.applied, [15000])
        # A repeated REMB cannot consume that same RR again.
        self.now = 5
        self.rate.receiver_estimate('a', 40000000)
        self.assertEqual(self.applied, [15000])

    def test_loss_decreases_fast_and_growth_waits_ten_healthy_seconds(self):
        self.rr()
        self.now = 2
        self.rr(loss=.2)
        self.assertEqual(self.rate.current_kbps, 10000)
        self.now = 3
        self.rate.receiver_estimate('a', 40000000)
        self.assertEqual(self.rate.current_kbps, 10000)
        self.rr()
        for t in range(4, 12):
            self.now = t
            self.rr()
        self.assertEqual(self.rate.current_kbps, 10000)
        self.now = 13
        self.rr()
        self.assertEqual(self.rate.current_kbps, 12500)

    def test_remb_is_a_ceiling_only_when_congestion_is_confirmed(self):
        first, second = _VideoPacer(20000), _VideoPacer(20000)
        self.rate.add('a', first.set_bitrate)
        self.rate.add('b', second.set_bitrate)
        self.rr()
        self.now = 2
        self.rate.receiver_estimate('a', 4000000)
        self.assertFalse(self.applied)
        self.rr(loss=.2)
        self.assertEqual(self.applied, [3072])
        self.assertEqual(first._rate, second._rate)
        self.assertEqual(first._rate, 3072 * 1000 / 8 * 1.25)
        for t in range(3, 180):
            self.now = t
            self.rate.receiver_estimate('a', 40000000)
            self.rr()
        self.assertEqual(self.rate.current_kbps, 20000)

    def test_static_source_does_not_probe_up_but_active_video_recovers(self):
        self.rr()
        self.now = 2
        self.rr(loss=.2)
        for t in range(3, 120):
            self.now = t
            self.rr(sending_kbps=64)
        self.assertEqual(self.applied, [10000])
        for t in range(120, 180):
            self.now = t
            self.rr(sending_kbps=18000)
        self.assertEqual(self.rate.current_kbps, 20000)

    def test_recovery_has_material_steps_at_most_once_per_ten_seconds(self):
        self.rr()
        self.now = 2
        self.rr(loss=.2)
        changes = []
        self.rate._apply = lambda rate: changes.append((self.now, rate))
        for t in range(3, 120):
            self.now = t
            self.rr()
        self.assertEqual(self.rate.current_kbps, 20000)
        self.assertLessEqual(len(changes), 4)
        self.assertTrue(all(b[0] - a[0] >= 10 for a, b in zip(changes, changes[1:])))

    def test_shared_slow_viewer_and_removal(self):
        self.rr('fast')
        self.rr('slow')
        self.now = 2
        self.rr('slow', loss=.2)
        self.assertEqual(self.rate.current_kbps, 10000)
        self.now = 3
        self.rate.remove('slow')
        for t in range(3, 30):
            self.now = t
            self.rr('fast')
        self.assertGreater(self.rate.current_kbps, 10000)
        self.assertNotIn('slow', self.rate._receivers)

    def test_duplicate_reports_and_silent_peers_do_not_drive_recovery(self):
        self.rr()
        self.now = 2
        self.rr(loss=.2)
        self.now = 4
        self.rr(loss=.2, sequence=2000)
        self.assertEqual(self.rate.current_kbps, 10000)
        self.now = 100
        self.rate.receiver_estimate('a', 80000000)
        self.assertEqual(self.rate.current_kbps, 10000)

    def test_floor_fixed_mode_and_worker_failure(self):
        self.rr()
        self.now = 3
        self.rate.receiver_estimate('a', 1)
        self.rr(loss=.2)
        self.assertEqual(self.rate.current_kbps, 500)
        fixed = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now, enabled=False)
        self.now = 103
        fixed.receiver_report('a', SimpleNamespace(highest_sequence=1000, packets_lost=500,
                                                  fraction_lost=128), .3, 1000000)
        self.assertEqual(fixed.current_kbps, 8000)
        def failed(rate):
            raise BridgeError('gone')
        self.rate._apply = failed
        self.now = 106
        self.rr()
        self.assertEqual(self.rate.current_kbps, 500)

    def test_hardware_low_rate_headroom_does_not_change_encoder_target(self):
        gpu = _VideoPacer(8000, hardware=True)
        self.assertEqual(gpu._rate, _VideoPacer(8000)._rate)
        gpu.set_bitrate(2000)
        self.assertEqual(gpu._rate, 2600 * 1000 / 8 * 1.25)
        self.assertGreater(gpu._rate, 2.58e6 / 8)
        gpu.set_bitrate(500)
        self.assertGreater(gpu._rate, 1.32e6 / 8)


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
            self.assertFalse(applied)
            report=RtcpReceiverInfo(ssrc=123,fraction_lost=51,packets_lost=200,highest_sequence=1000,jitter=0,lsr=0,dlsr=0)
            await sender._handle_rtcp_packet(RtcpRrPacket(ssrc=1,reports=[report]))
            self.assertEqual(applied,[1472])
            self.assertIn(bridge,rate._receivers)
            await bridge.close()
            self.assertNotIn(bridge,rate._receivers)
            self.assertNotIn(bridge,rate._listeners)

if __name__=='__main__':unittest.main()
