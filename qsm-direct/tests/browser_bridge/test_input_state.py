"""Input ownership and expiry across delayed reliable delivery, not video loss."""
import json
import tempfile
import unittest
from pathlib import Path

from extensions.browser_bridge.qsm_browser_bridge import (
    BrowserInputState, UnixInputEgress, BridgeError, INPUT_HEADER, INPUT_KEYBOARD,
    BrowserWebRtcBridge,
    INPUT_KEY_TAP,
)


class InputStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.egress = UnixInputEgress(Path(self.directory.name))
        self.clock = 100.0
        self.sent = []
        self.egress._send_packet = lambda packet: self.sent.append((self.clock, packet)) or True
        self.peer = BrowserInputState(self.egress, now=lambda: self.clock)

    @staticmethod
    def key(peer, down, key=30, **fields):
        peer.handle(json.dumps(dict(op='keyboard', key=key, down=down, modifiers=0, **fields)))

    @staticmethod
    def snapshot(peer, keys=(), buttons=()):
        peer.handle(json.dumps(dict(op='input_state', keys=list(keys), buttons=list(buttons))))

    def edges(self):
        return [(round(t - 100, 3), p[INPUT_HEADER.size + 2]) for t, p in self.sent
                if p[5] == INPUT_KEYBOARD]

    def test_delayed_keyup_cannot_turn_a_tap_into_native_autorepeat(self):
        self.snapshot(self.peer)
        self.key(self.peer, True, client_time_ms=1000)
        self.clock += .22
        self.peer.expire()
        self.assertEqual(self.edges(), [(0, 1), (.22, 0)])
        self.clock += .65
        self.snapshot(self.peer, [30])  # queued before the real keyup
        self.assertEqual(len(self.sent), 2, 'stale snapshots never re-press a released key')
        self.key(self.peer, False, client_time_ms=1100)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.peer.expired_keys, 1)
        self.assertEqual(self.peer.max_client_hold_ms, 100)
        self.assertEqual(self.peer.max_hold_ms, 870)

    def test_real_hold_renews_without_synthetic_release_or_repress(self):
        self.snapshot(self.peer)
        self.key(self.peer, True)
        for _ in range(60):
            self.clock += .05
            self.snapshot(self.peer, [30])
            self.peer.expire()
        self.assertEqual(self.edges(), [(0, 1)])
        self.key(self.peer, False)
        self.assertEqual(self.edges(), [(0, 1), (3, 0)])
        self.assertEqual(self.peer.expired_keys, 0)

    def test_latest_empty_state_repairs_a_failed_release_send(self):
        self.snapshot(self.peer)
        self.key(self.peer, True)
        self.clock += .05
        self.snapshot(self.peer)
        self.assertEqual(self.edges(), [(0, 1), (.05, 0)])

    def test_late_event_loop_wakeup_does_not_renew_expired_key(self):
        self.snapshot(self.peer)
        self.key(self.peer, True)
        self.clock += .7
        self.snapshot(self.peer, [30])
        self.assertEqual(self.edges(), [(0, 1), (.7, 0)])
        self.clock += .1
        self.snapshot(self.peer, [30])
        self.assertEqual(len(self.sent), 2)

    def test_pointer_activity_does_not_keep_a_stale_key_held(self):
        self.snapshot(self.peer)
        self.key(self.peer, True)
        self.clock += .21
        self.peer.handle('{"op":"mouse_position","x":1,"y":1,"width":64,"height":64}')
        self.assertEqual(self.edges(), [(0, 1), (.21, 0)])

    def test_shared_producer_releases_only_the_departing_viewers_keys(self):
        other = BrowserInputState(self.egress, now=lambda: self.clock)
        self.key(self.peer, True)
        self.key(other, True)
        self.peer.release()
        self.assertEqual(self.edges(), [(0, 1)])
        other.release()
        self.assertEqual(self.edges(), [(0, 1), (0, 0)])
        other.release()
        self.assertEqual(len(self.sent), 2)

    def test_snapshot_does_not_inject_keys_and_old_clients_still_hold(self):
        self.snapshot(self.peer, [31])
        self.assertEqual(self.sent, [])
        other = BrowserInputState(self.egress, now=lambda: self.clock)
        self.key(other, True)
        self.clock += 3
        other.expire()
        self.assertEqual(self.edges(), [(0, 1)])
        other.release()
        self.assertEqual(self.edges(), [(0, 1), (3, 0)])

    def test_snapshot_validation_and_bounded_key_state(self):
        for raw in ('{"op":"input_state","keys":[true],"buttons":[]}',
                    '{"op":"input_state","keys":[30,30],"buttons":[]}',
                    '{"op":"input_state","keys":[],"buttons":[6]}'):
            with self.assertRaises(BridgeError): self.peer.handle(raw)
        for key in range(32): self.key(self.peer, True, key)
        with self.assertRaises(BridgeError): self.key(self.peer, True, 50)

    def test_button_lease_and_ownership_use_the_same_safety_boundary(self):
        self.snapshot(self.peer)
        self.peer.handle('{"op":"mouse_button","button":1,"down":true}')
        self.clock += .21
        self.peer.expire()
        self.assertEqual(self.peer.expired_buttons, 1)
        self.assertEqual([p[-1] for _, p in self.sent], [1, 0])

    def test_atomic_tap_is_one_local_packet_with_no_held_character(self):
        self.peer.handle('{"op":"key_tap","key":30,"modifiers":[42]}')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1][5], INPUT_KEY_TAP)
        self.assertEqual(self.sent[0][1][INPUT_HEADER.size:], b'\x00\x1e\x00\x2a')
        self.clock += 2
        self.peer.expire()
        self.assertEqual(len(self.sent), 1, 'network delay cannot create another text action')
        self.assertEqual(self.peer.taps, 1)

    def test_atomic_tap_does_not_release_another_viewers_held_key(self):
        other = BrowserInputState(self.egress, now=lambda: self.clock)
        self.key(other, True)
        self.peer.handle('{"op":"key_tap","key":30}')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.peer.taps, 0)
        other.release()
        self.assertEqual(len(self.sent), 2)

    def test_atomic_action_repairs_its_owners_stale_modifier(self):
        self.key(self.peer, True, 29)
        self.peer.handle('{"op":"key_tap","key":30,"modifiers":[]}')
        self.assertEqual([p[5] for _, p in self.sent], [INPUT_KEYBOARD, INPUT_KEYBOARD, INPUT_KEY_TAP])
        self.assertEqual(self.sent[1][1][INPUT_HEADER.size:], b'\x00\x1d\x00\x00')

    def test_tap_modifiers_are_bounded_and_are_only_modifier_keys(self):
        for modifiers in ([True], [29, 29], [30], [31], list(range(10))):
            with self.assertRaises(BridgeError):
                self.peer.handle(json.dumps(dict(op='key_tap',key=30,modifiers=modifiers)))


class SharedBridgeInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_close_releases_one_peer_and_stops_queued_input(self):
        from pyee import EventEmitter

        class Channel(EventEmitter):
            label = 'qsm-control'
            ordered = True
            maxRetransmits = maxPacketLifeTime = None
            readyState = 'open'

            def send(self, _message): pass
            def close(self):
                self.readyState = 'closed'
                self.emit('close')

        with tempfile.TemporaryDirectory() as tmp:
            egress = UnixInputEgress(Path(tmp))
            sent = []
            egress._send_packet = lambda packet: sent.append(packet) or True
            first = BrowserWebRtcBridge(Path(tmp) / 'first', shared_input=egress)
            second = BrowserWebRtcBridge(Path(tmp) / 'second', shared_input=egress)
            try:
                channel = Channel()
                first._on_datachannel(channel)
                channel.emit('message', '{"op":"keyboard","key":30,"down":true,"modifiers":0}')
                InputStateTests.key(second._input_state, True, 31)
                channel.close()
                channel.emit('message', '{"op":"keyboard","key":32,"down":true,"modifiers":0}')
                self.assertEqual([p[INPUT_HEADER.size:] for p in sent],
                                 [b'\x00\x1e\x01\x00', b'\x00\x1f\x01\x00', b'\x00\x1e\x00\x00'])
                await first.close()
                self.assertEqual(len(sent), 3, 'retiring one bridge must not release another viewer')
                await second.close()
                self.assertEqual(sent[-1][INPUT_HEADER.size:], b'\x00\x1f\x00\x00')
            finally:
                await first.close()
                await second.close()


if __name__ == '__main__': unittest.main()
