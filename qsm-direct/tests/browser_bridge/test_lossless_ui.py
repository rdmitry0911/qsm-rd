import asyncio
from collections import OrderedDict
from dataclasses import replace
import struct
import time
import unittest
from unittest.mock import patch as mock_patch
import zlib
from extensions.browser_bridge.qsm_lossless_ui import (
    CLEAR, COPY, FEATURE_COPY, FEATURE_HISTORY, Flight, GAP, HEADER, HELLO, MAGIC, MAX_BUFFERED, MAX_COPY_SEGMENTS,
    MAX_STREAM_BYTES, PATCH, RESET, SCENE, Peer, Source, Update, decode,
)


def patch(serial=2, x=0, rgba=b'\x11\x22\x33\xff' * 4096):
    return Update(PATCH, 1, serial, 128, 64, x, 0, 64, 64, zlib.compress(rgba))


def source():
    s = Source()
    s.feed(Update(HELLO).wire(0, 0))
    s.feed(Update(RESET, width=128, height=64).wire(1, 1))
    return s


class Channel:
    readyState = 'open'
    bufferedAmount = 0
    def __init__(self): self.sent = []
    def send(self, value): self.sent.append(value)

class NotifyingChannel(Channel):
    def __init__(self): super().__init__(); self.listeners = {}
    def on(self, event, callback): self.listeners[event] = callback
    def flushed(self):
        self.bufferedAmount = 0
        self.listeners['bufferedamountlow']()

class EventDrivenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.debug = asyncio.get_running_loop().get_debug()
        asyncio.get_running_loop().set_debug(False)

    async def asyncTearDown(self):
        asyncio.get_running_loop().set_debug(self.debug)

    async def turns(self):
        for _ in range(20): await asyncio.sleep(0)

    async def test_damage_and_buffer_low_deliver_without_advancing_timer_clock(self):
        # Freeze timer time, while still allowing ready callbacks to run.
        # A periodic sender would stay asleep throughout this test.
        with mock_patch('extensions.browser_bridge.qsm_lossless_ui.time.monotonic', return_value=100):
            s = source(); p = Peer(s, lambda _: None, lambda: None); c = NotifyingChannel()
            p.opened(c); p.enable(True)
            try:
                await self.turns(); self.assertEqual(len(c.sent), 1)
                s.feed(patch().wire(1, 2)); await self.turns()
                self.assertEqual(decode(c.sent[-1]).op, PATCH)
                c.bufferedAmount = MAX_BUFFERED + 1
                s.feed(patch(3, x=64).wire(1, 3)); await self.turns()
                self.assertEqual(len(c.sent), 2)
                c.flushed(); await self.turns()
                self.assertEqual(decode(c.sent[-1]).x, 64)
                self.assertEqual(len(c.sent), 3)
            finally: await p.close()

    async def test_missing_geometry_waits_for_source_rather_than_busy_spinning(self):
        with mock_patch('extensions.browser_bridge.qsm_lossless_ui.time.monotonic', return_value=100):
            s = Source(); s.feed(Update(HELLO).wire(0, 0))
            p = Peer(s, lambda _: None, lambda: None); c = NotifyingChannel()
            calls = 0; drain = p.drain
            def counted():
                nonlocal calls
                calls += 1; drain()
            p.drain = counted; p.opened(c); p.enable(True)
            try:
                await self.turns(); self.assertEqual(calls, 1); self.assertFalse(c.sent)
                s.feed(Update(RESET, width=128, height=64).wire(1, 1)); await self.turns()
                self.assertEqual(len(c.sent), 1)
            finally: await p.close()


