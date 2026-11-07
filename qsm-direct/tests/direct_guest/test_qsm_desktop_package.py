#!/usr/bin/env python3
"""Keep the optional in-guest package installable and self-starting."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class QsmDesktopPackageTests(unittest.TestCase):
    def test_setup_enables_the_system_virtio_agent_for_the_selected_user(self) -> None:
        setup = (ROOT / "packaging/guest/qsm-desktop-agent-setup").read_text(encoding="utf-8")
        self.assertIn('install -d -m 0700 -o "$user" -g "$user"', setup)
        self.assertIn('Environment=QSM_GUEST_HOME=$user_home', setup)
        self.assertIn('ReadWritePaths=$desktop_state', setup)
        self.assertIn("systemctl daemon-reload", setup)
        self.assertIn('systemctl enable "qsm-desktop-agent@${user}.service"', setup)
        self.assertIn('systemctl start "qsm-desktop-agent@${user}.service"', setup)
        self.assertIn('qsm-desktop-clipboard.service "$clipboard_wants/qsm-desktop-clipboard.service"', setup)
        self.assertIn('mv -- "$legacy_state" "$desktop_state"', setup)
        self.assertIn('The bridge becomes ready when that user logs into a Wayland desktop.', setup)
        self.assertNotIn("loginctl terminate-user", setup)

    def test_service_uses_only_the_private_direct_virtio_port(self) -> None:
        unit = (ROOT / "packaging/guest/qsm-desktop-agent@.service").read_text(encoding="utf-8")
        self.assertIn("ConditionPathExists=/dev/virtio-ports/org.qsm.direct.agent", unit)
        self.assertIn("--device /dev/virtio-ports/org.qsm.direct.agent", unit)
        self.assertIn("Group=qsm-desktop", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("Environment=QSM_GUEST_HOME=/home/%i", unit)
        self.assertIn("--state-dir ${QSM_GUEST_HOME}/.local/share/qsm-desktop-agent", unit)
        self.assertIn("ReadWritePaths=/home/%i/.local/share/qsm-desktop-agent", unit)
        self.assertNotIn("%h/.local/share/qsm-desktop-agent", unit)

    def test_wayland_bridge_waits_for_the_logged_in_compositor(self) -> None:
        bridge = (ROOT / "guest/qsf_wayland_clipboard_bridge.sh").read_text(encoding="utf-8")
        # A user manager may start through SSH before Plasma creates any
        # Wayland socket.  This must wait, not exhaust systemd Restart= and
        # leave a stale ready marker that claims clipboard support.
        self.assertIn("wait_for_graphical_session()", bridge)
        self.assertIn('for socket in "$runtime_dir"/wayland-[0-9]*', bridge)
        self.assertIn('export WAYLAND_DISPLAY="$wayland_socket"', bridge)
        self.assertNotIn('[ -S "$runtime_dir/wayland-0" ] || fail wayland_socket_missing', bridge)
        self.assertIn('rm -f "$ready_file" "$candidate" "$validated"', bridge)
        self.assertIn('clipboard_applied="$state_dir/qsf-clipboard-applied"', bridge)
        self.assertIn('publish_clipboard_applied', bridge)
        self.assertIn('QSF_WAYLAND_BRIDGE_CLIPBOARD_APPLIED=', bridge)

    def test_plasma_clipboard_is_event_driven_not_a_wayland_poll_storm(self) -> None:
        bridge = (ROOT / "guest/qsf_wayland_clipboard_bridge.sh").read_text(encoding="utf-8")
        # KWin makes each wl-paste connection observable.  Do not turn an
        # idle Direct Console into a stream of clipboard offers/repaints.
        self.assertIn('busctl --user monitor org.kde.klipper', bridge)
        self.assertIn('clipboardHistoryUpdated', bridge)
        self.assertIn("QSF_WAYLAND_BRIDGE_NATIVE_WATCHER=kde_dbus", bridge)
        self.assertIn('for candidate in qdbus6 qdbus-qt6 qdbus', bridge)
        self.assertIn('qdbus_binary=$(command -v "$candidate")', bridge)
        self.assertIn("while IFS= read -r bridge_event <\"$event_pipe\"", bridge)
        self.assertIn("native) synchronise_native_clipboard", bridge)
        self.assertNotIn('if wl-paste --no-newline --type', bridge)
        self.assertIn('qsm-desktop-state-watcher', bridge)
        self.assertIn('start_qsf_state_watcher', bridge)
        self.assertIn('state_poll_interval=0.10', bridge)
        self.assertIn('fallback_probe_ticks=20', bridge)
        self.assertIn('backend_retry_ticks=50', bridge)
        self.assertIn("QSF_WAYLAND_BRIDGE_BACKEND_UPGRADE=kde_dbus", bridge)
        self.assertIn('export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$runtime_dir/bus}"', bridge)
        self.assertIn("wl-copy --foreground --type 'text/plain;charset=utf-8'", bridge)
        self.assertIn('wl_copy_pid=$!', bridge)
        self.assertIn("timeout --foreground 1s wl-paste", bridge)
        self.assertIn('requested_generation=$(clipboard_file_generation "$clipboard_generation"', bridge)

    def test_clipboard_service_has_a_deterministic_session_bus(self) -> None:
        unit = (ROOT / "packaging/guest/qsm-desktop-clipboard.service").read_text(encoding="utf-8")
        self.assertIn("Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus", unit)

    def test_package_builds_the_event_driven_state_watcher(self) -> None:
        build = (ROOT / "packaging/guest/build-qsm-desktop-agent-deb.sh").read_text(encoding="utf-8")
        self.assertIn('guest/qsf_state_watcher.c', build)
        self.assertIn('qsm-desktop-state-watcher', build)
        self.assertIn('Package: qsm-desktop-agent', build)
        self.assertIn('-DQSM_DESKTOP_CLIPBOARD_ONLY=1', build)
        self.assertIn('Conflicts: qsm-guest-agent', build)
        watcher = (ROOT / "guest/qsf_state_watcher.c").read_text(encoding="utf-8")
        self.assertIn('inotify_init1(IN_CLOEXEC)', watcher)
        self.assertIn('IN_MOVED_TO', watcher)

    def test_package_installs_the_file_helper_and_its_unit(self) -> None:
        build = (ROOT / "packaging/guest/build-qsm-desktop-agent-deb.sh").read_text(encoding="utf-8")
        self.assertIn('"$ROOT_DIR/guest/qsm_desktop_files.py" "$package_root/qsm_desktop_files.py"', build)
        self.assertIn('packaging/guest/qsm-desktop-files.service" "$stage_root/usr/lib/systemd/user/qsm-desktop-files.service"', build)

    def test_file_helper_unit_runs_in_the_session_with_normal_file_modes(self) -> None:
        unit = (ROOT / "packaging/guest/qsm-desktop-files.service").read_text(encoding="utf-8")
        self.assertIn("PartOf=graphical-session.target", unit)
        self.assertIn("WantedBy=graphical-session.target", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertNotIn("UMask", unit)
        self.assertIn("/usr/lib/qsm-desktop-agent/qsm_desktop_files.py --listen %h/.local/share/qsm-desktop-agent/files.sock", unit)

    def test_readme_is_honest_about_the_file_helper(self) -> None:
        readme = (ROOT / "packaging/guest/README.qsm-desktop-agent").read_text(encoding="utf-8")
        self.assertNotIn("never exposes", readme.replace("\n", " "))
        self.assertIn("qsm-desktop-files.service", readme)
        self.assertIn("--no-files", readme)

    def test_package_upgrades_do_not_change_the_file_helper_choice(self) -> None:
        build = (ROOT / "packaging/guest/build-qsm-desktop-agent-deb.sh").read_text(encoding="utf-8")
        postinst = build[build.index('cat > "$stage_root/DEBIAN/postinst"'):build.index('chmod 0755 "$stage_root/DEBIAN/postinst"')]
        calls = [line.strip() for line in postinst.splitlines() if "/usr/bin/qsm-desktop-agent-setup" in line]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIn("--keep-files", call)
        self.assertNotIn("qsm-desktop-files", postinst)

    def run_setup(self, home: Path, *arguments: str) -> subprocess.CompletedProcess:
        """The real script, with its privileged commands and /etc path replaced."""
        base = home.parent
        (base / "etc").mkdir(exist_ok=True)
        tools = base / "tools"
        tools.mkdir(exist_ok=True)
        stubs = {
            "id": "exit 0",
            "usermod": "exit 0",
            "chown": "exit 0",
            "systemctl": "exit 0",
            "getent": f'echo "dima:x:1000:1000::{home}:/bin/sh"',
            # Only -d is used: create the last argument, ignoring owner and mode.
            "install": 'for last; do :; done; exec mkdir -p "$last"',
        }
        for name, body in stubs.items():
            (tools / name).write_text(f"#!/bin/sh\n{body}\n")
            os.chmod(tools / name, 0o755)
        script = (ROOT / "packaging/guest/qsm-desktop-agent-setup").read_text(encoding="utf-8")
        copy = base / "setup"
        copy.write_text(script.replace("/etc/systemd/system", str(base / "etc")))
        return subprocess.run(["/bin/sh", str(copy), *arguments], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"})

    def test_setup_enables_removes_and_keeps_the_file_helper_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            wants = home / ".config/systemd/user/graphical-session.target.wants"
            link = wants / "qsm-desktop-files.service"
            metadata = wants / "qsm-desktop-metadata.service"

            # A plain upgrade of a configured user must not turn the helper on.
            self.assertEqual(self.run_setup(home, "--keep-files", "dima").returncode, 0)
            self.assertTrue(metadata.is_symlink())
            self.assertFalse(link.is_symlink())

            self.assertEqual(self.run_setup(home, "dima").returncode, 0)
            self.assertEqual(os.readlink(link), "/usr/lib/systemd/user/qsm-desktop-files.service")

            # Nor change it once an administrator has turned it on...
            self.assertEqual(self.run_setup(home, "--keep-files", "dima").returncode, 0)
            self.assertTrue(link.is_symlink())

            self.assertEqual(self.run_setup(home, "--no-files", "dima").returncode, 0)
            self.assertFalse(link.is_symlink())
            self.assertTrue(metadata.is_symlink())
            self.assertEqual(self.run_setup(home, "--no-files", "dima").returncode, 0)

            # ... or off.
            self.assertEqual(self.run_setup(home, "--keep-files", "dima").returncode, 0)
            self.assertFalse(link.is_symlink())

    def test_setup_rejects_bad_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            for arguments in ((), ("--no-files",), ("--keep-files",), ("--nonsense", "dima"), ("--no-files", "a b"),
                              ("--no-files", "--keep-files"), ("--no-files", "dima", "extra"), ("dima", "--no-files")):
                with self.subTest(arguments=arguments):
                    self.assertEqual(self.run_setup(home, *arguments).returncode, 2)
            self.assertFalse((home / ".config").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
