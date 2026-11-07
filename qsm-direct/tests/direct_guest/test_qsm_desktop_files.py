#!/usr/bin/env python3
"""Run the real file manager helper as a process and speak QSF1 to its socket."""
from __future__ import annotations

import base64
from collections import deque
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import random
import re
import resource
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'guest/qsm_desktop_files.py'
PART = re.compile(rb'^\..*\.qsm-part-[0-9a-f]{8}$')
SID = b'a1'


def load_module():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location('qsm_desktop_files_under_test', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frame_bytes(head, payload=b''):
    header = json.dumps(head).encode('ascii')
    return struct.pack('>H', len(header)) + header + payload


def frame_line(head, payload=b'', sid=SID):
    return b'FS %s D %s' % (sid, base64.b64encode(frame_bytes(head, payload)))


def fs(path):
    """A path as it travels in JSON: non-UTF-8 bytes become lone surrogates."""
    return os.fsencode(path).decode('utf-8', 'surrogateescape')


class Client:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(str(path))
        self.buffer = bytearray()
        self.frames = {}
        self.others = deque()
        self.next_id = 0
        self.closed = False
        self.greeting = self.line()

    def close(self):
        self.socket.close()

    def send(self, *lines):
        self.socket.sendall(b''.join(line + b'\n' for line in lines))

    def _fill(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([self.socket], [], [], remaining)[0]:
            raise TimeoutError('the helper did not answer')
        try:
            block = self.socket.recv(1 << 20)
        except ConnectionError:
            block = b''
        if not block:
            self.closed = True
            raise ConnectionError('the helper closed the connection')
        self.buffer += block

    def line(self, timeout=10):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            self._fill(deadline)
        raw, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        return bytes(raw)

    def pump(self, timeout=10):
        """Sort one incoming line: frames by session and id, the rest aside."""
        line = self.line(timeout)
        fields = line.split(b' ')
        if len(fields) == 4 and fields[0] == b'FS' and fields[2] == b'D':
            frame = base64.b64decode(fields[3], validate=True)
            assert len(frame) <= 65536
            length = int.from_bytes(frame[:2], 'big')
            raw_head = frame[2:2 + length]
            assert raw_head.isascii()
            head = json.loads(raw_head)
            self.frames.setdefault((fields[1], head['id']), deque()).append((head, frame[2 + length:]))
        else:
            self.others.append(line)

    def frame(self, rid, sid=SID, timeout=10):
        deadline = time.monotonic() + timeout
        inbox = self.frames.setdefault((sid, rid), deque())
        while not inbox:
            self.pump(max(0.001, deadline - time.monotonic()))
        return inbox.popleft()

    def other(self, timeout=10):
        deadline = time.monotonic() + timeout
        while not self.others:
            self.pump(max(0.001, deadline - time.monotonic()))
        return self.others.popleft()

    def quiet(self, seconds=0.2):
        """Everything that arrives within the time, as (session, id, head)."""
        deadline = time.monotonic() + seconds
        try:
            while True:
                self.pump(max(0.001, deadline - time.monotonic()))
        except TimeoutError:
            pass
        arrived = [(key, head) for key, inbox in self.frames.items() for head, _ in inbox]
        self.frames.clear()
        return arrived, list(self.others)

    def allocate(self):
        self.next_id += 1
        return self.next_id

    def open(self, sid=SID, rights='rw'):
        self.send(b'FS %s O %s' % (sid, rights.encode('ascii')))
        return self.other()

    def start(self, head, payload=b'', sid=SID):
        rid = self.allocate()
        self.send(frame_line({**head, 'id': rid}, payload, sid))
        return rid

    def final(self, rid, sid=SID, timeout=30):
        """(final response, frames before it)."""
        before = []
        while True:
            head, payload = self.frame(rid, sid, timeout)
            if 'ok' in head:
                return head, before
            before.append((head, payload))

    def call(self, head, payload=b'', sid=SID, timeout=30):
        return self.final(self.start(head, payload, sid), sid, timeout)[0]


class HelperCase(unittest.TestCase):
    helper_args = ('--lock-interval', '0')
    helper_env = None

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qsf-')
        self.base = Path(self.temporary.name)
        self.root = self.base / 'files'
        self.root.mkdir()
        self.socket_path = self.base / 'files.sock'
        self.lock_file = self.base / 'locked'
        self.process = None
        self.clients = []
        self.start_helper()
        self.client = self.connect()
        self.assertEqual(self.client.open(), b'FS a1 R')

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.stop_helper()
        self.temporary.cleanup()

    def start_helper(self, *extra, env=None, preexec_fn=None, use_lock_file=True):
        arguments = [sys.executable, '-I', str(HELPER), '--listen', str(self.socket_path)]
        if use_lock_file:
            arguments += ['--lock-state-file', str(self.lock_file)]
        self.process = subprocess.Popen([*arguments, *self.helper_args, *extra],
                                        env=env or self.helper_env, preexec_fn=preexec_fn)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.connect(str(self.socket_path))
                probe.close()
                return
            except OSError:
                probe.close()
                time.sleep(0.01)
        self.fail('the helper did not start')

    def stop_helper(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def restart_helper(self, *extra, **options):
        for client in self.clients:
            client.close()
        self.clients = []
        self.stop_helper()
        self.start_helper(*extra, **options)
        self.client = self.connect()
        self.assertEqual(self.client.open(), b'FS a1 R')

    def connect(self):
        client = Client(self.socket_path)
        self.clients.append(client)
        self.assertEqual(client.greeting, b'READY QSF1')
        return client

    def path(self, *names):
        return str(self.root.joinpath(*names))

    def parts(self, directory=None):
        return [name for name in os.listdir(os.fsencode(directory or self.root)) if PART.match(name)]

    def assertNoParts(self, directory=None, timeout=5):
        deadline = time.monotonic() + timeout
        while self.parts(directory) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.parts(directory), [])

    def assertFails(self, response, code):
        self.assertEqual((response['ok'], response.get('err')), (False, code), response)
        self.assertIsInstance(response['msg'], str)

    def assertOk(self, response):
        self.assertTrue(response['ok'], response)

    def assertNothingMore(self, seconds=0.2):
        self.assertEqual(self.client.quiet(seconds), ([], []))

    def assertAlive(self):
        self.assertOk(self.client.call({'op': 'hello'}))

    def begin_put(self, name='file', size=11, overwrite='fail', **extra):
        """A put whose temporary file exists: the first ack proves it."""
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path(name), 'size': size,
                                     'overwrite': overwrite, **extra}),
                         frame_line({'op': 'data', 'id': rid, 'o': 0}, b'hello'))
        self.assertEqual(self.client.frame(rid)[0], {'id': rid, 'ack': 5})
        return rid

    def put_all(self, name, data, overwrite='fail', **extra):
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path(name), 'size': len(data),
                                     'overwrite': overwrite, **extra}),
                         frame_line({'op': 'data', 'id': rid, 'o': 0}, data),
                         frame_line({'op': 'end', 'id': rid, 'size': len(data)}))
        return self.client.final(rid)[0]

    def read_file(self, path, credit=1 << 40, **extra):
        rid = self.client.start({'op': 'get', 'path': path, 'credit': credit, **extra})
        final, before = self.client.final(rid)
        return final, before[0][0] if before else None, b''.join(payload for head, payload in before[1:])


class TransportTests(HelperCase):
    def test_greeting_capability_and_unknown_lines(self):
        self.client.send(b'FS_CAPS')
        self.assertEqual(self.client.other(), b'OK FS_CAPS 1')
        self.client.send(b'CLIP_GET')
        self.assertEqual(self.client.other(), b'ERR UNKNOWN_COMMAND')

    def test_socket_is_private_and_goes_away_on_exit(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.socket_path).st_mode), 0o600)
        self.assertTrue(stat.S_ISSOCK(os.stat(self.socket_path).st_mode))
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertFalse(self.socket_path.exists())

    def test_stale_socket_is_replaced_but_another_file_is_not(self):
        self.process.kill()
        self.process.wait()
        self.assertTrue(self.socket_path.exists())
        self.restart_helper()
        self.assertAlive()
        self.stop_helper()
        self.socket_path.write_text('keep me')
        failed = subprocess.run([sys.executable, '-I', str(HELPER), '--listen', str(self.socket_path)],
                                capture_output=True, timeout=10)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.socket_path.read_text(), 'keep me')

    def test_connections_have_their_own_sessions(self):
        second = self.connect()
        self.assertEqual(second.open(), b'FS a1 R')
        rid = self.begin_put('first')
        self.assertOk(second.call({'op': 'hello'}))
        self.assertEqual(len(self.parts()), 1)
        second.close()
        self.clients.remove(second)
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 5}, b' world'),
                         frame_line({'op': 'end', 'id': rid, 'size': 11}))
        self.assertOk(self.client.final(rid)[0])
        self.assertEqual(Path(self.path('first')).read_bytes(), b'hello world')

    def test_at_most_eight_sessions_per_connection(self):
        for index in range(7):
            self.assertEqual(self.client.open(b'b%d' % index), b'FS b%d R' % index)
        self.assertEqual(self.client.open(b'c0'), b'FS c0 C limit')
        self.client.send(b'FS b0 C')
        self.assertEqual(self.client.open(b'c0'), b'FS c0 R')
        other = self.connect()
        self.assertEqual(other.open(b'c1'), b'FS c1 R')

    def test_reopening_replaces_the_session_and_its_rights(self):
        rid = self.begin_put()
        self.assertEqual(self.client.open(rights='r'), b'FS a1 R')
        self.assertNoParts()
        self.assertFails(self.client.call({'op': 'mkdir', 'path': self.path('x')}), 'ERIGHTS')
        self.client.send(frame_line({'op': 'end', 'id': rid, 'size': 5}))
        self.assertEqual(self.client.call({'op': 'hello'})['rights'], 'r')

    def test_bad_session_lines_are_ignored(self):
        for line in (b'FS', b'FS a1', b'FS a1 X', b'FS ABC O rw', b'FS ' + b'a' * 17 + b' O rw', b'FS g O rw',
                     b'FS * O rw', b'FS  O rw', b'FS b1 O', b'FS b1 O x', b'FS b1 O rw extra', b'FS a1 D a b c',
                     b'FS unknown D AAAA', b'FS b2 C', b'FS b1 C extra'):
            self.client.send(line)
        self.assertNothingMore()
        self.assertAlive()
        self.assertEqual(self.client.open(b'0123456789abcdef'), b'FS 0123456789abcdef R')


