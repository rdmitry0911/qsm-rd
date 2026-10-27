import asyncio
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


class ExactUiTests(unittest.TestCase):
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
