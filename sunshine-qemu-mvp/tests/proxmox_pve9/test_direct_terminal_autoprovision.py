#!/usr/bin/env python3
"""Regression coverage for QSM Display1 bus provisioning from PVE config."""

from __future__ import annotations

import os
import shutil
import sys
import stat
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_LIBRARY = os.environ.get("QSM_DIRECT_PACKAGE_LIBRARY")
IMPORT_ROOT = Path(PACKAGE_LIBRARY) if PACKAGE_LIBRARY else PROJECT_ROOT
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

if PACKAGE_LIBRARY:
    from direct_terminal.qsm_direct_terminal import DirectSessionManager
else:
    from extensions.direct_terminal.qsm_direct_terminal import DirectSessionManager


@unittest.skipUnless(shutil.which("dbus-daemon"), "dbus-daemon is required")
class DirectTerminalAutoprovisionTests(unittest.TestCase):
    def test_saved_display1_argument_starts_a_private_bus_without_policy_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-direct-autoprovision.") as temporary:
            root = Path(temporary)
            instance_directory = root / "instances"
            pve_config_directory = root / "qemu-server"
            runtime_directory = root / "sessions"
            vm_runtime_directory = root / "display"
            instance_directory.mkdir(mode=0o700)
            pve_config_directory.mkdir(mode=0o700)
            vmid = 321
            config = pve_config_directory / f"{vmid}.conf"
            config.write_text("vga: none\n", encoding="utf-8")
            os.chmod(config, 0o600)

            manager = DirectSessionManager(
                instance_directory=instance_directory,
                runtime_directory=runtime_directory,
                vm_runtime_directory=vm_runtime_directory,
                pve_config_directory=pve_config_directory,
                local_node=None,
            )
            bus_process = None
            replacement = None
            try:
                socket_path = vm_runtime_directory / str(vmid) / "qemu-display1.bus"
                self.assertFalse(socket_path.exists(), "a VM without qsm Display1 must not receive a bus")
                config.write_text(
                    "vga: none\n"
                    "args: -device virtio-vga-gl,id=qsm-direct-gpu "
                    f"-display dbus,addr=unix:path={vm_runtime_directory}/{vmid}/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128\n",
                    encoding="utf-8",
                )
                os.chmod(config, 0o600)
                deadline = time.monotonic() + 3.0
                while (time.monotonic() < deadline and
                       (not socket_path.exists() or vmid not in manager._dbus._children)):
                    time.sleep(0.05)
                self.assertTrue(socket_path.exists(), "saving QSM Display1 must provision its bus automatically")
                self.assertTrue(stat.S_ISSOCK(socket_path.stat().st_mode))
                self.assertTrue(manager._display_is_configured(vmid))
                bus_process = manager._dbus._children[vmid]

                # A terminal upgrade/restart must adopt the existing bus.
                # QEMU has no reconnect protocol for Display1, so replacing
                # this socket would make a running VM unusable.
                manager.close()
                self.assertIsNone(bus_process.poll(), "the private bus must outlive the terminal process")
                replacement = DirectSessionManager(
                    instance_directory=instance_directory,
                    runtime_directory=runtime_directory,
                    vm_runtime_directory=vm_runtime_directory,
                    pve_config_directory=pve_config_directory,
                    local_node=None,
                )
                self.assertTrue(replacement._dbus._socket_is_live(socket_path))
                self.assertNotIn(vmid, replacement._dbus._children)
            finally:
                if replacement is not None:
                    replacement.close()
                manager.close()
                if bus_process is not None:
                    manager._dbus._terminate(bus_process)


if __name__ == "__main__":
    unittest.main(verbosity=2)