class OperationTests(HelperCase):
    def test_hello(self):
        response = self.client.call({'op': 'hello'})
        account = pwd.getpwuid(os.getuid())
        self.assertEqual(response, {'id': response['id'], 'ok': True, 'v': 1, 'home': account.pw_dir,
                                    'user': account.pw_name, 'rights': 'rw', 'locked': False, 'chunk': 60000})

    def test_list_describes_every_type(self):
        (self.root / 'file').write_bytes(b'12345')
        os.utime(self.root / 'file', ns=(1_500_000_000_123_456_789, 1_500_000_000_123_456_789))
        os.chmod(self.root / 'file', 0o640)
        (self.root / 'dir').mkdir()
        os.mkfifo(self.root / 'fifo')
        for name, target in (('to-file', 'file'), ('to-dir', 'dir'), ('to-fifo', 'fifo'),
                             ('dangling', 'nothing'), ('loop-a', 'loop-b'), ('loop-b', 'loop-a')):
            (self.root / name).symlink_to(target)
        rid = self.client.start({'op': 'list', 'path': str(self.root)})
        final, parts = self.client.final(rid)
        entries = {entry[0]: entry for head, _ in parts for entry in head['part']}
        self.assertEqual(final, {'id': rid, 'ok': True, 'path': str(self.root), 'total': 9, 'truncated': False})
        self.assertEqual(len(entries), 9)
        self.assertEqual(entries['file'], ['file', 'f', 5, 1_500_000_000_123, 0o640, None])
        self.assertEqual(entries['dir'][1], 'd')
        self.assertIsNone(entries['dir'][5])
        self.assertEqual(entries['fifo'][1], 'o')
        self.assertEqual({name: entries[name][1:2] + entries[name][5:] for name in
                          ('to-file', 'to-dir', 'to-fifo', 'dangling', 'loop-a', 'loop-b')},
                         {'to-file': ['l', 'f'], 'to-dir': ['l', 'd'], 'to-fifo': ['l', 'o'],
                          'dangling': ['l', 'x'], 'loop-a': ['l', 'x'], 'loop-b': ['l', 'x']})

    def test_list_errors(self):
        (self.root / 'file').touch()
        self.assertFails(self.client.call({'op': 'list', 'path': self.path('nothing')}), 'ENOENT')
        self.assertFails(self.client.call({'op': 'list', 'path': self.path('file')}), 'ENOTDIR')

    def test_list_parts_stay_under_their_limit(self):
        for index in range(1500):
            (self.root / f'{index:05d}{"x" * 200}').touch()
        rid = self.client.start({'op': 'list', 'path': str(self.root)})
        final, parts = self.client.final(rid)
        self.assertEqual(final['total'], 1500)
        self.assertGreater(len(parts), 1)
        for head, _ in parts:
            self.assertLessEqual(len(json.dumps(head, separators=(',', ':'))), 48 * 1024)
        self.assertEqual(sum(len(head['part']) for head, _ in parts), 1500)

    def test_a_list_of_long_names_stops_at_its_byte_budget(self):
        directory = self.root / 'long'
        directory.mkdir()
        fd = os.open(directory, os.O_RDONLY)
        try:
            for index in range(60000):
                os.close(os.open(f'{index:06d}' + 'n' * 200, os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
            rid = self.client.start({'op': 'list', 'path': str(directory)})
            final, parts = self.client.final(rid, timeout=60)
            self.assertTrue(final['truncated'])
            self.assertLess(final['total'], 60000)
            self.assertLess(sum(len(json.dumps(head)) for head, _ in parts), 9 * 1024 * 1024)
        finally:
            os.close(fd)

    def test_list_stops_at_the_limit(self):
        directory = self.root / 'many'
        directory.mkdir()
        fd = os.open(directory, os.O_RDONLY)
        try:
            for index in range(200000):
                os.close(os.open(str(index), os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
            rid = self.client.start({'op': 'list', 'path': str(directory)})
            final, parts = self.client.final(rid, timeout=60)
            self.assertEqual((final['total'], final['truncated']), (200000, False))
            self.assertEqual(sum(len(head['part']) for head, _ in parts), 200000)
            os.close(os.open('one-more', os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
            rid = self.client.start({'op': 'list', 'path': str(directory)})
            final, parts = self.client.final(rid, timeout=60)
            self.assertEqual((final['total'], final['truncated']), (200000, True))
            self.assertEqual(sum(len(head['part']) for head, _ in parts), 200000)
        finally:
            os.close(fd)

    def test_stat(self):
        (self.root / 'file').write_bytes(b'abc')
        os.chmod(self.root / 'file', 0o604)
        os.utime(self.root / 'file', (1000, 1000))
        (self.root / 'link').symlink_to('file')
        (self.root / 'gone').symlink_to('nothing')
        response = self.client.call({'op': 'stat', 'path': self.path('file')})
        self.assertEqual({key: response[key] for key in ('type', 'size', 'mtime', 'mode', 'target')},
                         {'type': 'f', 'size': 3, 'mtime': 1000000, 'mode': 0o604, 'target': None})
        self.assertEqual(self.client.call({'op': 'stat', 'path': str(self.root)})['type'], 'd')
        link = self.client.call({'op': 'stat', 'path': self.path('link')})
        self.assertEqual((link['type'], link['target']), ('l', 'f'))
        self.assertEqual(self.client.call({'op': 'stat', 'path': self.path('gone')})['target'], 'x')
        self.assertFails(self.client.call({'op': 'stat', 'path': self.path('nothing')}), 'ENOENT')
        self.assertFails(self.client.call({'op': 'stat', 'path': self.path('file', 'x')}), 'ENOTDIR')

    def test_mkdir(self):
        self.assertOk(self.client.call({'op': 'mkdir', 'path': self.path('one')}))
        self.assertTrue((self.root / 'one').is_dir())
        self.assertFails(self.client.call({'op': 'mkdir', 'path': self.path('one')}), 'EEXIST')
        self.assertOk(self.client.call({'op': 'mkdir', 'path': self.path('one'), 'parents': True}))
        self.assertFails(self.client.call({'op': 'mkdir', 'path': self.path('a', 'b')}), 'ENOENT')
        self.assertOk(self.client.call({'op': 'mkdir', 'path': self.path('a', 'b'), 'parents': True}))
        self.assertTrue((self.root / 'a/b').is_dir())
        (self.root / 'file').touch()
        self.assertFails(self.client.call({'op': 'mkdir', 'path': self.path('file'), 'parents': True}), 'EEXIST')
        self.assertFails(self.client.call({'op': 'mkdir', 'path': self.path('file', 'x')}), 'ENOTDIR')

    def test_rename_never_replaces(self):
        (self.root / 'a').write_text('a')
        (self.root / 'b').write_text('b')
        (self.root / 'dir').mkdir()
        self.assertFails(self.client.call({'op': 'rename', 'from': self.path('a'), 'to': self.path('b')}), 'EEXIST')
        self.assertEqual((self.root / 'a').read_text() + (self.root / 'b').read_text(), 'ab')
        self.assertFails(self.client.call({'op': 'rename', 'from': self.path('a'), 'to': self.path('a')}), 'EEXIST')
        self.assertFails(self.client.call({'op': 'rename', 'from': self.path('a'), 'to': self.path('dir')}), 'EEXIST')
        self.assertFails(self.client.call({'op': 'rename', 'from': self.path('nothing'), 'to': self.path('c')}), 'ENOENT')
        self.assertOk(self.client.call({'op': 'rename', 'from': self.path('a'), 'to': self.path('dir', 'c')}))
        self.assertEqual((self.root / 'dir/c').read_text(), 'a')
        self.assertOk(self.client.call({'op': 'rename', 'from': self.path('dir'), 'to': self.path('moved')}))
        self.assertTrue((self.root / 'moved/c').exists())
        (self.root / 'link').symlink_to('moved')
        self.assertOk(self.client.call({'op': 'rename', 'from': self.path('link') + '/', 'to': self.path('new')}))
        self.assertTrue((self.root / 'new').is_symlink())
        self.assertTrue((self.root / 'moved').is_dir())

    def test_delete(self):
        (self.root / 'file').touch()
        response = self.client.call({'op': 'delete', 'path': self.path('file')})
        self.assertEqual((response['ok'], response['removed']), (True, 1))
        self.assertFalse((self.root / 'file').exists())
        (self.root / 'empty').mkdir()
        self.assertEqual(self.client.call({'op': 'delete', 'path': self.path('empty')})['removed'], 1)
        (self.root / 'full').mkdir()
        (self.root / 'full/x').touch()
        self.assertFails(self.client.call({'op': 'delete', 'path': self.path('full')}), 'ENOTEMPTY')
        self.assertFails(self.client.call({'op': 'delete', 'path': self.path('nothing')}), 'ENOENT')
        self.assertEqual(self.client.call({'op': 'delete', 'path': self.path('full'), 'recursive': True})['removed'], 2)
        self.assertFalse((self.root / 'full').exists())

    def test_delete_never_follows_links(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').touch()
        tree = self.root / 'tree'
        (tree / 'sub').mkdir(parents=True)
        (tree / 'sub/file').touch()
        (tree / 'sub/link').symlink_to(outside)
        (tree / 'link').symlink_to(outside)
        (self.root / 'top').symlink_to(outside)
        (self.root / 'top2').symlink_to(outside)
        self.assertEqual(self.client.call({'op': 'delete', 'path': self.path('tree'), 'recursive': True})['removed'], 5)
        self.assertFalse(tree.exists())
        self.assertEqual(self.client.call({'op': 'delete', 'path': self.path('top'), 'recursive': True})['removed'], 1)
        # A trailing slash would make the kernel resolve the link.
        self.assertEqual(self.client.call({'op': 'delete', 'path': self.path('top2') + '/', 'recursive': True})['removed'], 1)
        self.assertFalse((self.root / 'top').is_symlink() or (self.root / 'top2').is_symlink())
        self.assertTrue((outside / 'keep').exists())

    def test_delete_refuses_a_path_without_a_last_component(self):
        for path in ('/', '//', self.path('x') + '/..', self.path('x') + '/.', self.path('x') + '/../'):
            self.assertFails(self.client.call({'op': 'delete', 'path': path, 'recursive': True}), 'EINVAL')
        self.assertTrue(self.root.exists())

    def make_tree(self, name='big', directories=6000, files=4):
        tree = self.root / name
        tree.mkdir()
        fd = os.open(tree, os.O_RDONLY)
        for index in range(directories):
            os.mkdir(str(index), dir_fd=fd)
            sub = os.open(str(index), os.O_RDONLY, dir_fd=fd)
            for entry in range(files):
                os.close(os.open(str(entry), os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=sub))
            os.close(sub)
        os.close(fd)
        return tree, directories * (files + 1) + 1

    def test_delete_reports_progress_at_most_every_200_ms(self):
        tree, count = self.make_tree()
        started = time.monotonic()
        rid = self.client.start({'op': 'delete', 'path': str(tree), 'recursive': True})
        values = []
        while True:
            head, _ = self.client.frame(rid, timeout=60)
            if 'ok' in head:
                break
            values.append(head['progress'])
        elapsed = time.monotonic() - started
        self.assertEqual(head['removed'], count)
        self.assertGreaterEqual(len(values), 1)
        self.assertEqual(values, sorted(values))
        self.assertLessEqual(len(values), elapsed / 0.2 + 1)

    def test_cancel_stops_a_delete(self):
        tree, count = self.make_tree('cancelled')
        rid = self.client.start({'op': 'delete', 'path': str(tree), 'recursive': True})
        self.client.send(frame_line({'op': 'cancel', 'id': rid}))
        self.assertFails(self.client.final(rid)[0], 'ECANCELED')
        time.sleep(0.05)
        remaining = sum(len(names) + len(folders) for _, folders, names in os.walk(tree))
        self.assertGreater(remaining, 0)
        time.sleep(0.2)
        self.assertEqual(sum(len(names) + len(folders) for _, folders, names in os.walk(tree)), remaining)

    def test_rename_falls_back_safely_without_renameat2(self):
        module = load_module()
        real = module._renameat2

        def unsupported(source, target, dir_fd):
            raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))

        module._renameat2 = unsupported
        try:
            source, target = os.fsencode(self.path('a')), os.fsencode(self.path('b'))
            (self.root / 'a').write_text('a')
            module.rename_noreplace(source, target)
            self.assertEqual((self.root / 'b').read_text(), 'a')
            (self.root / 'a').write_text('new')
            with self.assertRaises(FileExistsError):
                module.rename_noreplace(source, target)
            self.assertEqual((self.root / 'b').read_text(), 'a')
            (self.root / 'c').mkdir()
            (self.root / 'c/x').touch()
            with self.assertRaises(FileExistsError):
                module.rename_noreplace(os.fsencode(self.path('c')), target)
            module.rename_noreplace(os.fsencode(self.path('c')), os.fsencode(self.path('d')))
            self.assertTrue((self.root / 'd/x').exists())
            with self.assertRaises(FileNotFoundError):
                module.rename_noreplace(os.fsencode(self.path('nothing')), os.fsencode(self.path('e')))
            self.assertFalse((self.root / 'e').exists())
            with self.assertRaises(OSError):  # a directory into itself, after the placeholder
                module.rename_noreplace(os.fsencode(self.path('d')), os.fsencode(self.path('d', 'inside')))
            self.assertFalse((self.root / 'd/inside').exists())
        finally:
            module._renameat2 = real
        with self.assertRaises(FileExistsError):
            module.rename_noreplace(source, target)

    def test_error_codes_of_the_contract(self):
        module = load_module()
        for number, name in ((errno.ENOSPC, 'ENOSPC'), (errno.EDQUOT, 'EDQUOT'), (errno.EROFS, 'EROFS'),
                             (errno.EPERM, 'EPERM'), (errno.EACCES, 'EACCES'), (errno.EBUSY, 'EBUSY'),
                             (errno.ENAMETOOLONG, 'ENAMETOOLONG')):
            self.assertEqual(module.os_error(OSError(number, os.strerror(number)))[0], name)
        self.assertEqual(module.os_error(OSError(errno.ELOOP, 'Too many levels')), ('EIO', 'Too many levels'))

    @unittest.skipIf(os.geteuid() == 0, 'root ignores permissions')
    def test_permission_errors(self):
        locked = self.root / 'closed'
        locked.mkdir()
        (locked / 'inside').touch()
        (self.root / 'secret').touch()
        os.chmod(self.root / 'secret', 0)
        os.chmod(locked, 0o500)
        try:
            self.assertFails(self.client.call({'op': 'mkdir', 'path': str(locked / 'x')}), 'EACCES')
            self.assertFails(self.client.call({'op': 'delete', 'path': str(locked / 'inside')}), 'EACCES')
            self.assertFails(self.put_all('closed/x', b'data'), 'EACCES')
            self.assertFails(self.read_file(self.path('secret'))[0], 'EACCES')
        finally:
            os.chmod(locked, 0o700)


class RightsTests(HelperCase):
    def test_read_only_session(self):
        self.assertEqual(self.client.open(b'b1', 'r'), b'FS b1 R')
        (self.root / 'file').write_text('content')
        sid = b'b1'
        self.assertEqual(self.client.call({'op': 'hello'}, sid=sid)['rights'], 'r')
        self.assertOk(self.client.call({'op': 'stat', 'path': self.path('file')}, sid=sid))
        rid = self.client.start({'op': 'list', 'path': str(self.root)}, sid=sid)
        self.assertOk(self.client.final(rid, sid)[0])
        rid = self.client.start({'op': 'get', 'path': self.path('file'), 'credit': 100}, sid=sid)
        self.assertOk(self.client.final(rid, sid)[0])
        for head in ({'op': 'mkdir', 'path': self.path('x')},
                     {'op': 'rename', 'from': self.path('file'), 'to': self.path('y')},
                     {'op': 'delete', 'path': self.path('file')},
                     {'op': 'put', 'path': self.path('z'), 'size': 1, 'overwrite': 'fail'}):
            self.assertFails(self.client.call(head, sid=sid), 'ERIGHTS')
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('w'), 'size': 1, 'overwrite': 'fail'}, sid=sid),
                         frame_line({'op': 'data', 'id': rid, 'o': 0}, b'x', sid=sid),
                         frame_line({'op': 'end', 'id': rid, 'size': 1}, sid=sid))
        self.assertFails(self.client.final(rid, sid)[0], 'ERIGHTS')
        self.assertNothingMore()
        self.assertEqual(sorted(os.listdir(self.root)), ['file'])
        self.assertEqual((self.root / 'file').read_text(), 'content')


class UploadTests(HelperCase):
    def test_pipelined_put_data_end(self):
        rid = self.client.allocate()
        chunks = [bytes([index]) * 32768 for index in range(5)]
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('file'), 'size': 5 * 32768, 'overwrite': 'fail'}),
                         *(frame_line({'op': 'data', 'id': rid, 'o': index * 32768}, chunk) for index, chunk in enumerate(chunks)),
                         frame_line({'op': 'end', 'id': rid, 'size': 5 * 32768}))
        final, before = self.client.final(rid)
        self.assertEqual(final, {'id': rid, 'ok': True, 'size': 5 * 32768})
        self.assertEqual([head['ack'] for head, _ in before][-1], 5 * 32768)
        self.assertEqual((self.root / 'file').read_bytes(), b''.join(chunks))
        self.assertNoParts()

    def test_payload_on_put_and_offsets_after_it(self):
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('file'), 'size': 6, 'overwrite': 'fail'}, b'abc'),
                         frame_line({'op': 'data', 'id': rid, 'o': 3}, b'def'),
                         frame_line({'op': 'end', 'id': rid, 'size': 6}))
        self.assertOk(self.client.final(rid)[0])
        self.assertEqual((self.root / 'file').read_bytes(), b'abcdef')

    def test_last_is_the_whole_file_in_one_frame(self):
        rid = self.client.start({'op': 'put', 'path': self.path('file'), 'size': 5, 'overwrite': 'fail', 'last': True}, b'hello')
        final, _ = self.client.final(rid)
        self.assertEqual(final, {'id': rid, 'ok': True, 'size': 5})
        self.assertEqual((self.root / 'file').read_bytes(), b'hello')
        self.assertNoParts()
        response = self.client.call({'op': 'put', 'path': self.path('empty'), 'size': 0, 'overwrite': 'fail', 'last': True})
        self.assertEqual(response['size'], 0)
        self.assertEqual((self.root / 'empty').read_bytes(), b'')

    def test_last_with_a_wrong_size_fails(self):
        for size, payload in ((5, b'hi'), (1, b'hi')):
            self.assertFails(self.client.call({'op': 'put', 'path': self.path('file'), 'size': size,
                                               'overwrite': 'fail', 'last': True}, payload), 'EINVAL')
        self.assertEqual(os.listdir(self.root), [])

    def test_data_goes_to_a_private_temporary_file_until_the_end(self):
        rid = self.begin_put('name')
        parts = self.parts()
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0].startswith(b'.name.qsm-part-'))
        path = os.fsencode(self.root) + b'/' + parts[0]
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertFalse((self.root / 'name').exists())
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 5}, b' world'),
                         frame_line({'op': 'end', 'id': rid, 'size': 11}))
        self.assertOk(self.client.final(rid)[0])
        self.assertEqual((self.root / 'name').read_bytes(), b'hello world')
        self.assertEqual(self.parts(), [])

    def test_a_name_near_the_limit_still_has_a_temporary_file(self):
        name = 'n' * 255
        rid = self.begin_put(name)
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 5}, b' world'),
                         frame_line({'op': 'end', 'id': rid, 'size': 11}))
        self.assertOk(self.client.final(rid)[0])
        self.assertEqual((self.root / name).read_bytes(), b'hello world')
        self.assertFails(self.put_all('n' * 256, b'x'), 'ENAMETOOLONG')

    def test_new_file_modes_and_mtime(self):
        self.assertOk(self.put_all('default', b'x'))
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'default').st_mode), 0o666 & ~umask)
        self.assertOk(self.put_all('chosen', b'x', mode=0o750, mtime=1_600_000_000_321))
        info = os.stat(self.root / 'chosen')
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o750)
        self.assertEqual(info.st_mtime_ns // 10**6, 1_600_000_000_321)
        self.assertOk(self.put_all('setuid', b'x', mode=0o6755))
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'setuid').st_mode), 0o755)

    def test_overwrite_fail_replaces_nothing(self):
        (self.root / 'file').write_text('old')
        (self.root / 'link').symlink_to('nothing')
        for name in ('file', 'link'):
            rid = self.client.allocate()
            self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path(name), 'size': 1, 'overwrite': 'fail'}),
                             frame_line({'op': 'data', 'id': rid, 'o': 0}, b'x'),
                             frame_line({'op': 'end', 'id': rid, 'size': 1}))
            self.assertFails(self.client.final(rid)[0], 'EEXIST')
            self.assertNothingMore()
        self.assertEqual((self.root / 'file').read_text(), 'old')
        self.assertTrue((self.root / 'link').is_symlink())
        self.assertNoParts()

    def test_overwrite_fail_holds_in_a_race(self):
        rid = self.begin_put('raced')
        (self.root / 'raced').write_text('winner')
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 5}, b' world'),
                         frame_line({'op': 'end', 'id': rid, 'size': 11}))
        self.assertFails(self.client.final(rid)[0], 'EEXIST')
        self.assertEqual((self.root / 'raced').read_text(), 'winner')
        self.assertNoParts()

    def test_replace_keeps_the_mode(self):
        (self.root / 'file').write_text('old')
        os.chmod(self.root / 'file', 0o640)
        response = self.put_all('file', b'new', overwrite='replace', mode=0o777)
        self.assertOk(response)
        self.assertEqual((self.root / 'file').read_bytes(), b'new')
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'file').st_mode), 0o640)
        self.assertOk(self.put_all('fresh', b'new', overwrite='replace', mode=0o700))
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'fresh').st_mode), 0o700)

    def test_replace_keeps_the_latest_mode_at_the_end(self):
        (self.root / 'file').write_text('old')
        rid = self.begin_put('file', overwrite='replace')
        os.chmod(self.root / 'file', 0o604)
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 5}, b' world'),
                         frame_line({'op': 'end', 'id': rid, 'size': 11}))
        self.assertOk(self.client.final(rid)[0])
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'file').st_mode), 0o604)

    def test_replace_of_a_link_replaces_the_link_only(self):
        (self.root / 'real').write_text('old')
        os.chmod(self.root / 'real', 0o640)
        (self.root / 'link').symlink_to('real')
        self.assertOk(self.put_all('link', b'new', overwrite='replace'))
        self.assertFalse((self.root / 'link').is_symlink())
        self.assertEqual((self.root / 'link').read_text(), 'new')
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'link').st_mode), 0o640)
        self.assertEqual((self.root / 'real').read_text(), 'old')

    def test_a_directory_target_is_eisdir(self):
        (self.root / 'dir').mkdir()
        (self.root / 'link').symlink_to('dir')
        self.assertFails(self.put_all('dir', b'x'), 'EISDIR')
        self.assertFails(self.put_all('dir', b'x', overwrite='replace'), 'EISDIR')
        self.assertFails(self.put_all('link', b'x', overwrite='replace'), 'EISDIR')
        self.assertNoParts()

    def test_a_directory_appearing_during_replace_is_eisdir(self):
        rid = self.begin_put('raced', overwrite='replace')
        (self.root / 'raced').mkdir()
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 5}, b' world'),
                         frame_line({'op': 'end', 'id': rid, 'size': 11}))
        self.assertFails(self.client.final(rid)[0], 'EISDIR')
        self.assertTrue((self.root / 'raced').is_dir())
        self.assertNoParts()

    def test_replace_of_something_else_is_refused(self):
        os.mkfifo(self.root / 'fifo')
        self.assertFails(self.put_all('fifo', b'x', overwrite='replace'), 'ENOTREG')
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.root / 'fifo').st_mode))
        self.assertFails(self.put_all('fifo', b'x'), 'EEXIST')

    def test_missing_parent(self):
        self.assertFails(self.put_all('no/such/dir', b'x'), 'ENOENT')
        (self.root / 'file').touch()
        self.assertFails(self.put_all('file/x', b'x'), 'ENOTDIR')

    def test_offset_mismatch_fails_the_upload(self):
        rid = self.begin_put()
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 6}, b'world'))
        self.assertFails(self.client.final(rid)[0], 'EINVAL')
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 5}, b'world'),
                         frame_line({'op': 'end', 'id': rid, 'size': 11}))
        self.assertNothingMore()
        self.assertNoParts()
        self.assertEqual(os.listdir(self.root), [])

    def test_more_data_than_declared_fails(self):
        rid = self.begin_put()
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 5}, b'x' * 7))
        self.assertFails(self.client.final(rid)[0], 'EINVAL')
        self.assertNoParts()
        self.assertEqual(os.listdir(self.root), [])

    def test_size_mismatch_at_the_end(self):
        for sent, end_size in ((b'hello', 11), (b'hello world', 5), (b'hello', 5)):
            rid = self.client.allocate()
            self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('file'), 'size': 11, 'overwrite': 'fail'}),
                             frame_line({'op': 'data', 'id': rid, 'o': 0}, sent),
                             frame_line({'op': 'end', 'id': rid, 'size': end_size}))
            self.assertFails(self.client.final(rid)[0], 'EINVAL')
            self.assertNoParts()
            self.assertEqual(os.listdir(self.root), [])

    def test_acks_come_as_soon_as_nothing_more_is_queued(self):
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('file'), 'size': 1000, 'overwrite': 'fail'}),
                         frame_line({'op': 'data', 'id': rid, 'o': 0}, b'x' * 10))
        self.assertEqual(self.client.frame(rid)[0], {'id': rid, 'ack': 10})
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 10}, b'y'))
        self.assertEqual(self.client.frame(rid)[0], {'id': rid, 'ack': 11})
        self.assertEqual(os.path.getsize(os.fsencode(self.root) + b'/' + self.parts()[0]), 11)

    def test_acks_come_at_least_every_write_batch_in_a_burst(self):
        chunk, total = 32768, 4 * 1024 * 1024
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('file'), 'size': total, 'overwrite': 'fail'}),
                         *(frame_line({'op': 'data', 'id': rid, 'o': offset}, b'z' * chunk)
                           for offset in range(0, total, chunk)),
                         frame_line({'op': 'end', 'id': rid, 'size': total}))
        final, before = self.client.final(rid)
        self.assertOk(final)
        acks = [head['ack'] for head, _ in before]
        self.assertEqual(acks[-1], total)
        for previous, current in zip([0, *acks], acks):
            self.assertGreater(current, previous)
            self.assertLessEqual(current - previous, 1024 * 1024)

    def test_a_128_kib_window_cannot_stall(self):
        chunk, total, window = 32768, 2 * 1024 * 1024, 128 * 1024
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('file'), 'size': total, 'overwrite': 'fail'}))
        sent = acked = 0
        while acked < total:
            while sent < total and sent - acked + chunk <= window:
                self.client.send(frame_line({'op': 'data', 'id': rid, 'o': sent}, b'w' * chunk))
                sent += chunk
            acked = self.client.frame(rid, timeout=5)[0]['ack']
        self.client.send(frame_line({'op': 'end', 'id': rid, 'size': total}))
        self.assertOk(self.client.final(rid)[0])
        self.assertEqual(os.path.getsize(self.root / 'file'), total)

    def test_cancel_removes_the_temporary_file(self):
        rid = self.begin_put()
        self.client.send(frame_line({'op': 'cancel', 'id': rid}))
        self.assertFails(self.client.final(rid)[0], 'ECANCELED')
        self.client.send(frame_line({'op': 'data', 'id': rid, 'o': 5}, b'world'),
                         frame_line({'op': 'end', 'id': rid, 'size': 11}))
        self.assertNothingMore()
        self.assertNoParts()
        self.assertEqual(os.listdir(self.root), [])

    def test_cancel_in_the_same_breath_as_the_put(self):
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('file'), 'size': 1, 'overwrite': 'fail'}),
                         frame_line({'op': 'cancel', 'id': rid}))
        self.assertFails(self.client.final(rid)[0], 'ECANCELED')
        self.assertNothingMore()
        self.assertEqual(os.listdir(self.root), [])

    def test_session_close_removes_the_temporary_file(self):
        self.begin_put()
        self.client.send(b'FS a1 C')
        self.assertNoParts()
        self.assertNothingMore()
        self.assertEqual(self.client.open(), b'FS a1 R')
        self.assertAlive()

    def test_lost_connection_removes_the_temporary_file(self):
        self.begin_put()
        self.client.close()
        self.assertNoParts()

    def test_helper_exit_removes_the_temporary_file(self):
        self.begin_put()
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(self.parts(), [])

    def test_other_os_errors_are_eio_with_the_system_text(self):
        limit = lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
        self.restart_helper(preexec_fn=limit)
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('big'), 'size': 200000, 'overwrite': 'fail'}),
                         *(frame_line({'op': 'data', 'id': rid, 'o': offset}, b'b' * 40000) for offset in range(0, 200000, 40000)))
        response = self.client.final(rid)[0]
        self.assertFails(response, 'EIO')
        self.assertEqual(response['msg'], os.strerror(errno.EFBIG))
        self.assertNoParts()

    def test_too_much_unwritten_data_is_refused(self):
        module = load_module()

        class Stub:
            unwritten = module.MAX_UNWRITTEN

        upload = module.Upload(Stub(), 1, 'put', {'path': '/x', 'size': 100, 'overwrite': 'fail'}, b'')
        with self.assertRaises(module.RequestError) as caught:
            upload.on_data({'o': 0}, b'x')
        self.assertEqual(caught.exception.code, 'EBUSY')


