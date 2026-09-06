#!/usr/bin/env python3
"""Static contract for the PVE API wrapper service drop-ins.

These two unit files replace PVE's daemon launch commands.  A single missing
hyphen therefore turns a package upgrade into a pvedaemon/pveproxy outage.
Keep the installed executable name derived from the same role spelling as the
packaged wrapper file, and assert all lifecycle commands rather than only
ExecStart.
"""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = PROJECT_ROOT / "integration/proxmox/pve9/direct_api"
PACKAGE_ROOT = PROJECT_ROOT / "packaging/debian"


class DirectApiServiceIntegrationTests(unittest.TestCase):
    def test_systemd_dropins_reference_packaged_role_wrappers_exactly(self) -> None:
        for role in ("pveproxy", "pvedaemon"):
            with self.subTest(role=role):
                wrapper = API_ROOT / f"qsm-pve-direct-{role}"
                self.assertTrue(wrapper.is_file(), f"missing packaged {role} wrapper")
                self.assertTrue(wrapper.read_text(encoding="utf-8").startswith("#!/usr/bin/perl -T\n"))

                dropin = API_ROOT / "systemd" / f"{role}.service.d/qsm-pve-direct-api.conf"
                content = dropin.read_text(encoding="utf-8")
                executable = f"/usr/lib/qsm-pve-direct/pve9-api/qsm-pve-direct-{role}"
                for directive, operation in (
                    ("ExecStart", "start"),
                    ("ExecStop", "stop"),
                    ("ExecReload", "restart"),
                ):
                    self.assertIn(f"{directive}={executable} {operation}\n", content)
                self.assertNotIn(f"qsm-pve-direct{role}", content)

    def test_package_stages_all_wrapper_and_dropin_sources(self) -> None:
        builder = (PACKAGE_ROOT / "build-qsm-pve-direct-deb.sh").read_text(encoding="utf-8")
        self.assertIn("for role in pveproxy pvedaemon pvesh; do", builder)
        self.assertIn('"$ROOT_DIR/integration/proxmox/pve9/direct_api/qsm-pve-direct-$role"', builder)
        self.assertIn("for role in pveproxy pvedaemon; do", builder)
        self.assertIn('"$ROOT_DIR/integration/proxmox/pve9/direct_api/systemd/$role.service.d/qsm-pve-direct-api.conf"', builder)


if __name__ == "__main__":
    unittest.main(verbosity=2)
