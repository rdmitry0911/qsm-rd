#!/usr/bin/env python3
"""The native login screen offers no way to power off a container."""
import configparser
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'packaging/lxc/qsm-native-profile'))
without = MODULE['without_power_off']
BREEZE = """actionItems: [
    ActionButton {
        iconSource: "system-reboot"
        onClicked: sddm.reboot()
        enabled: sddm.canReboot
    },
    ActionButton {
        iconSource: "system-shutdown"
        text: i18nd("x", "Shut Down")
        onClicked: sddm.powerOff()
        enabled: sddm.canPowerOff
    },
    ActionButton {
        onClicked: mainStack.push(userPromptComponent)
        visible: !userListComponent.showUsernamePrompt
    }]
"""


class WithoutPowerOffTests(unittest.TestCase):
    def test_the_button_is_hidden_and_the_others_stay(self):
        result = without(BREEZE)
        self.assertNotIn('powerOff()', result)
        self.assertIn('sddm.reboot()', result)
        shutdown = result[result.index('system-shutdown'):result.index('userPromptComponent')]
        self.assertIn('visible: false', shutdown)
        self.assertEqual(result.count('visible: false'), 1)
        self.assertEqual(without(result), result, 'nothing is left to change')

    def test_every_copy_in_a_file_goes(self):
        self.assertEqual(without(BREEZE + BREEZE).count('visible: false'), 2)

    def test_visible_is_never_set_twice_on_one_object(self):
        text = 'Button {\n  visible: ready\n  onClicked: sddm.powerOff()\n}\n'
        result = without(text)
        self.assertEqual(result.count('visible:'), 1, 'QML refuses a property set twice')
        self.assertNotIn('powerOff', result)

    def test_visible_of_a_nested_object_does_not_count(self):
        text = 'Button {\n  Item {\n    visible: ready\n  }\n  onClicked: sddm.powerOff()\n}\n'
        self.assertIn('visible: false', without(text))

    def test_a_call_in_a_handler_block_goes_too(self):
        result = without('Button {\n  onClicked: {\n    sddm.powerOff()\n  }\n}\n')
        self.assertNotIn('powerOff', result)
        self.assertNotIn('visible', result)


class LoginThemeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.system = self.root / 'themes'
        self.overlay = self.root / 'run' / 'themes'
        (self.root / 'run').mkdir()
        self.etc = self.root / 'etc'
        self.etc.mkdir()
        theme = self.system / 'breeze'
        (theme / 'components').mkdir(parents=True)
        (theme / 'Main.qml').write_text(BREEZE)
        (theme / 'Login.qml').write_text('Item {}\n')
        (theme / 'theme.conf').write_text('[General]\n')
        (theme / 'components' / 'ActionButton.qml').write_text('Item {}\n')
        (self.system / 'ubuntu-theme').symlink_to(theme)
        (self.system / 'nested').mkdir()
        (self.system / 'nested' / 'parts').mkdir()
        (self.system / 'nested' / 'parts' / 'Power.qml').write_text('Button {\n  onClicked: sddm.powerOff()\n}\n')
        (self.system / 'nested' / 'Main.qml').write_text('Item {}\n')
        (self.etc / 'sddm.conf').write_text('[General]\nHaltCommand=/bin/false\n[Theme]\nCurrent=ubuntu-theme\nThemeDir=/usr/share/sddm/themes\n')
        os.chmod(self.etc / 'sddm.conf', 0o640)

    def build(self):
        function = MODULE['login_theme']
        with mock.patch.dict(function.__globals__, {'SYSTEM_THEMES': self.system, 'LOGIN_THEMES': self.overlay,
                                                    'DIRECTORY': self.etc}):
            function()

    def test_a_theme_is_links_and_a_changed_copy(self):
        self.build()
        for name in ('breeze', 'ubuntu-theme'):
            main = self.overlay / name / 'Main.qml'
            self.assertFalse(main.is_symlink(), name)
            self.assertNotIn('powerOff', main.read_text())
            self.assertTrue((self.overlay / name / 'theme.conf').is_symlink())
            self.assertTrue((self.overlay / name / 'components').is_symlink())
            self.assertTrue((self.overlay / name / 'components' / 'ActionButton.qml').is_file())
        self.assertIn('powerOff', (self.system / 'breeze' / 'Main.qml').read_text(), 'the installed theme stays')

    def test_a_nested_file_that_can_power_off_is_changed_too(self):
        self.build()
        self.assertNotIn('powerOff', (self.overlay / 'nested' / 'parts' / 'Power.qml').read_text())
        self.assertTrue((self.overlay / 'nested' / 'Main.qml').is_symlink())

    def test_the_configuration_points_at_the_copies_and_keeps_the_rest(self):
        self.build()
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read(self.etc / 'sddm.conf')
        self.assertEqual(config['Theme']['ThemeDir'], str(self.overlay))
        self.assertEqual(config['Theme']['Current'], 'ubuntu-theme')
        self.assertEqual(config['General']['HaltCommand'], '/bin/false')
        self.assertEqual((self.etc / 'sddm.conf').stat().st_mode & 0o777, 0o640)
        self.assertEqual([item.name for item in self.etc.iterdir()], ['sddm.conf'], 'no temporary file stays')

    def test_each_start_begins_again(self):
        self.build()
        (self.system / 'breeze' / 'Main.qml').write_text('Item { }\n')
        self.build()
        self.assertTrue((self.overlay / 'breeze' / 'Main.qml').is_symlink())

    def test_a_theme_that_cannot_be_read_is_kept_rather_than_lost(self):
        (self.system / 'broken').mkdir()
        (self.system / 'broken' / 'Main.qml').write_bytes(b'sddm.powerOff() \xff\xfe')
        self.build()
        self.assertTrue((self.overlay / 'broken').is_symlink())
        self.assertTrue((self.overlay / 'breeze' / 'Main.qml').is_file(), 'the others are not affected')


if __name__ == '__main__':
    unittest.main()