class DownloadTests(HelperCase):
    def test_get_sends_meta_data_and_final(self):
        data = os.urandom(100000)
        (self.root / 'file').write_bytes(data)
        os.chmod(self.root / 'file', 0o640)
        os.utime(self.root / 'file', (2000, 2000))
        rid = self.client.start({'op': 'get', 'path': self.path('file'), 'credit': 1 << 30})
        final, before = self.client.final(rid)
        self.assertEqual(before[0][0], {'id': rid, 'meta': True, 'size': 100000, 'mtime': 2000000, 'mode': 0o640})
        offset = 0
        for head, payload in before[1:]:
            self.assertEqual(head, {'id': rid, 'o': offset})
            self.assertLessEqual(len(payload), 60000)
            offset += len(payload)
        self.assertEqual(b''.join(payload for _, payload in before[1:]), data)
        self.assertEqual(final, {'id': rid, 'ok': True, 'size': 100000, 'changed': False})

    def test_offset(self):
        (self.root / 'file').write_bytes(b'0123456789')
        final, meta, data = self.read_file(self.path('file'), offset=4)
        self.assertEqual((data, final['size'], meta['size']), (b'456789', 10, 10))
        final, _, data = self.read_file(self.path('file'), offset=50)
        self.assertEqual((data, final['ok']), (b'', True))

    def received(self, rid, expected, seconds=0.25):
        """Collect data until no more arrives; never more than ``expected``."""
        data, _ = bytearray(), None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                head, payload = self.client.frame(rid, timeout=max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                break
            self.assertNotIn('ok', head)
            data += payload
            self.assertLessEqual(len(data), expected)
        return bytes(data)

    def test_credit_is_a_hard_limit(self):
        data = os.urandom(2 * 1024 * 1024)
        (self.root / 'file').write_bytes(data)
        first = 128 * 1024
        rid = self.client.start({'op': 'get', 'path': self.path('file'), 'credit': first})
        self.assertTrue(self.client.frame(rid)[0]['meta'])
        got = self.received(rid, first)
        self.assertEqual(got, data[:first])
        got2 = bytearray()
        for grant in (1, 100, 60000, 60001, 300000):
            self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': grant}))
            piece = self.received(rid, grant)
            self.assertEqual(len(piece), grant)
            got2 += piece
        self.assertEqual(got + bytes(got2), data[:len(got) + len(got2)])
        self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': 0}))
        self.assertEqual(self.received(rid, 0), b'')
        self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': len(data)}))
        final, before = self.client.final(rid)
        self.assertEqual(got + bytes(got2) + b''.join(payload for _, payload in before), data)
        self.assertEqual((final['size'], final['changed']), (len(data), False))

    def test_zero_credit_sends_only_the_meta(self):
        (self.root / 'file').write_bytes(b'data')
        rid = self.client.start({'op': 'get', 'path': self.path('file'), 'credit': 0})
        self.assertEqual(self.client.frame(rid)[0]['size'], 4)
        self.assertEqual(self.received(rid, 0), b'')
        self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': 4}))
        final, before = self.client.final(rid)
        self.assertEqual(b''.join(payload for _, payload in before), b'data')
        self.assertTrue(final['ok'])

    def test_a_file_ending_exactly_at_the_credit_finishes(self):
        (self.root / 'file').write_bytes(b'x' * 1000)
        final, _, data = self.read_file(self.path('file'), credit=1000)
        self.assertEqual((final['ok'], len(data)), (True, 1000))
        (self.root / 'empty').touch()
        final, meta, data = self.read_file(self.path('empty'), credit=0)
        self.assertEqual((final['ok'], meta['size'], data), (True, 0, b''))

    def test_changed_when_size_or_mtime_moves(self):
        path = self.root / 'file'
        path.write_bytes(b'x' * 10000)
        rid = self.client.start({'op': 'get', 'path': str(path), 'credit': 100})
        self.client.frame(rid)
        self.received(rid, 100)
        with path.open('ab') as target:
            target.write(b'more')
        self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': 20000}))
        final, before = self.client.final(rid)
        self.assertEqual((final['size'], final['changed']), (10004, True))
        self.assertEqual(sum(len(payload) for _, payload in before) + 100, 10004)

        rid = self.client.start({'op': 'get', 'path': str(path), 'credit': 100})
        self.client.frame(rid)
        self.received(rid, 100)
        os.utime(path, (5, 5))
        self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': 20000}))
        self.assertTrue(self.client.final(rid)[0]['changed'])

    def test_truncation_ends_the_read_and_reports_it(self):
        path = self.root / 'file'
        path.write_bytes(b'x' * 10000)
        rid = self.client.start({'op': 'get', 'path': str(path), 'credit': 100})
        self.client.frame(rid)
        self.received(rid, 100)
        os.truncate(path, 100)
        self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': 1}))
        final = self.client.final(rid)[0]
        self.assertEqual((final['size'], final['changed']), (100, True))

    def test_fifo_device_and_directory_are_refused_without_hanging(self):
        os.mkfifo(self.root / 'fifo')
        (self.root / 'to-fifo').symlink_to('fifo')
        started = time.monotonic()
        self.assertFails(self.client.call({'op': 'get', 'path': self.path('fifo'), 'credit': 10}, timeout=5), 'ENOTREG')
        self.assertFails(self.client.call({'op': 'get', 'path': self.path('to-fifo'), 'credit': 10}, timeout=5), 'ENOTREG')
        self.assertFails(self.client.call({'op': 'get', 'path': '/dev/null', 'credit': 10}, timeout=5), 'ENOTREG')
        self.assertFails(self.client.call({'op': 'get', 'path': '/dev/zero', 'credit': 10}, timeout=5), 'ENOTREG')
        self.assertFails(self.client.call({'op': 'get', 'path': str(self.root), 'credit': 10}, timeout=5), 'EISDIR')
        self.assertLess(time.monotonic() - started, 5)
        self.assertAlive()

    def test_the_receiver_chooses_the_frame_size(self):
        (self.root / 'file').write_bytes(b'x' * 10000)
        rid = self.client.start({'op': 'get', 'path': self.path('file'), 'credit': 4000, 'chunk': 1024})
        sizes = []
        while sum(sizes) < 4000:
            head, payload = self.client.frame(rid)
            if 'o' in head:
                sizes.append(len(payload))
        self.assertEqual(sizes, [1024, 1024, 1024, 928])
        self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': 6000, 'chunk': 2048}))
        final, parts = self.client.final(rid, timeout=5)
        self.assertTrue(final['ok'])
        self.assertEqual([len(payload) for head, payload in parts if 'o' in head], [2048, 2048, 1904])
        for bad in (1023, 60001, 'x', None, True):
            with self.subTest(chunk=bad):
                self.assertFails(self.client.call({'op': 'get', 'path': self.path('file'), 'credit': 10, 'chunk': bad}, timeout=5), 'EINVAL')

    def test_refusing_a_fifo_does_not_disturb_its_writer(self):
        import threading
        os.mkfifo(self.root / 'fifo')
        outcome = []

        def writer():
            try:
                with open(self.root / 'fifo', 'wb') as out:  # Blocks until a reader opens it.
                    out.write(b'x')
                outcome.append('written')
            except OSError as error:
                outcome.append(type(error).__name__)
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        time.sleep(.1)
        self.assertFails(self.client.call({'op': 'get', 'path': self.path('fifo'), 'credit': 10}, timeout=5), 'ENOTREG')
        time.sleep(.2)
        self.assertEqual(outcome, [], 'the writer must still be waiting for a real reader')
        with open(self.root / 'fifo', 'rb') as source:
            self.assertEqual(source.read(), b'x')
        thread.join(timeout=5)
        self.assertEqual(outcome, ['written'])

    def test_links_are_followed_for_reading(self):
        (self.root / 'file').write_bytes(b'target')
        (self.root / 'link').symlink_to('file')
        (self.root / 'gone').symlink_to('nothing')
        self.assertEqual(self.read_file(self.path('link'))[2], b'target')
        self.assertFails(self.client.call({'op': 'get', 'path': self.path('gone'), 'credit': 1}), 'ENOENT')

    def test_cancel_ends_a_download(self):
        (self.root / 'file').write_bytes(b'x' * 100000)
        rid = self.client.start({'op': 'get', 'path': self.path('file'), 'credit': 100})
        self.client.frame(rid)
        self.received(rid, 100)
        self.client.send(frame_line({'op': 'cancel', 'id': rid}))
        self.assertFails(self.client.final(rid)[0], 'ECANCELED')
        self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': 1000}))
        self.assertNothingMore()

    def test_transfers_are_limited_per_session(self):
        (self.root / 'file').write_bytes(b'x' * 1000)
        rids = []
        for _ in range(16):
            rids.append(self.client.start({'op': 'get', 'path': self.path('file'), 'credit': 0}))
            self.client.frame(rids[-1])
        self.assertFails(self.client.call({'op': 'get', 'path': self.path('file'), 'credit': 0}), 'EBUSY')
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('up'), 'size': 1, 'overwrite': 'fail'}),
                         frame_line({'op': 'data', 'id': rid, 'o': 0}, b'x'),
                         frame_line({'op': 'end', 'id': rid, 'size': 1}))
        self.assertFails(self.client.final(rid)[0], 'EBUSY')
        self.assertAlive()
        self.client.send(frame_line({'op': 'cancel', 'id': rids[0]}))
        self.assertFails(self.client.final(rids[0])[0], 'ECANCELED')
        response = self.client.start({'op': 'get', 'path': self.path('file'), 'credit': 0})
        self.assertTrue(self.client.frame(response)[0]['meta'])
        self.assertEqual(self.client.open(b'b1'), b'FS b1 R')
        other = self.client.start({'op': 'get', 'path': self.path('file'), 'credit': 0}, sid=b'b1')
        self.assertTrue(self.client.frame(other, b'b1')[0]['meta'])


