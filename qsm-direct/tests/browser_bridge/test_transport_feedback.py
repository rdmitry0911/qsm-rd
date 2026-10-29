"""Transport feedback vectors, hostile messages and packet-credit ownership."""
import struct
import unittest

from extensions.browser_bridge.qsm_transport_feedback import (
    MAX_REPORTED_PACKETS, PacketHistory, Report, decode, separate)


def feedback(base=0, statuses=(1,), deltas=(0,), *, reference=0, media=1):
    body = struct.pack('!IIHH', 2, media, base, len(statuses)) + reference.to_bytes(3, 'big') + b'\0'
    for start in range(0, len(statuses), 7):
        chunk = 0xc000
        for i, symbol in enumerate(statuses[start:start + 7]):
            chunk |= symbol << (12 - 2 * i)
        body += struct.pack('!H', chunk)
    delta = iter(deltas)
    for symbol in statuses:
        if symbol in (1, 2):
            body += next(delta).to_bytes(1 if symbol == 1 else 2, 'big', signed=symbol == 2)
    pad = (-len(body)) % 4
    if pad:
        body += bytes(pad - 1) + bytes([pad])
    return struct.pack('!BBH', 0x8f | (32 if pad else 0), 205, len(body) // 4) + body


class ParseTests(unittest.TestCase):
    def test_actual_firefox_run_with_rtcp_padding(self):
        raw = bytes.fromhex('afcd0006811627f23dd77b69000000040010800020049800000a0002')
        value = decode(raw)
        self.assertEqual(value.media_ssrc, 0x3dd77b69)
        self.assertEqual(value.received, ((0, 270374000), (1, 270374000),
                                         (2, 270374000), (3, 270376500)))

    def test_two_bit_status_negative_delta_loss_and_sequence_wrap(self):
        value = decode(feedback(65534, (1, 0, 2, 1), (4, -8, 3), reference=1))
        self.assertEqual(value.received, ((65534, 65000), (0, 63000), (1, 63750)))

    def test_one_bit_vector_and_unused_tail(self):
        raw = bytearray(feedback(10, (1, 1, 0), (1, 2)))
        raw[20:22] = struct.pack('!H', 0xb7ff)  # 1,1,0 and unused one bits.
        value = decode(bytes(raw))
        self.assertEqual(value.received, ((10, 250), (11, 750)))

    def test_empty_report_cannot_acknowledge_anything(self):
        self.assertEqual(decode(feedback(statuses=(), deltas=())).received, ())

    def test_malformed_headers_lengths_padding_counts_and_statuses(self):
        good = feedback()
        cases = [b'', good[:-1], bytes([0x4f]) + good[1:], good[:1] + b'\xcc' + good[2:]]
        for offset, value in ((2, b'\0\0'), (14, struct.pack('!H', MAX_REPORTED_PACKETS + 1)),
                              (20, b'\0\0'), (20, b'\x60\x01')):
            raw = bytearray(good);raw[offset:offset + len(value)] = value;cases.append(bytes(raw))
        padded = bytearray(good);padded[0] |= 32;padded[-1] = 0;cases.append(bytes(padded))
        padded[-1] = 255;cases.append(bytes(padded))
        cases.append(feedback(statuses=(3,), deltas=()))
        for raw in cases:
            with self.subTest(raw=raw.hex()), self.assertRaises(ValueError):
                decode(raw)

    def test_missing_large_delta_and_nonzero_tail_are_rejected(self):
        raw = bytearray(feedback(statuses=(1,), deltas=(0,)))
        raw[20:22] = struct.pack('!H', 0xe000)  # Two-byte delta, only one data byte.
        with self.assertRaises(ValueError):
            decode(bytes(raw))
        raw = bytearray(feedback());raw[0] &= ~32;raw[-1] = 5
        with self.assertRaises(ValueError):
            decode(bytes(raw))

    def test_all_truncations_and_mutated_count_remain_bounded(self):
        raw = feedback(statuses=(1, 2, 0, 1), deltas=(2, -4, 3))
        for length in range(len(raw)):
            with self.assertRaises(ValueError):
                decode(raw[:length])
        for count in (4097, 32768, 65535):
            malformed = bytearray(raw);malformed[14:16] = struct.pack('!H', count)
            with self.assertRaises(ValueError):
                decode(bytes(malformed))

    def test_compound_preserves_other_rtcp_byte_for_byte(self):
        nack = bytes.fromhex('81cd0003000000020000000100070000')
        raw = feedback(statuses=(1, 1), deltas=(0, 0))
        reports, remaining = separate(nack + raw + nack)
        self.assertEqual(len(reports), 1)
        self.assertEqual(remaining, nack + nack)
        self.assertEqual(separate(nack), ([], nack))

    def test_padding_on_a_nonfinal_compound_block_gives_no_credit(self):
        nack = bytes.fromhex('81cd0003000000020000000100070000')
        self.assertEqual(separate(feedback() + nack), ([], nack))

    def test_bad_feedback_does_not_consume_a_valid_nack_or_apply_partial_credit(self):
        nack = bytes.fromhex('81cd0003000000020000000100070000')
        bad = feedback(statuses=(3,), deltas=())
        self.assertEqual(separate(feedback() + bad + nack), ([], nack))
        malformed = feedback() + b'\x80'
        self.assertEqual(separate(malformed), ([], malformed))
        oversized = b'\x80' * 65537
        self.assertEqual(separate(oversized), ([], oversized))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.history = PacketHistory({1, 2}, clock=lambda: self.now)

    def report(self, *sequences, arrival=50000, media=1):
        return Report(media, (arrival // 64000) & 0xffffff,
                      tuple((s, arrival) for s in sequences))

    def test_partial_video_prefix_frees_credit_without_a_complete_frame(self):
        h = self.history
        h.sent(100);h.sent(200);h.sent(300)
        self.now += .05
        self.assertTrue(h.receive(self.report(0, 1)))
        self.assertEqual(h.credit(0), 300)
        self.assertEqual(h.credit(300), 300, 'frame and packet credit must not be added twice')

    def test_out_of_order_received_packets_do_not_forgive_a_hole(self):
        h = self.history
        h.sent(100);h.sent(200)
        self.now += .05
        self.assertFalse(h.receive(self.report(1)))
        self.assertEqual(h.credit(0), 0)
        self.assertTrue(h.receive(self.report(0, arrival=49000)))
        self.assertEqual(h.credit(0), 300)

    def test_duplicate_unknown_future_and_foreign_reports_cannot_create_credit(self):
        h = self.history;h.sent(100);self.now += .05
        for report in (self.report(0, media=99), self.report(4), self.report(65535)):
            self.assertFalse(h.receive(report))
        self.assertEqual(h.credit(0), 0)
        self.assertTrue(h.receive(self.report(0)))
        when = h._last_video
        self.now += 1
        self.assertFalse(h.receive(self.report(0)))
        self.assertEqual(h._last_video, when)
        self.assertEqual(h.credit(101), 0, 'unmatched future completed bytes are not trusted')

    def test_audio_is_numbered_but_cannot_acknowledge_video(self):
        h = self.history
        self.assertEqual([h.sent(100), h.sent(0), h.sent(200)], [0, 1, 2])
        self.now += .05
        self.assertFalse(h.receive(self.report(1)))
        self.assertEqual(h.credit(0), 0)
        self.assertTrue(h.receive(self.report(0)))
        self.assertEqual(h.credit(0), 100)

    def test_frame_ack_does_not_erase_packet_delivery_timing(self):
        h = self.history;h.sent(100);h.sent(200);self.now += .05
        self.assertEqual(h.credit(100), 100)
        self.assertFalse(h.receive(self.report(0, arrival=10000)))
        self.assertEqual(h.take_delivery(), ((10., .01, 100),))
        self.assertEqual(h.take_delivery(), ())
        self.assertFalse(h.receive(self.report(0, arrival=10000)))
        self.assertEqual(h.take_delivery(), ())
        self.assertTrue(h.receive(self.report(1, arrival=20000)))
        self.assertEqual(h.take_delivery(), ((10., .02, 200),))
        self.assertEqual(h.credit(100), 300)

    def test_frame_ack_repairs_an_old_hole_before_later_rtx_credit(self):
        h = self.history
        h.sent(100);h.sent(200);h.sent(80)  # Original, following video, repair RTP.
        self.now += .05
        h.receive(self.report(1, 2))
        self.assertEqual(h.credit(0), 0)
        self.assertEqual(h.credit(100), 380)
        self.assertEqual(h.credit(380), 380)

    def test_stale_packet_feedback_restores_complete_frame_credit(self):
        h = self.history;h.sent(100);self.now += .05;h.receive(self.report(0))
        self.now += 1.6
        self.assertEqual(h.credit(0), 0)
        self.assertEqual(h.credit(100), 100)
        h.sent(200);self.now += .05
        self.assertTrue(h.receive(self.report(1, arrival=1700000)))
        self.assertEqual(h.credit(100), 300)

    def test_history_eviction_never_implicitly_acknowledges_video(self):
        h = self.history;h.MAX_PACKETS = 3
        for _ in range(4):
            h.sent(10)
        self.assertEqual(len(h._packets), 3)
        self.now += .05;h.receive(self.report(1, 2, 3))
        self.assertEqual(h.credit(0), 0)
        self.assertEqual(h.credit(10), 40, 'a host-matched frame can cover the evicted hole')

    def test_old_records_expire_and_close_retires_feedback(self):
        h = self.history;h.sent(100);self.now += 8
        self.assertFalse(h.receive(self.report(0)))
        self.assertFalse(h._packets)
        h.sent(200);self.now += .05;h.clear()
        self.assertFalse(h.receive(self.report(1, arrival=8050000)))
        self.assertEqual(h.credit(100), 100)

    def test_sequence_wrap_and_old_payload_replay_do_not_acknowledge_new_packet(self):
        h = self.history;h.sent(100);self.now += .05;old = self.report(0);h.receive(old)
        for _ in range(65535):
            h.sent(0)
        h.credit(100)
        self.now = 12.
        self.assertEqual(h.sent(200), 0)
        self.now += .05
        self.assertFalse(h.receive(old))
        self.assertEqual(h.credit(100), 100)
        self.assertTrue(h.receive(self.report(0, arrival=2000000)))
        self.assertEqual(h.credit(100), 300)

    def test_receiver_reference_wrap(self):
        h = self.history;h.sent(100)
        arrival = ((1 << 24) - 1) * 64000 + 32000
        self.now += .05;self.assertTrue(h.receive(self.report(0, arrival=arrival)))
        h.sent(200);self.now += .05
        self.assertTrue(h.receive(Report(1, 0, ((1, 10000),))))
        self.assertEqual(h.credit(0), 300)

    def test_bad_local_costs_do_not_allocate_sequence_or_history(self):
        for value in (-1, True, 65600, '100'):
            with self.assertRaises(ValueError):
                self.history.sent(value)
        self.assertEqual(self.history.next_sequence, 0)
