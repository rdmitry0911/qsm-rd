#!/usr/bin/env python3
"""Native keyboard selection: real XKB identifiers and per-seat boundaries."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parents[2] / 'packaging/lxc/qsm_native_keyboard.py'
spec = importlib.util.spec_from_file_location('qsm_native_keyboard', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
LAYOUTS = {'us': 'English (US)', 'ru': 'Russian', 'de': 'German'}


def keyboard_input(description):
    return {'type': 'keyboard', 'xkb_layout_names': [description], 'xkb_active_layout_index': 0}


class NativeKeyboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name)
        self.patch = mock.patch.multiple(module, RUNTIME=self.runtime,
                                         registered_layouts=lambda: dict(LAYOUTS))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        account = mock.patch.object(module.pwd, "getpwnam",
                                    return_value=SimpleNamespace(pw_uid=107, pw_gid=107))
        account.start()
        self.addCleanup(account.stop)

    def test_default_is_parsed_as_data_and_must_be_registered(self):
        defaults = self.runtime / 'keyboard'
        with mock.patch.object(module, 'DEFAULTS', defaults):
            defaults.write_text('XKBLAYOUT="ru,us" # system selection\n')
            self.assertEqual(module.default_layout(LAYOUTS), 'ru')
            defaults.write_text('XKBLAYOUT="$(touch /tmp/should-never-run)"\n')
            self.assertEqual(module.default_layout(LAYOUTS), 'us')

    def test_unregistered_layout_and_non_sddm_peer_cannot_issue_commands(self):
        keyboard = module.NativeKeyboard(1)
        with mock.patch.object(module, 'sway') as sway:
            with self.assertRaises(ValueError): keyboard.request({'op':'set','layout':'us; exec id'}, 0)
            uid = 65000 if os.getuid() != 65000 else 65001
            with self.assertRaises(PermissionError): keyboard.request({'op':'set','layout':'ru'}, uid)
            sway.assert_not_called()

    def test_desktop_owns_its_layout_and_failed_apply_does_not_save(self):
        keyboard = module.NativeKeyboard(1, has_user=lambda: True)
        with mock.patch.object(keyboard, 'apply') as apply:
            with self.assertRaises(PermissionError): keyboard.request({'op':'set','layout':'ru'}, 0)
            apply.assert_not_called()
        keyboard.has_user = lambda: False
        with mock.patch.object(keyboard, 'apply', side_effect=ValueError('unavailable')):
            with self.assertRaises(ValueError): keyboard.request({'op':'set','layout':'ru'}, 0)
        self.assertFalse(keyboard.state.exists())

    def test_reconnect_restores_real_map_even_if_sway_cached_it(self):
        keyboard = module.NativeKeyboard(1)
        inputs = [keyboard_input('English (US)'), keyboard_input('Russian')]
        with mock.patch.object(module, 'sway', return_value=[{'success':True},{'success':True}]) as sway:
            keyboard.apply('ru', inputs)
            self.assertEqual(sway.call_args.args, (1,0,
                'input type:keyboard xkb_layout us; input type:keyboard xkb_layout ru'))
        with mock.patch.object(module, 'sway') as sway:
            keyboard.apply('ru', [keyboard_input('Russian')])
            sway.assert_not_called()

    def test_sync_waits_for_uploaded_worker_map_before_restoring_selection(self):
        keyboard = module.NativeKeyboard(1)
        with mock.patch.object(keyboard, 'selected', return_value='ru'), \
             mock.patch.object(keyboard, 'apply') as apply, \
             mock.patch.object(module.time, 'sleep'), \
             mock.patch.object(module, 'sway', side_effect=[
                 [keyboard_input('Russian')],
                 [keyboard_input('Russian'), {'type':'keyboard','xkb_layout_names':[]}],
                 [keyboard_input('Russian'), keyboard_input('English (US)')]]) as sway:
            keyboard.sync_input()
            self.assertEqual(sway.call_count, 3)
            self.assertEqual(apply.call_args.args[0], 'ru')
        keyboard.has_user=lambda: True
        with mock.patch.object(keyboard,'apply') as apply, mock.patch.object(module,'sway',return_value=[
                keyboard_input('Russian'), keyboard_input('English (US)')]):
            keyboard.sync_input()
            self.assertEqual(apply.call_args.args[0], 'us')

    def test_interrupted_save_is_recovered_and_unsafe_state_is_rejected(self):
        keyboard = module.NativeKeyboard(1)
        keyboard.state.with_suffix('.new').write_text('interrupted write')
        keyboard.save('ru')
        self.assertEqual(json.loads(keyboard.state.read_text()), {'layout':'ru'})
        keyboard.state.chmod(0o666)
        with self.assertRaises(ValueError): keyboard.selected()

    def test_sddm_can_read_layouts_without_changing_input(self):
        keyboard = module.NativeKeyboard(2)
        with mock.patch.object(keyboard, 'selected', return_value='us'), \
             mock.patch.object(module, 'sway') as sway:
            reply = keyboard.request({'op':'get'}, 107)
            self.assertTrue(reply['ok'])
            self.assertEqual(reply['layout'], 'us')
            self.assertEqual({v['name'] for v in reply['layouts']}, set(LAYOUTS))
            sway.assert_not_called()

    def test_seats_have_independent_state_and_state_symlinks_are_rejected(self):
        first, second = module.NativeKeyboard(1), module.NativeKeyboard(2)
        with mock.patch.object(first, 'apply'), mock.patch.object(second, 'apply'):
            first.save('ru')
            second.save('de')
        self.assertEqual(json.loads(first.state.read_text()), {'layout':'ru'})
        self.assertEqual(json.loads(second.state.read_text()), {'layout':'de'})
        first.state.unlink(); first.state.symlink_to(second.state)
        with self.assertRaises(OSError):first.selected()


if __name__ == '__main__':
    unittest.main()