class LockTests(HelperCase):
    def set_locked(self, value):
        self.lock_file.write_text('1' if value else '0')

    def test_locked_session_refuses_new_work_and_open_transfers_finish(self):
        (self.root / 'down').write_bytes(b'd' * 1000)
        (self.root / 'cancelled').write_bytes(b'c' * 1000)
        upload = self.begin_put('up')
        download = self.client.start({'op': 'get', 'path': self.path('down'), 'credit': 10})
        self.assertTrue(self.client.frame(download)[0]['meta'])
        self.assertEqual(self.client.frame(download)[1], b'd' * 10)
        cancelled = self.client.start({'op': 'get', 'path': self.path('cancelled'), 'credit': 0})
        self.assertTrue(self.client.frame(cancelled)[0]['meta'])

        self.set_locked(True)
        self.assertTrue(self.client.call({'op': 'hello'})['locked'])
        for head in ({'op': 'list', 'path': str(self.root)}, {'op': 'stat', 'path': str(self.root)},
                     {'op': 'mkdir', 'path': self.path('x')},
                     {'op': 'rename', 'from': self.path('down'), 'to': self.path('y')},
                     {'op': 'delete', 'path': self.path('down')},
                     {'op': 'get', 'path': self.path('down'), 'credit': 10},
                     {'op': 'put', 'path': self.path('z'), 'size': 1, 'overwrite': 'fail', 'last': True}):
            self.assertFails(self.client.call(head, b'x' if head['op'] == 'put' else b''), 'ELOCKED')
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('w'), 'size': 1, 'overwrite': 'fail'}),
                         frame_line({'op': 'data', 'id': rid, 'o': 0}, b'x'),
                         frame_line({'op': 'end', 'id': rid, 'size': 1}))
        self.assertFails(self.client.final(rid)[0], 'ELOCKED')
        self.assertNothingMore()

        self.client.send(frame_line({'op': 'data', 'id': upload, 'o': 5}, b' world'),
                         frame_line({'op': 'end', 'id': upload, 'size': 11}))
        self.assertOk(self.client.final(upload)[0])
        self.assertEqual((self.root / 'up').read_bytes(), b'hello world')
        self.client.send(frame_line({'op': 'credit', 'id': download, 'n': 990}))
        final, before = self.client.final(download)
        self.assertOk(final)
        self.assertEqual(sum(len(payload) for _, payload in before), 990)
        self.client.send(frame_line({'op': 'cancel', 'id': cancelled}))
        self.assertFails(self.client.final(cancelled)[0], 'ECANCELED')

        self.set_locked(False)
        self.assertFalse(self.client.call({'op': 'hello'})['locked'])
        self.assertOk(self.client.call({'op': 'mkdir', 'path': self.path('x')}))