class ExactUiTests(unittest.TestCase):
    def test_backpressure_does_not_prepare_bitmaps_but_still_sends_invalidations(self):
        s = source(); p = Peer(s, lambda _: None, lambda: None)
        p.version = 3; p.channel = Channel(); p.enable(True); p.drain()
        s.feed(patch().wire(1, 2))
        for blocked in ('decode', 'sctp'):
            p.ack_pending = 39 if blocked == 'decode' else 0
            p.channel.bufferedAmount = MAX_BUFFERED // 2 if blocked == 'sctp' else 0
            with mock_patch.object(p, '_command', side_effect=AssertionError('blocked bitmap work')):
                p.drain(); p._delay(time.monotonic(), time.monotonic())
        p.ack_pending = 39; p.channel.bufferedAmount = 0
        s.feed(Update(CLEAR, width=128, height=64, w=64, h=64, capture=100).wire(1, 3))
        s.feed(patch(4).wire(1, 4))
        with mock_patch.object(p, '_command', side_effect=AssertionError('blocked replacement')):
            p.drain()
        self.assertEqual(p.channel.sent[-1][4], CLEAR)
        self.assertTrue(p.pending, 'replacement waits without preserving stale visible pixels')
        p.ack_pending = 0; p.drain()
        self.assertEqual([raw[4] for raw in p.channel.sent][-2:], [CLEAR, 6])

    def test_replacement_cannot_coalesce_away_an_unsent_clear(self):
        s = source(); s.feed(patch().wire(1, 2))
        p = Peer(s, lambda _: None, lambda: None); p.version = 2
        p.channel = Channel(); p.enable(True); p.drain()
        p.channel.bufferedAmount = MAX_BUFFERED
        s.feed(Update(CLEAR, width=128, height=64, w=64, h=64, capture=100).wire(1, 3))
        s.feed(patch(4).wire(1, 4)); p.drain()
        self.assertEqual(len(p.invalidations), 1)
        self.assertEqual(len(p.pending), 1)
        p.channel.bufferedAmount = 0; p.drain()
        self.assertEqual([raw[4] for raw in p.channel.sent][-2:], [CLEAR, PATCH])
        self.assertFalse(p.invalidations)

    def test_wire_accounting_includes_reset_compact_patches_and_clear_only_after_send(self):
        s = source(); s.features = 7
        s.feed(patch().wire(1, 2))
        costs = []
        p = Peer(s, lambda _: None, lambda: None, account=costs.append)
        p.version = 4; p.channel = Channel(); p.enable(True); p.drain()
        self.assertEqual([raw[4] for raw in p.channel.sent], [RESET, 6])
        s.feed(Update(CLEAR, 1, 3, 128, 64, 0, 0, 64, 64, capture=123).wire(1, 3))
        p.drain()
        self.assertEqual(costs, [len(raw) for raw in p.channel.sent])
        self.assertEqual(p.channel.sent[-1][4], CLEAR)
        class FailingChannel(Channel):
            def send(self, value): raise OSError("closed test channel")
        p.channel = FailingChannel(); p.enable(True)
        before = list(costs)
        with self.assertRaises(OSError): p.drain()
        self.assertEqual(costs, before, "failed sends cannot create phantom video debt")

    def test_negotiated_capabilities_and_capture_stamp_survive_shared_cache(self):
        s = Source(); s.feed(Update(HELLO, payload=struct.pack("!I", 3)).wire(0, 0))
        s.feed(Update(RESET, width=128, height=64, capture=100).wire(1, 1))
        from dataclasses import replace
        update = replace(patch(), capture=123)
        s.feed(update.wire(1, 2))
        self.assertEqual(s.features, 3); self.assertEqual(s.tiles[(0, 0)].capture, 123)
        old = Peer(s, lambda _: None, lambda: None); old.channel = Channel(); old.enable(True); old.drain()
        self.assertEqual(decode(old.channel.sent[-1]).capture, 0, "old browser receives the original protocol")
        new = Peer(s, lambda _: None, lambda: None); new.version = 2
        new.channel = Channel(); new.enable(True); new.drain()
        self.assertEqual(decode(new.channel.sent[-1]).capture, 123)
        bad = bytearray(update.wire(1, 3)); bad[7] = 7
        with self.assertRaises(ValueError): decode(bytes(bad))

    def test_large_foreground_tile_keeps_refill_and_cached_replay_reserves_video_budget(self):
        from dataclasses import replace
        s = source(); s.features = 1; reservations = []
        with mock_patch('extensions.browser_bridge.qsm_lossless_ui.time.monotonic', return_value=100):
            p = Peer(s, lambda _: None, lambda: None, bitrate=lambda: 500,
                     demand=reservations.append, interactive=lambda: True)
            p.channel = Channel(); p.enable(True); p.drain(); p.credit = 0
            # A large latest foreground tile must not lose its credit to
            # many cheap background tiles while it waits to fit.
            noisy = bytes((i * 71 + i // 13) % 256 for i in range(4096))
            front = Update(PATCH, width=128, height=64, w=64, h=64,
                           payload=zlib.compress(noisy), encoding=1, priority=True)
            p.pending[(0, 0)] = front
            p.pending[(64, 0)] = patch(x=64)
            p.drain(); self.assertEqual(len(p.channel.sent), 1)
            p._reservation(); self.assertGreater(reservations[-1], 0)
        with mock_patch('extensions.browser_bridge.qsm_lossless_ui.time.monotonic', return_value=100.1):
            p.drain()
            self.assertEqual(decode(p.channel.sent[1]).x, 0)
            self.assertFalse(p.pending)
            p._reservation(); self.assertEqual(reservations[-1], 0)
        self.assertEqual(p.channel.bufferedAmountLowThreshold, 0)

    def test_bulk_repaint_cannot_borrow_typing_bandwidth_or_accumulate_large_idle_burst(self):
        from dataclasses import replace
        import random
        s = source(); s.features = 1
        s.feed(Update(RESET, width=2048, height=64).wire(2, 1))
        payload = zlib.compress(random.Random(123).randbytes(4096))
        for n in range(32):
            s.feed(Update(PATCH, width=2048, height=64, x=n * 64, w=64, h=64,
                          payload=payload, encoding=1).wire(2, n + 2))
        typing = False; reservations = []
        with mock_patch('extensions.browser_bridge.qsm_lossless_ui.time.monotonic', return_value=100):
            p = Peer(s, lambda _: None, lambda: None, bitrate=lambda: 1500,
                     demand=reservations.append, interactive=lambda: typing)
            p.channel = Channel(); p.enable(True)
            # A full replay or already-seen/changed tiles are bulk work until
            # actual keyboard activity grants a bounded foreground lease.
            p.pending = OrderedDict((key, replace(u, priority=True)) for key, u in s.tiles.items())
            p.channel.bufferedAmount = MAX_BUFFERED
            p._reservation()
            self.assertEqual(reservations[-1], 15)
            self.assertEqual(p._rate(), 1500 * 125 * .15)
            p.channel.bufferedAmount = 0; p.drain()
            self.assertLessEqual(sum(map(len, p.channel.sent)), HEADER.size + 6190)
            self.assertTrue(p.pending, 'bulk snapshot must be spread across refills')
            typing = True; p.channel.bufferedAmount = MAX_BUFFERED; p._reservation()
            self.assertEqual(reservations[-1], 50)
            typing = False; p._reservation()
            self.assertEqual(reservations[-1], 15)
        with mock_patch('extensions.browser_bridge.qsm_lossless_ui.time.monotonic', return_value=160):
            p.channel.bufferedAmount = 0; p.channel.sent.clear(); p.drain()
            self.assertLessEqual(sum(map(len, p.channel.sent)), 6190,
                                 'idle time cannot refill a 16 KiB burst on a narrow path')

    def test_acknowledged_bitmap_copy_fill_scroll_and_epoch_invalidation(self):
        s = source(); p = Peer(s, lambda _: None, lambda: None); p.version = 3
        # A predictable but non-uniform bitmap, then a five-line vertical scroll.
        raw = b''.join(bytes((y, x, (x + y) % 256, 255)) for y in range(64) for x in range(64))
        original = Update(PATCH, width=128, height=64, w=64, h=64, payload=zlib.compress(raw))
        command, entry = p._command(original)
        self.assertEqual(command.op, 7); p._remember(entry)
        again, _ = p._command(original)
        self.assertNotEqual(again.op, 8, "an unprocessed client bitmap cannot be referenced")
        p.confirm_cache([1])
        again, entry = p._command(original)
        self.assertEqual(again.op, 8); self.assertIsNone(entry)
        scrolled = raw[5 * 64 * 4:] + b'\x11\x22\x33\xff' * (5 * 64)
        from dataclasses import replace
        command, entry = p._command(replace(original, payload=zlib.compress(scrolled)))
        self.assertEqual(command.op, 9)
        new_id, old_id, delta = struct.unpack_from("!IIh", command.payload)
        self.assertEqual((new_id, old_id, delta), (2, 1, -5))
        self.assertEqual(raw[-delta * 256:] + zlib.decompress(command.payload[10:]), scrolled)
        # Copying cannot mutate the source used by another overlapping copy.
        self.assertEqual(p.cache[1][0], raw)
        solid, _ = p._command(patch())
        self.assertEqual(solid.op, 6); self.assertEqual(solid.payload, b'\x11\x22\x33\xff')
        p.channel = Channel(); p.enable(True); p.drain()
        self.assertFalse(p.cache); self.assertFalse(p.cache_keys)
        for n in range(300): p._remember((n + 1, raw, 64, 64, (n, 0)))
        self.assertEqual(len(p.cache), 256); self.assertEqual(len(p.cache_keys), 256)

    def test_node_default_is_advertised_without_enabling_worker_or_peer(self):
        s = source(); policies = []
        p = Peer(s, policies.append, lambda: None, default_enabled=True)
        p.announce()
        self.assertTrue(policies[-1]['default_enabled'])
        self.assertFalse(p.enabled, 'browser support and preference must subscribe explicitly')
        p = Peer(s, policies.append, lambda: None); p.announce()
        self.assertFalse(policies[-1]['default_enabled'])

    def test_geometry_compression_and_decompression_bomb_are_bounded(self):
        self.assertEqual(decode(patch().wire(1, 2)).payload, patch().payload)
        for bad in [b'', patch().wire(1, 2)[:-1],
                    Update(PATCH, width=128, height=64, x=1, w=64, h=64,
                           payload=patch().payload).wire(1, 2),
                    patch(rgba=b'x' * 1000000).wire(1, 2),
                    patch(rgba=b'\0' * 16384).wire(1, 2)]:
            with self.assertRaises(ValueError): decode(bad)

    def test_neutral_text_uses_one_exact_sample_per_pixel(self):
        p = Update(PATCH, width=128, height=64, w=64, h=64,
                   payload=zlib.compress(bytes([123]) * 4096), encoding=1)
        self.assertEqual(decode(p.wire(1, 2)), Update(PATCH, 1, 2, 128, 64, 0, 0, 64, 64, p.payload, 1))
        with self.assertRaises(ValueError): decode(Update(RESET, width=128, height=64, encoding=1).wire(1, 3))
        with self.assertRaises(ValueError): decode(Update(PATCH, width=128, height=64, w=64, h=64,
            payload=zlib.compress(b'x' * 1000000), encoding=1).wire(1, 4))

    def test_gap_invalidates_cache_and_requires_real_reset(self):
        s = source(); events = []; s.subscribe(events.append)
        s.feed(patch().wire(1, 2)); self.assertEqual(len(s.tiles), 1)
        s.feed(patch(4).wire(1, 4))
        self.assertTrue(s.awaiting_reset); self.assertFalse(s.tiles)
        self.assertEqual(events[-1].op, GAP)
        s.feed(patch(5).wire(1, 5)); self.assertFalse(s.tiles)
        s.feed(Update(RESET, width=128, height=64).wire(2, 6))
        s.feed(patch(7).wire(2, 7)); self.assertEqual(len(s.tiles), 1)

    def test_expired_worker_lease_does_not_leave_frozen_overlay(self):
        s = source(); s.feed(patch().wire(1, 2))
        s.poll(time.monotonic() + 1)
        self.assertFalse(s.tiles); self.assertTrue(s.awaiting_reset)

    def test_reenable_cannot_replay_pixels_from_before_capture_was_disabled(self):
        s = source(); s.feed(patch().wire(1, 2))
        s.invalidate()
        p = Peer(s, lambda v: None, lambda: None); p.channel = Channel(); p.enable(True); p.drain()
        self.assertEqual(len(p.channel.sent), 1)
        self.assertEqual(decode(p.channel.sent[0]).op, RESET)
        self.assertFalse(s.tiles)

    def test_slow_viewer_gets_latest_tile_without_blocking_another(self):
        s = source(); policies = []
        a = Peer(s, policies.append, lambda: None)
        b = Peer(s, policies.append, lambda: None)
        a.channel, b.channel = Channel(), Channel()
        a.enable(True); b.enable(True)
        a.channel.bufferedAmount = MAX_BUFFERED + 1
        s.feed(patch().wire(1, 2)); a.drain(); b.drain()
        self.assertFalse(a.channel.sent); self.assertEqual(len(b.channel.sent), 2)
        s.feed(patch(3, rgba=b'\x44\x55\x66\xff' * 4096).wire(1, 3))
        s.feed(Update(CLEAR, width=128, height=64, w=64, h=64).wire(1, 4))
        a.channel.bufferedAmount = 0; a.drain()
        self.assertEqual(len(a.channel.sent), 1, 'new viewer must not replay cleared old content')
        a.enable(False); self.assertFalse(a.pending)

    def test_pending_state_replaces_history_and_replays_for_second_viewer(self):
        s = source(); a = Peer(s, lambda p: None, lambda: None); a.channel = Channel(); a.enable(True); a.drain()
        a.channel.bufferedAmount = MAX_BUFFERED + 1
        for n in range(2, 102): s.feed(patch(n).wire(1, n))
        self.assertEqual(len(a.pending), 1)
        a.channel.bufferedAmount = 0; a.drain()
        self.assertEqual(len(a.channel.sent), 2)
        b = Peer(s, lambda p: None, lambda: None); b.channel = Channel(); b.enable(True); b.drain()
        self.assertEqual(len(b.channel.sent), 2)
        s.feed(Update(RESET, width=64, height=64).wire(2, 103)); b.drain()
        self.assertEqual(decode(b.channel.sent[-1]).op, RESET)
        self.assertEqual(decode(b.channel.sent[-1]).width, 64)

    def test_cached_snapshot_respects_rate_and_fresh_input_and_clear_take_priority(self):
        s = source(); s.feed(patch().wire(1, 2)); s.feed(patch(3, x=64).wire(1, 3))
        with mock_patch('extensions.browser_bridge.qsm_lossless_ui.time.monotonic', return_value=100):
            p = Peer(s, lambda v: None, lambda: None, bitrate=lambda: 500)
            p.channel = Channel(); p.enable(True); p.credit = HEADER.size; p.drain()
            self.assertEqual(len(p.channel.sent), 1, 'no unmetered cached snapshot burst')
            s.feed(patch(4, x=64).wire(1, 4))
            self.assertEqual(next(iter(p.pending)), (64, 0), 'fresh input bypasses cached background')
            s.feed(Update(CLEAR, width=128, height=64, w=64, h=64).wire(1, 5))
            p.drain()
            self.assertEqual(decode(p.channel.sent[-1]).op, CLEAR, 'stale overlay clears despite zero credit')
        with mock_patch('extensions.browser_bridge.qsm_lossless_ui.time.monotonic', return_value=100.1):
            p.drain()
            self.assertEqual(decode(p.channel.sent[-1]).x, 64)
            self.assertEqual(len(p.channel.sent), 3)


class CopyProtocolTests(unittest.TestCase):
    def setUp(self):
        self.source = Source()
        self.source.feed(Update(HELLO, payload=struct.pack('!I', FEATURE_COPY | 8 | 1)).wire(0, 0))
        self.source.feed(Update(RESET, width=256, height=128).wire(1, 1))
        self.serial = 1
        self.resets = []
        self.peer = Peer(self.source, lambda _: None, lambda: self.resets.append(1))
        self.peer.version = 6
        self.channel = Channel()
        self.peer.channel = self.channel
        self.peer.enable(True)
        self.peer.credit = 1 << 20
        self.peer._burst = lambda: 1 << 20
        self.peer.drain()
        self.channel.sent.clear()

    def feed(self, update):
        self.serial += 1
        self.source.feed(replace(update, width=256, height=128).wire(1, self.serial, advanced=True))

    def copy(self, x=64, y=0, sx=0, sy=0, w=64, h=64):
        self.feed(Update(COPY, x=x, y=y, w=w, h=h, payload=struct.pack('!HH', sx, sy)))

    def tile(self, x=0, value=9):
        self.feed(Update(PATCH, x=x, w=64, h=64, payload=zlib.compress(bytes((value, 1, 2, 255)) * 4096)))

    def ops(self):
        return [decode(raw[:HEADER.size] + raw[HEADER.size:]).op if raw[4] < 6 or raw[4] == 10 else raw[4] for raw in self.channel.sent]

    def test_validation_of_copy_geometry_and_flags(self):
        good = Update(COPY, epoch=1, width=256, height=128, x=8, y=7, w=64, h=64, payload=struct.pack('!HH', 0, 0))
        self.assertEqual(decode(good.wire(1, 2)).op, COPY)
        for bad in (replace(good, payload=b'\0\0'), replace(good, w=0), replace(good, x=250),
                    replace(good, payload=struct.pack('!HH', 200, 0)), replace(good, payload=struct.pack('!HH', 8, 7))):
            with self.assertRaises(ValueError):
                decode(bad.wire(1, 2))

    def test_copy_without_negotiated_feature_invalidates_the_source(self):
        s = source()
        s.feed(Update(COPY, width=128, height=64, w=64, h=64, x=64, payload=b'\0\0\0\0').wire(1, 2))
        self.assertTrue(s.awaiting_reset)

    def test_copy_cannot_pass_older_tiles_and_later_tiles_stay_behind_it(self):
        self.peer.credit = 0; self.peer._burst = lambda: 0
        self.tile(0, 1)
        self.copy()
        self.tile(0, 2)
        self.assertEqual(len(self.peer.segments), 1)
        self.peer.drain()
        self.assertEqual(self.ops(), [], 'tile without credit must hold back the copy')
        self.peer._burst = lambda: 1 << 20; self.peer.credit = 1 << 20
        self.peer.drain()
        self.assertEqual([r[4] for r in self.channel.sent], [6, 10, 6])
        self.assertEqual(self.channel.sent[0][-4], 1)
        self.assertEqual(self.channel.sent[2][-4], 2)
        self.assertFalse(self.peer.segments)
        self.assertEqual(self.peer.copies, 1)
        serials = [struct.unpack_from('!I', r, 12)[0] for r in self.channel.sent]
        self.assertEqual(serials, sorted(serials))

    def test_snapshot_after_copy_asks_the_worker_instead_of_replaying_stale_tiles(self):
        self.copy()
        self.peer.drain(); self.channel.sent.clear()
        self.assertTrue(self.source.copied)
        self.peer.enable(False); self.peer.enable(True)
        self.peer.drain()
        self.assertEqual(self.channel.sent, [], 'stale cache cannot seed a viewer')
        self.assertEqual(len(self.resets), 1)
        self.source.feed(Update(RESET, width=256, height=128).wire(2, 1))
        self.assertFalse(self.source.copied)
        self.peer.drain()
        self.assertEqual(self.channel.sent[0][4], RESET)

    def test_backlog_is_bounded_by_a_fresh_epoch(self):
        self.peer.credit = 0; self.peer._burst = lambda: 0
        self.tile(0, 1)
        for _ in range(MAX_COPY_SEGMENTS + 1):
            self.copy()
        self.assertTrue(self.peer.snapshot)
        self.assertFalse(self.peer.segments)
        self.assertFalse(self.peer.pending)

    def test_older_peer_never_receives_a_copy_or_unfenced_flag(self):
        self.peer.version = 5
        self.copy()
        self.assertTrue(self.peer.snapshot)
        self.assertFalse(self.peer.segments)
        raw = Update(PATCH, epoch=1, width=256, height=128, w=64, h=64, unfenced=True, payload=zlib.compress(b'\1\2\3\xff' * 4096)).wire(1, 2, advanced=False)
        self.assertEqual(raw[5], 0)

    def test_recent_copy_lifts_the_band_share_and_expires(self):
        self.assertEqual(self.peer._rate(), 8000 * 125 * .15)
        self.copy()
        self.assertEqual(self.peer._rate(), 8000 * 125 * .75)
        self.source.copy_at = time.monotonic() - 1
        self.assertEqual(self.peer._rate(), 8000 * 125 * .15)


class DeliveredRateTests(unittest.TestCase):
    def test_a_seen_queue_goes_straight_to_what_the_path_delivers(self) -> None:
        flow = Flight()
        flow.window = flow.MAX_WINDOW
        serial, now = 0, 100.
        # A fast path sets the base round trip.
        for _ in range(4):
            serial += 1; flow.on_send(serial, 1000, now); flow.on_ack(serial, now + .02); now += .03
        # Now each record takes 200 ms more, at about 100 KB/s delivered: one round
        # closes on old evidence, and the next one states the queue.
        windows = []
        for _ in range(40):
            serial += 1; flow.on_send(serial, 1000, now); now += .01
            flow.on_ack(serial, now + .2); windows.append(flow.window)
            if flow.window < flow.MAX_WINDOW:
                break
        self.assertLess(flow.window, flow.MAX_WINDOW * .25, 'the first reduction reaches the path')
        self.assertGreaterEqual(flow.window, flow.MIN_WINDOW)

    def test_flight_reports_what_recent_records_took_of_the_path(self) -> None:
        flow = Flight()
        self.assertEqual(flow.delivered_rate(10.), 0)
        for serial in range(1, 6):
            flow.on_send(serial, 1000, 10.)
        flow.on_ack(5, 10.1)
        self.assertEqual(flow.delivered_rate(10.1), 5 * (1000 + flow.RECORD_OVERHEAD) / flow.DELIVERED_SECONDS)
        # A repeated acknowledgement delivered nothing new, and old bytes age out.
        flow.on_ack(5, 10.2)
        self.assertEqual(flow.delivered_rate(10.2), 5 * (1000 + flow.RECORD_OVERHEAD) / flow.DELIVERED_SECONDS)
        self.assertEqual(flow.delivered_rate(10.36), 0)
        self.assertFalse(flow.delivered)


class HistoryStreamTests(unittest.TestCase):
    """History records reference what came before: none may be dropped, replaced or overtaken."""
    FEATURES = 1 | 2 | 4 | 8 | 16 | FEATURE_COPY | FEATURE_HISTORY

    def setUp(self):
        self.s = Source()
        self.s.feed(Update(HELLO, payload=struct.pack("!I", self.FEATURES)).wire(0, 0))
        self.s.feed(self.reset(1))
        self.serial = 1
        self.resyncs = 0

    def reset(self, epoch):
        return Update(RESET, width=128, height=64).wire(epoch, 1, advanced=True)

    def resync(self):
        self.resyncs += 1

    def peer(self, version=7):
        p = Peer(self.s, lambda _: None, self.resync, bitrate=lambda: 20000); p.version = version
        p.channel = Channel(); p.enable(True)
        p._resync_at = float('-inf')
        return p

    def feed(self, update):
        self.serial += 1
        self.s.feed(update.wire(1, self.serial, advanced=True))

    def history(self, x=0, marker=b'x'):
        return Update(PATCH, 1, 0, 128, 64, x, 0, 64, 64, b'\0\0\0\x10' + marker * 8, history=True)

    def wire_ops(self, p):
        return [decode(r) for r in p.channel.sent]

    def seeded(self, version=7):
        # A viewer that was enabled before the worker's epoch began.
        p = self.peer(version)
        self.s.feed(self.reset(1))
        self.serial = 1
        return p

    def test_feature_and_validation(self):
        record = self.history()
        self.assertTrue(decode(record.wire(1, 2, advanced=True)).history)
        with self.assertRaises(ValueError):
            decode(Update(PATCH, 1, 0, 128, 64, 0, 0, 64, 64, b'\0', history=True).wire(1, 2, advanced=True))
        with self.assertRaises(ValueError):
            decode(replace(record, encoding=1).wire(1, 2, advanced=True))
        without = source()
        without.feed(record.wire(1, 2, advanced=True))
        self.assertTrue(without.awaiting_reset, 'an unrequested history record invalidates the source')

    def test_same_tile_twice_is_not_coalesced_and_order_is_kept(self):
        p = self.seeded()
        self.feed(self.history(marker=b'a'))
        self.feed(Update(COPY, 1, 0, 128, 64, 64, 0, 32, 32, struct.pack('!HH', 0, 0), capture=9))
        self.feed(self.history(marker=b'b'))
        self.feed(Update(CLEAR, 1, 0, 128, 64, 64, 0, 64, 64, capture=9))
        self.feed(self.history(marker=b'c'))
        p.drain()
        sent = self.wire_ops(p)
        self.assertEqual(sent[0].op, RESET)
        self.assertEqual([u.op for u in sent[1:]], [PATCH, COPY, PATCH, CLEAR, PATCH])
        self.assertEqual([u.payload[-1:] for u in sent[1:] if u.op == PATCH], [b'a', b'b', b'c'])
        self.assertTrue(all(u.history for u in sent[1:] if u.op == PATCH))
        serials = [struct.unpack_from('!I', r, 12)[0] for r in p.channel.sent]
        self.assertEqual(serials, list(range(serials[0], serials[0] + len(serials))))
        self.assertEqual(p.stream_bytes, 0)
        self.assertEqual(self.resyncs, 0, 'a viewer holding the whole epoch needs no new one')

    def test_stream_passes_no_bitmap_cache_or_pacing(self):
        p = self.seeded()
        p.credit = -1e9; p.credit_at = time.monotonic()
        self.feed(self.history())
        p.drain()
        self.assertEqual(self.wire_ops(p)[-1].op, PATCH, 'the worker already paced these bytes')

    def test_flow_window_and_decode_window_hold_the_head(self):
        p = self.seeded()
        p.drain()
        p.ack_pending = 24
        self.feed(self.history(marker=b'a'))
        before = len(p.channel.sent)
        p.drain()
        self.assertEqual(len(p.channel.sent), before)
        self.feed(Update(CLEAR, 1, 0, 128, 64, 64, 0, 64, 64, capture=9))
        p.drain()
        self.assertEqual(len(p.channel.sent), before, 'a later record cannot overtake a held patch')
        p.ack_pending = 0
        p.channel.bufferedAmount = MAX_BUFFERED
        p.drain()
        self.assertEqual(len(p.channel.sent), before)
        p.channel.bufferedAmount = 0
        p.drain()
        self.assertEqual([u.op for u in self.wire_ops(p)[before:]], [PATCH, CLEAR])

    def test_late_viewer_asks_for_a_new_epoch(self):
        first = self.seeded(); first.drain()
        self.feed(self.history()); first.drain()
        late = self.peer()
        self.assertTrue(late.snapshot and not late.synced)
        late.drain()
        self.assertEqual(self.resyncs, 1)
        self.assertEqual(late.channel.sent, [])
        self.feed(self.history(marker=b'b'))
        late.drain()
        self.assertEqual(late.channel.sent, [], 'it missed the start of this history')
        self.s.feed(self.reset(2)); self.serial = 1
        self.assertTrue(late.synced)
        self.s.feed(self.history(marker=b'c').wire(2, 2, advanced=True))
        late.drain()
        self.assertEqual([u.op for u in self.wire_ops(late)], [RESET, PATCH])

    def test_snapshot_stays_after_viewer_without_history_joins(self):
        older = self.seeded(version=6); older.drain()
        self.feed(self.history())
        older.drain()
        self.assertFalse(any(u.history for u in self.wire_ops(older)))
        self.assertTrue(older.snapshot, 'it must wait for an epoch without history')

    def test_overflow_restarts_the_epoch_rather_than_replaying_old_states(self):
        p = self.seeded(); p.drain()
        p.ack_pending = 24
        big = Update(PATCH, 1, 0, 128, 64, 0, 0, 64, 64, b'\0\0\0\x10' + b'x' * 6000, history=True)
        for _ in range(MAX_STREAM_BYTES // 6000 + 2):
            self.feed(big)
        self.assertEqual(p.overflows, 1)
        self.assertFalse(p.stream); self.assertEqual(p.stream_bytes, 0)
        self.assertTrue(p.snapshot and not p.synced)
        p.drain()
        self.assertEqual(self.resyncs, 1)

    def test_copy_without_history_still_forces_a_new_epoch_for_snapshot(self):
        # A viewer that supports history is not excused from a plain copied epoch.
        p = self.seeded()
        self.feed(Update(COPY, 1, 0, 128, 64, 64, 0, 32, 32, struct.pack('!HH', 0, 0), capture=9))
        self.assertTrue(self.s.copied and not self.s.history)
        p.drain()
        self.assertEqual(self.resyncs, 1)
        self.assertEqual(p.channel.sent, [])

    def test_backlog_reports_capacity_and_reserves_video_budget(self):
        demands = []
        p = Peer(self.s, lambda _: None, self.resync, bitrate=lambda: 2000, demand=demands.append); p.version = 7
        p.channel = Channel(); p.enable(True)
        self.s.feed(self.reset(1)); self.serial = 1
        p.drain()
        p.ack_pending = 24
        self.feed(self.history())
        p._reservation()
        self.assertTrue(demands and demands[-1] > 0)

    def test_stream_unfenced_record_keeps_the_band_share(self):
        p = self.seeded(); p.drain(); p.ack_pending = 24
        self.s.copy_at = float('-inf')
        self.assertLess(p._rate(), 20000 * 125 * .5)
        p.stream.append(replace(self.history(), unfenced=True))
        self.assertEqual(p._rate(), min(1024 * 1024, 20000 * 125 * .75))
        p.stream.clear()


class FlightTests(unittest.TestCase):
    def test_jitter_does_not_shrink_the_window_but_a_standing_queue_does(self):
        f = Flight(); t = 100.0; serial = 0
        f.on_send(1, 100, t); t += .2; f.on_ack(1, t); serial = 1
        before = f.window
        # 200 ms path where some replies are late by 150 ms: the fastest stays at base.
        for n in range(40):
            for extra in (0.0, .15):
                serial += 1; f.on_send(serial, 100, t)
                t += .2 + extra; f.on_ack(serial, t)
        self.assertEqual(f.window, before)
        self.assertEqual(f.queued_at, -float('inf'))
        for n in range(20):
            serial += 1; f.on_send(serial, 100, t)
            t += .2 + .15; f.on_ack(serial, t)
        self.assertLess(f.window, before)
        self.assertGreater(f.queued_at, 100.0)

    def test_growing_acknowledgement_delay_shrinks_the_window(self):
        f = Flight(); t = 100.0; serial = 0
        # A quiet path: 20 ms acknowledgements while blocked grow the window.
        for _ in range(40):
            serial += 1; f.on_send(serial, 1000, t)
            f.blocked = True
            t += .02; f.on_ack(serial, t)
            t += .05
        grown = f.window
        self.assertGreater(grown, Flight.START_WINDOW)
        # The same path with a growing queue must back off below what it had.
        for n in range(40):
            serial += 1; f.on_send(serial, 1000, t)
            t += .02 + .1 + n * .01; f.on_ack(serial, t)
        self.assertLess(f.window, grown)
        self.assertGreaterEqual(f.window, Flight.MIN_WINDOW)

    def test_window_blocks_but_one_record_always_progresses_and_silence_expires(self):
        f = Flight(); f.window = 10000
        self.assertTrue(f.admits(50000, 1.0), 'an oversized record may go when nothing is in flight')
        f.on_send(1, 9000, 1.0)
        self.assertTrue(f.admits(1000, 1.1))
        self.assertFalse(f.admits(1001, 1.1)); self.assertTrue(f.blocked)
        self.assertTrue(f.admits(1001, 1.0 + Flight.LOST_SECONDS + .1), 'no acknowledgement cannot hold pixels forever')
        self.assertEqual(f.flight, 0)

    def test_rate_is_measured_only_while_the_window_holds_bytes_back(self):
        f = Flight(); f.window = 1000; t = 50.0
        f.on_send(1, 500, t); t += .3; f.on_ack(1, t)
        self.assertEqual(f.rate, 0, 'sparse traffic does not describe the path')
        f.on_send(2, 1000, t)
        self.assertFalse(f.admits(1000, t))
        t += .3; f.on_ack(2, t)
        self.assertAlmostEqual(f.rate, 1000 / .3, places=3)
        self.assertEqual(f.rate_time, t)
        f.on_send(3, 100, t); t += .3; f.on_ack(3, t)
        self.assertAlmostEqual(f.rate, 1000 / .3, places=3, msg='no update without blocking')

    def test_ack_ignores_newer_serials_and_handles_wrap(self):
        f = Flight(); f.on_send(0xffff_ffff, 100, 1.0); f.on_send(0, 100, 1.0); f.on_send(1, 100, 1.0)
        f.on_ack(0, 1.05)
        self.assertEqual(list(f.sent), [1]); self.assertEqual(f.flight, 100)


class PeerFlowTests(CopyProtocolTests):
    def setUp(self):
        super().setUp()
        self.capacity = []
        self.peer.capacity = self.capacity.append

    def test_blocked_tiles_wait_for_an_acknowledgement_and_copies_keep_their_order(self):
        self.peer.flow.window = Flight.MIN_WINDOW
        for x in range(0, 256, 64):
            self.tile(x, 1)
        self.copy()
        self.peer.flow.reset(); self.channel.sent.clear()
        self.peer.flow.on_send(self.peer.serial, Flight.MIN_WINDOW, time.monotonic())
        self.peer.drain()
        self.assertEqual(self.channel.sent, [])
        self.assertTrue(self.peer.flow.blocked)
        self.assertEqual(len(self.peer.segments), 1, 'the copy stays behind the blocked tiles')

    def test_capacity_is_reported_only_while_the_path_is_the_limit(self):
        self.peer.flow.rate = 100000.0; self.peer.flow.blocked_at = time.monotonic()
        self.peer.flow.rate_time = time.monotonic()
        self.peer.pending[(0, 0)] = Update(PATCH, x=0, w=64, h=64)
        self.peer._report_capacity(time.monotonic())
        self.assertEqual(self.capacity, [], 'a small window alone does not describe the path')
        self.peer.flow.queued_at = time.monotonic()
        self.peer._report_capacity(time.monotonic())
        self.assertEqual(self.capacity, [90000])
        self.peer.flow.rate = 95000.0
        self.peer._report_capacity(time.monotonic())
        self.assertEqual(self.capacity, [90000], 'small changes do not chatter')
        self.peer.flow.blocked_at = time.monotonic() - 2
        self.peer.pending.clear()
        self.peer._report_capacity(time.monotonic())
        self.assertEqual(self.capacity, [90000, 0])

    def test_stale_rate_never_limits_the_worker(self):
        self.peer.flow.rate = 8000.0; self.peer.flow.blocked_at = time.monotonic()
        self.peer.flow.queued_at = time.monotonic()
        self.peer.flow.rate_time = time.monotonic() - 5
        self.peer.pending[(0, 0)] = Update(PATCH, x=0, w=64, h=64)
        self.peer._report_capacity(time.monotonic())
        self.assertEqual(self.capacity, [])

    def test_keystroke_response_does_not_wait_behind_a_repaint(self):
        self.peer.flow.window = Flight.MIN_WINDOW
        self.peer.flow.on_send(1, Flight.MIN_WINDOW, time.monotonic())
        plain = Update(PATCH, w=64, h=64, payload=b'x' * 100)
        self.assertFalse(self.peer._flow_admits(plain, time.monotonic()))
        self.assertTrue(self.peer._flow_admits(replace(plain, early=True), time.monotonic()))
        self.peer.interactive = lambda: True
        self.assertTrue(self.peer._flow_admits(plain, time.monotonic()))

    def test_older_peer_has_no_flow_limit(self):
        self.peer.version = 5
        self.peer.flow.window = 1; self.peer.flow.on_send(1, 100000, time.monotonic())
        self.assertTrue(self.peer._flow_admits(Update(PATCH, w=64, h=64), time.monotonic()))
        self.peer.acknowledged(1)
        self.assertEqual(self.peer.flow.flight, 100000)

    def test_sctp_window_follows_the_flow_window_and_never_lowers(self):
        class Transport: _cwnd = 3600
        self.channel.transport = Transport()
        self.peer.drain()
        self.assertEqual(self.channel.transport._cwnd, Flight.START_WINDOW)
        self.channel.transport._cwnd = 500000
        self.peer.drain()
        self.assertEqual(self.channel.transport._cwnd, 500000)


class BandBacklogTests(CopyProtocolTests):
    def test_queued_strips_keep_the_share_after_motion_stops(self):
        self.peer.credit = 0; self.peer._burst = lambda: 0
        self.feed(Update(PATCH, x=0, w=64, h=64, unfenced=True, payload=zlib.compress(b'\1\2\3\xff' * 4096)))
        self.source.copy_at = time.monotonic() - 10
        self.assertTrue(self.peer.pending)
        self.assertEqual(self.peer._rate(), 8000 * 125 * .75)
        self.peer.pending.clear()
        self.assertEqual(self.peer._rate(), 8000 * 125 * .15)


class InputResponseShareTests(CopyProtocolTests):
    def test_unfenced_response_lifts_the_share_without_any_copy(self):
        self.assertEqual(self.peer._rate(), 8000 * 125 * .15)
        self.feed(Update(PATCH, x=0, w=64, h=64, unfenced=True, payload=zlib.compress(b'\1\2\3\xff' * 4096)))
        self.assertEqual(self.peer._rate(), 8000 * 125 * .75)
        self.assertFalse(self.source.copied, 'tile cache still describes the desktop')


class LayoutProtocolTests(unittest.TestCase):
    @staticmethod
    def advanced_source():
        s=Source();s.feed(Update(HELLO,payload=struct.pack('!I',15)).wire(0,0))
        s.feed(Update(RESET,width=128,height=64,capture=100,layout=7).wire(1,1,advanced=True))
        return s

    def test_layout_and_early_flag_roundtrip_and_legacy_downgrade(self):
        p=patch();p=Update(**{**p.__dict__,'capture':200,'layout':7,'early':True})
        new=decode(p.wire(1,2,advanced=True))
        self.assertEqual((new.layout,new.early,new.capture),(7,True,200))
        old=decode(p.wire(1,2))
        self.assertEqual((old.layout,old.early,old.capture),(0,False,200))
        with self.assertRaises(ValueError):
            decode(Update(**{**p.__dict__,'layout':0}).wire(1,2,advanced=True))

    def test_scene_requires_negotiated_capability_and_bounded_layout(self):
        s=source()
        s.feed(Update(SCENE,width=128,height=64,capture=200,layout=7).wire(1,2,advanced=True))
        self.assertTrue(s.awaiting_reset)
        with self.assertRaises(ValueError):
            decode(Update(SCENE,width=128,height=64,capture=200,layout=1<<53).wire(1,2,advanced=True))

    def test_old_peer_receives_no_scene_or_early_flags(self):
        s=self.advanced_source();p=Peer(s,lambda _:None,lambda:None);p.version=2;p.channel=Channel();p.enable(True);p.drain()
        self.assertEqual(decode(p.channel.sent[0]).layout,0)
        s.feed(Update(SCENE,width=128,height=64,capture=200,layout=8).wire(1,2,advanced=True));p.drain()
        self.assertEqual(len(p.channel.sent),1)
        candidate=patch(serial=3)
        candidate=Update(**{**candidate.__dict__,'capture':300,'layout':8,'early':True})
        s.feed(candidate.wire(1,3,advanced=True));p.drain()
        self.assertFalse(decode(p.channel.sent[-1]).early)
        s.unsubscribe(p.receive)

    def test_scene_precedes_early_updates_and_background_cannot_take_reserved_slots(self):
        s=self.advanced_source();p=Peer(s,lambda _:None,lambda:None);p.version=5;p.channel=Channel();p.enable(True);p.drain()
        p.channel.sent.clear();p.ack_pending=24
        s.feed(Update(SCENE,width=128,height=64,capture=200,layout=8).wire(1,2,advanced=True))
        for serial,x,early in [(3,0,True),(4,64,False)]:
            candidate=patch(serial=serial,x=x)
            candidate=Update(**{**candidate.__dict__,'capture':300,'layout':8,'early':early})
            s.feed(candidate.wire(1,serial,advanced=True))
        p.channel.bufferedAmount=MAX_BUFFERED+1;p.drain();self.assertEqual(p.channel.sent,[])
        p.channel.bufferedAmount=0;p.drain()
        self.assertEqual([HEADER.unpack_from(r)[1] for r in p.channel.sent],[SCENE,6])
        self.assertEqual(HEADER.unpack_from(p.channel.sent[-1])[2]&2,2)
        self.assertEqual(len(p.pending),1)
        self.assertEqual(HEADER.unpack_from(p.channel.sent[0])[3],16)
        s.unsubscribe(p.receive)

if __name__ == '__main__': unittest.main()
