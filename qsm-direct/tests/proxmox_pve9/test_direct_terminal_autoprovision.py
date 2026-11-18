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
    from direct_terminal.qsm_direct_encoder_probe import DirectEncoderSelection
    from direct_terminal.qsm_direct_terminal import (DirectSession, DirectSessionManager,
                                                     DirectVmTransport, _managed_display_enabled, _managed_display_profile,
                                                     _managed_guest_channel_enabled,
                                                     _qemu_process_generation, _load_optional_instance,
                                                     DirectTerminalError)
else:
    from extensions.direct_terminal.qsm_direct_encoder_probe import DirectEncoderSelection
    from extensions.direct_terminal.qsm_direct_terminal import (DirectSession, DirectSessionManager,
                                                                 DirectVmTransport, _managed_display_enabled, _managed_display_profile,
                                                                 _managed_guest_channel_enabled,
                                                                 _qemu_process_generation, _load_optional_instance,
                                                                 DirectTerminalError)


@unittest.skipUnless(shutil.which("dbus-daemon"), "dbus-daemon is required")
class DirectTerminalAutoprovisionTests(unittest.TestCase):
    def test_cpu_display1_accepts_only_stock_non_gl_vga_profiles(self) -> None:
        runtime = Path("/run/qsm-pve-direct")
        argument = (
            "-display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=off"
        )
        self.assertTrue(_managed_display_enabled(f"vga: std\nargs: {argument}\n", 321, runtime))
        self.assertTrue(_managed_display_enabled(
            f"vga: virtio,memory=256\nargs: {argument}\n", 321, runtime))
        self.assertTrue(_managed_display_enabled(
            f"args: {argument}\n", 321, runtime), "PVE's absent vga key is Standard VGA")
        self.assertTrue(_managed_display_enabled(
            f"vga: vmware,memory=128\nargs: {argument}\n", 321, runtime),
            "VMware SVGA is a stock non-GL 2D adapter the CPU profile captures")
        for vga in ("none", "virtio-gl", "qxl"):
            self.assertFalse(_managed_display_enabled(
                f"vga: {vga}\nargs: {argument}\n", 321, runtime), vga)
        self.assertFalse(_managed_display_enabled(
            f"vga: std\nargs: {argument} -device virtio-vga-gl,id=foreign\n", 321, runtime))
        self.assertFalse(_managed_display_enabled(
            "vga: std\nargs: -display dbus,addr=unix:path=/run/qsm-pve-direct/321/"
            "qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128\n", 321, runtime))

    def test_display1_with_managed_audio_is_recognised(self) -> None:
        runtime = Path("/run/qsm-pve-direct")
        bus = "-display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus"
        audio = (" -audiodev dbus,id=qsm-direct-audio,out.frequency=48000,out.channels=2"
                 " -device ich9-intel-hda,id=qsm-direct-hda -device hda-output,id=qsm-direct-hda-codec,"
                 "bus=qsm-direct-hda.0,audiodev=qsm-direct-audio")
        self.assertTrue(_managed_display_enabled(
            f"vga: std\nargs: {bus},gl=off,audiodev=qsm-direct-audio{audio}\n", 321, runtime))
        self.assertTrue(_managed_display_enabled(
            f"vga: none\nargs: -device virtio-vga-gl,id=qsm-direct-gpu {bus},gl=on,"
            f"rendernode=/dev/dri/renderD128,audiodev=qsm-direct-audio{audio}\n", 321, runtime))
        self.assertFalse(_managed_display_enabled(
            f"vga: std\nargs: {bus},gl=off,audiodev=foreign\n", 321, runtime),
            "another audiodev is an administrator's display configuration")

    def test_a_qdev_host_gpu_under_the_virgl_display_is_recognised(self) -> None:
        runtime = Path("/run/qsm-pve-direct")
        bus = ("-display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,"
               "rendernode=/dev/dri/renderD128")
        gpu = ("-chardev socket,id=qdev-gpu0,path=/run/qdev-host/321-gpu0/gpu.sock "
               "-device vhost-user-vga,id=gpu0,chardev=qdev-gpu0,bus=qdev-rp0,max_outputs=1,xres=1280,yres=800")
        self.assertEqual(_managed_display_profile(f"vga: none\nargs: {bus} {gpu}\n", 321, runtime), "virgl")
        secondary = gpu.replace("vhost-user-vga", "vhost-user-gpu-pci")
        self.assertEqual(_managed_display_profile(
            f"vga: none\nargs: -device virtio-vga-gl,id=qsm-direct-gpu {bus} {secondary}\n", 321, runtime), "virgl",
            "QSM's own GPU stays the first display next to an additional qdev-host GPU (vga=0)")
        self.assertIsNone(_managed_display_profile(
            f"vga: none\nargs: {bus} {gpu} {gpu.replace('gpu0', 'gpu1')}\n", 321, runtime))
        self.assertEqual(_managed_display_profile(
            f"vga: std\nargs: {bus.replace('gl=on,rendernode=/dev/dri/renderD128', 'gl=off')} {secondary}\n", 321, runtime),
            "cpu", "PVE's Standard VGA stays the first display next to an additional qdev-host GPU")

    # The external profile, with the config shapes of three real VMs: 103 (VirGL
    # display, qdev-host vhost-user GPU), 104 and 118 (macOS, vga none, a qdev-host
    # vfio-user PVG device whose unit serves Display1 with --display-bus).
    QSM_AGENT = ("-chardev socket,id=qsm-direct-agent,path=/run/qsm-pve-direct/{vmid}/qsm-agent.sock,server=on,"
                 "wait=off -device virtio-serial-pci,id=qsm-direct-serial "
                 "-device virtserialport,chardev=qsm-direct-agent,name=org.qsm.direct.agent")
    PVG = ("-device '{{\"driver\":\"vfio-user-pci\",\"id\":\"{id}\",\"bus\":\"pcie.0\",\"addr\":\"0x5\","
           "\"socket\":{{\"type\":\"unix\",\"path\":\"/run/qdev-host/{vmid}-{id}/vfio.sock\"}},"
           "\"romfile\":\"/opt/apple-virgl/roms/ApplePVG-stock-eeee.rom\"}}'")
    VM104_ARGS = (
        "-readconfig /var/lib/qdev-host/tb/104.cfg -acpitable file=/var/lib/qdev-host/tb/104.aml "
        "-cpu Skylake-Client-v4,vendor=GenuineIntel -device qemu-xhci -device usb-kbd -device usb-tablet "
        "-device isa-applesmc,osk=\"ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc\" "
        + QSM_AGENT.format(vmid=104) +
        " -machine vmport=off -machine i8042=off -device usb-kbd,id=qsm-direct-keyboard "
        "-global ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off "
        "-object memory-backend-memfd,id=applevirgl-mem,size=32768M,share=on -machine memory-backend=applevirgl-mem "
        "-chardev socket,id=applevirgl-qmp,path=/run/applevirgl/104-qmp.sock,server=on,wait=off "
        "-mon chardev=applevirgl-qmp,mode=control " + PVG.format(vmid=104, id="pvg0"))
    VM118_ARGS = (
        "-device isa-applesmc,osk=\"ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc\" -smbios type=2 "
        "-device usb-kbd,bus=ehci.0,port=2 -global nec-usb-xhci.msi=off "
        "-global ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off "
        "-cpu Haswell-noTSX,vendor=GenuineIntel,+invtsc,+hypervisor,kvm=on,vmware-cpuid-freq=on "
        "-global vmware-svga.vgamem_mb=1 " + QSM_AGENT.format(vmid=118) +
        " -machine vmport=off -machine i8042=off "
        "-object memory-backend-memfd,id=applevirgl-mem,size=32768M,share=on -machine memory-backend=applevirgl-mem "
        "-chardev socket,id=applevirgl-qmp,path=/run/applevirgl/118-qmp.sock,server=on,wait=off "
        "-mon chardev=applevirgl-qmp,mode=control " + PVG.format(vmid=118, id="pvg0"))
    VM118_QEMU_DISPLAY = " -display dbus,addr=unix:path=/run/qsm-pve-direct/118/qemu-display1.bus,gl=off"
    VM103_ARGS = (
        QSM_AGENT.format(vmid=103) + " -machine vmport=off -machine i8042=off -device usb-kbd,id=qsm-direct-keyboard "
        "-device pcie-root-port,id=qdev-rp0,bus=pcie.0,addr=0x08,chassis=60,slot=60,port=60,pref64-reserve=256M,"
        "mem-reserve=16M -display dbus,addr=unix:path=/run/qsm-pve-direct/103/qemu-display1.bus,gl=on,"
        "rendernode=/dev/dri/renderD128,audiodev=qsm-direct-audio -audiodev dbus,id=qsm-direct-audio,"
        "in.frequency=48000,in.channels=2,out.frequency=48000,out.channels=2 -device ich9-intel-hda,id=qsm-direct-hda "
        "-device hda-micro,id=qsm-direct-hda-codec,bus=qsm-direct-hda.0,audiodev=qsm-direct-audio "
        "-object memory-backend-memfd,id=qdev-mem,size=16384M,share=on -machine memory-backend=qdev-mem "
        "-chardev socket,id=qdev-virglgpu0,path=/run/qdev-host/103-virglgpu0/gpu.sock "
        "-device vhost-user-gpu-pci,id=virglgpu0,chardev=qdev-virglgpu0,bus=qdev-rp0,max_outputs=1,xres=1280,yres=800")

    @staticmethod
    def _qdev_unit(directory: Path, vmid: int, device: str, *, runtime: Path = Path("/run/qsm-pve-direct"),
                   bus: str | None = None, extra: str = "", mode: int = 0o640) -> Path:
        """A unit env as qdevctl writes it (118) or a VM hook keeps it (104)."""
        bus = f"unix:path={runtime}/{vmid}/qemu-display1.bus" if bus is None else bus
        display = f" --display-bus {bus}" if bus else ""
        path = directory / f"{vmid}-{device}.env"
        path.write_text(
            f"QDEV_ARGS=--plugin /usr/lib/qdev-host/plugins/apple-pvg.so --socket /run/qdev-host/{vmid}-{device}/vfio.sock "
            f"--dump-dir /var/lib/qdev-host --vm-name VM{vmid}-{device} --qemu-qmp /run/applevirgl/{vmid}-qmp.sock "
            f"--control /run/qdev-host/{vmid}-{device}/ctl.sock --plugin-args xres=1920,yres=1080{display}{extra}\n",
            encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_a_qdev_host_display1_device_is_the_external_profile(self) -> None:
        runtime = Path("/run/qsm-pve-direct")
        with tempfile.TemporaryDirectory(prefix="qsm-direct-qdev-units.") as temporary:
            units = Path(temporary)
            self._qdev_unit(units, 104, "pvg0", extra=" --display-name org.qemu")
            self._qdev_unit(units, 118, "pvg0")
            self._qdev_unit(units, 103, "virglgpu0", bus="")
            profile = lambda config, vmid: _managed_display_profile(config, vmid, runtime, units)  # noqa: E731
            self.assertEqual(profile(f"vga: none\nargs: {self.VM104_ARGS}\n", 104), "external")
            self.assertEqual(profile(f"vga: none\nargs: {self.VM118_ARGS}\n", 118), "external",
                             "118 after QEMU's own -display is removed")
            self.assertTrue(_managed_display_enabled(f"vga: none\nargs: {self.VM118_ARGS}\n", 118, runtime, units))
            self.assertEqual(profile(f"vga: none\nargs: {self.VM103_ARGS}\n", 103), "virgl",
                             "the VirGL display with a qdev-host GPU is unchanged")
            self.assertEqual(profile(f"vga: type=none\nargs: {self.VM104_ARGS}\n", 104), "external")
            self.assertEqual(profile(f"vga: none\nargs: {self.VM104_ARGS}\n\n[PENDING]\nvga: std\n", 104),
                             "external", "a pending change does not hide the running display")
            other = self.PVG.format(vmid=104, id="tb0")
            self.assertEqual(profile(f"vga: none\nargs: {self.VM104_ARGS} {other}\n", 104), "external",
                             "another vfio-user device without a display is not a second display")

    def test_the_external_profile_refuses_everything_it_does_not_own(self) -> None:
        runtime = Path("/run/qsm-pve-direct")
        with tempfile.TemporaryDirectory(prefix="qsm-direct-qdev-units.") as temporary:
            units = Path(temporary)
            unit = self._qdev_unit(units, 104, "pvg0")
            profile = lambda config, vmid=104, directory=units: _managed_display_profile(  # noqa: E731
                config, vmid, runtime, directory)
            vm104 = f"vga: none\nargs: {self.VM104_ARGS}\n"
            self.assertEqual(profile(vm104), "external")
            # 118 as found: QEMU's own -display dbus on the same bus (both would claim org.qemu).
            self._qdev_unit(units, 118, "pvg0")
            self.assertIsNone(profile(f"vga: none\nargs: {self.VM118_ARGS}{self.VM118_QEMU_DISPLAY}\n", 118))
            # PVE's own adapter (implicit Standard VGA, or an explicit one) is another display.
            for vga in ("", "vga: std\n", "vga: vmware\n", "vga: virtio\n", "vga: virtio-gl\n", "vga: none,memory=16\n"):
                self.assertIsNone(profile(f"{vga}args: {self.VM104_ARGS}\n"), vga or "absent vga")
            # Another display adapter or QEMU display in args.
            for added in ("-device VGA", "-device vmware-svga,id=v", "-device virtio-vga-gl,id=qsm-direct-gpu",
                          "-device virtio-gpu-pci", "-vga std", "-display gtk", "-spice port=5900",
                          "-chardev socket,id=qdev-g,path=/run/qdev-host/104-g/gpu.sock "
                          "-device vhost-user-gpu-pci,id=g,chardev=qdev-g"):
                self.assertIsNone(profile(f"vga: none\nargs: {self.VM104_ARGS} {added}\n"), added)
            # No unit, or another VM's device socket in this VM's args.
            self.assertIsNone(profile(vm104, directory=units / "missing"))
            self.assertIsNone(profile(f"vga: none\nargs: {self.VM104_ARGS.replace('/104-pvg0/', '/105-pvg0/')}\n"))
            self.assertIsNone(profile(vm104.replace("/run/qdev-host/104-pvg0/vfio.sock", "/run/other/vfio.sock")))
            self.assertIsNone(profile("vga: none\nargs: -device '{\"driver\":\"vfio-user-pci\"'\n"),
                              "an unparseable device is not read as a display")
            self.assertIsNone(profile(f"vga: none\nargs: {self.VM104_ARGS} '\n"), "unbalanced quoting")
            # The unit must declare exactly this VM's bus, for this device, as org.qemu.
            for bus, extra in (("", ""),
                               ("unix:path=/run/qsm-pve-direct/105/qemu-display1.bus", ""),
                               ("unix:path=/run/qdev/other.bus", ""),
                               (None, " --display-name org.example"),
                               (None, " --display-bus unix:path=/run/qsm-pve-direct/104/qemu-display1.bus"),
                               (None, " --socket /run/qdev-host/104-other/vfio.sock")):
                self._qdev_unit(units, 104, "pvg0", bus=bus, extra=extra)
                self.assertIsNone(profile(vm104), (bus, extra))
            self._qdev_unit(units, 104, "pvg0", extra=" --display-name=org.qemu")
            self.assertEqual(profile(vm104), "external", "--name=value is the same option")
            # Two devices serving the same bus would both claim org.qemu.
            self._qdev_unit(units, 104, "pvg1")
            self.assertIsNone(profile(f"vga: none\nargs: {self.VM104_ARGS} {self.PVG.format(vmid=104, id='pvg1')}\n"))
            # The unit file must be root's and not writable by others, with one plain QDEV_ARGS.
            self._qdev_unit(units, 104, "pvg0", mode=0o664)
            self.assertIsNone(profile(vm104), "a group-writable unit")
            self._qdev_unit(units, 104, "pvg0")
            text = unit.read_text(encoding="utf-8")
            for content in (text + text, text.replace("QDEV_ARGS=", "QDEV_ARGS=\"") , "# no arguments\n"):
                unit.write_text(content, encoding="utf-8")
                self.assertIsNone(profile(vm104), content[:24])
            unit.write_text(text, encoding="utf-8")
            unit.rename(units / "real.env")
            unit.symlink_to(units / "real.env")
            self.assertIsNone(profile(vm104), "a symlinked unit")
            unit.unlink()
            (units / "real.env").rename(unit)
            self.assertEqual(profile(vm104), "external")
            os.chmod(units, 0o777)
            try:
                self.assertIsNone(profile(vm104), "a unit directory writable by others")
            finally:
                os.chmod(units, 0o700)

    def test_a_pending_change_does_not_hide_the_running_display(self) -> None:
        runtime = Path("/run/qsm-pve-direct")
        bus = "-display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus"
        config = (f"vga: std\nargs: {bus},gl=off\n\n[PENDING]\nargs: {bus},gl=off,audiodev=qsm-direct-audio "
                  "-audiodev dbus,id=qsm-direct-audio\n\n[snap1]\nargs: -display gtk\n")
        self.assertTrue(_managed_display_enabled(config, 321, runtime),
                        "a running VM keeps its console while a change waits for the next start")

    def test_vm_policy_accepts_bounded_video_bitrate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-direct-bitrate.") as temporary:
            root = Path(temporary)
            path = root / "321.conf"
            for bitrate in ("500", "12000", "100000"):
                path.write_text("QSM_DIRECT_QEMU_DBUS_ADDRESS=unix:path=/run/qsm/321/qemu-display1.bus\n"
                                "QSM_DIRECT_BITRATE_KBPS=" + bitrate + "\n")
                path.chmod(0o600)
                policy = _load_optional_instance(root, 321, Path("/run/qsm"))
                self.assertEqual(policy["QSM_DIRECT_BITRATE_KBPS"], bitrate)
            for bitrate in ("499", "100001", "1.5", "unlimited"):
                path.write_text("QSM_DIRECT_QEMU_DBUS_ADDRESS=unix:path=/run/qsm/321/qemu-display1.bus\n"
                                "QSM_DIRECT_BITRATE_KBPS=" + bitrate + "\n")
                with self.assertRaises(DirectTerminalError):
                    _load_optional_instance(root, 321, Path("/run/qsm"))

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

            explicit.write_text(
                "QSM_DIRECT_QEMU_DBUS_ADDRESS="
                f"unix:path={runtime}/321/qemu-display1.bus\n"
                "QSM_DIRECT_CODEC=hevc\n"
                "QSM_DIRECT_ENCODER_MODE=hardware\n",
                encoding="utf-8")
            policy = _load_optional_instance(instances, 321, runtime)
            self.assertEqual(policy["QSM_DIRECT_CODEC"], "hevc")
            self.assertEqual(policy["QSM_DIRECT_ENCODER_MODE"], "hardware")

    def test_policy_without_address_uses_the_private_bus(self) -> None:
        # The Settings writer keeps only an address it found, and a VM moved
        # off the native shared display has a codec-only policy file.
        with tempfile.TemporaryDirectory(prefix="qsm-direct-codec-policy.") as temporary:
            root = Path(temporary)
            path = root / "321.conf"
            path.write_text("QSM_DIRECT_CODEC=h264\nQSM_DIRECT_ENCODER_MODE=software\n", encoding="utf-8")
            path.chmod(0o600)
            policy = _load_optional_instance(root, 321, Path("/run/qsm"))
            self.assertEqual(policy["QSM_DIRECT_QEMU_DBUS_ADDRESS"], "unix:path=/run/qsm/321/qemu-display1.bus")
            self.assertEqual(policy["QSM_DIRECT_CODEC"], "h264")
            path.write_text("QSM_DIRECT_QEMU_DBUS_ADDRESS=unix:path=/run/qsm/999/qemu-display1.bus\n", encoding="utf-8")
            with self.assertRaises(DirectTerminalError):
                _load_optional_instance(root, 321, Path("/run/qsm"))

    def test_one_invalid_vm_policy_does_not_stop_the_service(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsm-direct-bad-policy.") as temporary:
            root = Path(temporary)
            instance_directory = root / "instances"
            pve_config_directory = root / "qemu-server"
            vm_runtime_directory = root / "display"
            instance_directory.mkdir(mode=0o700)
            pve_config_directory.mkdir(mode=0o700)
            for vmid in (321, 322):
                config = pve_config_directory / f"{vmid}.conf"
                config.write_text(
                    "vga: none\n"
                    "args: -device virtio-vga-gl,id=qsm-direct-gpu "
                    f"-display dbus,addr=unix:path={vm_runtime_directory}/{vmid}/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128\n",
                    encoding="utf-8")
                os.chmod(config, 0o600)
            bad = instance_directory / "321.conf"
            bad.write_text("QSM_DIRECT_QEMU_DBUS_ADDRESS=unix:path=/elsewhere.bus\n", encoding="utf-8")
            os.chmod(bad, 0o600)
            manager = DirectSessionManager(
                instance_directory=instance_directory,
                runtime_directory=root / "sessions",
                vm_runtime_directory=vm_runtime_directory,
                pve_config_directory=pve_config_directory,
                local_node=None,
            )
            children = dict(manager._dbus._children)
            try:
                self.assertIn(322, children, "the other VM still gets its bus")
                self.assertNotIn(321, children, "the VM with a foreign address gets none")
            finally:
                manager.close()
                for process in children.values():
                    manager._dbus._terminate(process)

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
                    directory=transport_directory, qemu_generation="retired-qemu", codec="h264")
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

    def test_service_start_prepares_the_bus_an_external_display_device_joins(self) -> None:
        """After a node reboot qdev-host's --display-bus must find the VM's bus before the VM starts."""
        with tempfile.TemporaryDirectory(prefix="qsm-direct-external-bus.") as temporary:
            root = Path(temporary)
            instance_directory = root / "instances"
            pve_config_directory = root / "qemu-server"
            vm_runtime_directory = root / "display"
            units = root / "units"
            for directory in (instance_directory, pve_config_directory, units):
                directory.mkdir(mode=0o700)
            for vmid in (104, 105):
                config = pve_config_directory / f"{vmid}.conf"
                config.write_text(f"vga: none\nargs: {self.VM104_ARGS.replace('104', str(vmid))}\n", encoding="utf-8")
                os.chmod(config, 0o600)
            self._qdev_unit(units, 104, "pvg0", runtime=vm_runtime_directory)
            self._qdev_unit(units, 105, "pvg0", runtime=vm_runtime_directory, bus="")  # no Display1
            manager = DirectSessionManager(
                instance_directory=instance_directory, runtime_directory=root / "sessions",
                vm_runtime_directory=vm_runtime_directory, pve_config_directory=pve_config_directory,
                local_node=None, qdev_unit_directory=units)
            bus_process = None
            try:
                socket_path = vm_runtime_directory / "104" / "qemu-display1.bus"
                self.assertTrue(stat.S_ISSOCK(socket_path.lstat().st_mode),
                                "the bus exists when the service is ready, before the VM starts")
                self.assertEqual(socket_path.lstat().st_mode & 0o077, 0)
                bus_process = manager._dbus._children[104]
                self.assertTrue(manager._display_is_configured(104))
                self.assertEqual(manager._configured_vms(), (104,))
                self.assertFalse(manager._display_is_configured(105))
                self.assertFalse((vm_runtime_directory / "105" / "qemu-display1.bus").exists(),
                                 "a qdev-host device without Display1 gets no bus")
                self._qdev_unit(units, 104, "pvg0", runtime=vm_runtime_directory, bus="")
                self.assertFalse(manager._display_is_configured(104), "the unit's declaration is re-read")
            finally:
                manager.close()
                if bus_process is not None:
                    manager._dbus._terminate(bus_process)

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

    def test_automatic_codec_policy_prefers_hevc_when_offered_and_probed(self) -> None:
        """``auto`` picks HEVC when the offer has H.265 and a probe cached one; else H.264."""
        offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
                 "m=video 9 UDP/TLS/RTP/SAVPF 96 97\r\nc=IN IP4 0.0.0.0\r\n"
                 "a=rtpmap:96 H265/90000\r\na=rtpmap:97 H264/90000\r\n"
                 "a=fmtp:97 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f\r\n")
        with tempfile.TemporaryDirectory(prefix="qsm-direct-codec-policy.") as temporary:
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
            try:
                # Cached selections stand in for the ffmpeg probes.
                manager._auto_encoders["h264"] = DirectEncoderSelection("libx264")
                manager._auto_encoders["hevc"] = DirectEncoderSelection("hevc_nvenc")
                codec, selection = manager._codec_for_offer(offer, {"QSM_DIRECT_CODEC": "auto"})
                self.assertEqual(codec, "hevc")
                self.assertEqual(selection, DirectEncoderSelection("hevc_nvenc"))
                # With no H.265 in the offer, auto falls back to H.264.
                h264_only = offer.replace("a=rtpmap:96 H265/90000\r\n", "")
                codec, selection = manager._codec_for_offer(h264_only, {})
                self.assertEqual(codec, "h264")
                self.assertEqual(selection, DirectEncoderSelection("libx264"))
                # Forced HEVC without an H.265 offer is refused.
                with self.assertRaisesRegex(DirectTerminalError, "does not offer WebRTC HEVC"):
                    manager._codec_for_offer(h264_only, {"QSM_DIRECT_CODEC": "hevc"})
            finally:
                manager.close()

    def test_connected_browser_session_is_not_closed_by_the_idle_lease(self) -> None:
        """A live WebRTC peer refreshes its lease; a vanished one still expires."""
        class LiveWorker:
            def poll(self) -> None:
                return None

        class StateBridge:
            closed = False

            def __init__(self, state: str) -> None:
                self.connection_state = state

            async def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory(prefix="qsm-direct-session-lease.") as temporary:
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
            try:
                sessions = {}
                for name, state in (("connected", "connected"), ("vanished", "disconnected")):
                    directory = root / "sessions" / "vm-321" / name
                    directory.parent.mkdir(mode=0o700, exist_ok=True)
                    directory.mkdir(mode=0o700)
                    bridge = StateBridge(state)
                    # An already expired lease: only a connected peer survives it.
                    manager._sessions[name] = DirectSession(
                        vmid=321, bridge=bridge, worker=LiveWorker(), directory=directory,
                        expires_at=time.monotonic() - 1)
                    sessions[name] = (bridge, directory)
                vanished = asyncio.run_coroutine_threadsafe(
                    manager._watch_session("vanished"), manager._loop)
                vanished.result(timeout=2)
                self.assertTrue(sessions["vanished"][0].closed)
                self.assertNotIn("vanished", manager._sessions)

                connected = asyncio.run_coroutine_threadsafe(
                    manager._watch_session("connected"), manager._loop)
                time.sleep(0.8)
                self.assertFalse(connected.done())
                self.assertFalse(sessions["connected"][0].closed)
                self.assertIn("connected", manager._sessions)
                self.assertGreater(manager._sessions["connected"].expires_at, time.monotonic() + 60)
                connected.cancel()
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

    def test_closed_browser_peer_retires_its_own_session_immediately(self) -> None:
        class RunningWorker:
            def poll(self) -> None:
                return None

        class ClosingBridge:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory(prefix="qsm-direct-browser-close.") as temporary:
            root = Path(temporary)
            for name in ("instances", "qemu-server", "sessions", "display"):
                (root / name).mkdir(mode=0o700)
            manager = DirectSessionManager(
                instance_directory=root / "instances", runtime_directory=root / "sessions",
                vm_runtime_directory=root / "display", pve_config_directory=root / "qemu-server",
                local_node=None,
            )
            bridge = ClosingBridge()
            directory = root / "sessions" / "vm-321" / "closed"
            directory.parent.mkdir(mode=0o700)
            directory.mkdir(mode=0o700)
            try:
                manager._sessions["closed"] = DirectSession(
                    vmid=321, bridge=bridge, worker=RunningWorker(), directory=directory,
                    expires_at=time.monotonic() + 600)
                manager._loop.call_soon_threadsafe(manager._schedule_browser_retirement, "closed")
                deadline = time.monotonic() + 2
                while "closed" in manager._sessions and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(bridge.closed)
                self.assertNotIn("closed", manager._sessions)
                self.assertFalse(directory.exists())
            finally:
                manager.close()

    def test_unconnected_browser_peer_does_not_hold_a_vm_worker_for_ten_minutes(self) -> None:
        """An ICE path which can never connect must release its shared transport."""
        class RunningWorker:
            def poll(self) -> None:
                return None

        class Bridge:
            def __init__(self, connection_state: str) -> None:
                self.connection_state = connection_state
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory(prefix="qsm-direct-negotiation-timeout.") as temporary:
            root = Path(temporary)
            for name in ("instances", "qemu-server", "sessions", "display"):
                (root / name).mkdir(mode=0o700)
            manager = DirectSessionManager(
                instance_directory=root / "instances", runtime_directory=root / "sessions",
                vm_runtime_directory=root / "display", pve_config_directory=root / "qemu-server",
                local_node=None,
            )
            connecting = Bridge("connecting")
            connected = Bridge("connected")
            timed_out_directory = root / "sessions" / "vm-321" / "timed-out"
            connected_directory = root / "sessions" / "vm-321" / "connected"
            timed_out_directory.parent.mkdir(mode=0o700)
            timed_out_directory.mkdir(mode=0o700)
            connected_directory.mkdir(mode=0o700)
            try:
                now = time.monotonic()
                manager._sessions["timed-out"] = DirectSession(
                    vmid=321, bridge=connecting, worker=RunningWorker(), directory=timed_out_directory,
                    expires_at=now + 600, negotiation_expires_at=now - 1)
                # Keep one real peer on this VM so the test isolates the
                # failed browser's cleanup from producer shutdown mechanics.
                manager._sessions["connected"] = DirectSession(
                    vmid=321, bridge=connected, worker=RunningWorker(), directory=connected_directory,
                    expires_at=now + 600, negotiation_expires_at=now - 1)
                future = asyncio.run_coroutine_threadsafe(
                    manager._watch_session("timed-out"), manager._loop)
                future.result(timeout=2)
                self.assertTrue(connecting.closed)
                self.assertFalse(connected.closed)
                self.assertNotIn("timed-out", manager._sessions)
                self.assertIn("connected", manager._sessions)
                self.assertFalse(timed_out_directory.exists())
                self.assertTrue(connected_directory.exists())
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