FAKE_BUSCTL = r'''#!/bin/sh
echo "busctl $*" >> "$FAKE_LOG"
answer() {
    case "$1" in
        true|false) echo "b $1"; exit 0 ;;
        unknown) echo "Call failed: Unknown method 'GetActive'" >&2; exit 1 ;;
        object) echo "Call failed: Unknown object '/ScreenSaver'." >&2; exit 1 ;;
        glib) echo 'Call failed: No such interface "org.freedesktop.ScreenSaver" on object' >&2; exit 1 ;;
        dbus) echo "Call failed: org.freedesktop.ScreenSaver does not understand message GetActive" >&2; exit 1 ;;
        broken) echo "Call failed: Input/output error" >&2; exit 1 ;;
        strange) echo "s nonsense"; exit 0 ;;
        hang) exec sleep 10 ;;
    esac
}
case "$*" in
    *ListNames*)
        [ -n "${FAKE_SLOW:-}" ] && sleep "$FAKE_SLOW"
        [ -n "${FAKE_NO_BUS:-}" ] && { echo "Failed to connect to bus: No such file or directory" >&2; exit 1; }
        printf 'as 3 ":1.1" "org.freedesktop.DBus"'
        [ -n "${FAKE_FD:-}" ] && printf ' "org.freedesktop.ScreenSaver"'
        [ -n "${FAKE_GNOME:-}" ] && printf ' "org.gnome.ScreenSaver"'
        echo
        exit 0
        ;;
    *org.gnome.ScreenSaver*) answer "$FAKE_GNOME" ;;
    *org.freedesktop.ScreenSaver*) answer "$FAKE_FD" ;;
esac
exit 3
'''
FAKE_LOGINCTL = r'''#!/bin/sh
echo "loginctl $*" >> "$FAKE_LOG"
case "${FAKE_HINT:-}" in
    yes|no) echo "$FAKE_HINT"; exit 0 ;;
    hang) exec sleep 10 ;;
    *) echo "Could not get session path: No session 'auto' known" >&2; exit 1 ;;
esac
'''


