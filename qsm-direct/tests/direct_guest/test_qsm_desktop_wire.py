#!/usr/bin/env python3
"""Run the actual desktop-agent binary over a serial-like Linux PTY."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import tty
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('gcc') and os.uname().sysname == 'Linux', 'Linux C agent build required')
class DesktopWireTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='qsm-desktop-wire-build-')
        cls.binary = Path(cls.build.name) / 'qsm-desktop-agent'
        subprocess.run(['gcc', '-std=c11', '-O2', '-D_POSIX_C_SOURCE=200809L', '-Wall', '-Wextra',
            '-Wpedantic', '-Werror', '-DQSM_DESKTOP_CLIPBOARD_ONLY=1', str(ROOT / 'guest/qsf_guest_agent.c'),
            '-o', str(cls.binary)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls): cls.build.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qsm-desktop-wire-')
        self.directory = Path(self.temporary.name)
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.buffer = b''
        self.process = subprocess.Popen([str(self.binary), '--device', os.ttyname(self.slave),
            '--state-dir', str(self.directory)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.assertEqual(self.line(), 'READY QSF1')

    def tearDown(self):
        self.process.terminate(); self.process.communicate(timeout=3)
        os.close(self.master); os.close(self.slave); self.temporary.cleanup()

    def line(self, timeout=2):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.master], [], [], remaining)[0]:
                raise AssertionError('agent response timed out')
            self.buffer += os.read(self.master, 65536)
        raw, self.buffer = self.buffer.split(b'\n', 1)
        return raw.decode('ascii')

    def command(self, value): os.write(self.master, (value + '\n').encode())

    def publish(self, sequence=1, version=1):
        value = {'version': version, 'session': 'serial-test', 'sequence': sequence, 'generation': 1,
            'available': True, 'locked': False, 'capabilities': ['windows'],
            'desktop': {'rect': [0, 0, 1280, 800], 'scale': 1},
            'windows': [], 'focus': None, 'damage': [], 'caret': None}
        temporary = self.directory / 'next'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as target: json.dump(value, target)
        name = 'desktop-metadata-v2.json' if version == 2 else 'desktop-metadata.json'
        os.replace(temporary, self.directory / name)
        return value

    def event(self):
        line = self.line()
        self.assertTrue(line.startswith('EVENT_DESKTOP '))
        encoded = line[14:]
        return None if encoded == '-' else json.loads(base64.b64decode(encoded))

    def test_subscription_is_optional_and_notifications_follow_atomic_changes(self):
        first = self.publish()
        self.command('PING'); self.assertEqual(self.line(), 'OK PONG')
        self.assertFalse(select.select([self.master], [], [], .05)[0])
        self.command('DESKTOP_SUBSCRIBE 1'); self.assertEqual(self.line(), 'OK DESKTOP_SUBSCRIBE')
        self.assertEqual(self.event(), first)
        second = self.publish(2)
        self.assertEqual(self.event(), second)
        self.command('CLIP_GET'); self.assertEqual(self.line(), 'CLIP -')
        self.command('DESKTOP_SUBSCRIBE 0'); self.assertEqual(self.line(), 'OK DESKTOP_SUBSCRIBE')
        self.publish(3)
        self.assertFalse(select.select([self.master], [], [], .1)[0])

    def test_stale_or_world_readable_payload_provides_no_geometry(self):
        self.publish()
        path = self.directory / 'desktop-metadata.json'
        os.utime(path, (time.time() - 10,) * 2)
        self.command('DESKTOP_SUBSCRIBE 1'); self.assertEqual(self.line(), 'OK DESKTOP_SUBSCRIBE')
        self.assertIsNone(self.event())
        self.publish(2); self.assertIsNotNone(self.event())
        path.chmod(0o644)
        self.assertIsNone(self.event())

    def test_v2_subscription_selects_only_the_v2_file_and_can_downgrade(self):
        first = self.publish()
        second = self.publish(2, version=2)
        self.command('DESKTOP_SUBSCRIBE 2'); self.assertEqual(self.line(),'OK DESKTOP_SUBSCRIBE')
        self.assertEqual(self.event(),second)
        self.command('DESKTOP_SUBSCRIBE 1'); self.assertEqual(self.line(),'OK DESKTOP_SUBSCRIBE')
        self.assertEqual(self.event(),first)
        (self.directory/'desktop-metadata-v2.json').unlink()
        self.command('DESKTOP_SUBSCRIBE 2'); self.assertEqual(self.line(),'OK DESKTOP_SUBSCRIBE')
        self.assertIsNone(self.event())

    def test_symlink_is_never_followed(self):
        self.publish()
        path = self.directory / 'desktop-metadata.json'
        other = self.directory / 'other'; path.rename(other); path.symlink_to(other)
        self.command('DESKTOP_SUBSCRIBE 1'); self.assertEqual(self.line(), 'OK DESKTOP_SUBSCRIBE')
        self.assertIsNone(self.event())

    def test_desktop_build_exposes_no_legacy_file_commands(self):
        for command in ['LIST', 'FILE_LIST', 'READ example', 'WRITE example YQ==']:
            self.command(command); self.assertEqual(self.line(), 'ERR UNKNOWN_COMMAND')

    def start_helper(self):
        files = self.directory / 'files'
        files.mkdir(exist_ok=True)
        helper = subprocess.Popen([sys.executable, '-I', str(ROOT / 'guest/qsm_desktop_files.py'),
            '--listen', str(self.directory / 'files.sock')])
        self.addCleanup(self.kill_helper, helper)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.directory / 'files.sock'))
                return helper
            except OSError:
                time.sleep(0.01)
            finally:
                probe.close()
        self.fail('the helper did not start')

    @staticmethod
    def kill_helper(helper):
        helper.kill(); helper.wait()

    def write(self, data):
        """Write to the port while reading it, as the host's reader thread does."""
        os.set_blocking(self.master, False)
        try:
            view = memoryview(data)
            while view:
                readable, writable, _ = select.select([self.master], [self.master], [], 10)
                self.assertTrue(readable or writable, 'the agent stopped taking data')
                if readable:
                    self.buffer += os.read(self.master, 65536)
                if writable:
                    try:
                        view = view[os.write(self.master, view[:4096]):]
                    except BlockingIOError:
                        pass
        finally:
            os.set_blocking(self.master, True)

    @staticmethod
    def frame_line(head, payload=b'', sid='a1'):
        header = json.dumps(head).encode('ascii')
        frame = struct.pack('>H', len(header)) + header + payload
        return f'FS {sid} D {base64.b64encode(frame).decode("ascii")}'.encode('ascii') + b'\n'

    @staticmethod
    def parse_frame(line):
        frame = base64.b64decode(line.split(' ')[3], validate=True)
        length = int.from_bytes(frame[:2], 'big')
        return json.loads(frame[2:2 + length]), frame[2 + length:]

    def assertNoLine(self, seconds=0.1):
        if b'\n' not in self.buffer:
            self.assertFalse(select.select([self.master], [], [], seconds)[0])
        self.assertEqual(self.buffer, b'')

    def test_fs_caps_is_answered_by_the_agent(self):
        self.command('FS_CAPS'); self.assertEqual(self.line(), 'OK FS_CAPS 1')
        self.command('FS_CAPS extra'); self.assertEqual(self.line(), 'ERR UNKNOWN_COMMAND')

    def test_no_helper_is_answered_for_the_session(self):
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 C nohelper')
        self.command('FS 0123456789abcdef D QUJD'); self.assertEqual(self.line(), 'FS 0123456789abcdef C nohelper')
        self.command('PING'); self.assertEqual(self.line(), 'OK PONG')

    def test_bad_fs_lines_are_dropped_without_an_answer(self):
        for line in ['FS', 'FS ', 'FS a1', 'FS  O rw', 'FS ABC O rw', 'FS * O rw', 'FS 0123456789abcdef0 O rw', 'FS a1\tO rw']:
            self.command(line)
        self.command('PING')
        self.assertEqual(self.line(), 'ERR UNKNOWN_COMMAND')  # a bare "FS" is no FS line
        self.assertEqual(self.line(), 'OK PONG')
        self.assertNoLine()

    def test_frames_reach_the_real_helper_and_come_back(self):
        self.start_helper()
        files = self.directory / 'files'
        self.command('FS a1 O rw')
        self.assertEqual(self.line(), 'FS a1 R')  # its READY QSF1 stays on its socket
        self.write(self.frame_line({'op': 'hello', 'id': 1}))
        head, _ = self.parse_frame(self.line())
        self.assertEqual((head['id'], head['ok'], head['rights'], head['chunk']), (1, True, 'rw', 32768))
        self.write(self.frame_line({'op': 'put', 'id': 2, 'path': str(files / 'up'), 'size': 5, 'overwrite': 'fail', 'last': True}, b'hello'))
        self.assertEqual(self.parse_frame(self.line())[0], {'id': 2, 'ack': 5})
        self.assertEqual(self.parse_frame(self.line())[0], {'id': 2, 'ok': True, 'size': 5})
        self.assertEqual((files / 'up').read_bytes(), b'hello')
        self.write(self.frame_line({'op': 'get', 'id': 3, 'path': str(files / 'up'), 'credit': 1000}))
        self.assertTrue(self.parse_frame(self.line())[0]['meta'])
        self.assertEqual(self.parse_frame(self.line())[1], b'hello')
        self.assertTrue(self.parse_frame(self.line())[0]['ok'])
        self.command('PING'); self.assertEqual(self.line(), 'OK PONG')
        self.command('FS a1 C')
        self.assertNoLine()

    def test_a_new_host_ends_what_an_earlier_host_left_open(self):
        self.start_helper()
        files = self.directory / 'files'
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 R')
        self.write(self.frame_line({'op': 'put', 'id': 2, 'path': str(files / 'left'), 'size': 100, 'overwrite': 'fail'}))
        deadline = time.monotonic() + 5
        while not list(files.glob('.left.qsm-part-*')) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(list(files.glob('.left.qsm-part-*'))), 1)
        self.command('FS * C')
        deadline = time.monotonic() + 5
        while list(files.glob('.left.qsm-part-*')) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(list(files.glob('.left.qsm-part-*')), [])
        self.assertNoLine()
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 R')

    def test_a_killed_helper_is_reported_and_later_lines_reconnect(self):
        helper = self.start_helper()
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 R')
        helper.kill(); helper.wait()
        self.assertEqual(self.line(), 'FS * C gone')
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 C nohelper')
        self.start_helper()
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 R')
        self.write(self.frame_line({'op': 'hello', 'id': 1}))
        self.assertTrue(self.parse_frame(self.line())[0]['ok'])

    def test_a_stuck_helper_cannot_block_the_clipboard_for_long(self):
        helper = self.start_helper()
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 R')
        helper.send_signal(signal.SIGSTOP)
        data = self.frame_line({'op': 'data', 'id': 1, 'o': 0}, b'x' * 32768)
        started = time.monotonic()
        for _ in range(400):
            self.write(data)
            if b'FS * C gone\n' in self.buffer:
                break
        self.assertIn(b'FS * C gone\n', self.buffer)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(self.line(), 'FS * C gone')
        self.command('PING')
        self.assertEqual(self.line(timeout=5), 'OK PONG')
        helper.kill(); helper.wait()
        self.start_helper()
        for _ in range(5):  # the first line may still find the old helper's socket
            self.command('FS a1 O rw')
            line = self.line(timeout=5)
            while line == 'FS * C gone':
                line = self.line(timeout=5)
            if line == 'FS a1 R':
                return
        self.fail('no new session after a stuck helper')

    def fake_helper(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(self.directory / 'files.sock'))
        listener.listen(1)
        return listener

    def test_connects_at_the_first_line_and_forwards_only_whole_fs_lines(self):
        listener = self.fake_helper()
        self.command('PING'); self.assertEqual(self.line(), 'OK PONG')
        self.assertFalse(select.select([listener], [], [], 0.05)[0])
        self.command('FS abc O rw')
        connection, _ = listener.accept()
        self.addCleanup(connection.close)
        connection.settimeout(5)
        self.assertEqual(connection.recv(100), b'FS abc O rw\n')
        connection.sendall(b'READY QSF1\nEVENT_CLIP QUJD\nOK PONG\nERR X\nFS\nFS abc R\n')
        self.assertEqual(self.line(), 'FS abc R')
        connection.sendall(b'FS abc D Q')
        self.assertFalse(select.select([self.master], [], [], 0.05)[0])
        self.command('PING'); self.assertEqual(self.line(), 'OK PONG')
        connection.sendall(b'UJD\n')
        self.assertEqual(self.line(), 'FS abc D QUJD')
        self.assertNoLine()

    def test_an_over_long_line_from_the_helper_drops_it(self):
        listener = self.fake_helper()
        self.command('FS abc O rw')
        connection, _ = listener.accept()
        self.addCleanup(connection.close)
        connection.settimeout(5)
        connection.recv(100)
        try:
            connection.sendall(b'FS abc D ' + b'A' * (256 * 1024))
        except OSError:
            pass
        self.assertEqual(self.line(), 'FS * C gone')
        self.assertNoLine()
        self.command('PING'); self.assertEqual(self.line(), 'OK PONG')

    def test_an_over_long_line_to_the_helper_drops_it(self):
        listener = self.fake_helper()
        self.command('FS abc O rw')
        connection, _ = listener.accept()
        self.addCleanup(connection.close)
        connection.settimeout(5)
        self.assertEqual(connection.recv(100), b'FS abc O rw\n')
        self.write(b'FS abc D ' + b'A' * (256 * 1024) + b'\n')
        self.assertEqual(self.line(), 'FS * C gone')
        self.assertEqual(connection.recv(100), b'')
        self.command('PING'); self.assertEqual(self.line(), 'OK PONG')

    def test_clipboard_and_metadata_work_while_file_frames_flow(self):
        self.start_helper()
        files = self.directory / 'files'
        content = os.urandom(3 * 1024 * 1024)
        (files / 'down').write_bytes(content)
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 R')
        self.publish()
        self.command('DESKTOP_SUBSCRIBE 1')
        chunk = 32768
        self.write(self.frame_line({'op': 'put', 'id': 1, 'path': str(files / 'up'), 'size': len(content), 'overwrite': 'fail'}))
        self.write(self.frame_line({'op': 'get', 'id': 2, 'path': str(files / 'down'), 'credit': len(content)}))
        pings = clips = 0
        sequence = 1
        published = {}
        clipboard = self.directory / 'qsf-clipboard.txt'
        for offset in range(0, len(content), chunk):
            self.write(self.frame_line({'op': 'data', 'id': 1, 'o': offset}, content[offset:offset + chunk]))
            if offset % (8 * chunk) == 0:
                self.write(b'PING\n'); pings += 1
            if offset % (16 * chunk) == 0:
                self.write(b'CLIP_GET\n'); clips += 1
            if offset % (24 * chunk) == 0:
                sequence += 1
                self.publish(sequence)
                published[sequence] = time.monotonic()
        temporary = self.directory / 'clipboard-next'
        temporary.write_text('copied in the guest')
        temporary.replace(clipboard)
        clipboard_changed = time.monotonic()
        self.write(self.frame_line({'op': 'end', 'id': 1, 'size': len(content)}))

        well_formed = re.compile(r'(OK PONG|OK DESKTOP_SUBSCRIBE|CLIP -|CLIP [A-Za-z0-9+/=]+|'
                                 r'EVENT_CLIP [A-Za-z0-9+/=]+|EVENT_DESKTOP [A-Za-z0-9+/=]+|FS a1 D [A-Za-z0-9+/=]+)')
        down = bytearray()
        finals = {}
        pongs = clip_replies = 0
        latest_event = None
        clip_event = None
        while len(finals) < 2 or pongs < pings or clip_replies < clips or latest_event != sequence or clip_event is None:
            line = self.line(timeout=20)
            self.assertTrue(well_formed.fullmatch(line), line[:80])
            if line == 'OK PONG':
                pongs += 1
            elif line.startswith('CLIP '):
                clip_replies += 1
            elif line.startswith('EVENT_CLIP '):
                self.assertEqual(base64.b64decode(line[11:]), b'copied in the guest')
                clip_event = time.monotonic()
            elif line.startswith('EVENT_DESKTOP '):
                latest_event = json.loads(base64.b64decode(line[14:]))['sequence']
            elif line.startswith('FS a1 D '):
                head, payload = self.parse_frame(line)
                if 'ok' in head:
                    finals[head['id']] = head
                elif head['id'] == 2 and 'o' in head:
                    self.assertEqual(head['o'], len(down))
                    down += payload
        self.assertEqual(finals[1], {'id': 1, 'ok': True, 'size': len(content)})
        self.assertEqual((finals[2]['ok'], finals[2]['size'], finals[2]['changed']), (True, len(content), False))
        self.assertEqual(hashlib.sha256(down).digest(), hashlib.sha256(content).digest())
        self.assertEqual(hashlib.sha256((files / 'up').read_bytes()).digest(), hashlib.sha256(content).digest())
        self.assertLess(clip_event - clipboard_changed, 5)

    def test_events_stay_prompt_under_constant_file_traffic(self):
        self.start_helper()
        files = self.directory / 'files'
        (files / 'down').write_bytes(b'x' * (64 * 1024 * 1024))
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 R')
        self.publish()
        self.command('DESKTOP_SUBSCRIBE 1')
        self.assertEqual(self.line(), 'OK DESKTOP_SUBSCRIBE')
        self.assertIsNotNone(self.event())
        self.write(self.frame_line({'op': 'get', 'id': 1, 'path': str(files / 'down'), 'credit': 64 * 1024 * 1024}))
        self.assertTrue(self.parse_frame(self.line())[0]['meta'])

        def wait_for(prefix):
            started = time.monotonic()
            while True:
                line = self.line(timeout=10)
                if line.startswith(prefix):
                    return line, time.monotonic() - started
                self.assertTrue(line.startswith('FS a1 D '), line[:60])

        for sequence in range(2, 7):
            time.sleep(0.02)  # the agent tells publications apart by their mtime
            self.publish(sequence)
            line, elapsed = wait_for('EVENT_DESKTOP ')
            self.assertEqual(json.loads(base64.b64decode(line[14:]))['sequence'], sequence)
            self.assertLess(elapsed, 1.0)
        for index in range(3):
            time.sleep(0.02)
            temporary = self.directory / 'clipboard-next'
            temporary.write_text(f'copy {index}')
            temporary.replace(self.directory / 'qsf-clipboard.txt')
            line, elapsed = wait_for('EVENT_CLIP ')
            self.assertEqual(base64.b64decode(line[11:]).decode(), f'copy {index}')
            self.assertLess(elapsed, 1.0)

    def test_a_lost_host_ends_the_helper_sessions(self):
        self.start_helper()
        files = self.directory / 'files'
        self.command('FS a1 O rw'); self.assertEqual(self.line(), 'FS a1 R')
        self.write(self.frame_line({'op': 'put', 'id': 1, 'path': str(files / 'up'), 'size': 10, 'overwrite': 'fail'}, b'hello'))
        self.assertEqual(self.parse_frame(self.line())[0], {'id': 1, 'ack': 5})
        self.assertEqual(len(os.listdir(files)), 1)
        master, self.master = self.master, os.open(os.devnull, os.O_RDWR)
        os.close(master)
        deadline = time.monotonic() + 5
        while os.listdir(files) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(os.listdir(files), [])


if __name__ == '__main__': unittest.main(verbosity=2)
