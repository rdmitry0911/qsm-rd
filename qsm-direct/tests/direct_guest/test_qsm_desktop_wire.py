#!/usr/bin/env python3
"""Run the actual desktop-agent binary over a serial-like Linux PTY."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
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

    def publish(self, sequence=1):
        value = {'version': 1, 'session': 'serial-test', 'sequence': sequence, 'generation': 1,
            'available': True, 'locked': False, 'capabilities': ['windows'],
            'desktop': {'rect': [0, 0, 1280, 800], 'scale': 1},
            'windows': [], 'focus': None, 'damage': [], 'caret': None}
        temporary = self.directory / 'next'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as target: json.dump(value, target)
        os.replace(temporary, self.directory / 'desktop-metadata.json')
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

    def test_symlink_is_never_followed(self):
        self.publish()
        path = self.directory / 'desktop-metadata.json'
        other = self.directory / 'other'; path.rename(other); path.symlink_to(other)
        self.command('DESKTOP_SUBSCRIBE 1'); self.assertEqual(self.line(), 'OK DESKTOP_SUBSCRIBE')
        self.assertIsNone(self.event())

    def test_desktop_build_exposes_no_legacy_file_commands(self):
        for command in ['LIST', 'FILE_LIST', 'READ example', 'WRITE example YQ==']:
            self.command(command); self.assertEqual(self.line(), 'ERR UNKNOWN_COMMAND')


if __name__ == '__main__': unittest.main(verbosity=2)
