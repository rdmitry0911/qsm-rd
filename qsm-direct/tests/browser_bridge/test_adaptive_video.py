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
    INPUT_HEADER, INPUT_MAGIC, INPUT_VERSION, INPUT_VIDEO_BITRATE, _VideoPacer,
    _VideoPresentation)


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

    def raw_rr(self, sequence, lost, fraction=0):
        self.rate.receiver_report('a', SimpleNamespace(highest_sequence=sequence,
            packets_lost=lost, fraction_lost=fraction), .08, int(self.now * 2500000))

    def test_isolated_short_report_loss_never_changes_static_quality(self):
        self.raw_rr(20000, 0)
        sequence, lost = 20000, 0
        for t in range(1, 181):
            self.now = t
            # The observed NVENC HEVC regression: 1/20 = 5%; also 2/8 = 25%.
            count, missing = ((20, 1) if t % 30 == 1 else
                              (8, 2) if t % 30 == 2 else (1000, 0))
            sequence += count
            lost += missing
            self.raw_rr(sequence, lost, int(missing / count * 256))
        self.assertEqual(self.applied, [])

    def test_first_report_small_loss_and_late_corrections_do_not_reduce(self):
        self.now = 2
        self.raw_rr(53000, 2, 64)
        self.now = 3
        self.raw_rr(53008, 0)
        self.now = 4
        self.raw_rr(53028, 1, 12)
        self.assertEqual(self.applied, [])

    def test_repeated_moderate_loss_needs_time_and_volume(self):
        self.raw_rr(20000, 0)
        self.now = 1
        self.raw_rr(20100, 5, 12)
        self.assertFalse(self.applied)
        self.now = 2
        self.raw_rr(20200, 10, 12)
        self.assertEqual(self.applied, [15000])
        self.now = 3
        self.raw_rr(20300, 10)
        self.assertEqual(self.applied, [15000])

    def test_old_isolated_loss_expires_instead_of_accumulating_forever(self):
        self.raw_rr(20000, 0)
        for n in range(1, 30):
            self.now = n * 4
            self.raw_rr(20000 + n * 128, n * 4, 8)
        self.assertEqual(self.applied, [])

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

    def test_clean_reports_do_not_probe_above_fresh_reduced_path_estimate(self):
        self.rr()
        self.now = 2
        self.rate.receiver_estimate('a', 4000000)
        self.rr(loss=.2)
        self.assertEqual(self.rate.current_kbps, 3072)
        for t in range(3, 120):
            self.now = t
            self.rate.receiver_estimate('a', 4000000)
            self.rr(sending_kbps=3500)
        self.assertEqual(self.rate.current_kbps, 3600)
        # Real capacity returning permits recovery; the lower estimate is
        # not a permanent cap after the link improves.
        for t in range(120, 240):
            self.now = t
            self.rate.receiver_estimate('a', 40000000)
            self.rr()
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

    def test_low_rate_pacing_does_not_add_hardware_overshoot_credit(self):
        gpu = _VideoPacer(8000)
        self.assertEqual(gpu._rate, _VideoPacer(8000)._rate)
        gpu.set_bitrate(2000)
        self.assertEqual(gpu._rate, 2000 * 1000 / 8 * 1.25)
        gpu.set_bitrate(500)
        self.assertEqual(gpu._rate, 500 * 1000 / 8 * 1.25)

    def test_presentation_detects_tcp_queue_while_rtcp_rtt_stays_low(self):
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)
        total = 0
        for n in range(1, 11):
            self.now = n / 10
            total += 50000
            self.rr(rtt=.02, sending_kbps=4000)
            self.rate.presentation_report('a', self.now - .06, total)
        sent_at = self.now - .06
        for n in range(11, 23):
            self.now = n / 10
            sent_at += .04
            total += 25000
            self.rr(rtt=.02, sending_kbps=4000)
            self.rate.presentation_report('a', sent_at, total)
        self.assertEqual(len(self.applied), 1)
        self.assertGreaterEqual(self.rate.current_kbps, 1500)
        self.assertLessEqual(self.rate.current_kbps, 3000)
        self.assertIsNone(self.rate._receivers['a'].queue_since)

    def test_active_low_age_presentation_recovers_despite_app_limited_remb(self):
        self.rr()
        self.now = 2
        self.rr(loss=.2)
        total = 0
        for n in range(30, 700):
            self.now = n / 10
            current = self.rate.current_kbps
            total += int(current * .55 * 1000 / 8 / 10)
            self.rate.presentation_report('a', self.now - .06, total)
            self.rate.receiver_estimate('a', int(current * .65 * 1000))
            if n % 10 == 0:
                self.rr(sending_kbps=current * .55)
        self.assertEqual(self.rate.current_kbps, 20000)

    def test_received_feedback_ignores_display_delay_and_falls_back_with_a_fresh_baseline(self):
        self.now = 1.
        self.rate.received_report('a', .98, 1000)
        state = self.rate._receivers['a']
        self.rate.presentation_report('a', .6, 800)
        self.assertEqual(state.feedback_kind,'received')
        self.assertAlmostEqual(state.presented_delay,.02)
        self.assertEqual(len(state.presented_samples),1)
        self.now = 3.
        self.rate.presentation_report('a',2.88,2000)
        self.assertEqual(state.feedback_kind,'presented')
        self.assertAlmostEqual(state.presented_base,.12)
        self.assertEqual(len(state.presented_samples),1)
        self.rate.received_report('a',2.97,2200)
        self.assertEqual(state.feedback_kind,'received')
        self.assertAlmostEqual(state.presented_base,.03)
        self.assertEqual(len(state.presented_samples),1)
        received_at = state.received_time
        self.now += .1
        self.rate.received_report('a',2.97,2200)
        self.assertEqual(state.received_time,received_at,'replay cannot renew receipt preference')

    def test_recent_received_delivery_limits_the_first_congestion_reduction(self):
        self.rate = AdaptiveVideoRate(8000,self.applied.append,clock=lambda:self.now)
        total = 0
        for n in range(1,21):
            self.now = n*.05;total += 30000
            self.rate.received_report('a',self.now-.03,total)
        sent_at = self.now-.03
        for n in range(1,5):
            self.now = 1.+n*.05;sent_at += 1/60;total += 10000
            self.rate.received_report('a',sent_at,total)
            self.rate.presentation_backpressure('a',.025)
        self.assertEqual(len(self.applied),1)
        self.assertLessEqual(self.rate.current_kbps,2000)
        self.assertGreaterEqual(self.rate.current_kbps,1000)
        self.assertLess(self.now-1.,.25,'avoid averaging the pre-drop fast path into the first cut')

    def test_idle_presentation_and_short_display_stall_do_not_change_quality(self):
        total = 0
        for n in range(1, 1800):
            self.now = n / 10
            total += 800
            # One delayed callback, then an ordinary fresh picture.
            age = .35 if n == 205 else .06
            self.rate.presentation_report('a', self.now - age, total)
            if n % 10 == 0:
                self.rr(sending_kbps=64)
                self.rate.receiver_estimate('a', 200000)
        self.assertEqual(self.applied, [])
        state = self.rate._receivers['a']
        self.assertLessEqual(len(state.presented_delays), 600)
        self.assertLessEqual(len(state.presented_samples), 128)

    def test_old_high_demand_rr_does_not_drive_idle_presentation_probes(self):
        self.rr()
        self.now = 2
        self.rr(loss=.2)
        for n in range(30, 1200):
            self.now = n / 10
            self.rate.presentation_report('a', self.now - .06, n * 800)
        self.assertEqual(self.applied, [10000])

    def test_credit_backpressure_reduces_even_when_pacing_hides_queue_growth(self):
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)
        total = 0
        for n in range(1, 11):
            self.now = n / 10
            total += 25000
            self.rate.presentation_report('a', self.now - .06, total)
        for n in range(11, 16):
            self.now = n / 10
            total += 25000
            self.rate.presentation_report('a', self.now - .25, total)
            self.rate.presentation_backpressure('a', .08)
        self.assertEqual(len(self.applied), 1)
        self.assertGreaterEqual(self.rate.current_kbps, 500)
        self.assertLessEqual(self.rate.current_kbps, 3000)

    def test_short_credit_wait_and_slower_delivery_act_before_raw_fifo_overflows(self):
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)
        total = 0
        for n in range(1, 11):
            self.now = n / 10
            total += 50000
            self.rate.presentation_report('a', self.now - .06, total)
        sent_at = self.now - .06
        for n in range(11, 15):
            self.now = n / 10
            sent_at += .04
            total += 25000
            self.rate.presentation_report('a', sent_at, total)
        state = self.rate._receivers['a']
        self.assertLess(self.now - state.presented_queue_since, .3)
        self.assertEqual(self.applied, [])
        self.rate.presentation_backpressure('a', .025)
        self.assertLess(self.rate.current_kbps, 8000)

    def test_fast_presenting_viewer_cannot_probe_while_other_viewer_is_delayed(self):
        self.rr()
        self.now = 2
        self.rr(loss=.2)
        total = 0
        for n in range(30, 500):
            self.now = n / 10
            total += int(self.rate.current_kbps * .55 * 1000 / 8 / 10)
            self.rate.presentation_report('a', self.now - .06, total)
            age = .06 if n < 100 else .4
            self.rate.presentation_report('slow', self.now - age, total)
        self.assertTrue(all(b <= a for a, b in zip(self.applied, self.applied[1:])))
        held = self.rate.current_kbps
        for n in range(500, 900):
            self.now = n / 10
            total += int(self.rate.current_kbps * .55 * 1000 / 8 / 10)
            for peer in ('a', 'slow'):
                self.rate.presentation_report(peer, self.now - .06, total)
        self.assertGreater(self.rate.current_kbps, held)


