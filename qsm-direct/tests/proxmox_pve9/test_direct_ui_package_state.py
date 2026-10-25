"""PVE UI stays available during an ordinary multi-package upgrade."""
import subprocess
import tempfile
import hashlib
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "integration/proxmox/pve9/direct_ui"))
from qsm_pve_direct_ui import (_default_package_version, UnsupportedPve, UiIntegration,
    Paths, SandboxDiversions, UnsafeUiState, MAX_OVERLAY_BYTES, MAX_TEMPLATE_BYTES, _read_regular)


class PackageStateTests(unittest.TestCase):
    def test_configured_package_with_pending_triggers_is_usable(self):
        for state in ("installed", "triggers-pending", "triggers-awaited"):
            with self.subTest(state=state), patch("qsm_pve_direct_ui.subprocess.run", return_value=
                    subprocess.CompletedProcess([], 0, state + "\t9.2.21\n")):
                self.assertEqual(_default_package_version(), "9.2.21")

    def test_unconfigured_or_malformed_packages_remain_unsupported(self):
        for value in ("half-configured\t9.2.21", "half-installed\t9.2.21",
                      "config-files\t9.2.21", "not-installed\t9.2.21", "installed", "installed\t../bad"):
            with self.subTest(value=value), patch("qsm_pve_direct_ui.subprocess.run", return_value=
                    subprocess.CompletedProcess([], 0, value)):
                with self.assertRaises(UnsupportedPve):
                    _default_package_version()


class OverlayAssetTests(unittest.TestCase):
    def test_shipped_viewer_is_accepted_independently_of_template_limit(self):
        source = Path(__file__).resolve().parents[2] / "integration/proxmox/pve9/direct_ui/qsm-direct-console.js"
        with tempfile.TemporaryDirectory() as root:
            paths = Paths(Path(root)); paths.overlay.parent.mkdir(parents=True)
            shutil.copyfile(source, paths.overlay)
            integration = UiIntegration(paths, SandboxDiversions(Path(root) / "diversion-state"), require_root_owner=False)
            self.assertEqual(integration._check_overlay_asset(), hashlib.sha256(source.read_bytes()).hexdigest())
            paths.template.parent.mkdir(parents=True, exist_ok=True)
            paths.template.write_bytes(b"x" * (MAX_TEMPLATE_BYTES + 1))
            with self.assertRaises(UnsafeUiState):
                _read_regular(paths.template, maximum=MAX_TEMPLATE_BYTES, require_root_owner=False)

    def test_overlay_still_rejects_oversize_symlink_and_writable_files(self):
        with tempfile.TemporaryDirectory() as root:
            paths = Paths(Path(root)); paths.overlay.parent.mkdir(parents=True)
            integration = UiIntegration(paths, SandboxDiversions(Path(root) / "diversion-state"), require_root_owner=False)
            paths.overlay.write_bytes(b"x" * (MAX_OVERLAY_BYTES + 1))
            with self.assertRaises(UnsafeUiState): integration._check_overlay_asset()
            paths.overlay.unlink(); target = Path(root) / "target"; target.write_bytes(b"/*\n * qsm-pve-direct Console menu integration */")
            paths.overlay.symlink_to(target)
            with self.assertRaises(UnsafeUiState): integration._check_overlay_asset()
            paths.overlay.unlink(); shutil.copyfile(target, paths.overlay); paths.overlay.chmod(0o666)
            with self.assertRaises(UnsafeUiState): integration._check_overlay_asset()


if __name__ == "__main__":
    unittest.main()
