"""PVE UI stays available during an ordinary multi-package upgrade."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "integration/proxmox/pve9/direct_ui"))
from qsm_pve_direct_ui import _default_package_version, UnsupportedPve


class PackageStateTests(unittest.TestCase):
    def test_configured_package_with_pending_triggers_is_usable(self):
        for state in ("installed", "triggers-pending", "triggers-awaited"):
            with self.subTest(state=state), patch("qsm_pve_direct_ui.subprocess.run", return_value=
                    subprocess.CompletedProcess([], 0, state + "\t9.2.21\n")):
                self.assertEqual(_default_package_version(), "9.2.21")

    def test_unconfigured_or_malformed_packages_remain_unsupported(self):
        for value in ("half-configured\t9.2.21", "half-installed\t9.2.21",
                      "config-files\t9.2.21", "not-installed\t9.2.21", "installed", "installed\t../bad"):
            with self.subTest(value=value), patch("qsm_pve_direct_ui.subprocess.run", return_value=
                    subprocess.CompletedProcess([], 0, value)):
                with self.assertRaises(UnsupportedPve):
                    _default_package_version()


if __name__ == "__main__":
    unittest.main()
