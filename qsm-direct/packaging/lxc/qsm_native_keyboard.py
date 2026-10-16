# SPDX-License-Identifier: AGPL-3.0-or-later
"""Restricted per-seat keyboard control for the native SDDM greeter.

Only root and the SDDM account may select a registered XKB layout. No caller
can submit Sway commands, paths, key events or authentication information.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import socket
import stat
import struct
import threading
import time
import xml.etree.ElementTree as ET

RUNTIME = Path('/run/qsm-native')
RULES = Path('/usr/share/X11/xkb/rules/evdev.xml')
DEFAULTS = Path('/etc/default/keyboard')


def registered_layouts() -> dict[str, str]:
    layouts = {}
    for node in ET.parse(RULES).findall('./layoutList/layout/configItem'):
        name, description = node.findtext('name', ''), node.findtext('description', '')
        if re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', name) and description:
            layouts[name] = description
    if 'us' not in layouts:
        raise ValueError('XKB registry has no US layout')
    return layouts


def default_layout(layouts: dict[str, str]) -> str:
    try:
        lines = DEFAULTS.read_text().splitlines()
    except FileNotFoundError:
        return 'us'
    for line in lines:
        if re.match(r'^\s*XKBLAYOUT\s*=', line):
            values = shlex.split(line.split('=', 1)[1], comments=True)
            candidate = values[0].split(',')[0] if len(values) == 1 else ''
            return candidate if candidate in layouts else 'us'
    return 'us'


def read_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = connection.recv(size - len(data))
        if not part:
            raise ConnectionError('Sway IPC closed')
        data.extend(part)
    return bytes(data)


def sway(slot: int, kind: int, payload: str = ''):
    directory = Path(f'/run/qsm-display-{slot}')
    endpoints = [p for p in directory.glob('sway-ipc.*.sock')
                 if stat.S_ISSOCK(p.lstat().st_mode) and p.lstat().st_uid == pwd.getpwnam('qsm-display').pw_uid]
    if len(endpoints) != 1:
        raise ValueError('native display IPC is unavailable or ambiguous')
    encoded = payload.encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(endpoints[0]))
        connection.sendall(b'i3-ipc' + struct.pack('<II', len(encoded), kind) + encoded)
        header = read_exact(connection, 14)
        size, reply_type = struct.unpack('<II', header[6:])
        if header[:6] != b'i3-ipc' or reply_type != kind or size > 262144:
            raise ValueError('invalid Sway IPC response')
        return json.loads(read_exact(connection, size))


class NativeKeyboard:
    def __init__(self, slot: int, has_user=lambda: False):
        if type(slot) is not int or slot not in range(1, 9):
            raise ValueError('invalid keyboard seat')
        self.slot = slot
        self.layouts = registered_layouts()
        self.has_user = has_user
        self.state = RUNTIME / f'keyboard-{slot}.json'
        self.lock = RUNTIME / f'keyboard-{slot}.lock'

    def selected(self) -> str:
        try:
            fd = os.open(self.state, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return default_layout(self.layouts)
        with os.fdopen(fd) as source:
            meta = os.fstat(source.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022 or meta.st_size > 1024:
                raise ValueError('unsafe native keyboard state')
            layout = json.load(source)['layout']
        if not isinstance(layout, str) or layout not in self.layouts:
            raise ValueError('unregistered native keyboard layout')
        return layout

    def save(self, layout: str) -> None:
        temporary = self.state.with_suffix('.new')
        # The root-owned runtime and per-seat lock make removal safe and allow
        # recovery after an interrupted atomic write.
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'w') as target:
            json.dump({'layout': layout}, target)
        temporary.replace(self.state)

    def apply(self, layout: str, inputs=None) -> None:
        if layout not in self.layouts:
            raise ValueError('unregistered keyboard layout')
        inputs = sway(self.slot, 100) if inputs is None else inputs
        keyboards = [entry for entry in inputs if entry.get('type') == 'keyboard']
        if not keyboards:
            raise ValueError('native seat has no keyboard')
        if all(entry.get('xkb_layout_names') == [self.layouts[layout]] and
               entry.get('xkb_active_layout_index') == 0 for entry in keyboards):
            return
        # A virtual keyboard can replace its keymap without invalidating Sway's
        # cached configuration. Force a distinct intermediate map so every
        # current keyboard really receives the selected map, also on reconnect.
        intermediate = 'us' if layout != 'us' else 'de'
        if intermediate not in self.layouts:
            raise ValueError('XKB registry lacks the intermediate keyboard map')
        reply = sway(self.slot, 0, f'input type:keyboard xkb_layout {intermediate}; '
                                  f'input type:keyboard xkb_layout {layout}')
        if not isinstance(reply, list) or len(reply) != 2 or not all(r.get('success') is True for r in reply):
            raise ValueError('native keyboard layout was not applied')

    def reset(self, greeter: bool) -> None:
        with self.lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            layout = default_layout(self.layouts) if greeter else 'us'
            self.apply(layout)
            if greeter:
                self.save(layout)

    def sync_input(self, expected_keyboards: int = 2) -> None:
        deadline = time.monotonic() + 5
        while True:
            inputs = sway(self.slot, 100)
            keyboards = [entry for entry in inputs if entry.get('type') == 'keyboard']
            if len(keyboards) >= expected_keyboards and all(k.get('xkb_layout_names') for k in keyboards):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('console keyboard did not become ready')
            time.sleep(0.05)
        with self.lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.apply('us' if self.has_user() else self.selected(), inputs)

    def request(self, request: dict, uid: int) -> dict:
        if uid not in (0, pwd.getpwnam('sddm').pw_uid):
            raise PermissionError('native keyboard control is restricted to SDDM')
        if not isinstance(request, dict) or request.get('op') not in ('get', 'set'):
            raise ValueError('invalid keyboard request')
        with self.lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if request['op'] == 'set':
                layout = request.get('layout')
                if not isinstance(layout, str) or layout not in self.layouts:
                    raise ValueError('unregistered keyboard layout')
                if self.has_user():
                    raise PermissionError('the native desktop owns its keyboard layout')
                self.apply(layout)
                self.save(layout)
            return {'ok': True, 'layout': self.selected(),
                    'layouts': [{'name': name, 'description': description}
                                for name, description in self.layouts.items()]}

    def serve(self, ready: threading.Event) -> None:
        path = RUNTIME / f'keyboard-{self.slot}.sock'
        if path.exists() or path.is_symlink():
            meta = path.lstat()
            if not stat.S_ISSOCK(meta.st_mode) or meta.st_uid not in (0, pwd.getpwnam('sddm').pw_uid):
                raise ValueError('unsafe native keyboard endpoint')
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            previous = os.umask(0o177)
            try:
                server.bind(str(path))
            finally:
                os.umask(previous)
            os.chown(path, pwd.getpwnam('sddm').pw_uid, pwd.getpwnam('sddm').pw_gid)
            server.listen(4)
            ready.set()
            while True:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(1)
                    try:
                        uid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                        data = bytearray()
                        while b'\n' not in data:
                            part = connection.recv(4096 - len(data))
                            if not part or len(data) + len(part) >= 4096:
                                raise ValueError('invalid keyboard request frame')
                            data.extend(part)
                        reply = self.request(json.loads(data), uid)
                    except (OSError, ValueError, KeyError, TypeError):
                        reply = {'ok': False}
                    try:
                        connection.sendall(json.dumps(reply).encode() + b'\n')
                    except OSError:
                        pass
