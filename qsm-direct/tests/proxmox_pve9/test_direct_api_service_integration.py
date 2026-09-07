#!/usr/bin/env python3
"""Static contract for the node-local signalling service packaging.

The structural design authorises the browser through the node's own
``/access/ticket`` and runs a standalone HTTPS signalling service, so the
package must NOT load anything into pveproxy/pvedaemon: no launcher, no daemon
drop-in, no PVE ABI allow-list.  These checks lock that in.
"""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "packaging/debian"
SIGNAL_SOURCE = PROJECT_ROOT / "extensions/signal/qsm_direct_signal.py"


class DirectSignalServiceIntegrationTests(unittest.TestCase):
    def test_signal_service_and_unit_are_packaged(self) -> None:
        self.assertTrue(SIGNAL_SOURCE.is_file(), "signalling service source is missing")
        launcher = PACKAGE_ROOT / "qsm-pve-direct-signal"
        self.assertTrue(launcher.is_file(), "signalling launcher is missing")
        self.assertIn("signal_service.qsm_direct_signal", launcher.read_text(encoding="utf-8"))
        unit = (PACKAGE_ROOT / "qsm-pve-direct-signal.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/bin/qsm-pve-direct-signal --local-node %H", unit)
        # It must not reach for privileges or run before the services it needs.
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("After=pve-cluster.service pveproxy.service qsm-pve-direct-terminal.service", unit)

    def test_build_stages_the_signal_service_and_no_pve_daemon_hooks(self) -> None:
        builder = (PACKAGE_ROOT / "build-qsm-pve-direct-deb.sh").read_text(encoding="utf-8")
        self.assertIn('"$package_root/signal_service/qsm_direct_signal.py"', builder)
        self.assertIn('"$stage_root/usr/lib/systemd/system/qsm-pve-direct-signal.service"', builder)
        # The retired API-route machinery must be gone from the package.
        for retired in (
            "pve9-api",
            "QsmDirect.pm",
            "Compatibility.pm",
            "qsm-pve-direct-api.conf",
            "pveproxy.service.d",
            "pvedaemon.service.d",
        ):
            self.assertNotIn(retired, builder, f"retired API-route artefact still staged: {retired}")

    def test_postinst_migrates_off_the_retired_daemon_dropins(self) -> None:
        postinst = (PACKAGE_ROOT / "qsm-pve-direct-postinst").read_text(encoding="utf-8")
        self.assertIn("pveproxy.service.d/qsm-pve-direct-api.conf", postinst)
        self.assertIn("try-restart pveproxy.service", postinst)
        self.assertIn("try-restart qsm-pve-direct-signal.service", postinst)


if __name__ == "__main__":
    unittest.main(verbosity=2)
