#!/usr/bin/env python3
"""Static guards for the PVE API2 package integration."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = PROJECT_ROOT / "integration" / "proxmox" / "pve9" / "api"
PACKAGE_ROOT = PROJECT_ROOT / "packaging" / "debian"
TRIGGERS = PROJECT_ROOT / "integration" / "proxmox" / "pve9" / "ui" / "debian" / "triggers"


class PveApiPackagingTests(unittest.TestCase):
    def test_transport_package_stages_the_reversible_pve_api_assets(self) -> None:
        source = (PACKAGE_ROOT / "build-proxmox9-deb.sh").read_text(encoding="utf-8")
        for asset in (
            "PVE/API2/QSunshine.pm",
            "PVE/QSunshine/Compatibility.pm",
            "q-sunshine-pveproxy",
            "q-sunshine-pvedaemon",
            "q-sunshine-pvesh",
            "systemd/pveproxy.service.d/q-sunshine-api.conf",
            "systemd/pvedaemon.service.d/q-sunshine-api.conf",
        ):
            self.assertIn(asset, source)
        self.assertNotIn(
            'require_file "$ROOT_DIR/integration/proxmox/pve9/q-sunshine-verify-vnc-ticket"',
            source,
        )
        self.assertNotIn(
            'install -Dm700 "$ROOT_DIR/integration/proxmox/pve9/q-sunshine-verify-vnc-ticket"',
            source,
        )
        self.assertIn("Q_SUNSHINE_TRANSPORT_NO_APPDATA_OK", source)

    def test_service_profile_files_have_precise_dpkg_triggers(self) -> None:
        profile = (API_ROOT / "PVE" / "QSunshine" / "Compatibility.pm").read_text(
            encoding="utf-8"
        )
        triggers = TRIGGERS.read_text(encoding="utf-8")
        service_paths = (
            "/usr/bin/pveproxy",
            "/usr/bin/pvedaemon",
            "/usr/share/perl5/PVE/API2/Qemu.pm",
            "/usr/share/perl5/PVE/QemuConfig.pm",
            "/usr/share/perl5/PVE/QemuServer.pm",
            "/usr/share/perl5/PVE/RPCEnvironment.pm",
            "/usr/share/perl5/PVE/JSONSchema.pm",
            "/usr/share/perl5/PVE/Service/pveproxy.pm",
            "/usr/share/perl5/PVE/Service/pvedaemon.pm",
        )
        for path in service_paths:
            self.assertIn(path, profile)
            self.assertIn(f"interest-noawait {path}", triggers)

    def test_wrapper_checks_before_loading_the_route_and_has_stock_fallback(self) -> None:
        for launcher, stock in (
            ("q-sunshine-pveproxy", "/usr/bin/pveproxy"),
            ("q-sunshine-pvedaemon", "/usr/bin/pvedaemon"),
        ):
            source = (API_ROOT / launcher).read_text(encoding="utf-8")
            self.assertIn("PVE::QSunshine::Compatibility", source)
            self.assertIn(stock, source)
            self.assertLess(source.index("->supported("), source.index("require PVE::API2::QSunshine"))
            self.assertIn("compatibility stock fallback failed", source)

    def test_pvedaemon_initializes_the_stock_registry_before_the_extension(self) -> None:
        source = (API_ROOT / "q-sunshine-pvedaemon").read_text(encoding="utf-8")
        self.assertLess(
            source.index("require PVE::Service::pvedaemon"),
            source.index("require PVE::API2::QSunshine"),
        )

    def test_descriptor_response_normalizes_only_browser_numeric_fields(self) -> None:
        source = (API_ROOT / "PVE" / "API2" / "QSunshine.pm").read_text(encoding="utf-8")
        validation = source.index("_unavailable() if !_valid_descriptor($descriptor);")
        expiry = source.index("$descriptor->{expires_at_utc_ms} = 0 + $descriptor->{expires_at_utc_ms};")
        port = source.index("$descriptor->{endpoint}->{port} = 0 + $descriptor->{endpoint}->{port};")
        returned = source.index("return $descriptor;", expiry)
        self.assertLess(validation, expiry)
        self.assertLess(validation, port)
        self.assertLess(expiry, returned)
        self.assertLess(port, returned)
        self.assertNotIn("$descriptor->{claim} = 0 +", source)
        self.assertNotIn("$descriptor->{endpoint}->{host} = 0 +", source)

    def test_scripts_have_parseable_static_syntax(self) -> None:
        checks = (
            ("bash", "-n", PACKAGE_ROOT / "build-proxmox9-deb.sh"),
            ("sh", "-n", PACKAGE_ROOT / "postinst"),
            ("sh", "-n", PACKAGE_ROOT / "prerm"),
            ("perl", "-T", "-c", API_ROOT / "q-sunshine-pveproxy"),
            ("perl", "-T", "-c", API_ROOT / "q-sunshine-pvedaemon"),
            ("perl", "-T", "-c", API_ROOT / "q-sunshine-pvesh"),
            (
                "perl",
                "-T",
                f"-I{API_ROOT}",
                "-c",
                API_ROOT / "PVE" / "QSunshine" / "Compatibility.pm",
            ),
        )
        for command in checks:
            subprocess.run([str(item) for item in command], check=True, capture_output=True, text=True)

    def test_direct_package_validates_its_pve9_ffmpeg_abi(self) -> None:
        source = (PACKAGE_ROOT / "build-qsm-pve-direct-deb.sh").read_text(encoding="utf-8")
        for library in ("libavcodec.so.61", "libavutil.so.59", "libswscale.so.8"):
            self.assertIn(library, source)
        self.assertIn('readelf --dynamic "$worker"', source)

    def test_trixie_driver_stages_the_direct_guest_channel(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "build-qsm-pve-direct-in-trixie.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("extensions/direct_guest", source)
        for package in ("libavcodec-dev", "libavutil-dev", "libswscale-dev"):
            self.assertIn(package, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
