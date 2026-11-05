"""qsm-desktop-effects against fake KDE tools: the user's own value always comes back."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'guest' / 'qsm_desktop_effects.py'
FAKE = r'''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
store = Path(os.environ['FAKE_STORE'])
config = json.loads(store.read_text()) if store.exists() else {}
tool = Path(sys.argv[0]).name; args = sys.argv[1:]
if tool.startswith('qdbus'):
    with open(os.environ['FAKE_STORE'] + '.reloads', 'a') as f: f.write(' '.join(args) + '\n')
    sys.exit(int(os.environ.get('FAKE_QDBUS_RC', '0')))
key = args[args.index('--file') + 1] + '/' + args[args.index('--group') + 1] + '/' + args[args.index('--key') + 1]
if tool.startswith('kread'):
    print(config.get(key, ''))
elif '--delete' in args:
    config.pop(key, None); store.write_text(json.dumps(config))
else:
    config[key] = args[-1]; store.write_text(json.dumps(config))
'''
KEY = 'breezerc/Common/ShadowSize'


class DesktopEffects(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        tools = self.root / 'bin'; tools.mkdir()
        for name in ('kreadconfig6', 'kwriteconfig6', 'qdbus6'):
            path = tools / name
            path.write_text(FAKE)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.store = self.root / 'config.json'
        self.env = {**os.environ, 'PATH': f'{tools}:/usr/bin:/bin', 'FAKE_STORE': str(self.store), 'XDG_STATE_HOME': str(self.root / 'state'), 'HOME': str(self.root)}

    def call(self, action, **env):
        return subprocess.run([sys.executable, str(SCRIPT), action], env={**self.env, **env}, capture_output=True, text=True)

    def config(self):
        return json.loads(self.store.read_text()) if self.store.exists() else {}

    def reloads(self):
        path = Path(str(self.store) + '.reloads')
        return path.read_text().splitlines() if path.exists() else []

    def test_unset_value_is_deleted_again(self):
        self.assertEqual(self.call('status').stdout.strip(), 'normal')
        self.assertEqual(self.call('reduce').stdout.strip(), 'reduced')
        self.assertEqual(self.config()[KEY], 'ShadowNone')
        self.assertEqual(self.call('status').stdout.strip(), 'reduced')
        self.assertEqual(self.call('restore').stdout.strip(), 'restored')
        self.assertNotIn(KEY, self.config())
        self.assertEqual(self.reloads(), ['org.kde.KWin /KWin reconfigure'] * 2)
        self.assertEqual(self.call('status').stdout.strip(), 'normal')

    def test_user_value_survives_reducing_twice(self):
        self.store.write_text(json.dumps({KEY: 'ShadowLarge'}))
        self.call('reduce'); self.call('reduce')
        self.assertEqual(self.config()[KEY], 'ShadowNone')
        self.call('restore')
        self.assertEqual(self.config()[KEY], 'ShadowLarge')

    def test_restore_without_reduce_changes_nothing(self):
        self.store.write_text(json.dumps({KEY: 'ShadowSmall'}))
        self.assertEqual(self.call('restore').stdout.strip(), 'nothing to restore')
        self.assertEqual(self.config()[KEY], 'ShadowSmall')
        self.assertEqual(self.reloads(), [])

    def test_kwin_not_running_is_not_an_error(self):
        result = self.call('reduce', FAKE_QDBUS_RC='1')
        self.assertEqual(result.returncode, 0)
        self.assertIn('next login', result.stderr)
        self.assertEqual(self.config()[KEY], 'ShadowNone')

    def test_other_decoration_is_reported(self):
        self.store.write_text(json.dumps({'kwinrc/org.kde.kdecoration2/library': 'org.kde.kwin.aurorae'}))
        self.assertIn('org.kde.kwin.aurorae', self.call('reduce').stderr)


if __name__ == '__main__':
    unittest.main()
