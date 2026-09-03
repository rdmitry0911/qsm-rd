#!/usr/bin/env python3
"""Keep the optional in-guest package installable and self-starting."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class QsmGuestPackageTests(unittest.TestCase):
    def test_setup_enables_the_system_virtio_agent_for_the_selected_user(self) -> None:
        setup = (ROOT / "packaging/guest/qsm-guest-agent-setup").read_text(encoding="utf-8")
        self.assertIn('systemctl enable "qsm-guest-agent@${user}.service"', setup)
        self.assertIn('systemctl start "qsm-guest-agent@${user}.service"', setup)

    def test_service_uses_only_the_private_direct_virtio_port(self) -> None:
        unit = (ROOT / "packaging/guest/qsm-guest-agent@.service").read_text(encoding="utf-8")
        self.assertIn("ConditionPathExists=/dev/virtio-ports/org.qsm.direct.agent", unit)
        self.assertIn("--device /dev/virtio-ports/org.qsm.direct.agent", unit)
        self.assertIn("Group=qsm-guest", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ReadWritePaths=%h/.local/share/qsm-guest-agent", unit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
