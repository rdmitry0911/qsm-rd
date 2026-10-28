#!/usr/bin/env python3
"""Bounds, lifetime and geometry contracts of the optional desktop hints."""
from __future__ import annotations
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'extensions'))
from direct_guest.qsm_desktop_metadata import (
    DesktopMetadataError, MAX_METADATA_BYTES, parse_metadata, read_metadata_file, validate_metadata)
spec = importlib.util.spec_from_file_location('qsm_metadata_bridge', ROOT / 'guest/qsm_desktop_metadata.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class DesktopMetadataTest(unittest.TestCase):
    def setUp(self):
        self.state = bridge.DesktopState()
        self.layout = {'desktop': {'rect': [0, 0, 1280, 800], 'scale': 1},
            'windows': [{'id': 'first', 'pid': 42, 'rect': [20, 30, 500, 400], 'client_rect': [20, 30, 500, 400], 'visible': True, 'opacity': 1}],
            'focus': 'first', 'available': True, 'locked': False}
        self.state.set_layout(self.layout)

    def test_window_order_and_focus_are_preserved_and_unknown_text_is_rejected(self):
        value = self.state.snapshot()
        self.assertEqual(parse_metadata(json.dumps(value).encode()), value)
        for field in ['caption', 'title', 'text']:
            invalid = copy.deepcopy(value)
            invalid['windows'][0][field] = 'private application content'
            with self.assertRaises(DesktopMetadataError): validate_metadata(invalid)

    def test_malformed_duplicate_and_oversized_json_are_rejected(self):
        for raw in [b'{"version":1,"version":1}', b'\xff', b'[' * 2000, b' ' * (MAX_METADATA_BYTES + 1)]:
            with self.assertRaises(DesktopMetadataError): parse_metadata(raw)

    def test_numeric_coordinate_identity_and_window_count_bounds(self):
        value = self.state.snapshot()
        for key, bad in [('version', True), ('sequence', 0), ('generation', 2**53),
                         ('focus', 'missing'), ('windows', value['windows'] * 129)]:
            invalid = copy.deepcopy(value); invalid[key] = bad
            with self.assertRaises(DesktopMetadataError): validate_metadata(invalid)
        for rect in [[65536, 0, 1, 1], [0, 0, -1, 1], [0, 0, True, 1], [0, 0, 1e30, 1]]:
            invalid = copy.deepcopy(value); invalid['windows'][0]['rect'] = rect
            with self.assertRaises(DesktopMetadataError): validate_metadata(invalid)
        invalid = copy.deepcopy(value); invalid['desktop']['scale'] = float('nan')
        with self.assertRaises(DesktopMetadataError): validate_metadata(invalid)

    def test_caret_is_only_accepted_in_the_visible_focused_process(self):
        self.state.caret_available = True
        self.state.set_caret(42, [35, 50, 1, 18])
        self.assertIsNotNone(self.state.snapshot()['caret'])
        self.state.set_caret(43, [35, 50, 1, 18])
        self.assertIsNone(self.state.snapshot()['caret'])
        with self.assertRaises(DesktopMetadataError): self.state.set_caret(42, [1, 1, 1, 18])
        changed = copy.deepcopy(self.layout); changed['windows'][0]['visible'] = False
        self.state.set_layout(changed)
        self.state.set_caret(42, [35, 50, 1, 18])
        self.assertIsNone(self.state.snapshot()['caret'])

    def test_layout_change_invalidates_caret_and_increments_generation(self):
        self.state.caret_available = True
        self.state.set_caret(42, [35, 50, 1, 18])
        before = self.state.snapshot()
        changed = copy.deepcopy(self.layout)
        changed['windows'][0]['rect'][0] += 10
        changed['windows'][0]['client_rect'][0] += 10
        self.state.set_layout(changed)
        after = self.state.snapshot()
        self.assertGreater(after['generation'], before['generation'])
        self.assertIsNone(after['caret'])

    def test_wayland_caret_coordinates_follow_the_window_client_origin(self):
        changed = copy.deepcopy(self.layout)
        changed['windows'][0]['client_rect'] = [22, 66, 496, 362]
        self.state.set_layout(changed); self.state.caret_available = True
        self.state.set_caret(42, [11, 100, 8, 15], window_relative=True)
        self.assertEqual(self.state.snapshot()['caret']['rect'], [33, 166, 8, 15])
        with self.assertRaises(DesktopMetadataError):
            self.state.set_caret(42, [495, 100, 8, 15], window_relative=True)

    def test_unknown_client_origin_keeps_window_hints_but_refuses_relative_caret(self):
        changed = copy.deepcopy(self.layout); changed['windows'][0]['client_rect'] = None
        self.state.set_layout(changed)
        self.assertTrue(self.state.snapshot()['available'])
        with self.assertRaises(DesktopMetadataError):
            self.state.set_caret(42, [11, 100, 8, 15], window_relative=True)

    def test_lock_and_compositor_loss_remove_all_live_hints(self):
        self.state.caret_available = True
        self.state.set_caret(42, [35, 50, 1, 18]); self.state.damage.add('first')
        self.state.set_locked(True)
        locked = self.state.snapshot()
        self.assertTrue(locked['locked'])
        self.assertEqual((locked['windows'], locked['focus'], locked['damage'], locked['caret']), ([], None, [], None))
        self.state.set_locked(False)
        self.assertEqual(len(self.state.snapshot()['windows']), 1)
        self.assertIsNone(self.state.snapshot()['caret'])
        self.state.invalidate()
        self.assertFalse(self.state.snapshot()['available'])

    def test_session_file_requires_fresh_private_regular_owned_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop-metadata.json'
            raw = json.dumps(self.state.snapshot()).encode()
            path.write_bytes(raw); path.chmod(0o600)
            self.assertEqual(read_metadata_file(path), raw)
            path.chmod(0o644); self.assertIsNone(read_metadata_file(path))
            path.chmod(0o600); os.utime(path, (time.time() - 10,) * 2)
            self.assertIsNone(read_metadata_file(path))
            alias = Path(directory) / 'alias'; alias.symlink_to(path)
            self.assertIsNone(read_metadata_file(alias))
            path.unlink(); os.mkfifo(path)
            self.assertIsNone(read_metadata_file(path))


class InvocationLifetimeTest(unittest.TestCase):
    def test_already_fixed_bindings_are_not_unrefed_again(self):
        for symbol in (None, 'g_dbus_connection_register_object_with_closures2'):
            saved = []
            def register(*args):
                saved.append(args)
                return 42
            if symbol:
                register.get_symbol = lambda: symbol
            invocation = SimpleNamespace(_unref=lambda: self.fail('double unref'))
            called = []
            bus = SimpleNamespace(register_object=register)
            self.assertEqual(bridge.register_metadata_object(bus, None, called.append), 42)
            saved[0][2](invocation)
            self.assertEqual(called, [invocation])

    @unittest.skipUnless(shutil.which('dbus-run-session') and importlib.util.find_spec('gi') and
                         Path('/proc/self/status').exists(), 'Gio and Linux private D-Bus required')
    def test_actual_invocations_release_payloads_and_keep_delayed_replies_alive(self):
        # This catches the old closure API's native leak, invisible to Python
        # tracemalloc. A fake invocation cannot prove GLib ownership safety.
        code = r'''
import gc, importlib.util, json, os, sys, threading
from pathlib import Path
import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib
spec = importlib.util.spec_from_file_location('qsm_metadata_bridge', sys.argv[1])
bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
xml = '<node><interface name="org.qsm.DesktopMetadata1"><method name="Echo"><arg type="s" direction="in"/><arg type="u" direction="out"/></method><method name="Delayed"><arg type="u" direction="out"/></method></interface></node>'
interface = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
def callback(connection, sender, path, interface, method, parameters, invocation):
    if method == 'Delayed':
        def reply():
            invocation.return_value(GLib.Variant('(u)', (17,)))
            return False
        GLib.timeout_add(1, reply)
    else:
        invocation.return_value(GLib.Variant('(u)', (len(parameters.unpack()[0]),)))
registration = bridge.register_metadata_object(bus, interface, callback)
loop = GLib.MainLoop(); worker = threading.Thread(target=loop.run); worker.start()
client = Gio.DBusConnection.new_for_address_sync(os.environ['DBUS_SESSION_BUS_ADDRESS'],
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
    None, None)
def call(method, parameters=None):
    return client.call_sync(bus.get_unique_name(), bridge.OBJECT, bridge.INTERFACE, method,
        parameters, GLib.VariantType.new('(u)'), Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
def rss():
    gc.collect()
    return int(next(l.split()[1] for l in Path('/proc/self/status').read_text().splitlines() if l.startswith('VmRSS:'))) * 1024
payload = GLib.Variant('(s)', ('x' * 16384,))
try:
    for n in range(200): assert call('Echo', payload) == 16384
    before = rss()
    for n in range(2000): assert call('Echo', payload) == 16384
    after = rss()
    for n in range(20): assert call('Delayed') == 17
    print(json.dumps({'rss_growth': after - before, 'calls': 2200, 'delayed': 20}))
finally:
    client.close_sync(None); bus.unregister_object(registration); loop.quit(); worker.join(2)
assert after - before < 12 * 1024 * 1024, (before, after)
'''
        result = subprocess.run(['dbus-run-session', '--', sys.executable, '-c', code,
            str(ROOT / 'guest/qsm_desktop_metadata.py')], capture_output=True, text=True,
            timeout=15, env={**os.environ, 'G_DEBUG': 'fatal-criticals'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence['calls'], 2200)
        self.assertEqual(evidence['delayed'], 20)


if __name__ == '__main__': unittest.main(verbosity=2)
