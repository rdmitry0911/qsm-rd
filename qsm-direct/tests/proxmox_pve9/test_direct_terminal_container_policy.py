#!/usr/bin/env python3
"""Container console policy: /etc/qsm-pve-direct/containers.d/<vmid>.conf."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_LIBRARY = os.environ.get("QSM_DIRECT_PACKAGE_LIBRARY")
IMPORT_ROOT = Path(PACKAGE_LIBRARY) if PACKAGE_LIBRARY else PROJECT_ROOT
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

if PACKAGE_LIBRARY:
    from direct_terminal.qsm_direct_terminal import DirectTerminalError, _load_container_instance
else:
    from extensions.direct_terminal.qsm_direct_terminal import DirectTerminalError, _load_container_instance


class ContainerPolicyTests(unittest.TestCase):
    def load(self, text: str) -> dict[str, str] | None:
        with tempfile.TemporaryDirectory(prefix="qsm-ct-policy.") as directory:
            path = Path(directory) / "105.conf"
            path.write_text(text)
            path.chmod(0o600)
            return _load_container_instance(Path(directory), 105)

    def test_absent_policy_disables_the_container(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-ct-policy.") as directory:
            self.assertIsNone(_load_container_instance(Path(directory), 105))

    def test_display_mode_defaults_to_the_per_user_display(self) -> None:
        policy = self.load("QSM_DIRECT_LXC_UID=1000\n")
        self.assertEqual(policy["QSM_DIRECT_LXC_DISPLAY"], "user")

    def test_system_display_of_the_login_screen(self) -> None:
        policy = self.load("QSM_DIRECT_LXC_UID=1000\nQSM_DIRECT_LXC_DISPLAY=system\n")
        self.assertEqual(policy["QSM_DIRECT_LXC_DISPLAY"], "system")

    def test_display_mode_is_an_enum_not_a_path(self) -> None:
        for value in ("../../etc", "run/qsm-display", "System", ""):
            with self.assertRaises(DirectTerminalError):
                self.load(f"QSM_DIRECT_LXC_DISPLAY={value}\n")


if __name__ == "__main__":
    unittest.main()
