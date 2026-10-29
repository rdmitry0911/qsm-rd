#!/usr/bin/env python3
"""Deterministic controller and real private-worker protocol checks."""
import asyncio
import json
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiortc.rtp import (RTCP_PSFB_APP, RtcpPsfbPacket, RtcpRrPacket,
                        RtcpReceiverInfo, pack_remb_fci)
from extensions.browser_bridge.qsm_browser_bridge import (
    AdaptiveVideoRate, BrowserWebRtcBridge, BridgeError, UnixInputEgress,
    INPUT_HEADER, INPUT_MAGIC, INPUT_VERSION, INPUT_VIDEO_BITRATE, _VideoPacer,
    _VideoPresentation, INPUT_VIDEO_BACKPRESSURE, EncodedUnit)
from extensions.browser_bridge.qsm_transport_feedback import DeliveryEstimator


class PacketDeliveryTests(unittest.TestCase):
    def test_early_packet_pressure_precedes_confirmed_cut_and_stays_clear_on_a_fast_link(self):
        estimator = DeliveryEstimator()
        for group in range(50):
            samples=tuple((n*.0012, 400+n*.0012+.02, 1200) for n in range(group*4, group*4+4))
            estimator.add(samples, samples[-1][1]-400+.02)
            self.assertFalse(estimator.pressure)
        hints=[];confirmed=[];last_arrival=samples[-1][1]
        for group in range(30):
            samples=[]
            for index in range(4):
                sent=(200+group*4+index)*.0012
                last_arrival=max(last_arrival,400+sent+.02)+.0048
                samples.append((sent,last_arrival,1200))
            estimator.add(tuple(samples), last_arrival-400+.02)
            if estimator.pressure:hints.append(estimator.updated)
            if estimator.queued:confirmed.append(estimator.updated)
        self.assertTrue(hints)
        self.assertTrue(confirmed)
        self.assertLess(hints[0], confirmed[0])

    def setUp(self):
        self.now = 0.
        self.applied = []
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)

    def model(self, duration, capacity, *, demand=.7, offset=400., repair=None, delayed_frames=False):
        """Serialized link, receiver clock and 50ms RTCP, without fake estimates."""
        from collections import deque
        pending = deque()
        send, previous_arrival, steps = 0., 0., []
        for tick in range(1, int(duration * 20) + 1):
            self.now = tick / 20
            while send < self.now:
                incoming = max(send + .02, previous_arrival) + 1200 * 8 / capacity(send)
                if repair is not None:
                    incoming = max(incoming, repair(send))
                pending.append((send, incoming + offset, 1200))
                previous_arrival = incoming
                send += 1200 * 8 / (self.rate.current_kbps * 1000 * demand)
            batch = []
            while pending and pending[0][1] - offset + .02 <= self.now:
                batch.append(pending.popleft())
            self.rate.transport_report('a', tuple(batch))
            if delayed_frames and tick > 20:
                # Frame-completion callbacks can lag even as actual packets
                # arrive promptly. They retain image fences, not capacity.
                self.rate.received_report('a', self.now - (.03 if tick < 40 else .4), tick * 5000)
                self.rate.presentation_backpressure('a', .05)
            state = self.rate._receivers.get('a')
            estimate = state.packet_delivery if state else None
            steps.append((self.now, self.rate.current_kbps,
                          estimate.queue_excess if estimate else 0.))
        return steps

    def test_real_capacity_step_and_full_active_recovery(self):
        steps = self.model(100, lambda t: 2_000_000 if 8 <= t < 25 else 20_000_000)
        narrow = [rate for t, rate, _ in steps if 8.2 <= t < 25]
        self.assertLess(min(narrow), 2500)
        self.assertGreaterEqual(min(narrow), 1400)
        self.assertLess(max(age for t, _, age in steps if 8 <= t < 10), .4)
        self.assertEqual(steps[-1][1], 8000)

    def test_short_tcp_repair_and_fast_drain_do_not_cut_capacity(self):
        self.model(12, lambda t: 20_000_000,
                   repair=lambda sent: 5.20 if 5 <= sent < 5.20 else 0.)
        self.assertEqual(self.applied, [])

    def test_short_tcp_throughput_dip_with_continuous_arrivals_does_not_cut(self):
        steps = self.model(12, lambda t: 4_000_000 if 5 <= t < 5.2 else 20_000_000)
        self.assertGreater(max(age for _, _, age in steps), .05)
        self.assertLess(max(age for _, _, age in steps), .1)
        self.assertEqual(self.applied, [])

    def test_packet_timing_overrides_delayed_complete_frame_callbacks(self):
        self.model(15, lambda t: 20_000_000, delayed_frames=True)
        self.assertEqual(self.applied, [])
        self.assertFalse(self.rate._presentation_queued(self.rate._receivers['a'], self.now))

    def test_receiver_clock_origin_does_not_change_control(self):
        first = self.model(12, lambda t: 2_000_000 if t >= 8 else 20_000_000)
        changes = self.applied[:]
        self.setUp()
        second = self.model(12, lambda t: 2_000_000 if t >= 8 else 20_000_000, offset=50000.)
        self.assertEqual(changes, self.applied)
        self.assertEqual([row[1] for row in first], [row[1] for row in second])

    def test_idle_packets_cannot_drive_recovery(self):
        self.rate.current_kbps = 2000
        self.model(20, lambda t: 20_000_000, demand=.02)
        self.assertEqual(self.applied, [])

    def test_duplicate_and_invalid_samples_do_not_renew_freshness(self):
        estimator = DeliveryEstimator()
        sample = ((0., 400., 1200),)
        self.assertTrue(estimator.add(sample, .05))
        self.assertFalse(estimator.add(sample, .5))
        for bad in (((0., float('nan'), 1200),), ((1., 401., -1),),
                    ((2., 402., 1200),), ((-9., 391., 1200),)):
            self.assertFalse(estimator.add(bad, 1.))
        self.assertEqual(estimator.updated, .05)
        self.assertFalse(estimator.fresh(1.6))
        self.assertFalse(estimator.add(sample * 4097, .5))

    def test_expired_packets_restore_legacy_frame_capacity_checks(self):
        self.model(2, lambda t: 20_000_000)
        self.assertTrue(self.rate._packet_fresh(self.rate._receivers['a'], self.now))
        for n in range(1, 31):
            self.now = 4 + n * .05
            sent = 4 + n * .02
            self.rate.received_report('a', sent, 1_000_000 + n * 8000)
            self.rate.presentation_backpressure('a', .05)
        self.assertFalse(self.rate._packet_fresh(self.rate._receivers['a'], self.now))
        self.assertLess(self.rate.current_kbps, 8000)

    def test_slow_legacy_viewer_still_limits_shared_producer(self):
        self.model(2, lambda t: 20_000_000)
        # Keep packet evidence fresh while the separate old viewer queues.
        for n in range(1, 21):
            self.now = 2 + n * .05
            self.rate.transport_report('a', ((self.now - .04, 400 + self.now - .02, 1200),))
            self.rate.presentation_report('old', 2 + n * .02, n * 8000)
            self.rate.presentation_backpressure('old', .05)
        self.assertLess(self.rate.current_kbps, 8000)


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
        self.assertEqual(first._rate, 3072 * 1000 / 8)
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
        self.assertEqual(gpu._rate, 2000 * 1000 / 8)
        gpu.set_bitrate(500)
        self.assertEqual(gpu._rate, 500 * 1000 / 8)

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

    def receipt_recovery(self, mixed=False, idle=False):
        self.rr()
        self.now = 2
        self.rr(loss=.2)
        changes = []
        self.rate._apply = lambda value: changes.append((self.now, value))
        total = 0
        for n in range(30, 500):
            self.now = n / 10
            total += int((64 if idle else self.rate.current_kbps * .55) * 1000 / 8 / 10)
            self.rate.received_report('a', self.now - .02, total)
            if mixed:
                self.rate.presentation_report('legacy', self.now - .12, total)
            if n % 10 == 0:
                self.rr(sending_kbps=64 if idle else self.rate.current_kbps * .55)
        return changes

    def test_receipts_recover_faster_after_first_healthy_interval(self):
        changes = self.receipt_recovery()
        self.assertEqual(self.rate.current_kbps, 20000)
        self.assertGreaterEqual(changes[0][0], 12)
        self.assertTrue(all(6 <= b[0] - a[0] < 7 for a, b in zip(changes, changes[1:])), changes)

    def test_mixed_legacy_viewer_retains_ten_second_probe_spacing(self):
        changes = self.receipt_recovery(mixed=True)
        self.assertTrue(all(b[0] - a[0] >= 10 for a, b in zip(changes, changes[1:])), changes)

    def test_idle_receipt_feedback_does_not_start_probing(self):
        self.assertEqual(self.receipt_recovery(idle=True), [])

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

    def test_drained_receipt_does_not_reuse_old_credit_waits_to_collapse_rate(self):
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)
        total = 0
        for n in range(1, 13):
            self.now = n * .05
            total += 10000
            self.rate.received_report('a', self.now - .02, total)
        # The sender waited while an old frame was still in flight. Before
        # the rate-change interval expires, a new receipt proves it drained.
        self.now = .9
        total += 10000
        self.rate.received_report('a', .7, total)
        for n in range(5):
            self.now = .9 + n * .023
            self.rate.presentation_backpressure('a', .025)
        self.assertEqual(self.applied, [])
        self.now = 1.01
        total += 1000
        self.rate.received_report('a', .99, total)
        self.assertEqual(self.applied, [], 'old waits cannot turn a drained, app-limited interval into congestion')
        self.assertEqual(self.rate.current_kbps, 8000)
        self.assertEqual(self.rate._receivers['a'].credit_samples, [])

    def test_shrinking_queue_does_not_trigger_another_bitrate_cut(self):
        self.rate = AdaptiveVideoRate(2034, self.applied.append, clock=lambda: self.now)
        total = 0
        for n in range(1, 11):
            self.now = n * .05
            total += 14000
            self.rate.received_report('a', self.now - .02, total)
        self.now = .8
        total += 10000
        self.rate.received_report('a', .49, total)
        for n in range(5):
            self.now = .8 + n * .03
            self.rate.presentation_backpressure('a', .025)
        # A real20-to-2M transition leaves older frames in flight. Their
        # delay falls310->300ms and delivery1846k exceeds offered1714k:
        # the existing queue is draining under the already lowered target.
        self.now = .93
        total += 30000
        self.rate.received_report('a', .63, total)
        self.now = 1.01
        self.rate.presentation_backpressure('a', .025)
        self.assertEqual(self.applied, [], 'draining flight is not a new capacity decrease')
        self.assertEqual(self.rate.current_kbps, 2034)
        # The exemption ends as soon as new samples establish growing age
        # and slower delivery; actual congestion must still reduce promptly.
        for now, sent in ((1.13, .67), (1.20, .70), (1.27, .73)):
            self.now = now
            total += 20000
            self.rate.received_report('a', sent, total)
        self.rate.presentation_backpressure('a', .03)
        self.assertLess(self.rate.current_kbps, 2034)

    def test_first_receipt_after_gap_does_not_reuse_old_interval_throughput(self):
        for n in range(1, 13):
            self.now = n * .05
            self.rate.received_report('a', self.now - .02, n * 10000)
        state = self.rate._receivers['a']
        self.assertGreater(state.presented_kbps, 0)
        self.now = 1.
        self.rate.received_report('a', .8, 121000)
        self.assertEqual(state.presented_kbps, 0)
        self.assertEqual(state.presented_offered_kbps, 0)
        self.assertIsNone(state.presented_queue_since)

    def test_first_receipt_after_loss_cannot_reuse_waits_as_capacity_evidence(self):
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)
        total = 0
        for n in range(1, 21):
            self.now = n * .05
            total += 10000
            self.rate.received_report('a', self.now - .02, total)
        # A TCP repair delays receipts. Waits while the last known frame was
        # healthy do not measure how much capacity remains after the repair.
        for now in (1.05, 1.11, 1.17):
            self.now = now
            self.rate.presentation_backpressure('a', .05)
        self.now = 1.29
        total += 15000
        self.rate.received_report('a', 1.02, total)
        self.assertEqual(self.rate._receivers['a'].presented_bytes, 0)
        self.assertEqual(self.applied, [], 'the first delayed receipt has no fresh delivery pair')
        self.now = 1.31
        total += 15000
        self.rate.received_report('a', 1.28, total)
        self.assertEqual(self.applied, [], 'the following low-age frame proves that flight drained')
        # A subsequent real capacity decrease still acts on sufficient new
        # bytes and independently slower delivery, rather than waiting forever.
        for now, sent in ((1.50, 1.30), (1.57, 1.33), (1.64, 1.35)):
            self.now = now
            total += 20000
            self.rate.received_report('a', sent, total)
            self.rate.presentation_backpressure('a', .05)
        self.assertLess(self.rate.current_kbps, 8000)

    def test_tiny_receipt_pair_after_format_gap_does_not_measure_path_capacity(self):
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)
        total = 0
        for n in range(1, 13):
            self.now = n * .05
            total += 10000
            self.rate.received_report('a', self.now - .02, total)
        self.now = 1.
        total += 1000
        self.rate.received_report('a', .98, total)
        # Two small P-frames straddle an exact-layer/TCP delivery burst. A
        # 2KB interval cannot establish a 100kbit/s path from an 8Mbit/s one.
        self.now = 1.17
        total += 2102
        self.rate.received_report('a', .994, total)
        self.rate.presentation_backpressure('a', .025)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.rate.current_kbps, 8000)

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

    def test_one_moderately_old_tcp_receipt_does_not_revoke_recovered_capacity(self):
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)
        for n in range(1, 21):
            self.now = n * .05
            self.rate.received_report('a', self.now - .02, n * 10000)
        # Matched receipt/credit events from the retained exp91 repaired-TCP
        # failure, translated to a local epoch. Only the last receipt exceeds
        # baseline +100ms; the short delivery interval is not a new capacity.
        events = [
            ('received', 2.000596540, 1.922406227, 51434580),
            ('received', 2.028567726, 1.940215229, 51446896),
            ('received', 2.059702215, 1.956735729, 51458555),
            ('wait', 2.059766587, .04618664784356952),
            ('wait', 2.126542263, .050447359681129456),
            ('received', 2.152846952, 2.013194479, 51510181),
            ('wait', 2.152916533, .011086063925176859),
        ]
        for kind, now, *values in events:
            self.now = now
            if kind == 'received':
                self.rate.received_report('a', values[0], values[1])
            else:
                self.rate.presentation_backpressure('a', values[0])
        self.assertEqual(self.applied, [], 'one moderately delayed receipt cannot establish a smaller link')
        # Fresh drained receipts end the incident; a later sustained moderate
        # slowdown still lowers the target once its age is independently seen.
        total = 51510181
        for n in range(1, 5):
            self.now = 2.16 + n * .04
            total += 22000
            self.rate.received_report('a', self.now - .025, total)
        self.assertEqual(self.rate.current_kbps, 8000)
        for now, sent in ((3.0, 2.8), (3.08, 2.85), (3.16, 2.9)):
            self.now = now
            total += 20000
            self.rate.received_report('a', sent, total)
            self.rate.presentation_backpressure('a', .03)
        self.assertLess(self.rate.current_kbps, 8000)

    def test_severe_delivery_step_keeps_immediate_first_old_receipt_response(self):
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)
        for n in range(1, 21):
            self.now = n * .05
            self.rate.received_report('a', self.now - .02, n * 10000)
        # Same exp91 run's actual 20-to-2M transition. Delivery is less than
        # half the offered load: waiting for another old frame would add debt.
        events = [
            ('received', 2.000962706, 1.971179888, 19903119),
            ('received', 2.023725564, 1.987414287, 19918811),
            ('received', 2.047594608, 2.003326786, 19933746),
            ('received', 2.096628791, 2.019128444, 19947792),
            ('wait', 2.096712650, .02363893808797002),
            ('wait', 2.162188684, .051860648207366467),
            ('received', 2.179714850, 2.037816942, 19964712),
        ]
        for kind, now, *values in events:
            self.now = now
            if kind == 'received':
                self.rate.received_report('a', values[0], values[1])
            else:
                self.rate.presentation_backpressure('a', values[0])
        self.assertEqual(len(self.applied), 1)
        self.assertLessEqual(self.rate.current_kbps, 3000)

    def test_short_train_of_old_receipts_needs_elapsed_delivery_evidence(self):
        self.rate = AdaptiveVideoRate(8000, self.applied.append, clock=lambda: self.now)
        for n in range(1, 21):
            self.now = n * .05
            self.rate.received_report('a', self.now - .02, n * 10000)
        # Actual exp93 recovery: two old receipts are only28ms apart. That
        # repairs the segment, but does not establish100ms of bad delivery.
        events = [
            ('received', 2.000000000, 1.935118425, 45908775),
            ('received', 2.037261725, 1.958581929, 45931203),
            ('received', 2.065000000, 1.965797199, 45939144),
            ('received', 2.096948197, 1.997815691, 45951302),
            ('wait', 2.097373974, .011114106979221106),
            ('received', 2.140049727, 2.014437243, 45964736),
            ('wait', 2.140467108, .03551101917400956),
            ('received', 2.167922966, 2.029002558, 45976168),
        ]
        for kind, now, *values in events:
            self.now = now
            if kind == 'received':
                self.rate.received_report('a', values[0], values[1])
            else:
                self.rate.presentation_backpressure('a', values[0])
        self.assertEqual(self.applied, [])
        # Time passing without a new matched receipt cannot manufacture that
        # evidence. Admission credit continues to bound the waiting producer.
        self.now += .11
        self.rate.presentation_backpressure('a', .025)
        self.assertEqual(self.applied, [])
        # Continued old receipts over100ms do establish persistent congestion,
        # even if the flight-credit waits have started pacing the offered load.
        for now, sent in ((2.30, 2.15), (2.36, 2.20), (2.42, 2.24)):
            self.now = now
            self.rate.received_report('a', sent, 45976168 + int((now - 2.30) * 400000) + 20000)
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
    async def test_early_hold_blocks_an_uncommitted_frame_and_expires_without_ack_or_retune(self):
        history = _VideoPresentation()
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.11, presented_delay=.11,
                                presented_kbps=8000, presented_offered_kbps=8000,
                                feedback_kind='received', received_rapid_time=now)
        until = now + .04
        leases = []
        pending = asyncio.create_task(history.wait_for_credit(lambda: 0, lambda: state,
            bitrate_kbps=8000, frame_wire_bytes=10000, admission_hold=lambda: until, on_wait=leases.append))
        await asyncio.sleep(.005)
        self.assertFalse(pending.done(), 'even a frame fitting byte credit is not yet committed')
        await asyncio.wait_for(pending, .08)
        self.assertGreaterEqual(time.monotonic(), until)
        self.assertEqual(history._acknowledged_bytes, 0)
        self.assertEqual(leases, [True, False])

    async def test_replaced_unsent_frame_interrupts_credit_wait_and_releases_lease(self):
        history = _VideoPresentation()
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.2, presented_delay=.25,
                                presented_kbps=8000, presented_offered_kbps=8000,
                                feedback_kind='received', received_rapid_time=now)
        history.sent(10, 1000000)
        leases = []
        superseded = False
        pending = asyncio.create_task(history.wait_for_credit(
            lambda: 1000000, lambda: state, bitrate_kbps=8000, frame_wire_bytes=20000,
            interrupted=lambda: superseded, on_wait=leases.append))
        await asyncio.sleep(.005)
        self.assertFalse(pending.done())
        superseded = True
        history._wake.set()
        await asyncio.wait_for(pending, .03)
        self.assertEqual(history._acknowledged_bytes, 0)
        self.assertEqual(leases, [True, False])

    def drain_state(self, now):
        return SimpleNamespace(presented_time=now, presented_base=.11, presented_delay=.115,
                               presented_kbps=6000, presented_offered_kbps=6000,
                               feedback_kind='received', received_rapid_time=now)

    async def test_retained_flight_drains_packets_without_acknowledging_a_frame(self):
        history = _VideoPresentation()
        state = self.drain_state(time.monotonic())
        history.sent(10, 100000)
        sent, delivered = [100000], [10000]
        history.retain_flight(100000, 0, 126000)
        async def admission():
            return await history.wait_for_credit(lambda: sent[0], lambda: state,
                bitrate_kbps=2000, frame_wire_bytes=20000, packet_credit=lambda _: delivered[0])
        await asyncio.wait_for(admission(), .02)
        self.assertEqual(history.credit_waits, 0)
        self.assertEqual(history._acknowledged_bytes, 0)
        sent[0] = 120000
        pending = asyncio.create_task(admission())
        await asyncio.sleep(.005)
        self.assertFalse(pending.done(), 'the old window cannot grow to fit a new picture')
        delivered[0] = 25000
        history._wake.set()
        await asyncio.wait_for(pending, .03)
        self.assertEqual(history._draining_limit, 100000)
        sent[0] = 240000
        pending = asyncio.create_task(admission())
        await asyncio.sleep(.005)
        delivered[0] = 100000
        history._wake.set()
        await asyncio.sleep(.005)
        self.assertEqual(history._draining_limit, 0, 'the old sent prefix has arrived')
        self.assertFalse(pending.done(), 'new flight must now fit the lower target')
        delivered[0] = 220000
        history._wake.set()
        await asyncio.wait_for(pending, .03)
        self.assertEqual(history._acknowledged_bytes, 0, 'packet delivery never acknowledges pixels')

    async def test_retained_flight_requires_packet_credit(self):
        history = _VideoPresentation()
        state = self.drain_state(time.monotonic())
        history.sent(10, 100000)
        history.retain_flight(100000, 0, 126000)
        pending = asyncio.create_task(history.wait_for_credit(lambda: 100000, lambda: state,
            bitrate_kbps=2000, frame_wire_bytes=20000))
        await asyncio.sleep(.005)
        self.assertEqual(history._draining_limit, 0)
        self.assertFalse(pending.done())
        history.acknowledge(10)
        await asyncio.wait_for(pending, .03)

    async def test_retained_flight_expires_without_inventing_delivery(self):
        now = [10.]
        history = _VideoPresentation(clock=lambda: now[0])
        history.sent(10, 100000)
        history.retain_flight(100000, 0, 126000)
        now[0] = 11.6
        state = self.drain_state(now[0])
        pending = asyncio.create_task(history.wait_for_credit(lambda: 100000, lambda: state,
            bitrate_kbps=2000, frame_wire_bytes=20000, packet_credit=lambda _: 0))
        await asyncio.sleep(.005)
        self.assertEqual(history._draining_limit, 0)
        self.assertFalse(pending.done())
        self.assertEqual(history._acknowledged_bytes, 0)
        history.acknowledge(10)
        await asyncio.wait_for(pending, .03)

    async def test_retained_flight_is_bounded_by_prior_limit_and_valid_host_bytes(self):
        history = _VideoPresentation()
        history.retain_flight(100000, 10000, 50000)
        self.assertEqual(history._draining_limit, 50000)
        for args in ((True, 0, 1000000), (100, 101, 1000000), (100, -1, 1000000), (100, 0, -1)):
            history.retain_flight(*args)
            self.assertEqual(history._draining_limit, 50000)
        history.retain_flight(100000, 100000, 1000000)
        self.assertEqual(history._draining_limit, 0)

    async def test_cancelled_credit_wait_releases_raw_producer(self):
        history = _VideoPresentation()
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.02, presented_delay=.025,
                                presented_kbps=8000, presented_offered_kbps=8000,
                                feedback_kind='received', received_rapid_time=now)
        history.sent(10, 100000)
        holds = []
        pending = asyncio.create_task(history.wait_for_credit(
            lambda: 100000, lambda: state, bitrate_kbps=8000, on_wait=holds.append))
        await asyncio.sleep(.005)
        self.assertEqual(holds, [True])
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(holds, [True, False])

    async def test_rapid_receipts_bound_flight_before_the_last_ack_looks_late(self):
        history = _VideoPresentation()
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.02, presented_delay=.025,
                                presented_kbps=64, presented_offered_kbps=64,
                                feedback_kind='received', received_rapid_time=now)
        history.sent(10, 100000)
        pending = asyncio.create_task(history.wait_for_credit(
            lambda: 100000, lambda: state, bitrate_kbps=8000))
        await asyncio.sleep(.01)
        self.assertFalse(pending.done(), 'a healthy old ACK must not admit an unbounded burst')
        history.acknowledge(10)
        await asyncio.wait_for(pending, .05)
        self.assertEqual(history.credit_waits, 1)
        # An old app-limited delivery rate cannot cap new traffic after ACK.
        await history.wait_for_credit(lambda: 110000, lambda: state, bitrate_kbps=8000)
        self.assertEqual(history.credit_waits, 1)

    async def test_rapid_receipt_loss_wait_is_bounded_and_old_cadence_is_unchanged(self):
        history = _VideoPresentation()
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.02, presented_delay=.025,
                                presented_kbps=8000, presented_offered_kbps=8000,
                                feedback_kind='received', received_rapid_time=now)
        history.sent(10, 100000)
        await asyncio.wait_for(history.wait_for_credit(
            lambda: 100000, lambda: state, bitrate_kbps=8000), .09)
        self.assertEqual(history.credit_waits, 1)
        state.received_rapid_time = now - 2
        await history.wait_for_credit(lambda: 100000, lambda: state, bitrate_kbps=8000)
        self.assertEqual(history.credit_waits, 1)

    async def test_partial_ack_does_not_restart_the_rapid_receipt_deadline(self):
        history = _VideoPresentation()
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.02, presented_delay=.025,
                                presented_kbps=8000, presented_offered_kbps=8000,
                                feedback_kind='received', received_rapid_time=now)
        history.sent(10, 10000)
        history.sent(11, 100000)
        pending = asyncio.create_task(history.wait_for_credit(
            lambda: 100000, lambda: state, bitrate_kbps=8000))
        await asyncio.sleep(.01)
        history.acknowledge(10)
        await asyncio.sleep(.01)
        self.assertFalse(pending.done(), 'an earlier ACK has not freed enough flight credit')
        history.acknowledge(11)
        await asyncio.wait_for(pending, .03)
        self.assertEqual(history.credit_waits, 1)

    async def test_frame_aware_credit_reserves_the_initial_paced_bytes(self):
        for target, base, outstanding, frame_size in (
                (2000, .02, 10000, 4000), (8000, .11, 120000, 20000),
                (2000, .02, 6000, 3500)):
            history = _VideoPresentation()
            now = time.monotonic()
            state = SimpleNamespace(presented_time=now, presented_base=base, presented_delay=base + .005,
                                    presented_kbps=64, presented_offered_kbps=64,
                                    feedback_kind='received', received_rapid_time=now)
            history.sent(10, outstanding)
            pending = asyncio.create_task(history.wait_for_credit(
                lambda: outstanding, lambda: state, bitrate_kbps=target,
                frame_wire_bytes=frame_size))
            await asyncio.sleep(.005)
            self.assertFalse(pending.done(), 'frame admission must reserve its initial paced bytes')
            history.acknowledge(10)
            await asyncio.wait_for(pending, .04)
            self.assertEqual(history.credit_waits, 1)
            # Drained frames cannot deadlock waiting for their own ACK.
            await history.wait_for_credit(lambda: outstanding, lambda: state,
                                          bitrate_kbps=target, frame_wire_bytes=frame_size)
            self.assertEqual(history.credit_waits, 1)

    async def test_large_paced_picture_does_not_wait_for_its_whole_wire_reservation(self):
        for target, base, outstanding, frame_size in (
                (1708, .109, 10968, 37322), (2000, .02, 1000, 20000)):
            history = _VideoPresentation()
            now = time.monotonic()
            state = SimpleNamespace(presented_time=now, presented_base=base, presented_delay=base + .005,
                                    presented_kbps=target, presented_offered_kbps=target,
                                    feedback_kind='received', received_rapid_time=now)
            history.sent(10, outstanding)
            await asyncio.wait_for(history.wait_for_credit(
                lambda: outstanding, lambda: state, bitrate_kbps=target,
                frame_wire_bytes=frame_size), .02)
            self.assertEqual(history.credit_waits, 0,
                             'pacing already spreads this AU over more than one feedback interval')

    async def test_frame_aware_credit_keeps_path_target_after_idle(self):
        history = _VideoPresentation()
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.11, presented_delay=.115,
                                presented_kbps=64, presented_offered_kbps=64,
                                feedback_kind='received', received_rapid_time=now)
        history.sent(10, 100000)
        # A previous idle source must not become a self-reinforcing limit on
        # a new clip. This whole120KB fits the actual8M path's RTT allowance.
        await asyncio.wait_for(history.wait_for_credit(
            lambda: 100000, lambda: state, bitrate_kbps=8000, frame_wire_bytes=20000), .02)
        self.assertEqual(history.credit_waits, 0)

    async def test_frame_aware_credit_rechecks_rate_on_partial_ack(self):
        history = _VideoPresentation()
        target = [8000]
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.11, presented_delay=.115,
                                presented_kbps=6400, presented_offered_kbps=6400,
                                feedback_kind='received', received_rapid_time=now)
        history.sent(10, 50000)
        history.sent(11, 120000)
        pending = asyncio.create_task(history.wait_for_credit(
            lambda: 120000, lambda: state, bitrate_kbps=8000, frame_wire_bytes=20000,
            current_bitrate=lambda: target[0]))
        await asyncio.sleep(.005)
        self.assertFalse(pending.done())
        target[0] = 2000
        history._wake.set()
        history.acknowledge(10)
        await asyncio.sleep(.005)
        self.assertFalse(pending.done(), 'a partial ACK cannot reuse the old high-rate window')
        history.acknowledge(11)
        await asyncio.wait_for(pending, .04)

    async def test_frame_aware_long_rtt_wait_renews_lease_and_stays_bounded(self):
        history = _VideoPresentation()
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.11, presented_delay=.115,
                                presented_kbps=6400, presented_offered_kbps=6400,
                                feedback_kind='received', received_rapid_time=now)
        history.sent(10, 200000)
        leases = []
        pending = asyncio.create_task(history.wait_for_credit(
            lambda: 200000, lambda: state, bitrate_kbps=8000,
            frame_wire_bytes=20000, on_wait=leases.append))
        await asyncio.sleep(.065)
        self.assertFalse(pending.done(), '50ms cannot prove loss on a110ms RTT path')
        self.assertGreaterEqual(leases.count(True), 2)
        await asyncio.wait_for(pending, .25)
        self.assertLess(time.monotonic() - now, .20, 'a prepared frame cannot wait for two full RTTs')
        self.assertEqual(leases[-1], False)

    async def test_partial_packet_credit_releases_admission_without_a_frame_ack(self):
        from extensions.browser_bridge.qsm_transport_feedback import PacketHistory, Report
        history = _VideoPresentation()
        packets = PacketHistory({1})
        for _ in range(16):
            packets.sent(12500)
        history.sent(10, 200000)
        now = time.monotonic()
        state = SimpleNamespace(presented_time=now, presented_base=.11, presented_delay=.115,
                                presented_kbps=6400, presented_offered_kbps=6400,
                                feedback_kind='received', received_rapid_time=now)
        pending = asyncio.create_task(history.wait_for_credit(
            lambda: 200000, lambda: state, bitrate_kbps=8000,
            frame_wire_bytes=20000, packet_credit=packets.credit))
        await asyncio.sleep(.005)
        self.assertFalse(pending.done())
        self.assertTrue(packets.receive(Report(1, 0, tuple((i, 0) for i in range(8)))))
        history._wake.set()
        await asyncio.wait_for(pending, .03)
        self.assertEqual(history._acknowledged, 0)
        self.assertEqual(history._acknowledged_bytes, 0, 'packet credit cannot acknowledge pixels or complete frames')
        self.assertEqual(history.credit_waits, 1)

    async def test_only_matched_rapid_receipts_enable_early_admission(self):
        now = [1.]
        rate = AdaptiveVideoRate(8000, lambda _: None, clock=lambda: now[0])
        for n in range(12):
            now[0] = 1 + n * .05
            rate.received_report('a', now[0] - .02, n * 10000)
        state = rate._receivers['a']
        self.assertEqual(state.received_rapid_time, float('-inf'))
        for n in range(1, 13):
            now[0] = 1.55 + n * .02
            rate.received_report('a', now[0] - .02, (12 + n) * 10000)
        self.assertEqual(state.received_rapid_time, now[0])
        # Losing the optional transform restores the old feedback mode.
        now[0] += 2
        rate.presentation_report('a', now[0] - .1, 250000)
        self.assertEqual(state.received_rapid_time, float('-inf'))

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
    async def test_packet_admission_hold_deduplicates_and_cannot_renew_a_congestion_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            state = self.packet_congestion(bridge)
            delivery = state.packet_delivery
            delivery.pressure = True; delivery.queue_excess = .04
            try:
                bridge._update_packet_admission()
                until = bridge._packet_hold_until
                self.assertGreater(until, time.monotonic())
                bridge._update_packet_admission()
                self.assertEqual(bridge._packet_hold_until, until)
                delivery.serial += 1
                bridge._update_packet_admission()
                self.assertEqual(bridge._packet_hold_until, until, 'new samples cannot extend this episode')
                bridge._packet_hold_until = time.monotonic() - .01
                delivery.serial += 1; delivery.queue_excess = .005; delivery.pressure = False
                bridge._update_packet_admission()
                self.assertTrue(bridge._packet_hold_armed)
                delivery.serial += 1; delivery.queue_excess = .04; delivery.pressure = True
                bridge._update_packet_admission()
                self.assertGreater(bridge._packet_hold_until, time.monotonic())
                bridge.video_track.current_region = None
                bridge._update_packet_admission()
                self.assertEqual(bridge._packet_hold_until, 0)
            finally:
                await bridge.close()

    async def test_full_desktop_and_unknown_crop_keep_the_established_prediction_chain(self):
        for region in (None, (1280, 768, 0, 0, 1280, 768)):
            with tempfile.TemporaryDirectory() as directory:
                bridge = BrowserWebRtcBridge(Path(directory))
                self.packet_congestion(bridge)
                bridge.video_track.current_region = region
                requests = []
                bridge.input.request_video_refresh = lambda rate: requests.append(rate) or 7
                bridge.video_track.put_nowait(EncodedUnit(b'I', True, 1500))
                bridge.video_track.put_nowait(EncodedUnit(b'P', False, 1500))
                try:
                    bridge._video_rate_changed(1700)
                    self.assertEqual(requests, [])
                    self.assertEqual(bridge.video_track.recovery_serial, 0)
                    self.assertEqual(bridge.video_track._queue.qsize(), 2)
                finally:
                    await bridge.close()

    async def test_private_refresh_requires_all_ready_regions_and_deduplicates_shared_viewers(self):
        from extensions.browser_bridge.qsm_browser_bridge import UnixInputEgress, INPUT_VIDEO_REFRESH, BridgeError
        with tempfile.TemporaryDirectory() as directory:
            output = UnixInputEgress(Path(directory))
            packets = []
            output._send_packet = lambda packet: packets.append(packet) or True
            self.assertEqual(output.request_video_refresh(1700), 0)
            output._region_owners = {'new': (True, True), 'old': (False, False)}
            self.assertEqual(output.request_video_refresh(1700), 0)
            output._region_owners.pop('old')
            token = output.request_video_refresh(1700)
            self.assertGreater(token, 0)
            self.assertEqual(output.request_video_refresh(1700), token)
            self.assertEqual(len(packets), 1)
            self.assertEqual(packets[0], INPUT_HEADER.pack(INPUT_MAGIC, INPUT_VERSION, INPUT_VIDEO_REFRESH, 12) + struct.pack('!IQ', 1700, token))
            with self.assertRaises(BridgeError): output.request_video_refresh(True)
            output._send_packet = lambda _: False
            self.assertEqual(output.request_video_refresh(1000), 0)

    @staticmethod
    def packet_congestion(bridge):
        from extensions.browser_bridge.qsm_browser_bridge import _ReceiverFeedback
        now = time.monotonic()
        state = _ReceiverFeedback(now, presented_time=now, presented_base=.15,
                                  presented_delay=.2, feedback_kind='received',
                                  received_rapid_time=now)
        state.packet_delivery = DeliveryEstimator()
        state.packet_delivery.updated = state.packet_delivery.sent_at = now
        state.packet_delivery.valid = state.packet_delivery.queued = True
        state.packet_delivery.serial = 1
        state.packet_delivery.kbps = 2000
        state.packet_delivery.offered_kbps = 6000
        bridge._video_rate._receivers[bridge] = state
        from extensions.browser_bridge.qsm_transport_feedback import PacketHistory
        bridge._twcc_history = PacketHistory({1})
        bridge._ui_source.features = 31
        bridge._ui_peer.enabled = True
        bridge._regions_ready = bridge._regions_eligible = True
        bridge.video_track.current_region = (1280, 768, 0, 64, 1280, 512)
        return state

    async def test_replaced_frame_never_enters_real_rtp_history_or_counters(self):
        from aiortc.rtp import RtpPacket
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            sender = bridge._pc.addTrack(bridge.video_track)
            history = bridge._video_received
            self.packet_congestion(bridge)
            bridge._video_pacer.sent_bytes = 1000000
            history.sent(123, 1000000)
            started = asyncio.Event()
            original_wait = history.wait_for_credit
            async def wait(*args, **kwargs):
                started.set()
                return await original_wait(*args, **kwargs)
            history.wait_for_credit = wait
            async def encoded(codec):
                packet = await bridge.video_track.recv()
                bridge.video_track.current_region = (1280, 768, 0, 64, 1280, 512)
                return SimpleNamespace(payloads=[bytes(packet)], timestamp=packet.pts, audio_level=None)
            sender._next_encoded_frame = encoded
            bridge._attach_video_frame_budget(sender)
            sent = []
            done = asyncio.Event()
            async def wire(data):
                sent.append(RtpPacket.parse(data))
                if len(sent) == 2:
                    done.set()
            sender.transport._send_rtp = wire
            old_capture = int((time.monotonic() - .1) * 90000)
            bridge.video_track.put_nowait(EncodedUnit(b'initial', True, 1500, capture=old_capture))
            await bridge.video_track.recv()
            bridge.video_track.put_nowait(EncodedUnit(b'obsolete P1', False, 1500, capture=old_capture))
            bridge.video_track.put_nowait(EncodedUnit(b'obsolete P2', False, 1500, capture=old_capture))
            requests = []
            def produce():
                bridge.video_track.put_nowait(EncodedUnit(b'old IDR in transit', True, 1500, capture=old_capture - 1500))
                fresh = int(time.monotonic() * 90000) + 1
                bridge.video_track.put_nowait(EncodedUnit(b'fresh IDR', True, 1500, capture=fresh, refresh_token=7, codec_bitrate=1150))
                bridge.video_track.put_nowait(EncodedUnit(b'fresh P', False, 1500, capture=fresh + 1500, refresh_token=7, codec_bitrate=1150))
            def request(rate):
                requests.append(rate)
                asyncio.get_running_loop().call_soon(produce)
                return 7
            bridge.input.request_video_refresh = request
            pending = asyncio.create_task(sender._run_rtp(SimpleNamespace(payloadType=99)))
            try:
                await asyncio.wait_for(started.wait(), .1)
                self.assertEqual(history.credit_waits, 1)
                self.assertEqual(sent, [])
                bridge._video_rate.current_kbps = 1700
                bridge._video_rate_changed(1700)
                history.acknowledge(123)
                await asyncio.wait_for(done.wait(), .2)
                self.assertEqual([packet.payload for packet in sent], [b'fresh IDR', b'fresh P'])
                self.assertEqual(sent[1].sequence_number, (sent[0].sequence_number + 1) & 65535)
                self.assertEqual((sent[1].timestamp - sent[0].timestamp) & 0xffffffff, 1500)
                self.assertEqual(sender._RTCRtpSender__packet_count, 2)
                self.assertEqual(sender._RTCRtpSender__octet_count, len(b'fresh IDRfresh P'))
                self.assertEqual(len(sender._RTCRtpSender__rtp_history), 2)
                self.assertEqual(requests, [1700])
                self.assertFalse(bridge._receipt_pause_active)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                await bridge.close()

    async def test_confirmed_cut_finishes_an_rtp_frame_already_in_progress(self):
        from aiortc.rtp import RtpPacket
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            sender = bridge._pc.addTrack(bridge.video_track)
            self.packet_congestion(bridge)
            async def encoded(codec):
                packet = await bridge.video_track.recv()
                bridge.video_track.current_region = (1280, 768, 0, 64, 1280, 512)
                payloads = [b'old-a', b'old-b', b'old-c'] if bytes(packet) == b'old' else [bytes(packet)]
                return SimpleNamespace(payloads=payloads, timestamp=packet.pts, audio_level=None)
            sender._next_encoded_frame = encoded
            bridge._attach_video_frame_budget(sender)
            sent = []
            done = asyncio.Event()
            def request(rate):
                fresh = int(time.monotonic() * 90000) + 1
                asyncio.get_running_loop().call_soon(bridge.video_track.put_nowait, EncodedUnit(b'new-IDR', True, 1500, capture=fresh, refresh_token=7, codec_bitrate=1150))
                return 7
            bridge.input.request_video_refresh = request
            async def wire(data):
                sent.append(RtpPacket.parse(data))
                if len(sent) == 1:
                    bridge._video_rate.current_kbps = 1700
                    bridge._video_rate_changed(1700)
                if len(sent) == 4:
                    done.set()
            sender.transport._send_rtp = wire
            capture = int((time.monotonic() - .02) * 90000)
            bridge.video_track.put_nowait(EncodedUnit(b'old', True, 1500, capture=capture))
            bridge.video_track.put_nowait(EncodedUnit(b'old-dependent', False, 1500, capture=capture))
            pending = asyncio.create_task(sender._run_rtp(SimpleNamespace(payloadType=99)))
            try:
                await asyncio.wait_for(done.wait(), .2)
                self.assertEqual([p.payload for p in sent], [b'old-a', b'old-b', b'old-c', b'new-IDR'])
                self.assertEqual([p.marker for p in sent], [0, 0, 1, 1])
                self.assertTrue(all(b.sequence_number == (a.sequence_number + 1) & 65535
                                    for a, b in zip(sent, sent[1:])))
                self.assertEqual(sender._RTCRtpSender__packet_count, 4)
                self.assertEqual(sender._RTCRtpSender__octet_count, len(b'old-aold-bold-cnew-IDR'))
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                await bridge.close()

    async def test_confirmed_packet_cut_retains_old_flight_with_new_pacing_and_no_idr(self):
        from extensions.browser_bridge.qsm_browser_bridge import _ReceiverFeedback
        from extensions.browser_bridge.qsm_transport_feedback import PacketHistory
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            now = time.monotonic()
            state = _ReceiverFeedback(now, presented_time=now, presented_base=.11,
                                      feedback_kind='received', received_rapid_time=now)
            state.packet_delivery = DeliveryEstimator()
            state.packet_delivery.updated = now
            state.packet_delivery.queued = True
            bridge._video_rate._receivers[bridge] = state
            bridge._twcc_history = PacketHistory({1})
            for _ in range(10):bridge._twcc_history.sent(10000)
            bridge._video_pacer.sent_bytes = 100000
            bridge._video_received.sent(10, 100000)
            requested = []
            bridge.input.request_keyframe = lambda: requested.append(True)
            bridge.video_track.put_nowait(EncodedUnit(b'I', True, 1500))
            bridge.video_track.put_nowait(EncodedUnit(b'P', False, 1500))
            try:
                bridge._video_rate_changed(1700)
                self.assertEqual(bridge._video_pacer._rate, 1700 * 1000 / 8)
                self.assertEqual(bridge._video_received._draining_end, 100000)
                self.assertEqual(bridge._video_received._draining_limit, 100000)
                self.assertEqual(bridge._video_received._acknowledged_bytes, 0)
                self.assertEqual(bridge.video_track._queue.qsize(), 2)
                self.assertEqual(requested, [])
            finally:
                await bridge.close()

    async def test_legacy_or_minor_cut_cannot_retain_an_unknown_flight(self):
        from extensions.browser_bridge.qsm_browser_bridge import _ReceiverFeedback
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            now = time.monotonic()
            state = _ReceiverFeedback(now, presented_time=now, presented_base=.11,
                                      feedback_kind='received', received_rapid_time=now)
            state.packet_delivery = DeliveryEstimator()
            state.packet_delivery.updated = now
            state.packet_delivery.queued = True
            bridge._video_rate._receivers[bridge] = state
            bridge._video_pacer.sent_bytes = 100000
            try:
                bridge._video_rate_changed(6000)
                self.assertEqual(bridge._video_received._draining_limit, 0)
                bridge._video_rate_changed(1000)
                self.assertEqual(bridge._video_received._draining_limit, 0, 'no negotiated packet history')
            finally:
                await bridge.close()

    async def test_one_future_frame_pauses_only_while_an_au_is_in_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            leases = []
            bridge.input.set_video_backpressure = lambda owner, ms: leases.append((owner, ms))
            try:
                bridge._video_frame_in_progress = True
                bridge.video_track.put_nowait(EncodedUnit(b'IDR', True, 1500))
                self.assertEqual(leases, [], 'one short pacing overlap must preserve raw capture history')
                await asyncio.sleep(.025)
                self.assertEqual(leases, [(bridge.video_track, 100)], 'do not queue several old pictures behind a held AU')
                bridge._video_frame_in_progress = False
                bridge._update_video_backpressure()
                self.assertEqual(leases[-1], (bridge.video_track, 0))
                await bridge.video_track.recv()
                before = len(leases)
                bridge.video_track.put_nowait(EncodedUnit(b'P', False, 1500))
                self.assertEqual(len(leases), before, 'an idle consumer may receive its first frame without a raw pause')
            finally:
                await bridge.close()

    async def test_short_future_frame_overlap_does_not_pulse_raw_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            leases = []
            bridge.input.set_video_backpressure = lambda owner, ms: leases.append((owner, ms))
            try:
                bridge._video_frame_in_progress = True
                bridge.video_track.put_nowait(EncodedUnit(b'IDR', True, 1500))
                await asyncio.sleep(.004)
                bridge._video_frame_in_progress = False
                bridge._update_video_backpressure()
                await asyncio.sleep(.025)
                self.assertEqual(leases, [], 'a short overlap cannot discard the bounded raw burst history')
            finally:
                await bridge.close()

    async def test_close_cancels_pending_future_frame_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            leases = []
            bridge.input.set_video_backpressure = lambda owner, ms: leases.append((owner, ms))
            bridge._video_frame_in_progress = True
            bridge.video_track.put_nowait(EncodedUnit(b'IDR', True, 1500))
            await bridge.close()
            await asyncio.sleep(.025)
            self.assertEqual(leases, [], 'teardown cannot activate a pending raw lease')

    async def test_packetized_frame_budget_matches_rtp_and_preserves_payloads(self):
        from aiortc.rtp import RtpPacket, HeaderExtensionsMap
        from aiortc.rtcrtpparameters import RTCRtpParameters, RTCRtpHeaderExtensionParameters
        from extensions.browser_bridge.qsm_transport_feedback import URI
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            extension_map = HeaderExtensionsMap()
            extension_map.configure(RTCRtpParameters(headerExtensions=[
                RTCRtpHeaderExtensionParameters(id=1, uri='urn:ietf:params:rtp-hdrext:sdes:mid'),
                RTCRtpHeaderExtensionParameters(id=2, uri='http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time'),
                RTCRtpHeaderExtensionParameters(id=3, uri=URI),
            ]))
            payloads = [b'a' * 1150, b'b' * 1137, b'c' * 23]
            frame = SimpleNamespace(payloads=payloads, timestamp=12345, audio_level=None)
            async def next_frame(codec):
                return frame
            sender = SimpleNamespace(_next_encoded_frame=next_frame, _ssrc=123,
                                     _RTCRtpSender__mid='video',
                                     _RTCRtpSender__rtp_header_extensions_map=extension_map)
            try:
                bridge._attach_video_frame_budget(sender)
                for twcc in (False, True):
                    bridge._twcc_video = twcc
                    for payload_type in (99, 49):
                        actual = await sender._next_encoded_frame(SimpleNamespace(payloadType=payload_type))
                        self.assertIs(actual, frame)
                        self.assertIs(actual.payloads, payloads)
                        wire = 0
                        for payload in payloads:
                            packet = RtpPacket(payload_type=payload_type, ssrc=123, payload=payload)
                            packet.extensions.abs_send_time = 123
                            packet.extensions.mid = 'video'
                            if twcc:
                                packet.extensions.transport_sequence_number = 1234
                            wire += len(packet.serialize(extension_map)) + 64
                        self.assertEqual(bridge._next_video_wire_bytes, wire)
            finally:
                await bridge.close()

    async def test_short_receipt_wait_keeps_history_and_sustained_wait_renews_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            leases = []
            bridge.input.set_video_backpressure = lambda owner, ms: leases.append((owner, ms))
            try:
                bridge._set_receipt_backpressure(True)
                await asyncio.sleep(.004)
                self.assertEqual(leases, [])
                bridge._set_receipt_backpressure(False)
                await asyncio.sleep(.025)
                self.assertEqual(leases, [], 'cancelled pause cannot affect a later frame')
                bridge._set_receipt_backpressure(True)
                await asyncio.sleep(.025)
                self.assertEqual(leases[-1], (bridge, 75))
                count = len(leases)
                bridge._set_receipt_backpressure(True)
                self.assertEqual(len(leases), count + 1, 'active raw leases renew immediately')
                bridge._set_receipt_backpressure(False)
                self.assertEqual(leases[-1], (bridge, 0))
            finally:
                await bridge.close()

    async def test_close_cancels_pending_receipt_pause_and_releases_active_lease(self):
        for active in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                bridge = BrowserWebRtcBridge(Path(directory))
                leases = []
                bridge.input.set_video_backpressure = lambda owner, ms: leases.append((owner, ms))
                bridge._set_receipt_backpressure(True)
                if active:
                    await asyncio.sleep(.025)
                await bridge.close()
                await asyncio.sleep(.025)
                self.assertEqual(leases, [(bridge, 75), (bridge, 0)] if active else [])

    async def test_encoded_queue_pauses_raw_source_before_prediction_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserWebRtcBridge(Path(directory))
            leases = []
            bridge.input.set_video_backpressure = lambda owner, ms: leases.append((owner, ms))
            track = bridge.video_track
            try:
                track.put_nowait(EncodedUnit(b'I', True, 1500))
                self.assertEqual(leases, [])
                track.put_nowait(EncodedUnit(b'P1', False, 1500))
                self.assertEqual(leases, [(track, 100)])
                self.assertEqual(bytes(await track.recv()), b'I')
                self.assertEqual(leases[-1], (track, 0))
                track.put_nowait(EncodedUnit(b'P2', False, 1500))
                self.assertEqual(leases[-1], (track, 100))
                # A long fragmented AU refreshes the same bounded lease
                # without adding another raw or encoded frame to the queue.
                with patch('extensions.browser_bridge.qsm_browser_bridge.time.monotonic',
                           return_value=bridge._queue_pause_refreshed + .051):
                    bridge._update_video_backpressure()
                self.assertEqual(leases[-2:], [(track, 100), (track, 100)])
                self.assertEqual(track.overflows, 0)
                self.assertEqual(track.dropped_units, 0)
            finally:
                await bridge.close()
            self.assertEqual(leases[-1], (track, 0), 'track teardown releases its queue lease')

    async def test_private_raw_backpressure_has_bounded_per_peer_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            egress = UnixInputEgress(Path(directory))
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            egress._connection = a
            owner, other = object(), object()
            def lease():
                packet = b.recv(100)
                self.assertEqual(packet[:8], INPUT_HEADER.pack(
                    INPUT_MAGIC, INPUT_VERSION, INPUT_VIDEO_BACKPRESSURE, 1))
                return packet[8]
            try:
                with patch('extensions.browser_bridge.qsm_browser_bridge.time.monotonic', return_value=1.):
                    egress.set_video_backpressure(owner, 75)
                self.assertEqual(lease(), 75)
                with patch('extensions.browser_bridge.qsm_browser_bridge.time.monotonic', return_value=1.01):
                    egress.set_video_backpressure(other, 75)
                self.assertEqual(lease(), 75)
                with patch('extensions.browser_bridge.qsm_browser_bridge.time.monotonic', return_value=1.02):
                    egress.set_video_backpressure(owner, 0)
                self.assertEqual(lease(), 65, 'one viewer cannot release another active pause')
                with patch('extensions.browser_bridge.qsm_browser_bridge.time.monotonic', return_value=1.2):
                    egress.set_video_backpressure(owner, 0)
                self.assertEqual(lease(), 0, 'expired viewers cannot retain a pause')
                for value in (-1, 101, True, '75'):
                    with self.assertRaises(BridgeError):
                        egress.set_video_backpressure(owner, value)
                with self.assertRaises(BridgeError):
                    egress.encode_browser_message(json.dumps({'op': 'video_backpressure', 'milliseconds': 75}))
            finally:
                egress.close()
                b.close()

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
