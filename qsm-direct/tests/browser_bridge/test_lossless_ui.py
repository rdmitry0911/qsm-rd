import asyncio
from collections import OrderedDict
import struct
import time
import unittest
from unittest.mock import patch as mock_patch
import zlib
from extensions.browser_bridge.qsm_lossless_ui import (
    CLEAR, GAP, HEADER, HELLO, MAGIC, MAX_BUFFERED, PATCH, RESET,
    Peer, Source, Update, decode,
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


if __name__ == '__main__': unittest.main()
