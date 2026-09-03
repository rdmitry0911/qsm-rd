#!/usr/bin/env python3
"""Keep the package-level `.qsm` receiver associations exact and local-file-only."""

from __future__ import annotations

import configparser
import plistlib
import unittest
from pathlib import Path


CLIENT_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = CLIENT_ROOT / "resources"
MIME = "application/x-q-sunshine-launch"
UTI = "io.qsunshine.pve-launch"


class ClientLaunchAssociationTest(unittest.TestCase):
    def test_linux_desktop_entry_invokes_the_required_receiver_option(self) -> None:
        parser = configparser.RawConfigParser()
        parser.read(RESOURCES / "qsunshine-client.desktop.in", encoding="utf-8")
        entry = parser["Desktop Entry"]
        self.assertEqual(entry["type"], "Application")
        self.assertEqual(entry["exec"], "qsunshine-client --launch-file %f")
        self.assertEqual(entry["tryexec"], "qsunshine-client")
        self.assertEqual(entry["mimetype"], MIME + ";")
        self.assertEqual(entry["terminal"], "false")

    def test_linux_mime_database_claims_only_qsm(self) -> None:
        contents = (RESOURCES / "q-sunshine-launch.xml").read_text(encoding="utf-8")
        self.assertIn(f'<mime-type type="{MIME}">', contents)
        self.assertIn('<glob pattern="*.qsm"/>', contents)

    def test_macos_bundle_declares_qsm_without_a_custom_url_scheme(self) -> None:
        plist = plistlib.loads((RESOURCES / "MacOSXBundleInfo.plist.in").read_bytes())
        self.assertNotIn("CFBundleURLTypes", plist)
        exported = plist["UTExportedTypeDeclarations"]
        self.assertEqual(exported[0]["UTTypeIdentifier"], UTI)
        tags = exported[0]["UTTypeTagSpecification"]
        self.assertEqual(tags["public.filename-extension"], ["qsm"])
        self.assertEqual(tags["public.mime-type"], [MIME])
        document = plist["CFBundleDocumentTypes"][0]
        self.assertEqual(document["CFBundleTypeRole"], "Viewer")
        self.assertEqual(document["LSHandlerRank"], "Owner")
        self.assertEqual(document["LSItemContentTypes"], [UTI])
        self.assertEqual(document["CFBundleTypeExtensions"], ["qsm"])
        self.assertEqual(document["CFBundleTypeMIMETypes"], [MIME])


if __name__ == "__main__":
    unittest.main(verbosity=2)
