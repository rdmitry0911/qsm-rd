#!/usr/bin/env python3
"""FS lines on the guest channel, and a real file peer process driven over WebRTC.

The guest here is a small stand-in that speaks the FS lines. The real helper has its
own tests, and the whole path runs with it in test_file_path.py.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'extensions'))
from browser_bridge import qsm_file_peer as peer_module  # noqa: E402
from direct_guest import qsm_guest_channel as channel_module  # noqa: E402
from direct_guest.qsm_guest_channel import GuestChannelError, QsmGuestChannel  # noqa: E402
from direct_terminal.qsm_file_session import FileSession, FileSessionError  # noqa: E402


class LineGuest:
    """One connected peer speaking QSF1, recording what arrives."""

    def __init__(self, *, capable: bool = True, auto_ready: bool = True, echo: bool = True) -> None:
        self.capable = capable
        self.auto_ready = auto_ready
        self.echo = echo
        self.lines: list[bytes] = []
        self.ours, self.theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def connector(self, _timeout: float) -> socket.socket:
        if self.theirs is None:
            raise OSError('used')
        connection, self.theirs = self.theirs, None
        return connection

    def send(self, line: bytes) -> None:
        with self.lock:
            try:
                self.ours.sendall(line)
            except OSError:
                pass  # The test has ended this guest.

    def serve(self) -> None:
        buffered = b''
        while True:
            try:
                block = self.ours.recv(1 << 20)
            except OSError:
                return
            if not block:
                return
            buffered += block
            while b'\n' in buffered:
                line, buffered = buffered.split(b'\n', 1)
                self.lines.append(line)
                self.handle(line)

    def handle(self, line: bytes) -> None:
        if line == b'FS_CAPS':
            self.send(b'OK FS_CAPS 1\n' if self.capable else b'ERR UNKNOWN\n')
        elif line == b'PING':
            self.send(b'OK PONG\n')
        elif line.startswith(b'FS '):
            parts = line.split(b' ', 3)
            if parts[2] == b'O' and self.auto_ready:
                self.send(b'FS %s R\n' % parts[1])
            elif parts[2] == b'D' and self.echo:
                self.send(b'FS %s D %s\n' % (parts[1], parts[3]))

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(predicate(line) for line in list(self.lines)):
                return True
            time.sleep(.01)
        return False

    def close(self) -> None:
        for item in (self.ours, self.theirs):
            if item is not None:
                try:
                    item.close()
                except OSError:
                    pass


def channel_for(guest: LineGuest) -> QsmGuestChannel:
    return QsmGuestChannel(connector=guest.connector, files=True)


class ChannelFileLines(unittest.TestCase):
    def setUp(self) -> None:
        self.guest = LineGuest()
        self.channel = channel_for(self.guest)
        self.addCleanup(self.guest.close)
        self.addCleanup(self.channel.close)

    def test_old_agent_and_clipboard_bridge_report_no_file_support(self) -> None:
        old = LineGuest(capable=False)
        self.addCleanup(old.close)
        channel = channel_for(old)
        self.addCleanup(channel.close)
        self.assertFalse(channel.files_capable())
        # The refusal was consumed as this request's answer, not a later one's.
        self.assertEqual(channel.dispatch({'op': 'status'}), {'agent': 'ready'})
        bridge = LineGuest()
        self.addCleanup(bridge.close)
        one_shot = QsmGuestChannel(connector=bridge.connector)
        self.addCleanup(one_shot.close)
        self.assertFalse(one_shot.files_capable())
        self.assertEqual(bridge.lines, [], 'nothing unknown may reach a peer that would not answer')
        with self.assertRaises(GuestChannelError):
            one_shot.open_file_session('ab', 'r', lambda kind, rest: None)

    def test_session_lines_and_routing(self) -> None:
        self.assertTrue(self.channel.files_capable())
        seen: dict[str, list] = {'a': [], 'b': []}
        self.channel.open_file_session('aa', 'rw', lambda kind, rest: seen['a'].append((kind, rest)))
        self.channel.open_file_session('bb', 'r', lambda kind, rest: seen['b'].append((kind, rest)))
        self.assertTrue(self.guest.wait_for(lambda line: line == b'FS aa O rw'))
        self.assertTrue(self.guest.wait_for(lambda line: line == b'FS bb O r'))
        self.channel.send_file_frame('aa', b'QUJD')
        deadline = time.monotonic() + 5
        while ('D', b'QUJD') not in seen['a'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIn(('R', b''), seen['a'])
        self.assertIn(('D', b'QUJD'), seen['a'])
        self.assertNotIn(('D', b'QUJD'), seen['b'])
        self.channel.close_file_session('aa')
        self.assertTrue(self.guest.wait_for(lambda line: line == b'FS aa C'))
        self.channel.close_file_session('aa')
        self.assertEqual(self.guest.lines.count(b'FS aa C'), 1)

    def test_invalid_session_values_never_reach_the_guest(self) -> None:
        self.assertTrue(self.channel.files_capable())
        for session, rights in (('', 'r'), ('XY', 'r'), ('a b', 'r'), ('a' * 17, 'r'), ('ab', 'x'), ('ab\nPING', 'r')):
            with self.subTest(session=session, rights=rights), self.assertRaises(GuestChannelError):
                self.channel.open_file_session(session, rights, lambda kind, rest: None)
        self.assertFalse(any(line.startswith(b'FS ') and line != b'FS * C' for line in self.guest.lines))

    def test_every_way_of_losing_the_guest_ends_its_sessions(self) -> None:
        self.assertTrue(self.channel.files_capable())
        ended: list[bytes] = []
        listener = lambda kind, rest: ended.append(rest) if kind == 'C' else None  # noqa: E731
        self.channel.open_file_session('a1', 'r', listener)
        self.guest.send(b'FS a1 C nohelper\n')
        self.channel.open_file_session('a2', 'r', listener)
        self.channel.open_file_session('a3', 'r', listener)
        self.guest.send(b'FS * C gone\n')
        self.channel.open_file_session('a4', 'r', listener)
        self.guest.send(b'READY QSF1\n')
        deadline = time.monotonic() + 5
        while len(ended) < 4 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(sorted(ended), [b'gone', b'gone', b'gone', b'nohelper'])
        self.channel.open_file_session('a5', 'r', listener)
        self.guest.ours.close()
        deadline = time.monotonic() + 5
        while len(ended) < 5 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(ended), 5)

    def test_unknown_session_and_short_lines_are_ignored(self) -> None:
        self.assertTrue(self.channel.files_capable())
        self.guest.send(b'FS nothere D QUJD\nFS x\nFS \n')
        self.assertEqual(self.channel.dispatch({'op': 'status'}), {'agent': 'ready'})

    def test_clipboard_requests_are_not_interleaved_with_file_frames(self) -> None:
        self.assertTrue(self.channel.files_capable())
        self.channel.open_file_session('ab', 'rw', lambda kind, rest: None)
        stop = threading.Event()
        frame = base64.b64encode(b'x' * 40000)

        def spam() -> None:
            while not stop.is_set():
                self.channel.send_file_frame('ab', frame)
        thread = threading.Thread(target=spam, daemon=True)
        thread.start()
        try:
            for _ in range(50):
                self.assertEqual(self.channel.dispatch({'op': 'status'}), {'agent': 'ready'})
        finally:
            stop.set()
            thread.join(timeout=5)
        for line in list(self.guest.lines):
            self.assertTrue(line in (b'PING', b'FS_CAPS', b'FS * C', b'FS ab O rw', b'FS ab D ' + frame), line[:40])

    def test_a_new_connection_tells_the_agent_once(self) -> None:
        self.assertTrue(self.channel.files_capable())
        self.assertTrue(self.guest.wait_for(lambda line: line == b'FS * C'))
        self.channel.open_file_session('ab', 'r', lambda kind, rest: None)
        self.assertTrue(self.channel.files_capable())
        self.channel.close_file_session('ab')
        self.assertTrue(self.guest.wait_for(lambda line: line == b'FS ab C'))
        self.assertEqual(self.guest.lines.count(b'FS * C'), 1, 'a second viewer must not end the first')

    def test_a_short_stall_does_not_end_requests_or_sessions(self) -> None:
        """The socket's own 1 s timeout must not decide this."""
        self.assertTrue(self.channel.files_capable())
        ended = []
        self.channel.open_file_session('ab', 'r', lambda kind, rest: ended.append(kind) if kind == 'C' else None)
        self.channel._socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        # Fill the socket while the guest does not read, then let it read after more than a second.
        stop_reading = threading.Event()
        release = threading.Timer(1.6, stop_reading.set)
        real_handle = self.guest.handle
        self.guest.handle = lambda line: (stop_reading.wait(5), real_handle(line))[1]
        self.addCleanup(setattr, self.guest, 'handle', real_handle)
        release.start()
        self.addCleanup(release.cancel)
        filler = threading.Thread(target=lambda: [self.channel.send_file_frame('ab', b'A' * 60000) for _ in range(40)], daemon=True)
        filler.start()
        started = time.monotonic()
        self.assertEqual(self.channel.dispatch({'op': 'status'}), {'agent': 'ready'})
        self.assertGreater(time.monotonic() - started, 1.0)
        filler.join(timeout=10)
        self.assertEqual(ended, [])

    def test_a_guest_that_stops_reading_fails_the_send_in_bounded_time(self) -> None:
        stuck = LineGuest(echo=False)
        self.addCleanup(stuck.close)
        channel = channel_for(stuck)
        self.addCleanup(channel.close)
        self.assertTrue(channel.files_capable())
        channel.open_file_session('ab', 'rw', lambda kind, rest: None)
        stuck.ours.shutdown(socket.SHUT_RD)
        original = channel_module.FILE_SEND_TIMEOUT_SECONDS
        channel_module.FILE_SEND_TIMEOUT_SECONDS = .3
        self.addCleanup(setattr, channel_module, 'FILE_SEND_TIMEOUT_SECONDS', original)
        started = time.monotonic()
        with self.assertRaises(GuestChannelError):
            for _ in range(200):
                channel.send_file_frame('ab', b'A' * 60000)
        self.assertLess(time.monotonic() - started, 10)


