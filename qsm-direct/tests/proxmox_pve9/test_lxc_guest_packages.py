#!/usr/bin/python3
"""The LXC installer carries complete guest packages before mutating a guest."""
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
loader=importlib.machinery.SourceFileLoader('qsm_lxc',str(ROOT/'packaging/debian/qsm-pve-direct-lxc'))
spec=importlib.util.spec_from_loader(loader.name,loader);lxc=importlib.util.module_from_spec(spec);loader.exec_module(lxc)


class GuestPackageTest(unittest.TestCase):
    def test_missing_geometry_package_fails_before_any_guest_command(self):
        with tempfile.TemporaryDirectory() as d:
            core=Path(d)/'core.deb';core.touch()
            with patch.object(lxc,'GUEST_PACKAGE',core), patch.object(lxc,'QT_GEOMETRY_PACKAGE',Path(d)/'missing.deb'), \
                    patch.object(lxc,'run') as run, patch.object(lxc,'ct') as ct:
                with self.assertRaises(lxc.SetupError):lxc.install_guest_package(108,'wayland')
                run.assert_not_called();ct.assert_not_called()

    def test_both_packages_are_explicit_and_existing_services_are_only_started(self):
        with tempfile.TemporaryDirectory() as d:
            core=Path(d)/'core.deb';qt=Path(d)/'qt.deb';core.touch();qt.touch()
            with patch.object(lxc,'GUEST_PACKAGE',core),patch.object(lxc,'QT_GEOMETRY_PACKAGE',qt), \
                    patch.object(lxc,'run') as run,patch.object(lxc,'ct') as ct,patch.object(lxc,'push'),patch.object(lxc,'info'):
                lxc.install_guest_package(108,'wayland')
                uploads=[c.args[0][3] for c in run.call_args_list]
                self.assertEqual(uploads,[str(core),str(qt)])
                install=ct.call_args_list[0].args[1]
                self.assertIn('/root/qsm-console-guest.deb /root/qsm-qt-input-geometry.deb',install)
                self.assertIn('rm -f /root/qsm-console-guest.deb /root/qsm-qt-input-geometry.deb',install)
                for call in ct.call_args_list:
                    self.assertNotIn('systemctl restart',call.args[1]);self.assertNotIn('systemctl stop',call.args[1])

    def test_console_guest_carries_and_starts_the_file_helper(self):
        build=(ROOT/'packaging/lxc/build-qsm-console-guest-deb.sh').read_text()
        self.assertIn('"$ROOT_DIR/guest/qsm_desktop_files.py" "$lib/qsm_desktop_files.py"',build)
        launch=(ROOT/'packaging/lxc/qsm-session-launch').read_text()
        started=launch.index('--listen "${QSM_CLIPBOARD_SOCKET%/*}/files.sock" &')
        self.assertIn('/usr/lib/qsm-console/qsm_desktop_files.py',launch)
        self.assertLess(launch.index('if [ -n "${QSM_CLIPBOARD_SOCKET:-}" ]'),started)
        self.assertLess(started,launch.index('case "$type" in'))


if __name__=='__main__':unittest.main()