class LockerTests(HelperCase):
    """The real lock check against fake busctl and loginctl programs."""

    helper_args = ()

    def setUp(self):
        super().setUp()
        self.tools = self.base / 'tools'
        self.tools.mkdir()
        (self.tools / 'busctl').write_text(FAKE_BUSCTL)
        (self.tools / 'loginctl').write_text(FAKE_LOGINCTL)
        os.chmod(self.tools / 'busctl', 0o755)
        os.chmod(self.tools / 'loginctl', 0o755)
        self.log = self.base / 'log'

    def locked_with(self, **fake):
        self.log.write_text('')
        env = {**os.environ, 'PATH': f'{self.tools}:{os.environ["PATH"]}', 'FAKE_LOG': str(self.log), **fake}
        self.restart_helper(env=env, use_lock_file=False)
        return self.client.call({'op': 'hello'})['locked']

    def test_who_answers_decides(self):
        for fake, expected in (
                ({'FAKE_FD': 'true', 'FAKE_GNOME': 'false', 'FAKE_HINT': 'no'}, True),
                ({'FAKE_FD': 'false', 'FAKE_GNOME': 'true', 'FAKE_HINT': 'yes'}, False),
                ({'FAKE_GNOME': 'true', 'FAKE_HINT': 'no'}, True),
                ({'FAKE_GNOME': 'false', 'FAKE_HINT': 'yes'}, False),
                ({'FAKE_HINT': 'yes'}, True),
                ({'FAKE_HINT': 'no'}, False)):
            with self.subTest(fake=fake):
                self.assertEqual(self.locked_with(**fake), expected)

    def test_owner_without_the_call_is_not_a_locker(self):
        for reply in ('unknown', 'object', 'glib', 'dbus'):
            with self.subTest(reply=reply):
                self.assertTrue(self.locked_with(FAKE_FD=reply, FAKE_GNOME='true', FAKE_HINT='no'))
                self.assertFalse(self.locked_with(FAKE_FD=reply, FAKE_GNOME='false', FAKE_HINT='yes'))
                self.assertTrue(self.locked_with(FAKE_FD=reply, FAKE_HINT='yes'))

    def test_a_locker_that_fails_counts_as_locked(self):
        for fake in ({'FAKE_FD': 'broken', 'FAKE_GNOME': 'false', 'FAKE_HINT': 'no'},
                     {'FAKE_FD': 'strange', 'FAKE_HINT': 'no'},
                     {'FAKE_GNOME': 'broken', 'FAKE_HINT': 'no'},
                     {'FAKE_FD': 'hang', 'FAKE_HINT': 'no'},
                     {'FAKE_HINT': 'hang'}):
            with self.subTest(fake=fake):
                self.assertTrue(self.locked_with(**fake))

    def test_no_locker_at_all_counts_as_not_locked(self):
        self.assertFalse(self.locked_with())
        self.assertFalse(self.locked_with(FAKE_NO_BUS='1'))
        empty = self.base / 'empty'
        empty.mkdir()
        self.restart_helper(env={**os.environ, 'PATH': str(empty)}, use_lock_file=False)
        self.assertFalse(self.client.call({'op': 'hello'})['locked'])

    def calls(self):
        return self.log.read_text().count('ListNames')

    def test_read_on_demand_and_at_most_every_two_seconds(self):
        self.assertFalse(self.locked_with(FAKE_HINT='no'))
        self.assertEqual(self.calls(), 1)
        for _ in range(5):
            self.assertOk(self.client.call({'op': 'stat', 'path': str(self.root)}))
        self.assertEqual(self.calls(), 1)
        time.sleep(2.1)
        self.assertEqual(self.calls(), 1)  # nothing asked: nothing read
        self.assertOk(self.client.call({'op': 'stat', 'path': str(self.root)}))
        self.assertEqual(self.calls(), 2)

    def test_nothing_is_read_without_a_session(self):
        self.log.write_text('')
        env = {**os.environ, 'PATH': f'{self.tools}:{os.environ["PATH"]}', 'FAKE_LOG': str(self.log), 'FAKE_HINT': 'no'}
        self.restart_helper(env=env, use_lock_file=False)
        time.sleep(0.3)
        self.assertEqual(self.log.read_text(), '')

    def test_requests_are_limited_per_session(self):
        self.log.write_text('')
        env = {**os.environ, 'PATH': f'{self.tools}:{os.environ["PATH"]}', 'FAKE_LOG': str(self.log),
               'FAKE_HINT': 'no', 'FAKE_SLOW': '0.6'}
        self.restart_helper(env=env, use_lock_file=False)
        # All wait for the one slow lock read, so all are in progress together.
        ids = [self.client.allocate() for _ in range(70)]
        self.client.send(*(frame_line({'op': 'stat', 'path': str(self.root), 'id': rid}) for rid in ids))
        answers = [self.client.final(rid)[0] for rid in ids]
        self.assertEqual(sum(answer['ok'] for answer in answers), 64)
        for answer in answers[64:]:
            self.assertFails(answer, 'EBUSY')
        self.assertAlive()


