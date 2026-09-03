#!/usr/bin/env python3
"""Focused tests for the non-invasive PVE 9 Console-menu integration."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_DIRECTORY = PROJECT_ROOT / "integration" / "proxmox" / "pve9" / "ui"
sys.path.insert(0, str(UI_DIRECTORY))

ui = importlib.import_module("q_sunshine_pve9_ui")


SAMPLE_TEMPLATE = b"""<!DOCTYPE html>
<html><head>
    <script type=\"text/javascript\" src=\"/pve2/js/pvemanagerlib.js?ver=[% version %]\"></script>
    <script type=\"text/javascript\" src=\"/pve2/ext6/locale/locale-[% lang %].js?ver=7.0.0\"></script>
</head></html>
"""


class Pve9UiTests(unittest.TestCase):
    def write_file(self, path: Path, content: bytes | str, mode: int = 0o644) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        path.write_bytes(content)
        os.chmod(path, mode)

    def make_integration(self, root: Path, version: str = "9.test") -> ui.UiIntegration:
        paths = ui.Paths(root)
        self.write_file(
            paths.manifest,
            f"{version}\t{ui._sha256(SAMPLE_TEMPLATE)}\n",
        )
        self.write_file(paths.template, SAMPLE_TEMPLATE)
        self.write_file(paths.overlay, (UI_DIRECTORY / "q-sunshine-console.js").read_bytes())
        return ui.UiIntegration(
            paths,
            ui.SandboxDiversions(root / "var/lib/q-sunshine/pve9-ui/test-diversion-owner"),
            package_version=lambda: version,
            require_root_owner=False,
        )

    def test_overlay_uses_same_origin_protected_q_sunshine_route(self) -> None:
        source_path = UI_DIRECTORY / "q-sunshine-console.js"
        source = source_path.read_text(encoding="utf-8")
        subprocess.run(["node", "--check", str(source_path)], check=True)
        self.assertIn("url: `/nodes/", source)
        self.assertIn("/q-sunshine`,", source)
        self.assertIn("method: 'POST'", source)
        self.assertIn("/api2/extjs", source)
        self.assertNotIn("url: `/api2/json", source)
        self.assertNotIn("vncproxy", source)
        self.assertNotIn("PVEVNC", source)
        self.assertNotIn("/pve/v1/launch", source)
        self.assertNotIn("QSunshinePveNodeEndpoints", source)
        self.assertNotIn("fetch(", source)
        self.assertNotIn("location.hostname", source)
        self.assertNotIn("console.log", source)
        self.assertIn("q-sunshine-pve-launch", source)

    def test_patched_template_preserves_stock_library_before_overlay(self) -> None:
        patched = ui.render_patched_template(SAMPLE_TEMPLATE)
        self.assertEqual(patched.count(ui.MARKER), 1)
        self.assertLess(
            patched.index(b"pvemanagerlib.js"),
            patched.index(b"q-sunshine-console.js"),
        )
        with self.assertRaises(ui.UnsupportedPve):
            ui.render_patched_template(patched)

    def test_sandbox_install_reconcile_and_remove_restore_the_exact_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            integration = self.make_integration(root)
            paths = integration.paths

            self.write_file(
                paths.legacy_config_asset,
                b"/* q-sunshine generated endpoint map; public metadata only. */\n"
                b"window.QSunshinePveNodeEndpoints = Object.freeze({});\n",
            )

            integration.install()
            self.assertEqual(integration.status(), "active")
            self.assertEqual(paths.diversion.read_bytes(), SAMPLE_TEMPLATE)
            patched = paths.template.read_bytes()
            self.assertIn(ui.MARKER, patched)
            self.assertIn(b"q-sunshine-console.js?ver=[% version %]-qsm1", patched)
            self.assertFalse(paths.legacy_config_asset.exists())
            self.assertEqual(integration.reconcile(), "refreshed")

            integration.remove()
            self.assertEqual(integration.status(), "inactive")
            self.assertEqual(paths.template.read_bytes(), SAMPLE_TEMPLATE)
            self.assertFalse(paths.diversion.exists())

    def test_unsupported_pve_upgrade_restores_stock_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            integration = self.make_integration(root)
            paths = integration.paths
            integration.install()

            upgraded_template = SAMPLE_TEMPLATE.replace(b"ext6", b"ext7")
            self.write_file(paths.diversion, upgraded_template)
            unsupported = ui.UiIntegration(
                paths,
                integration.diversions,
                package_version=lambda: "99.unreviewed",
                require_root_owner=False,
            )
            self.assertEqual(unsupported.reconcile(), "disabled-unsupported-pve")
            self.assertEqual(paths.template.read_bytes(), upgraded_template)
            self.assertEqual(unsupported.status(), "inactive")

    def test_failed_diversion_remove_restores_the_previous_managed_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            delegate = ui.SandboxDiversions(root / "var/lib/q-sunshine-pve9/fake-diversion-owner")

            class FailingRemove:
                def owner(self, target: Path) -> str | None:
                    return delegate.owner(target)

                def add(self, target: Path, diverted: Path) -> None:
                    delegate.add(target, diverted)

                def remove(self, target: Path, diverted: Path) -> None:
                    raise ui.UiIntegrationError("simulated dpkg-divert failure")

            integration = self.make_integration(root)
            integration.diversions = FailingRemove()
            integration.install()
            patched = integration.paths.template.read_bytes()
            with self.assertRaises(ui.UiIntegrationError):
                integration.remove()
            self.assertEqual(integration.paths.template.read_bytes(), patched)
            self.assertIn(ui.MARKER, integration.paths.template.read_bytes())
            self.assertTrue(integration.paths.diversion.exists())

    def test_manifest_is_an_allow_list_with_current_pve9_rows(self) -> None:
        supported = ui.load_supported_templates(
            UI_DIRECTORY / "pve-manager-index-template.sha256",
            require_root_owner=False,
        )
        self.assertIn("9.0.0~8", supported)
        self.assertIn("9.2.11", supported)
        self.assertEqual(len(supported["9.2.11"].sha256), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