class PresentationHistoryTests(unittest.TestCase):
    def test_known_completed_frames_only_with_wrap_duplicates_and_bounded_age(self):
        now = [1.]
        history = _VideoPresentation(lambda: now[0])
        history.sent(0xffffffff, 10000)
        now[0] = 1.05
        history.sent(0, 12000)
        now[0] = 1.1
        self.assertEqual(history.acknowledge(0), (1.05, 12000))
        self.assertIsNone(history.acknowledge(0xffffffff))
        self.assertIsNone(history.acknowledge(0))
        self.assertIsNone(history.acknowledge(123))
        history.sent(1, 13000)
        now[0] = 10
        self.assertIsNone(history.acknowledge(1))
        for rtp in range(2, 1002):
            history.sent(rtp, 13000 + rtp)
        self.assertEqual(len(history._frames), 512)
        self.assertIsNone(history.acknowledge(2))
        self.assertEqual(history.acknowledge(1001), (10, 14001))


class PresentationCreditTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_releases_frame_admission_and_absent_feedback_never_waits(self):
        history = _VideoPresentation()
        await history.wait_for_credit(lambda: 1000000, lambda: None)
        self.assertEqual(history.credit_waits, 0)
        now = __import__('time').monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.08, presented_delay=.4,
                                presented_kbps=2000, presented_offered_kbps=4000)
        history.sent(10, 100000)
        pending = asyncio.create_task(history.wait_for_credit(lambda: 100000, lambda: state))
        await asyncio.sleep(.01)
        self.assertFalse(pending.done())
        self.assertEqual(history.credit_waits, 1)
        history.acknowledge(10)
        await asyncio.wait_for(pending, .05)
        await history.wait_for_credit(lambda: 110000, lambda: state)
        self.assertEqual(history.credit_waits, 1)

    async def test_missing_ack_has_bounded_wait_for_loss_repair(self):
        history = _VideoPresentation()
        now = __import__('time').monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.08, presented_delay=.4,
                                presented_kbps=2000, presented_offered_kbps=4000)
        await asyncio.wait_for(history.wait_for_credit(lambda: 100000, lambda: state), .07)
        self.assertEqual(history.credit_waits, 1)

    async def test_starting_video_after_idle_is_not_capped_by_old_delivery_rate(self):
        history = _VideoPresentation()
        state = SimpleNamespace(presented_time=__import__('time').monotonic(),
                                presented_base=.08, presented_delay=.1,
                                presented_kbps=64, presented_offered_kbps=64)
        await asyncio.wait_for(history.wait_for_credit(lambda: 1000000, lambda: state), .03)
        self.assertEqual(history.credit_waits, 0)

    async def test_one_low_startup_delay_does_not_turn_normal_playout_into_congestion(self):
        now = [0.]
        rate = AdaptiveVideoRate(8000, lambda _: None, clock=lambda: now[0])
        history = _VideoPresentation(lambda: now[0])
        rate.presentation_report('a', -.03, 0)
        for n in range(1, 201):
            now[0] = n / 10
            age = .15 if n % 5 == 0 else .12
            rate.presentation_report('a', now[0] - age, n * 50000)
        state = rate._receivers['a']
        self.assertGreater(state.presented_base, .11)
        await asyncio.wait_for(history.wait_for_credit(lambda: 10000000, lambda: state), .03)
        self.assertEqual(history.credit_waits, 0)


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