class IdleTests(HelperCase):
    def wakeups(self):
        total = 0
        for task in os.listdir(f'/proc/{self.process.pid}/task'):
            status = Path(f'/proc/{self.process.pid}/task/{task}/status').read_text()
            total += int(re.search(r'^voluntary_ctxt_switches:\s*(\d+)', status, re.M)[1])
        return total

    def assertAsleep(self, seconds=1.2):
        time.sleep(0.2)
        before = self.wakeups()
        time.sleep(seconds)
        self.assertEqual(self.wakeups(), before)

    @unittest.skipUnless(os.path.exists('/proc/self/task'), 'needs /proc')
    def test_an_idle_helper_has_no_wakeups(self):
        self.assertAsleep()  # with an open session and no requests
        (self.root / 'file').write_bytes(b'x' * 100000)
        self.assertOk(self.client.call({'op': 'list', 'path': str(self.root)}))
        self.assertOk(self.read_file(self.path('file'))[0])
        self.assertOk(self.put_all('up', b'y' * 60000))
        self.assertAsleep()  # the workers of those requests sleep as well
        self.client.send(b'FS a1 C')
        self.assertAsleep()
        self.client.close()
        self.assertAsleep()


class NameTests(HelperCase):
    def test_non_utf8_names_round_trip(self):
        raw = b'caf\xe9-\xff\xfe.txt'
        directory = os.fsencode(self.root)
        with open(directory + b'/' + raw, 'wb') as target:
            target.write(b'latin')
        name = raw.decode('utf-8', 'surrogateescape')
        path = str(self.root) + '/' + name

        rid = self.client.start({'op': 'list', 'path': str(self.root)})
        final, parts = self.client.final(rid)
        names = [entry[0] for head, _ in parts for entry in head['part']]
        self.assertEqual(names, [name])
        self.assertIn('\udce9', names[0])

        self.assertEqual(self.client.call({'op': 'stat', 'path': path})['size'], 5)
        self.assertEqual(self.read_file(path)[2], b'latin')
        renamed = 'r\udce9'
        self.assertOk(self.client.call({'op': 'rename', 'from': path, 'to': str(self.root) + '/' + renamed}))
        self.assertEqual(os.listdir(directory), [b'r\xe9'])
        self.assertOk(self.put_all('p\udcff', b'up'))
        self.assertEqual(sorted(os.listdir(directory)), [b'p\xff', b'r\xe9'])
        self.assertOk(self.client.call({'op': 'mkdir', 'path': str(self.root) + '/d\udcfe'}))
        with open(directory + b'/d\xfe/in\xfd', 'wb'):
            pass
        response = self.client.call({'op': 'delete', 'path': str(self.root) + '/d\udcfe', 'recursive': True})
        self.assertEqual(response['removed'], 2)
        self.assertEqual(self.client.call({'op': 'delete', 'path': str(self.root) + '/' + renamed})['removed'], 1)

    def test_unicode_names(self):
        name = 'Приве́т \U0001f600 名前'
        self.assertOk(self.put_all(name, b'data'))
        self.assertEqual(os.listdir(self.root), [name])
        rid = self.client.start({'op': 'list', 'path': str(self.root)})
        final, parts = self.client.final(rid)
        self.assertEqual(parts[0][0]['part'][0][0], name)

    def test_raw_headers_are_ascii_even_for_strange_names(self):
        (self.root / 'Ё').touch()
        rid = self.client.start({'op': 'list', 'path': str(self.root)})
        raw = self.client.line()
        frame = base64.b64decode(raw.split(b' ')[3])
        self.assertTrue(frame[2:2 + int.from_bytes(frame[:2], 'big')].isascii())
        self.assertOk(self.client.final(rid)[0])


