#!/usr/bin/env python3
"""Native shared-display ownership and API contract regressions."""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from extensions.direct_terminal.qsm_direct_terminal import (
    DirectSessionManager, DirectTerminalError, _shared_display_profile,
    _load_optional_instance, _require_shared_bus,
)
from extensions.direct_terminal.qsm_shared_migration import migration_plan


class MigrationTests(unittest.TestCase):
    def config(self, gl=False, extra=""):
        args = "-display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl="
        args += ("on,rendernode=/dev/dri/renderD129,audiodev=qsm-direct-audio "
                 "-device virtio-vga-gl,id=qsm-direct-gpu -device virtio-keyboard-pci "
                 "-audiodev dbus,id=qsm-direct-audio -device ich9-intel-hda,id=qsm-direct-hda "
                 "-device hda-micro,id=qsm-direct-hda-codec,bus=qsm-direct-hda.0,audiodev=qsm-direct-audio"
                 if gl else "off")
        return f"vga: {'none' if gl else 'virtio,memory=32'}\nargs: {args} {extra}\n"

    def test_gl_replaces_owned_devices_and_keeps_other_arguments(self):
        plan = migration_plan(self.config(True, "-machine vmport=off -device usb-kbd,id=qsm-direct-hda-custom"), 321)
        self.assertEqual(plan["vga"], "virtio-gl,rendernode=/dev/dri/renderD129")
        self.assertEqual(plan["audio0"], "device=ich9-intel-hda,driver=dbus")
        self.assertEqual(plan["args"], "-machine vmport=off -device usb-kbd,id=qsm-direct-hda-custom")

    def test_cpu_preserves_adapter_and_guest_channel(self):
        channel = ("-chardev socket,id=qsm-direct-agent,path=/run/qsm-pve-direct/321/qsm-agent.sock,server=on,wait=off "
                   "-device virtio-serial-pci,id=qsm-direct-serial "
                   "-device virtserialport,chardev=qsm-direct-agent,name=org.qsm.direct.agent")
        plan = migration_plan(self.config(extra=channel), 321)
        self.assertEqual(plan["vga"], "virtio,memory=32")
        self.assertEqual(plan["args"], channel)
        self.assertEqual(plan["dbus-display"], 1)
        self.assertEqual(migration_plan(self.config(), 321)["delete"], "args")

    def test_rejects_foreign_display_or_conflicting_audio(self):
        with self.assertRaises(DirectTerminalError):
            migration_plan(self.config().replace("/321/", "/322/"), 321)
        with self.assertRaises(DirectTerminalError):
            migration_plan(self.config(True) + "audio0: device=AC97,driver=spice\n", 321)


class SharedDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qsm-shared-display.")
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.policy = self.root / "policy"
        self.busdir = self.root / "qemu-server"
        for directory in (self.config, self.policy, self.busdir):
            directory.mkdir(mode=0o700)
        self.vm = self.config / "321.conf"
        self.vm.write_text("dbus-display: 1\nvga: virtio-gl\n")
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.bus = self.busdir / "321.dbusdisplay"
        self.connection.bind(str(self.bus))
        self.bus.chmod(0o600)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def manager(self):
        # Settings/validation do not require a running media worker.
        manager = object.__new__(DirectSessionManager)
        manager._pve_config_directory = self.config
        manager._instance_directory = self.policy
        manager._qemu_pid_directory = self.busdir
        manager._vm_runtime_directory = self.root / "legacy"
        manager._closed = True
        return manager

    def test_profile_uses_only_the_running_opt_in(self):
        self.assertEqual(_shared_display_profile("dbus-display: 1\n"), "cpu")
        self.assertEqual(_shared_display_profile("dbus-display: 1\nvga: virtio-gl\n"), "virgl")
        self.assertIsNone(_shared_display_profile("vga: virtio-gl\n[PENDING]\ndbus-display: 1\n"))
        self.assertEqual(_shared_display_profile("dbus-display: 1\nvga: std\n[PENDING]\nvga: none\n"), "cpu")
        for config in ("dbus-display: 0\n", "dbus-display: 1\nvga: none\n",
                       "dbus-display: 1\nvga: serial0\n", "dbus-display: 1\ndbus-display: 1\n"):
            self.assertIsNone(_shared_display_profile(config))

    def test_policy_cannot_redirect_the_shared_vm_to_another_bus(self):
        f = self.policy / "321.conf"
        f.write_text("QSM_DIRECT_QEMU_DBUS_ADDRESS=unix:path=/tmp/foreign\nQSM_DIRECT_CODEC=h264\n")
        f.chmod(0o600)
        value = _load_optional_instance(self.policy, 321, self.root / "legacy", self.busdir)
        self.assertEqual(value["QSM_DIRECT_QEMU_DBUS_ADDRESS"], f"unix:path={self.bus}")
        self.assertEqual(value["QSM_DIRECT_CODEC"], "h264")

    def test_native_policy_does_not_need_an_address_in_the_file(self):
        f = self.policy / "321.conf"
        f.write_text("QSM_DIRECT_CODEC=h264\nQSM_DIRECT_ENCODER_MODE=software\n")
        f.chmod(0o600)
        self.assertEqual(_load_optional_instance(self.policy, 321, self.root / "legacy", self.busdir)
                         ["QSM_DIRECT_QEMU_DBUS_ADDRESS"], f"unix:path={self.bus}")

    def test_bus_must_be_private_and_cannot_be_a_symlink(self):
        _require_shared_bus(self.busdir, 321)
        self.bus.chmod(0o666)
        with self.assertRaises(DirectTerminalError):
            _require_shared_bus(self.busdir, 321)
        self.bus.chmod(0o600)
        other = self.busdir / "322.dbusdisplay"
        other.symlink_to(self.bus)
        with self.assertRaises(DirectTerminalError):
            _require_shared_bus(self.busdir, 322)

    def test_protocol_v2_never_accepts_a_descriptor_or_arbitrary_address(self):
        manager = self.manager()
        request = {"version": 2, "vmid": 321, "user": "u@pve", "offer": {"sdp": "v=0\r\n"}}
        with self.assertRaisesRegex(DirectTerminalError, "transport is invalid"):
            manager.answer_webrtc(request, self.connection.fileno())
        with self.assertRaisesRegex(DirectTerminalError, "request is invalid"):
            manager.answer_webrtc({**request, "address": "unix:path=/tmp/other"})
        with self.assertRaisesRegex(DirectTerminalError, "is stopped"):
            manager.answer_webrtc(request)

    def test_protocol_v2_rejects_a_legacy_only_config(self):
        self.vm.write_text("vga: std\nargs: -display dbus,addr=unix:path=/tmp/other\n")
        with self.assertRaisesRegex(DirectTerminalError, "not configured"):
            self.manager().answer_webrtc({"version": 2, "vmid": 321, "user": "u@pve",
                                         "offer": {"sdp": "v=0\r\n"}})

    def test_settings_round_trip_and_invalid_combinations(self):
        manager = self.manager()
        request = {"version": 2, "vmid": 321, "user": "u@pve", "settings": None}
        self.assertEqual(manager.webrtc_settings(request), {"codec": "auto", "encoder": "auto"})
        result = manager.webrtc_settings({**request, "settings": {"codec": "h264", "encoder": "software"}})
        self.assertEqual(manager.webrtc_settings(request), result)
        with self.assertRaises(DirectTerminalError):
            manager.webrtc_settings({**request, "settings": {"codec": "hevc", "encoder": "software"}})

    def test_settings_preserve_node_local_encoder_choices(self):
        f = self.policy / "321.conf"
        f.write_text("QSM_DIRECT_ENCODER=h264_vaapi\nQSM_DIRECT_VAAPI_RENDER_NODE=/dev/dri/renderD129\n")
        f.chmod(0o600)
        self.manager().webrtc_settings({"version": 2, "vmid": 321, "user": "u@pve",
                                        "settings": {"codec": "h264", "encoder": "hardware"}})
        self.assertIn("QSM_DIRECT_ENCODER=h264_vaapi\n", f.read_text())
        self.assertIn("QSM_DIRECT_VAAPI_RENDER_NODE=/dev/dri/renderD129\n", f.read_text())

    def test_a_native_vm_never_provisions_a_package_owned_bus(self):
        with patch("extensions.direct_terminal.qsm_direct_terminal.DbusManager.ensure",
                   side_effect=AssertionError("consumer tried to own the shared bus")):
            manager = DirectSessionManager(
                runtime_directory=self.root / "runtime", instance_directory=self.policy,
                vm_runtime_directory=self.root / "legacy", pve_config_directory=self.config,
                qemu_pid_directory=self.busdir, local_node="test",
            )
            try:
                self.assertTrue(manager._display_is_configured(321))
                self.assertEqual(manager._start_configured_display_buses(), ())
            finally:
                manager.close()
        self.assertTrue(self.bus.exists(), "a consumer must not delete the shared bus")

    def test_native_companion_prepares_only_its_serial_directory(self):
        self.vm.write_text("dbus-display: 1\nvga: virtio\nargs: "
            f"-chardev socket,id=qsm-direct-agent,path={self.root}/legacy/321/qsm-agent.sock,server=on,wait=off "
            "-device virtio-serial-pci,id=qsm-direct-serial "
            "-device virtserialport,chardev=qsm-direct-agent,name=org.qsm.direct.agent\n")
        with patch("extensions.direct_terminal.qsm_direct_terminal.DbusManager.ensure",
                   side_effect=AssertionError("consumer tried to own the shared bus")):
            manager = DirectSessionManager(runtime_directory=self.root / "runtime",
                instance_directory=self.policy, vm_runtime_directory=self.root / "legacy",
                pve_config_directory=self.config, qemu_pid_directory=self.busdir, local_node="test")
            try:
                self.assertEqual(manager._start_configured_display_buses(), ())
                self.assertEqual((self.root / "legacy/321").stat().st_mode & 0o777, 0o700)
                self.assertFalse((self.root / "legacy/321/qemu-display1.bus").exists())
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
