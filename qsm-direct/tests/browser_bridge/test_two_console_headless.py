"""Headless two-Console check of contract CONSOLE-ACTIVE-SIZE-1: no resize loop.

Two Consoles of different sizes (the VM118 pair 1280x798 / 1324x626, hz-main
vm118-pvg0.log 17:18:57-18:30:02 CET: one Display1 sender, ui_info ping-pong)
show one VM display.  The console's own resize/role code runs in node
(tests/proxmox_pve9/two_console_peers.mjs: shared fake clock and one fake
display); every message it sends goes into a real BrowserWebRtcBridge peer,
both sharing one UnixInputEgress like the Consoles of one media worker, and
the display applies only the size requests that egress forwards.  Data
channels, clock and display are doubles: not end-to-end evidence.
"""
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from extensions.browser_bridge.qsm_browser_bridge import (
    BrowserWebRtcBridge, UnixInputEgress, INPUT_HEADER, INPUT_RESIZE,
)

PEERS = Path(__file__).resolve().parents[1] / 'proxmox_pve9' / 'two_console_peers.mjs'


class Channel:
    def __init__(self, label):
        from pyee import EventEmitter
        self._events = EventEmitter()
        self.label = label
        self.ordered = label == 'qsm-control'
        self.maxRetransmits = None if label == 'qsm-control' else 0
        self.maxPacketLifeTime = None
        self.readyState = 'open'
        self.messages = []
        self.read = 0

    def on(self, *args): return self._events.on(*args)
    def emit(self, *args): self._events.emit(*args)
    def send(self, message): self.messages.append(message)

    def close(self):
        self.readyState = 'closed'
        self.emit('close')

    def take(self):
        new, self.read = self.messages[self.read:], len(self.messages)
        return new


@unittest.skipUnless(shutil.which('node'), 'node is required for the console side')
class TwoConsoleHeadlessCheck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.egress = UnixInputEgress(self.root)
        self.forwarded = []
        self.applied = 0
        self.egress._send_packet = lambda packet: self.forwarded.append(packet) or True
        self.bridges, self.channels = {}, {}
        self.node = subprocess.Popen(['node', str(PEERS)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.answer = None

    async def asyncTearDown(self):
        for bridge in self.bridges.values():
            await bridge.close()
        self.node.stdin.close()
        self.node.wait(timeout=10)

    def call(self, **command):
        self.node.stdin.write(json.dumps(command) + '\n')
        self.node.stdin.flush()
        answer = json.loads(self.node.stdout.readline())
        self.assertNotIn('error', answer, answer.get('error'))
        self.answer = answer
        return answer['outbox']

    def pump(self, outbox):
        # Console messages to the bridges, forwarded sizes to the display,
        # role messages to the Consoles, until nothing moves.
        while outbox:
            for message in outbox:
                self.channels[message['peer']][message['channel']].emit('message', message['data'])
            outbox = []
            for packet in self.forwarded[self.applied:]:
                if packet[5] == INPUT_RESIZE:
                    width, height = struct.unpack_from('!II', packet, INPUT_HEADER.size)
                    outbox += self.call(cmd='apply', width=width, height=height)
            self.applied = len(self.forwarded)
            for name, channels in self.channels.items():
                for data in channels['control'].take():
                    if json.loads(data).get('op') == 'qsm_display_role':
                        outbox += self.call(cmd='deliver', peer=name, data=data)

    def attach(self, name, box):
        bridge = BrowserWebRtcBridge(self.root / name, shared_input=self.egress)
        self.bridges[name] = bridge
        channels = {'control': Channel('qsm-control'), 'pointer': Channel('qsm-pointer')}
        self.channels[name] = channels
        # The terminal sees the channel open before the Console's first message.
        bridge._on_datachannel(channels['control'])
        bridge._on_datachannel(channels['pointer'])
        self.pump(self.call(cmd='attach', peer=name, box=box))

    def close(self, name):
        self.call(cmd='close', peer=name)
        self.channels[name]['control'].close()
        self.pump([])

    def work(self, name, ms, *, also=None):
        """The user works in one Console: pointer input every 100 ms; each
        Console's layout callback without a size change every second."""
        for t in range(0, ms, 100):
            self.pump(self.call(cmd='input', peer=name))
            if also is not None:
                also(t)
            if t % 1000 == 0:
                for other in self.channels:
                    if self.channels[other]['control'].readyState == 'open':
                        self.pump(self.call(cmd='layout', peer=other, box=self.boxes[other]))
            self.pump(self.call(cmd='tick', ms=100))

    def changes(self):
        self.call(cmd='state')
        return [entry['size'] for entry in self.answer['display']['log']]

    async def test_two_consoles_of_different_sizes_do_not_resize_loop(self):
        self.boxes = {'A': [1280, 798], 'B': [1324, 626]}
        self.attach('A', self.boxes['A'])
        self.work('A', 5_000)
        self.assertEqual(self.changes(), ['1280x798'], 'the only Console sizes the guest')

        # B opens while the user works in A, and B's window is dragged
        # (1324x626 -> 1400x700) without input in it: A keeps the guest.
        self.attach('B', self.boxes['B'])
        drag = [[1324 + 2 * i, 626 + (74 * i // 38) // 2 * 2] for i in range(1, 39)]

        def drag_b(t):
            step = t // 100
            if step < len(drag):
                self.boxes['B'] = drag[step]
                self.pump(self.call(cmd='layout', peer='B', box=drag[step]))
        self.work('A', 30_000, also=drag_b)
        self.assertEqual(self.boxes['B'], [1400, 700])
        self.assertEqual(self.changes(), ['1280x798'], 'a passive Console never resizes the guest')
        self.assertEqual(self.answer['guest'], '1280x798')

        # The user switches to B, back to A, and A's Console goes away.
        self.work('B', 20_000)
        self.assertEqual(self.changes(), ['1280x798', '1400x700'], 'input in B: one switch, one size')
        self.work('A', 20_000)
        self.assertEqual(self.changes(), ['1280x798', '1400x700', '1280x798'])
        self.close('A')
        del self.boxes['A']
        self.work('B', 10_000)
        changes = self.changes()
        print(f"\ntwo-console headless: mode changes={changes} "
              f"withheld={self.egress.size_requests_withheld} unchanged={self.answer['display']['unchanged']}")
        self.assertEqual(changes, ['1280x798', '1400x700', '1280x798', '1400x700'],
                         'one guest mode per activity switch, no alternation')
        self.assertEqual(self.answer['guest'], '1400x700')


if __name__ == '__main__':
    unittest.main()
