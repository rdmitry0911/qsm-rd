"""SCTP receive path: ordered control messages behind other channels' TSNs survive a FORWARD TSN."""
import asyncio
import types
import unittest

import aiortc.rtcsctptransport as sctp

from extensions.browser_bridge import qsm_browser_bridge as bridge

ORDERED = sctp.SCTP_DATA_FIRST_FRAG | sctp.SCTP_DATA_LAST_FRAG


def chunk(tsn, seq, data, stream=1, flags=ORDERED):
    value = sctp.DataChunk(flags=flags)
    value.tsn, value.stream_id, value.stream_seq, value.protocol, value.user_data = tsn, stream, seq, 51, data
    return value


def lost_message_scenario(stream):
    """Control message 0 (TSN 9) is retransmitted after messages 1 (TSN 10) and 2 (TSN 12)
    arrived; TSN 11 belongs to another data channel."""
    delivered = []
    for value in (chunk(10, 1, b"one"), chunk(12, 2, b"two")):
        stream.add_chunk(value)
        delivered += [message[2] for message in stream.pop_messages()]
    stream.add_chunk(chunk(9, 0, b"zero"))
    delivered += [message[2] for message in stream.pop_messages()]
    return delivered


class InboundStreamTests(unittest.TestCase):
    """Ordered-delivery expectations hold for the backport and for aiortc >= 1.16 itself."""

    @unittest.skipUnless(bridge.AIORTC_SCTP_RECEIVE_PATCHED, "aiortc >= 1.16 has its own fix")
    def test_patch_is_installed_for_the_pinned_aiortc(self):
        self.assertTrue(bridge.AIORTC_SCTP_RECEIVE_PATCHED)
        self.assertIs(sctp.InboundStream.pop_messages, bridge._sctp_pop_messages)
        self.assertIs(sctp.InboundStream.prune_chunks, bridge._sctp_prune_chunks)

    @unittest.skipUnless(bridge.AIORTC_SCTP_RECEIVE_PATCHED, "only aiortc < 1.16 has the defect")
    def test_unpatched_aiortc_strands_a_deliverable_message_and_prunes_it(self):
        # The defect this patch removes, reproduced with aiortc 1.11's own methods.
        stream = sctp.InboundStream()
        stream.pop_messages = types.MethodType(bridge.AIORTC_SCTP_RECEIVE_ORIGINAL[0], stream)
        stream.prune_chunks = types.MethodType(bridge.AIORTC_SCTP_RECEIVE_ORIGINAL[1], stream)
        self.assertEqual(lost_message_scenario(stream), [b"zero", b"one"])
        stream.prune_chunks(12)  # a FORWARD TSN of the lossy pointer channel
        self.assertEqual(stream.reassembly, [])
        self.assertEqual(list(stream.pop_messages()), [])
        self.assertEqual(stream.sequence_number, 2)  # message 2 is gone; the stream waits forever

    def test_every_deliverable_ordered_message_is_delivered(self):
        stream = sctp.InboundStream()
        self.assertEqual(lost_message_scenario(stream), [b"zero", b"one", b"two"])
        self.assertEqual(stream.sequence_number, 3)
        self.assertEqual(stream.reassembly, [])

    def test_waiting_messages_are_kept_and_a_gap_still_blocks(self):
        stream = sctp.InboundStream()
        stream.sequence_number = 5
        stream.add_chunk(chunk(20, 6, b"six"))
        self.assertEqual(list(stream.pop_messages()), [])
        stream.add_chunk(chunk(18, 5, b"five"))  # a retransmission of the earlier TSN
        self.assertEqual([m[2] for m in stream.pop_messages()], [b"five", b"six"])

    def test_fragmented_message_is_reassembled_before_the_next_message(self):
        stream = sctp.InboundStream()
        stream.add_chunk(chunk(1, 0, b"ab", flags=sctp.SCTP_DATA_FIRST_FRAG))
        stream.add_chunk(chunk(4, 1, b"next"))
        self.assertEqual(list(stream.pop_messages()), [])
        stream.add_chunk(chunk(2, 0, b"cd", flags=sctp.SCTP_DATA_LAST_FRAG))
        self.assertEqual([m[2] for m in stream.pop_messages()], [b"abcd", b"next"])

    def test_unordered_messages_pass_a_gap(self):
        stream = sctp.InboundStream()
        unordered = ORDERED | sctp.SCTP_DATA_UNORDERED
        stream.add_chunk(chunk(3, 0, b"late", stream=4, flags=unordered))
        stream.add_chunk(chunk(5, 0, b"later", stream=4, flags=unordered))
        self.assertEqual([m[2] for m in stream.pop_messages()], [b"late", b"later"])

    def test_prune_keeps_a_message_continuing_past_the_tsn(self):
        stream = sctp.InboundStream()
        stream.sequence_number = 1
        stream.add_chunk(chunk(5, 0, b"old"))  # abandoned, before the TSN
        stream.add_chunk(chunk(7, 1, b"he", flags=sctp.SCTP_DATA_FIRST_FRAG))
        stream.add_chunk(chunk(8, 1, b"llo", flags=0))
        self.assertEqual(stream.prune_chunks(8), 3)
        self.assertEqual([c.tsn for c in stream.reassembly], [7, 8])
        stream.add_chunk(chunk(9, 1, b"!", flags=sctp.SCTP_DATA_LAST_FRAG))
        self.assertEqual([m[2] for m in stream.pop_messages()], [b"hello!"])


@unittest.skipUnless(bridge.AIORTC_SCTP_RECEIVE_PATCHED, "the backported FORWARD TSN handler is not installed")
class ForwardTsnTests(unittest.TestCase):
    def transport(self):
        received = []
        streams = {}

        def get_stream(stream_id):
            return streams.setdefault(stream_id, sctp.InboundStream())

        async def receive(stream_id, protocol, data):
            received.append(data)
        value = types.SimpleNamespace(
            _sack_needed=False, _last_received_tsn=8, _sack_misordered={10, 12}, _sack_duplicates=[],
            _inbound_streams=streams, _advertised_rwnd=0, _get_inbound_stream=get_stream, _receive=receive)
        return value, received, get_stream

    def test_forward_tsn_after_retransmission_loses_no_control_message(self):
        transport, received, get_stream = self.transport()
        control = get_stream(1)
        lost_message_scenario(control)
        forward = sctp.ForwardTsnChunk()
        forward.cumulative_tsn = 12
        asyncio.run(bridge._sctp_receive_forward_tsn_chunk(transport, forward))
        self.assertEqual(control.sequence_number, 3)
        self.assertEqual(control.reassembly, [])

    def test_forward_tsn_never_moves_a_stream_backwards_and_skips_abandoned(self):
        transport, received, get_stream = self.transport()
        stream = get_stream(2)
        stream.sequence_number = 7
        stream.add_chunk(chunk(13, 8, b"eight", stream=2))
        forward = sctp.ForwardTsnChunk()
        forward.cumulative_tsn = 12
        forward.streams = [(2, 3)]
        asyncio.run(bridge._sctp_receive_forward_tsn_chunk(transport, forward))
        self.assertEqual(stream.sequence_number, 7)
        forward = sctp.ForwardTsnChunk()
        forward.cumulative_tsn = 13
        forward.streams = [(2, 7)]  # message 7 abandoned: 8 becomes deliverable
        transport._last_received_tsn = 12
        asyncio.run(bridge._sctp_receive_forward_tsn_chunk(transport, forward))
        self.assertEqual(received, [b"eight"])
        self.assertEqual(stream.sequence_number, 9)


if __name__ == "__main__":
    unittest.main()