class PacerTests(unittest.TestCase):
    def test_rate_bounds_and_burst(self) -> None:
        now = [100.0]
        pacer = peer_module.Pacer(rate=100000, clock=lambda: now[0])
        self.assertGreater(pacer.delay(32768), 0, 'nothing is banked at the start')
        now[0] += .5
        self.assertEqual(pacer.delay(32768), 0)
        now[0] += 100
        self.assertEqual(pacer.delay(32768), 0, 'a long pause banks one frame and a short burst')
        self.assertGreater(pacer.delay(32768), 0)
        for bad in (-1, 0, 'x', None, True, float('nan'), float('inf'), 1 << 41):
            before = pacer.rate
            pacer.set_rate(bad)
            self.assertEqual(pacer.rate, before, bad)
        pacer.set_rate(1)
        self.assertEqual(pacer.rate, peer_module.MIN_RATE)
        pacer.set_rate(1 << 30)
        self.assertEqual(pacer.queue_limit(), int((1 << 30) * peer_module.QUEUE_SECONDS))
        pacer.set_rate(16384)
        self.assertEqual(pacer.queue_limit(), peer_module.MIN_QUEUE_BYTES)

    def test_the_sctp_window_follows_the_pace_and_recovers_from_a_timeout(self) -> None:
        peer = peer_module.FilePeer.__new__(peer_module.FilePeer)
        peer._pacer = peer_module.Pacer(rate=1_000_000)
        sctp = type('Sctp', (), {'_cwnd': 1200, '_srtt': .1})()
        peer._pc = type('Pc', (), {'sctp': sctp})()
        peer._keep_window_open()
        self.assertEqual(sctp._cwnd, 200000 + peer_module.MIN_WINDOW_BYTES)
        sctp._cwnd = 10 ** 7
        peer._keep_window_open()
        self.assertEqual(sctp._cwnd, 10 ** 7, 'a window already larger stays')
        peer._pc = type('Pc', (), {'sctp': type('Other', (), {})()})()
        peer._keep_window_open()

    def test_mdns_offers_keep_discovery_open(self) -> None:
        plain = 'a=candidate:1 1 udp 1 10.0.0.1 5000 typ host\r\na=end-of-candidates\r\n'
        self.assertEqual(peer_module.without_end_of_candidates(plain), plain)
        mdns = 'a=candidate:1 1 udp 1 abc.local 5000 typ host\r\na=end-of-candidates\r\n'
        self.assertNotIn('end-of-candidates', peer_module.without_end_of_candidates(mdns))


