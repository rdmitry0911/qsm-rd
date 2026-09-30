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
    from direct_terminal.qsm_direct_terminal import (ContainerConsoleBusyError, DirectSession,
                                                     DirectSessionManager, DirectTerminalError,
                                                     _load_container_instance)
else:
    from extensions.direct_terminal.qsm_direct_terminal import (ContainerConsoleBusyError,
                                                                DirectSession, DirectSessionManager,
                                                                DirectTerminalError, _load_container_instance)


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

    def test_console_slots_like_ttys(self) -> None:
        policy = self.load("QSM_DIRECT_LXC_DISPLAY=slots\nQSM_DIRECT_LXC_SLOTS=2\nQSM_DIRECT_LXC_GRACE=300\n")
        self.assertEqual((policy["QSM_DIRECT_LXC_DISPLAY"], policy["QSM_DIRECT_LXC_SLOTS"],
                          policy["QSM_DIRECT_LXC_GRACE"]), ("slots", "2", "300"))

    def test_display_mode_is_an_enum_not_a_path(self) -> None:
        for line in ("QSM_DIRECT_LXC_DISPLAY=../../etc", "QSM_DIRECT_LXC_DISPLAY=run/qsm-display",
                     "QSM_DIRECT_LXC_DISPLAY=system", "QSM_DIRECT_LXC_DISPLAY=",
                     "QSM_DIRECT_LXC_SLOTS=0", "QSM_DIRECT_LXC_SLOTS=9", "QSM_DIRECT_LXC_GRACE=-1",
                     "QSM_DIRECT_LXC_GRACE=86401"):
            with self.assertRaises(DirectTerminalError, msg=line):
                self.load(line + "\n")


class ContainerSlotTests(unittest.TestCase):
    """A container console per PVE Console, like the ttys of the terminal console."""

    POLICY = {"QSM_DIRECT_LXC_SLOTS": "2", "QSM_DIRECT_LXC_GRACE": "300"}

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="qsm-ct-slots.")
        manager = DirectSessionManager.__new__(DirectSessionManager)
        manager._slots = {}
        manager._sessions = {}
        manager._closed = True
        manager._slots_path = Path(self.directory.name) / "lxc-slots.json"
        self.manager = manager

    def tearDown(self) -> None:
        self.directory.cleanup()

    def attach(self, key: tuple[int, int]) -> None:
        self.manager._sessions[f"s{len(self.manager._sessions)}"] = DirectSession(
            vmid=key[0], key=key, bridge=None, worker=None, directory=Path("/nonexistent"), expires_at=0.0)

    def claim(self, subject: str, generation: str = "100:1") -> int:
        return self.manager._claim_slot(105, subject, self.POLICY, generation)

    def test_another_user_never_gets_a_console_in_use(self) -> None:
        alice = self.claim("alice@pve")
        self.attach((105, alice))
        bob = self.claim("bob@pve")
        self.attach((105, bob))
        self.assertNotEqual(alice, bob)
        with self.assertRaises(ContainerConsoleBusyError):
            self.claim("carol@pve")

    def test_a_closed_console_waits_for_its_own_user_only(self) -> None:
        alice = self.claim("alice@pve")
        bob = self.claim("bob@pve")
        self.attach((105, bob))
        self.manager._slots[(105, alice)].detached_at = 1.0  # alice closed her tab
        with self.assertRaises(ContainerConsoleBusyError):
            self.claim("carol@pve")  # alice's desktop is not handed to carol
        self.assertEqual(self.claim("alice@pve"), alice)  # alice comes back to it
        self.assertIsNone(self.manager._slots[(105, alice)].detached_at)

    def test_a_second_tab_of_the_same_user_gets_its_own_console(self) -> None:
        first = self.claim("alice@pve")
        self.attach((105, first))
        self.assertNotEqual(self.claim("alice@pve"), first)

    def test_a_restarted_container_forgets_its_consoles(self) -> None:
        self.claim("alice@pve")
        self.claim("bob@pve")
        self.assertEqual(self.claim("carol@pve", generation="200:7"), 1)

    def test_owners_survive_a_service_restart(self) -> None:
        alice = self.claim("alice@pve")
        restarted = DirectSessionManager.__new__(DirectSessionManager)
        restarted._slots = {}
        restarted._slots_path = self.manager._slots_path
        restarted._load_slots()
        slot = restarted._slots[(105, alice)]
        self.assertEqual(slot.owner, "alice@pve")
        self.assertIsNotNone(slot.detached_at)  # no Console survives a restart: the grace period runs


if __name__ == "__main__":
    unittest.main()