class BigFileTests(HelperCase):
    def test_64_mib_round_trip(self):
        block = random.Random(213).randbytes(1024 * 1024)
        total = 64 * 1024 * 1024
        window = 4 * 1024 * 1024
        expected = hashlib.sha256()
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('big'), 'size': total,
                                     'overwrite': 'fail', 'mtime': 1_700_000_000_000}))
        sent = acked = 0
        stream = bytearray()
        counter = 0
        while acked < total:
            while sent < total and sent - acked < window:
                if len(stream) < 32768:
                    stream += counter.to_bytes(8, 'big') + block
                    counter += 1
                chunk = bytes(stream[:min(32768, total - sent)])
                del stream[:len(chunk)]
                expected.update(chunk)
                self.client.send(frame_line({'op': 'data', 'id': rid, 'o': sent}, chunk))
                sent += len(chunk)
            head, _ = self.client.frame(rid, timeout=60)
            self.assertLessEqual(head['ack'] - acked, 1024 * 1024)
            acked = head['ack']
        self.client.send(frame_line({'op': 'end', 'id': rid, 'size': total}))
        self.assertEqual(self.client.final(rid, timeout=60)[0], {'id': rid, 'ok': True, 'size': total})
        on_disk = hashlib.sha256()
        with open(self.root / 'big', 'rb') as source:
            while piece := source.read(1 << 20):
                on_disk.update(piece)
        self.assertEqual(on_disk.hexdigest(), expected.hexdigest())
        self.assertEqual(os.stat(self.root / 'big').st_mtime_ns // 10**6, 1_700_000_000_000)
        self.assertNoParts()

        rid = self.client.start({'op': 'get', 'path': self.path('big'), 'credit': window})
        self.assertEqual(self.client.frame(rid, timeout=60)[0]['size'], total)
        back = hashlib.sha256()
        received = 0
        while True:
            head, payload = self.client.frame(rid, timeout=60)
            if 'ok' in head:
                break
            self.assertEqual(head['o'], received)
            self.assertLessEqual(len(payload), 60000)
            back.update(payload)
            received += len(payload)
            self.client.send(frame_line({'op': 'credit', 'id': rid, 'n': len(payload)}))
        self.assertEqual((head['size'], head['changed'], received), (total, False, total))
        self.assertEqual(back.hexdigest(), expected.hexdigest())


class MalformedTests(HelperCase):
    def test_frames_without_a_usable_id_are_dropped(self):
        def framed(raw):
            return b'FS a1 D ' + base64.b64encode(struct.pack('>H', len(raw)) + raw)

        bad_ids = [0, -1, 2**31, 2**64, '1', True, False, None, 1.5, [1], {'a': 1}]
        lines = [
            b'FS a1 D !!!!', b'FS a1 D QQ', b'FS a1 D QQ==', b'FS a1 D ' + base64.b64encode(b'\xff\xff{}'),
            b'FS a1 D ' + base64.b64encode(b'\x00'), framed(b''), framed(b'not json'), framed(b'[1,2]'),
            framed(b'7'), framed(b'null'), framed(b'"text"'), framed(b'{"op":"hello"}'),
            framed(b'{"op":"hello","id":1,"x":NaN}'), framed(b'{"op":"hello","id":1,"x":Infinity}'),
            framed('{"op":"hello","id":1,"x":"é"}'.encode('utf-8')),
            framed(b'{"op":"hello","id":' + b'9' * 5000 + b'}'), framed(b'[' * 30000),
            framed(b'{"a":' * 10000),
            *(framed(json.dumps({'op': 'hello', 'id': bad}).encode('ascii')) for bad in bad_ids),
        ]
        self.client.send(*lines)
        self.assertNothingMore()
        self.assertTrue(self.client.call({'op': 'hello'})['ok'])
        self.assertIsNone(self.process.poll())

    def test_the_widest_usable_ids(self):
        for rid in (1, 2**31 - 1):
            self.client.send(frame_line({'op': 'hello', 'id': rid}))
            self.assertEqual(self.client.final(rid)[0]['id'], rid)

    def test_wrong_fields_get_einval(self):
        (self.root / 'file').write_bytes(b'data')
        path = self.path('new')
        put = {'op': 'put', 'path': path, 'size': 1, 'overwrite': 'fail'}
        for head in (
                {}, {'op': 42}, {'op': None}, {'op': []}, {'op': {}}, {'op': 'nonsense'}, {'op': 'HELLO'},
                {'op': 'list'}, {'op': 'list', 'path': 5}, {'op': 'list', 'path': None}, {'op': 'list', 'path': ['/']},
                {'op': 'stat'}, {'op': 'stat', 'path': True},
                {'op': 'mkdir'}, {'op': 'mkdir', 'path': path, 'parents': 'yes'}, {'op': 'mkdir', 'path': path, 'parents': 1},
                {'op': 'rename', 'from': path}, {'op': 'rename', 'to': path}, {'op': 'rename', 'from': path, 'to': []},
                {'op': 'delete'}, {'op': 'delete', 'path': path, 'recursive': 'all'}, {'op': 'delete', 'path': path, 'recursive': 1},
                {'op': 'put', 'path': path}, {**put, 'size': '1'}, {**put, 'size': -1}, {**put, 'size': True},
                {**put, 'size': 2**63}, {**put, 'size': 1.0}, {**put, 'overwrite': 'maybe'}, {**put, 'overwrite': None},
                {**put, 'overwrite': ['fail']}, {**put, 'mtime': 'now'}, {**put, 'mtime': 2**70}, {**put, 'mode': -1},
                {**put, 'mode': 0o10000}, {**put, 'mode': '644'}, {**put, 'last': 'yes'}, {**put, 'path': 7},
                {'op': 'get', 'path': path}, {'op': 'get', 'path': path, 'credit': -1}, {'op': 'get', 'path': path, 'credit': '1'},
                {'op': 'get', 'path': path, 'credit': 1.5}, {'op': 'get', 'path': path, 'credit': 1, 'offset': -1},
                {'op': 'get', 'path': path, 'credit': 1, 'offset': 'x'}, {'op': 'get', 'path': 1, 'credit': 1}):
            with self.subTest(head=head):
                self.assertFails(self.client.call(head), 'EINVAL')
        self.assertEqual(os.listdir(self.root), ['file'])
        self.assertNothingMore()

    def test_bad_paths(self):
        for path in ('/tmp/a\x00b', 'relative', './x', '', '\ud800', '/x\ud800', '/\udc41'):
            for head in ({'op': 'stat', 'path': path}, {'op': 'list', 'path': path}, {'op': 'mkdir', 'path': path},
                         {'op': 'delete', 'path': path}, {'op': 'get', 'path': path, 'credit': 1},
                         {'op': 'put', 'path': path, 'size': 0, 'overwrite': 'fail', 'last': True},
                         {'op': 'rename', 'from': path, 'to': self.path('x')},
                         {'op': 'rename', 'from': self.path('x'), 'to': path}):
                with self.subTest(head=head):
                    self.assertFails(self.client.call(head), 'EINVAL')

    def test_over_long_paths(self):
        too_long = '/' + 'x' * 4096
        self.assertEqual(len(too_long), 4097)
        for head in ({'op': 'stat', 'path': too_long}, {'op': 'list', 'path': too_long}, {'op': 'mkdir', 'path': too_long},
                     {'op': 'delete', 'path': too_long}, {'op': 'get', 'path': too_long, 'credit': 1},
                     {'op': 'put', 'path': too_long, 'size': 0, 'overwrite': 'fail', 'last': True}):
            self.assertFails(self.client.call(head), 'ENAMETOOLONG')
        self.assertFails(self.client.call({'op': 'stat', 'path': self.path('n' * 300)}), 'ENAMETOOLONG')
        self.assertEqual(self.client.call({'op': 'stat', 'path': '/' * 4095})['type'], 'd')

    def test_follow_ups_for_unknown_ids_get_no_answer(self):
        self.client.send(frame_line({'op': 'data', 'id': 900, 'o': 0}, b'x'),
                         frame_line({'op': 'data', 'id': 901}), frame_line({'op': 'end', 'id': 902, 'size': 'x'}),
                         frame_line({'op': 'credit', 'id': 903, 'n': -5}), frame_line({'op': 'cancel', 'id': 904}))
        self.assertNothingMore()

    def test_malformed_follow_ups_fail_their_transfer(self):
        (self.root / 'source').write_bytes(b'x' * 1000)
        for frame in ({'op': 'data'}, {'op': 'data', 'o': 'x'}, {'op': 'data', 'o': -1}, {'op': 'end'},
                      {'op': 'end', 'size': None}, {'op': 'credit', 'n': 1}):
            with self.subTest(frame=frame):
                rid = self.begin_put()
                self.client.send(frame_line({**frame, 'id': rid}))
                self.assertFails(self.client.final(rid)[0], 'EINVAL')
                self.assertNoParts()
        for frame in ({'op': 'credit'}, {'op': 'credit', 'n': -1}, {'op': 'credit', 'n': 'x'}, {'op': 'credit', 'n': 2**60},
                      {'op': 'data', 'o': 0}, {'op': 'end', 'size': 1}):
            with self.subTest(frame=frame):
                rid = self.client.start({'op': 'get', 'path': self.path('source'), 'credit': 0})
                self.client.frame(rid)
                self.client.send(frame_line({**frame, 'id': rid}))
                self.assertFails(self.client.final(rid)[0], 'EINVAL')

    def test_second_end_and_data_after_end_fail(self):
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('f1'), 'size': 2, 'overwrite': 'fail'}),
                         frame_line({'op': 'end', 'id': rid, 'size': 2}), frame_line({'op': 'end', 'id': rid, 'size': 2}))
        self.assertFails(self.client.final(rid)[0], 'EINVAL')
        rid = self.client.allocate()
        self.client.send(frame_line({'op': 'put', 'id': rid, 'path': self.path('f2'), 'size': 2, 'overwrite': 'fail'}),
                         frame_line({'op': 'end', 'id': rid, 'size': 2}), frame_line({'op': 'data', 'id': rid, 'o': 0}, b'xy'))
        self.assertFails(self.client.final(rid)[0], 'EINVAL')
        self.assertNoParts()
        self.assertEqual(os.listdir(self.root), [])

    def test_an_id_in_use_gets_one_failure(self):
        rid = self.begin_put()
        self.client.send(frame_line({'op': 'hello', 'id': rid}))
        self.assertFails(self.client.final(rid)[0], 'EINVAL')
        self.assertNothingMore()
        self.assertNoParts()

    def test_frame_size_limit(self):
        head = {'op': 'hello', 'id': self.client.allocate()}
        header = frame_bytes(head)
        self.client.send(b'FS a1 D ' + base64.b64encode(header + b'x' * (65536 - len(header))))
        self.assertOk(self.client.final(head['id'])[0])
        head = {'op': 'hello', 'id': self.client.allocate()}
        header = frame_bytes(head)
        self.client.send(b'FS a1 D ' + base64.b64encode(header + b'x' * (65537 - len(header))))
        self.assertFails(self.client.final(head['id'])[0], 'EINVAL')
        self.assertAlive()

    def assertDropped(self):
        with self.assertRaises(ConnectionError):
            while True:
                self.client.pump(5)

    def test_a_huge_line_drops_the_connection_and_its_sessions(self):
        self.begin_put()
        try:
            self.client.send(b'FS a1 D ' + b'A' * (1024 * 1024))
        except OSError:
            pass
        self.assertDropped()
        self.assertNoParts()
        self.client = self.connect()
        self.assertEqual(self.client.open(), b'FS a1 R')
        self.assertAlive()

    def test_a_huge_line_without_an_end_drops_the_connection(self):
        try:
            self.client.socket.sendall(b'x' * (1024 * 1024))
        except OSError:
            pass
        self.assertDropped()
        self.assertIsNone(self.process.poll())

    def test_random_input_does_not_hurt(self):
        rng = random.Random(9)
        lines = []
        for _ in range(400):
            size = rng.randrange(0, 300)
            blob = rng.randbytes(size)
            lines.append(b'FS a1 D ' + base64.b64encode(blob))
            head = bytes(rng.choice(b'{}[]",:0123456789abcdefgopid ') for _ in range(rng.randrange(1, 60)))
            lines.append(b'FS a1 D ' + base64.b64encode(struct.pack('>H', len(head)) + head))
            lines.append(frame_line({'op': rng.choice(['data', 'end', 'credit', 'cancel', 'put', 'get', 'stat', 'list', 'x']),
                                     'id': rng.randrange(1000, 1020),
                                     'path': rng.choice(['', '/', self.path('r'), 'x', 5, None]),
                                     'size': rng.choice([0, 1, -1, 'x', None]), 'credit': rng.choice([0, 1, -1, None]),
                                     'overwrite': rng.choice(['fail', 'replace', 0]),
                                     'o': rng.choice([0, 1, -1]), 'n': rng.choice([0, 1, -1])},
                                    rng.randbytes(rng.randrange(0, 20))))
        self.client.send(*lines)
        self.assertIsNone(self.process.poll())
        self.assertAlive()
        self.assertIsNone(self.process.poll())


if __name__ == '__main__':
    unittest.main(verbosity=2)