def frame(header: dict, payload: bytes = b'') -> bytes:
    encoded = json.dumps(header).encode('ascii')
    return len(encoded).to_bytes(2, 'big') + encoded + payload


class PeerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.guest = LineGuest()
        self.channel = channel_for(self.guest)
        self.addCleanup(self.guest.close)
        self.addCleanup(self.channel.close)
        from aiortc import RTCPeerConnection
        self.browser = RTCPeerConnection()
        self.files_channel = self.browser.createDataChannel('qsm-files', ordered=True)
        self.rtt_channel = self.browser.createDataChannel('qsm-files-rtt', ordered=False)
        self.messages: asyncio.Queue = asyncio.Queue()
        self.files_channel.on('message', self.messages.put_nowait)
        self.rtt_messages: asyncio.Queue = asyncio.Queue()
        self.rtt_channel.on('message', self.rtt_messages.put_nowait)
        self.session: FileSession | None = None

    async def asyncTearDown(self) -> None:
        if self.session is not None:
            await asyncio.to_thread(self.session.close)
        await self.browser.close()

    async def connect(self, rights: str = 'rw') -> None:
        from aiortc import RTCSessionDescription
        await self.browser.setLocalDescription(await self.browser.createOffer())
        self.session = FileSession(self.channel, rights)
        answer = await asyncio.to_thread(self.session.start, self.browser.localDescription.sdp)
        await self.browser.setRemoteDescription(RTCSessionDescription(sdp=answer, type='answer'))
        status = json.loads(await asyncio.wait_for(self.messages.get(), 20))
        self.assertEqual(status, {'op': 'status', 'state': 'ready', 'reason': None})

    async def test_frames_cross_both_ways_and_rights_come_from_the_terminal(self) -> None:
        await self.connect('r')
        identifier = self.session.identifier
        self.assertTrue(self.guest.wait_for(lambda line: line == b'FS %s O r' % identifier.encode()))
        for size in (2, 100, 32768, 65536 - 40):
            message = frame({'id': 9}, b'z' * max(0, size - 13))
            self.files_channel.send(message)
            self.assertEqual(await asyncio.wait_for(self.messages.get(), 20), message)

    async def test_a_down_rate_paces_frames(self) -> None:
        await self.connect()
        await self.wait_open(self.rtt_channel)
        self.rtt_channel.send(json.dumps({'rate': 40000}))
        await asyncio.sleep(.1)
        payload = base64.b64encode(frame({'id': 1, 'o': 0}, b'y' * 19000))
        for _ in range(6):
            self.guest.send(b'FS %s D %s\n' % (self.session.identifier.encode(), payload))
        started = None
        for _ in range(6):
            await asyncio.wait_for(self.messages.get(), 20)
            started = started or time.monotonic()
        # Five frames of about 19 KB after the first, at 40 KB/s.
        self.assertGreater(time.monotonic() - started, 1.5)

    async def test_commands_go_ahead_of_file_data_but_stay_behind_their_own(self) -> None:
        await self.connect()
        await self.wait_open(self.rtt_channel)
        self.rtt_channel.send(json.dumps({'rate': 40000}))
        await asyncio.sleep(.1)
        sid = self.session.identifier.encode()

        def down(header: dict, payload: bytes = b'') -> None:
            self.guest.send(b'FS %s D %s\n' % (sid, base64.b64encode(frame(header, payload))))
        for offset in range(6):
            down({'id': 1, 'o': offset * 19000}, b'y' * 19000)
        down({'id': 1, 'ok': True, 'size': 114000})
        await asyncio.sleep(.2)
        down({'id': 2, 'part': [['a', 'f', 1, 1, 420, None]]})
        down({'id': 2, 'ok': True, 'path': '/', 'total': 1, 'truncated': False})
        order = []
        for _ in range(9):
            message = await asyncio.wait_for(self.messages.get(), 20)
            head = json.loads(message[2:2 + int.from_bytes(message[:2], 'big')])
            order.append((head['id'], 'data' if 'o' in head else 'other'))
        self.assertLess(order.index((2, 'other')), order.index((1, 'data')) + 3, 'the listing does not wait for the file')
        ours = [item for item in order if item[0] == 1]
        self.assertEqual(ours, [(1, 'data')] * 6 + [(1, 'other')], 'the end of a file never overtakes its data')
        self.assertEqual([item for item in order if item[0] == 2], [(2, 'other')] * 2)

    async def test_an_echo_channel_with_a_retransmission_limit_is_refused(self) -> None:
        """Its lost messages would bring FORWARD-TSNs, and aiortc then loses file data."""
        limited = self.browser.createDataChannel('qsm-files-rtt', ordered=False, maxRetransmits=0)
        replies: asyncio.Queue = asyncio.Queue()
        limited.on('message', replies.put_nowait)
        await self.connect()
        await asyncio.sleep(1)
        if limited.readyState == 'open':
            limited.send(json.dumps({'t': 1}))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(replies.get(), 1)

    async def wait_open(self, channel) -> None:
        for _ in range(500):
            if channel.readyState == 'open':
                return
            await asyncio.sleep(.01)
        self.fail('channel did not open')

    async def test_rtt_channel_echoes_and_ignores_junk(self) -> None:
        await self.connect()
        await self.wait_open(self.rtt_channel)
        self.rtt_channel.send('not json')
        self.rtt_channel.send(json.dumps({'other': 1}))
        self.rtt_channel.send(b'bytes')
        probe = json.dumps({'t': 123.5})
        self.rtt_channel.send(probe)
        self.assertEqual(await asyncio.wait_for(self.rtt_messages.get(), 10), probe)

    async def test_guest_closing_reports_a_fixed_reason(self) -> None:
        await self.connect()
        self.guest.send(b'FS %s C nohelper\n' % self.session.identifier.encode())
        status = json.loads(await asyncio.wait_for(self.messages.get(), 20))
        self.assertEqual(status, {'op': 'status', 'state': 'closed', 'reason': 'nohelper'})

    async def test_text_from_the_browser_ends_the_session_and_tells_the_guest(self) -> None:
        await self.connect()
        identifier = self.session.identifier.encode()
        self.files_channel.send('text is not a frame')
        self.assertTrue(await asyncio.to_thread(self.guest.wait_for, lambda line: line == b'FS %s C' % identifier, 20))

    async def test_a_closed_session_stops_its_process_and_tells_the_guest(self) -> None:
        await self.connect()
        identifier = self.session.identifier.encode()
        process = self.session._peer
        await asyncio.to_thread(self.session.close)
        self.assertIsNotNone(process.poll())
        self.assertTrue(await asyncio.to_thread(self.guest.wait_for, lambda line: line == b'FS %s C' % identifier))
        await asyncio.to_thread(self.session.close)
        self.assertEqual(self.guest.lines.count(b'FS %s C' % identifier), 1)

    async def test_the_process_outlives_the_thread_that_started_it(self) -> None:
        """The terminal starts it on a request thread, which ends with the request."""
        await self.browser.setLocalDescription(await self.browser.createOffer())
        session = FileSession(self.channel, 'r')
        self.session = session
        result: list[str] = []
        thread = threading.Thread(target=lambda: result.append(session.start(self.browser.localDescription.sdp)))
        thread.start()
        await asyncio.to_thread(thread.join, 30)
        self.assertTrue(result)
        await asyncio.sleep(.5)
        self.assertIsNone(session._peer.poll())

    async def test_the_process_ends_when_the_terminal_goes_away(self) -> None:
        await self.connect()
        process = self.session._peer
        self.session._socket.shutdown(socket.SHUT_RDWR)
        await asyncio.to_thread(process.wait, 15)
        self.assertIsNotNone(process.poll())

    async def test_old_agent_starts_no_process(self) -> None:
        old = LineGuest(capable=False)
        self.addCleanup(old.close)
        channel = channel_for(old)
        self.addCleanup(channel.close)
        session = FileSession(channel, 'r')
        with self.assertRaises(FileSessionError):
            await asyncio.to_thread(session.start, 'v=0')
        self.assertIsNone(session._peer)

    async def test_a_flood_of_ready_lines_reaches_the_browser_once(self) -> None:
        await self.connect()
        identifier = self.session.identifier.encode()
        self.guest.send((b'FS %s R\n' % identifier) * 100000)
        probe = frame({'id': 1})
        self.files_channel.send(probe)
        self.assertEqual(await asyncio.wait_for(self.messages.get(), 20), probe, 'no second ready arrived first')

    async def test_tiny_guest_lines_count_for_more_than_their_bytes(self) -> None:
        await self.connect()
        session_module = sys.modules['direct_terminal.qsm_file_session']
        limit = session_module.MAX_QUEUED_BYTES // session_module.LINE_OVERHEAD_BYTES
        self.session._socket.shutdown(socket.SHUT_WR)  # Stops the queue from draining into the peer.
        for _ in range(limit + 10):
            self.session._from_guest('D', b'QQ==')
        for _ in range(500):
            if self.session._closed:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.session._closed)

    async def test_invalid_offer_fails_without_opening_a_guest_session(self) -> None:
        session = FileSession(self.channel, 'r')
        with self.assertRaises(FileSessionError):
            await asyncio.to_thread(session.start, 'not an sdp offer')
        self.assertFalse(any(line.startswith(b'FS ') and line != b'FS * C' for line in self.guest.lines))
        self.assertIsNotNone(session._peer.poll())

    async def test_too_much_from_a_guest_ends_only_that_session(self) -> None:
        await self.connect()
        original = sys.modules['direct_terminal.qsm_file_session'].MAX_QUEUED_BYTES
        sys.modules['direct_terminal.qsm_file_session'].MAX_QUEUED_BYTES = 1024
        self.addCleanup(setattr, sys.modules['direct_terminal.qsm_file_session'], 'MAX_QUEUED_BYTES', original)
        self.session._from_guest('D', b'A' * 4096)
        for _ in range(500):
            if self.session._closed:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.session._closed)
        self.assertEqual(self.channel.dispatch({'op': 'status'}), {'agent': 'ready'})


