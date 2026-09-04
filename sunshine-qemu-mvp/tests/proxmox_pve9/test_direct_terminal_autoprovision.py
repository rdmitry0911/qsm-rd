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
import asyncio
import signal
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_LIBRARY = os.environ.get("QSM_DIRECT_PACKAGE_LIBRARY")
IMPORT_ROOT = Path(PACKAGE_LIBRARY) if PACKAGE_LIBRARY else PROJECT_ROOT
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

if PACKAGE_LIBRARY:
    from direct_terminal.qsm_direct_terminal import (DirectSession, DirectSessionManager,
                                                     DirectVmTransport, _managed_guest_channel_enabled,
                                                     _qemu_process_generation, _load_optional_instance)
else:
    from extensions.direct_terminal.qsm_direct_terminal import (DirectSession, DirectSessionManager,
                                                                 DirectVmTransport, _managed_guest_channel_enabled,
                                                                 _qemu_process_generation, _load_optional_instance)


@unittest.skipUnless(shutil.which("dbus-daemon"), "dbus-daemon is required")
class DirectTerminalAutoprovisionTests(unittest.TestCase):
    def test_absent_encoder_policy_uses_verified_auto_selection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-direct-auto-policy.") as temporary:
            root = Path(temporary)
            instances = root / "instances"
            runtime = root / "display"
            instances.mkdir(mode=0o700)
            policy = _load_optional_instance(instances, 321, runtime)
            self.assertEqual(policy["QSM_DIRECT_ENCODER"], "auto")

            explicit = instances / "321.conf"
            explicit.write_text(
                "QSM_DIRECT_QEMU_DBUS_ADDRESS="
                f"unix:path={runtime}/321/qemu-display1.bus\n"
                "QSM_DIRECT_ENCODER=auto\n",
                encoding="utf-8")
            os.chmod(explicit, 0o600)
            self.assertEqual(
                _load_optional_instance(instances, 321, runtime)["QSM_DIRECT_ENCODER"], "auto")

    @staticmethod
    def _qemu_lookalike(vmid: int) -> subprocess.Popen[bytes]:
        """Run a harmless process whose argv follows PVE's ``kvm -id`` form."""
        return subprocess.Popen(
            ["/bin/bash", "-c",
             f'exec -a kvm /usr/bin/python3 -c "import time; time.sleep(60)" -id {vmid}'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)

    def test_qemu_generation_uses_pid_and_nonreusable_start_time(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-direct-qemu-generation.") as temporary:
            pid_directory = Path(temporary)
            vmid = 321
            process = self._qemu_lookalike(vmid)
            try:
                pid_file = pid_directory / f"{vmid}.pid"
                pid_file.write_text(f"{process.pid}\n", encoding="ascii")
                os.chmod(pid_file, 0o600)
                deadline = time.monotonic() + 1.0
                generation = None
                while time.monotonic() < deadline and generation is None:
                    generation = _qemu_process_generation(pid_directory, vmid)
                    time.sleep(0.01)
                self.assertIsNotNone(generation)
                self.assertTrue(generation.startswith(f"{process.pid}:"))
                self._stop(process)
                self.assertIsNone(_qemu_process_generation(pid_directory, vmid))
            finally:
                self._stop(process)

    def test_vm_restart_retires_stale_video_and_input_transport(self) -> None:
        """A QEMU restart must never retain its old final frame/input socket."""
        class ClosingBridge:
            closed = False

            async def close(self) -> None:
                self.closed = True

        class ClosingResource:
            closed = False

            def close(self) -> None:
                self.closed = True

            def raise_if_failed(self) -> None:
                return

        with tempfile.TemporaryDirectory(prefix="qsm-direct-vm-restart.") as temporary:
            root = Path(temporary)
            for name in ("instances", "qemu-server", "sessions", "display", "pids"):
                (root / name).mkdir(mode=0o700)
            vmid = 321
            (root / "qemu-server" / f"{vmid}.conf").write_text(
                "vga: none\n"
                "args: -device virtio-vga-gl,id=qsm-direct-gpu "
                f"-display dbus,addr=unix:path={root}/display/{vmid}/qemu-display1.bus,"
                "gl=on,rendernode=/dev/dri/renderD128\n",
                encoding="utf-8",
            )
            os.chmod(root / "qemu-server" / f"{vmid}.conf", 0o600)
            qemu = self._qemu_lookalike(vmid)
            worker = subprocess.Popen(["/bin/sleep", "60"], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
            manager = None
            bus_process = None
            try:
                pid_file = root / "pids" / f"{vmid}.pid"
                pid_file.write_text(f"{qemu.pid}\n", encoding="ascii")
                os.chmod(pid_file, 0o600)
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and _qemu_process_generation(root / "pids", vmid) is None:
                    time.sleep(0.01)
                manager = DirectSessionManager(
                    instance_directory=root / "instances", runtime_directory=root / "sessions",
                    vm_runtime_directory=root / "display", pve_config_directory=root / "qemu-server",
                    qemu_pid_directory=root / "pids", local_node=None,
                )
                bus_process = manager._dbus._children.get(vmid)
                media = ClosingResource()
                input_egress = ClosingResource()
                transport_directory = root / "sessions" / f"vm-{vmid}" / "producer"
                transport_directory.mkdir(parents=True, mode=0o700)
                manager._transports[vmid] = DirectVmTransport(
                    vmid=vmid, worker=worker, media=media, input=input_egress,
                    directory=transport_directory, qemu_generation="retired-qemu")
                bridge = ClosingBridge()
                session_directory = root / "sessions" / f"vm-{vmid}" / "browser"
                session_directory.mkdir(mode=0o700)
                manager._sessions["browser"] = DirectSession(
                    vmid=vmid, bridge=bridge, worker=worker, directory=session_directory,
                    expires_at=time.monotonic() + 60)
                future = asyncio.run_coroutine_threadsafe(
                    manager._reconcile_active_transports(frozenset({vmid})), manager._loop)
                future.result(timeout=3)
                self.assertTrue(bridge.closed)
                self.assertTrue(media.closed)
                self.assertTrue(input_egress.closed)
                self.assertNotIn(vmid, manager._transports)
                self.assertNotIn("browser", manager._sessions)
                self.assertIsNotNone(worker.poll(), "stale encoder must be terminated")
            finally:
                if manager is not None:
                    manager.close()
                if bus_process is not None:
                    manager._dbus._terminate(bus_process)
                self._stop(worker)
                self._stop(qemu)

    def test_saved_qsm_guest_channel_requires_the_exact_three_arguments(self) -> None:
        runtime = Path("/run/qsm-pve-direct")
        arguments = (
            "-chardev socket,id=qsm-direct-agent,"
            "path=/run/qsm-pve-direct/321/qsm-agent.sock,server=on,wait=off "
            "-device virtio-serial-pci,id=qsm-direct-serial "
            "-device virtserialport,chardev=qsm-direct-agent,name=org.qsm.direct.agent"
        )
        self.assertTrue(_managed_guest_channel_enabled(f"args: {arguments}\n", 321, runtime))
        self.assertFalse(_managed_guest_channel_enabled(
            f"args: {arguments.removesuffix('agent')}\n", 321, runtime))

    def test_terminal_service_allows_ice_interface_enumeration(self) -> None:
        unit = (PROJECT_ROOT / "packaging/debian/qsm-pve-direct-terminal.service").read_text(
            encoding="utf-8")
        self.assertIn(
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK", unit)
        self.assertIn("KillSignal=SIGINT", unit)
        self.assertIn("KillMode=process", unit)

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

    def test_exited_media_worker_closes_its_webrtc_session_without_a_new_request(self) -> None:
        class ExitedWorker:
            def poll(self) -> int:
                return 0

        class ClosingBridge:
            closed = False

            async def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory(prefix="qsm-direct-session-watch.") as temporary:
            root = Path(temporary)
            for name in ("instances", "qemu-server", "sessions", "display"):
                (root / name).mkdir(mode=0o700)
            manager = DirectSessionManager(
                instance_directory=root / "instances",
                runtime_directory=root / "sessions",
                vm_runtime_directory=root / "display",
                pve_config_directory=root / "qemu-server",
                local_node=None,
            )
            bridge = ClosingBridge()
            directory = root / "sessions" / "vm-321" / "ended"
            directory.parent.mkdir(mode=0o700)
            directory.mkdir(mode=0o700)
            try:
                manager._sessions["ended"] = DirectSession(
                    vmid=321, bridge=bridge, worker=ExitedWorker(), directory=directory,
                    expires_at=time.monotonic() + 60)
                future = asyncio.run_coroutine_threadsafe(
                    manager._watch_session("ended"), manager._loop)
                future.result(timeout=2)
                self.assertTrue(bridge.closed)
                self.assertNotIn("ended", manager._sessions)
                self.assertFalse(directory.exists())
            finally:
                manager.close()

    def test_replacing_a_browser_console_closes_only_that_vms_old_session(self) -> None:
        class ExitedWorker:
            def poll(self) -> int:
                return 0

        class ClosingBridge:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory(prefix="qsm-direct-session-replace.") as temporary:
            root = Path(temporary)
            for name in ("instances", "qemu-server", "sessions", "display"):
                (root / name).mkdir(mode=0o700)
            manager = DirectSessionManager(
                instance_directory=root / "instances",
                runtime_directory=root / "sessions",
                vm_runtime_directory=root / "display",
                pve_config_directory=root / "qemu-server",
                local_node=None,
            )
            old_bridge = ClosingBridge()
            other_bridge = ClosingBridge()
            old_directory = root / "sessions" / "vm-321" / "old"
            other_directory = root / "sessions" / "vm-322" / "other"
            old_directory.parent.mkdir(mode=0o700)
            old_directory.mkdir(mode=0o700)
            other_directory.parent.mkdir(mode=0o700)
            other_directory.mkdir(mode=0o700)
            try:
                manager._sessions["old"] = DirectSession(
                    vmid=321, bridge=old_bridge, worker=ExitedWorker(), directory=old_directory,
                    expires_at=time.monotonic() + 60)
                manager._sessions["other"] = DirectSession(
                    vmid=322, bridge=other_bridge, worker=ExitedWorker(), directory=other_directory,
                    expires_at=time.monotonic() + 60)
                future = asyncio.run_coroutine_threadsafe(
                    manager._close_vmid_sessions(321), manager._loop)
                future.result(timeout=2)
                self.assertTrue(old_bridge.closed)
                self.assertFalse(other_bridge.closed)
                self.assertNotIn("old", manager._sessions)
                self.assertIn("other", manager._sessions)
                self.assertFalse(old_directory.exists())
                self.assertTrue(other_directory.exists())
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