class TerminalBinding(unittest.IsolatedAsyncioTestCase):
    """answer_files on the real session manager: which console does a request belong to?"""

    def request(self, **changes) -> dict:
        return {'version': 1, 'op': 'pve_acl_files', 'node': 'lab', 'vmid': 200, 'subject': 'alice@pve', 'rights': 'rw',
                'token': 'a' * 32, 'sdp': 'v=0', 'sdp_type': 'offer', **changes}

    def setUp(self) -> None:
        from direct_terminal import qsm_direct_terminal as terminal
        self.terminal = terminal
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.manager = terminal.DirectSessionManager(
            instance_directory=root, runtime_directory=root, vm_runtime_directory=root, pve_config_directory=root,
            local_node='lab', qemu_pid_directory=root, container_instance_directory=root,
            lxc_config_directory=root, lxc_runtime_config_directory=root)
        self.addCleanup(self.manager.close)
        # Its reconciler would retire this stand-in transport, which has no VM behind it.
        self.manager._reconcile_stop.set()
        self.manager._reconcile_thread.join(timeout=5)
        self.guest = LineGuest()
        self.addCleanup(self.guest.close)
        self.channel = channel_for(self.guest)
        self.addCleanup(self.channel.close)
        worker = type('Worker', (), {'poll': lambda self: None})()
        self.session = terminal.DirectSession(vmid=200, bridge=None, worker=worker, directory=root, expires_at=time.monotonic() + 3600,
                                              key=200, subject='alice@pve', file_token='a' * 32)
        self.transport = terminal.DirectVmTransport(vmid=200, worker=worker, media=None, input=None, directory=root,
                                                    qemu_generation='g', codec='h264', guest=self.channel)
        self.manager._sessions['s'] = self.session
        self.manager._transports[200] = self.transport
        self.addCleanup(self.manager._sessions.clear)
        self.addCleanup(self.manager._transports.clear)

    async def asyncSetUp(self) -> None:
        from aiortc import RTCPeerConnection
        self.browser = RTCPeerConnection()
        self.browser.createDataChannel('qsm-files', ordered=True)
        await self.browser.setLocalDescription(await self.browser.createOffer())

    async def asyncTearDown(self) -> None:
        await self.browser.close()
        if self.session.files is not None:
            await asyncio.to_thread(self.session.files.close)

    async def test_a_request_must_name_this_console(self) -> None:
        for changes in ({'token': 'b' * 32}, {'subject': 'mallory@pve'}, {'vmid': 201}, {'node': 'other'},
                        {'token': 'a' * 31}, {'token': 'A' * 32}, {'rights': 'x'}, {'version': 9}, {'sdp_type': 'answer'},
                        {'sdp': '\u00e9'}, {'extra': 1}):
            with self.subTest(changes=changes), self.assertRaises(self.terminal.DirectTerminalError):
                await asyncio.to_thread(self.manager.answer_files, self.request(**changes))
        self.assertIsNone(self.session.files)
        self.assertFalse(any(line.startswith(b'FS ') and line != b'FS * C' for line in self.guest.lines))

    async def test_the_right_console_gets_a_session_and_a_second_one_replaces_it(self) -> None:
        sdp = self.browser.localDescription.sdp
        answer = await asyncio.to_thread(self.manager.answer_files, self.request(rights='r', sdp=sdp))
        self.assertEqual(answer['type'], 'answer')
        self.assertIn('m=application', answer['sdp'])
        first = self.session.files
        self.assertIsNotNone(first)
        self.assertTrue(await asyncio.to_thread(self.guest.wait_for, lambda line: line == b'FS %s O r' % first.identifier.encode()))
        await asyncio.to_thread(self.manager.answer_files, self.request(sdp=sdp))
        second = self.session.files
        self.assertIsNot(first, second)
        self.assertTrue(await asyncio.to_thread(self.guest.wait_for, lambda line: line == b'FS %s C' % first.identifier.encode()))
        self.assertTrue(await asyncio.to_thread(self.guest.wait_for, lambda line: line == b'FS %s O rw' % second.identifier.encode()))

    async def test_a_second_request_while_one_starts_is_refused(self) -> None:
        self.session.files_starting = True
        with self.assertRaises(self.terminal.DirectTerminalError):
            await asyncio.to_thread(self.manager.answer_files, self.request(sdp=self.browser.localDescription.sdp))
        self.session.files_starting = False

    async def test_the_node_caps_file_sessions_and_a_closed_one_frees_its_place(self) -> None:
        sdp = self.browser.localDescription.sdp
        slots = [self.manager._file_slots.acquire(blocking=False) for _ in range(self.terminal.MAX_FILE_SESSIONS)]
        self.assertTrue(all(slots))
        with self.assertRaises(self.terminal.DirectTerminalError):
            await asyncio.to_thread(self.manager.answer_files, self.request(sdp=sdp))
        self.assertFalse(any(line.startswith(b'FS ') and b' O ' in line for line in self.guest.lines))
        self.assertFalse(self.session.files_starting, 'a refusal leaves the console able to retry')
        self.manager._file_slots.release()
        await asyncio.to_thread(self.manager.answer_files, self.request(sdp=sdp))
        self.assertFalse(self.manager._file_slots.acquire(blocking=False))
        await asyncio.to_thread(self.session.files.close)
        self.assertTrue(self.manager._file_slots.acquire(blocking=False), 'closing gives the place back')
        for _ in range(self.terminal.MAX_FILE_SESSIONS):
            self.manager._file_slots.release()

    async def test_a_failed_start_gives_its_place_back(self) -> None:
        for _ in range(self.terminal.MAX_FILE_SESSIONS + 2):
            with self.assertRaises(self.terminal.DirectTerminalError):
                await asyncio.to_thread(self.manager.answer_files, self.request(sdp='not an offer'))
        self.assertTrue(self.manager._file_slots.acquire(blocking=False))
        self.manager._file_slots.release()

    async def test_a_gateway_without_a_usable_socket_ends_the_session(self) -> None:
        original = self.terminal.read_gateway
        self.terminal.read_gateway = lambda: {'relay_ipv4': '192.0.2.1', 'secret': 'x'}
        self.addCleanup(setattr, self.terminal, 'read_gateway', original)
        with self.assertRaises(self.terminal.DirectTerminalError):
            await asyncio.to_thread(self.manager.answer_files, self.request(sdp=self.browser.localDescription.sdp))
        self.assertIsNone(self.session.files)
        self.assertTrue(self.manager._file_slots.acquire(blocking=False))
        self.manager._file_slots.release()

    async def test_the_end_of_a_media_worker_ends_its_file_session(self) -> None:
        await asyncio.to_thread(self.manager.answer_files, self.request(sdp=self.browser.localDescription.sdp))
        files = self.session.files
        self.session.bridge = type('Bridge', (), {'close': lambda self: asyncio.sleep(0)})()
        self.manager._remove_directory = lambda directory: None
        self.manager._transports.pop(200)
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self.manager._close_vmid_sessions(200), self.manager._loop))
        self.assertTrue(files._closed)
        self.assertIsNotNone(files._peer.poll())
        self.session.files = None

    async def test_a_console_without_an_agent_channel_refuses(self) -> None:
        self.transport.guest = None
        with self.assertRaises(self.terminal.DirectTerminalError):
            await asyncio.to_thread(self.manager.answer_files, self.request(sdp=self.browser.localDescription.sdp))

    async def test_closing_the_console_ends_its_file_session(self) -> None:
        sdp = self.browser.localDescription.sdp
        await asyncio.to_thread(self.manager.answer_files, self.request(sdp=sdp))
        files = self.session.files
        self.session.bridge = type('Bridge', (), {'close': lambda self: asyncio.sleep(0)})()
        self.manager._remove_directory = lambda directory: None
        self.manager._transports.pop(200)
        closing = asyncio.run_coroutine_threadsafe(self.manager._close_session('s'), self.manager._loop)
        await asyncio.wrap_future(closing)
        self.assertTrue(files._closed)
        self.assertIsNotNone(files._peer.poll())
        self.assertEqual(self.manager._sessions, {})
        self.session.files = None


class DropPrivilegeTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid() == 0, 'needs root')
    def test_peer_runs_without_privileges(self) -> None:
        pid = os.fork()
        if pid == 0:
            peer_module.drop_privileges()
            os._exit(0 if os.geteuid() == 65534 and os.getegid() == 65534 and not os.getgroups() else 1)
        self.assertEqual(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]), 0)


if __name__ == '__main__':
    unittest.main()
