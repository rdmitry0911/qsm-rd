#!/usr/bin/env python3
"""Regression guards for the PVE-owned Q-Sunshine transport profile."""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUNSHINE_ROOT = PROJECT_ROOT / ".upstream" / "Sunshine"
NVHTTP = SUNSHINE_ROOT / "src" / "nvhttp.cpp"
PROCESS_TRANSPORT = SUNSHINE_ROOT / "src" / "process_transport.cpp"
PROCESS_HEADER = SUNSHINE_ROOT / "src" / "process.h"
TRANSPORT_TARGETS = SUNSHINE_ROOT / "cmake" / "compile_definitions" / "common.cmake"
PACKAGE_BUILD = PROJECT_ROOT / "packaging" / "debian" / "build-proxmox9-deb.sh"
PACKAGE_WRAPPER = PROJECT_ROOT / "packaging" / "debian" / "q-sunshine"

RAW_GAMESTREAM_ROUTES = {
    "^/serverinfo$",
    "^/applist$",
    "^/appasset$",
    "^/launch$",
    "^/resume$",
    "^/cancel$",
}


def transport_visible_lines(source: str) -> list[str]:
    """Return source lines selected when ``QSUNSHINE_TRANSPORT_ONLY`` is true.

    This deliberately handles only conditionals mentioning the profile macro.
    Other platform conditionals retain their parent state because they cannot
    make a legacy HTTP listener reachable in the transport profile.
    """

    active = True
    stack: list[tuple[bool, bool | None]] = []
    output: list[str] = []
    directive = re.compile(r"^\s*#\s*(ifdef|ifndef|if|elif|else|endif)\b(.*)$")

    def condition(kind: str, expression: str) -> bool | None:
        compact = re.sub(r"\s+", "", expression)
        if kind == "ifdef" and compact == "QSUNSHINE_TRANSPORT_ONLY":
            return True
        if kind == "ifndef" and compact == "QSUNSHINE_TRANSPORT_ONLY":
            return False
        if kind in {"if", "elif"}:
            if compact in {"defined(QSUNSHINE_TRANSPORT_ONLY)", "QSUNSHINE_TRANSPORT_ONLY"}:
                return True
            if compact in {"!defined(QSUNSHINE_TRANSPORT_ONLY)", "!QSUNSHINE_TRANSPORT_ONLY"}:
                return False
        return None

    for line in source.splitlines():
        match = directive.match(line)
        if not match:
            if active:
                output.append(line)
            continue

        kind, expression = match.groups()
        if kind in {"ifdef", "ifndef", "if"}:
            parent = active
            selected = condition(kind, expression)
            stack.append((parent, selected))
            active = parent and (selected is not False)
        elif kind == "else":
            parent, selected = stack[-1]
            active = parent and (selected is not True)
        elif kind == "elif":
            parent, selected = stack[-1]
            alternate = condition(kind, expression)
            active = parent and (selected is None or alternate is not False)
        else:
            active, _ = stack.pop()

    if stack:
        raise AssertionError("unterminated preprocessor conditional in nvhttp.cpp")
    return output


class SunshineTransportProfileTests(unittest.TestCase):
    def test_transport_source_has_only_the_six_mtls_gamestream_routes(self) -> None:
        visible = "\n".join(transport_visible_lines(NVHTTP.read_text(encoding="utf-8")))
        routes = set(re.findall(r'https_server\.resource\["(\^/[^" ]+\$)"\]\["GET"\]', visible))
        self.assertSetEqual(routes, RAW_GAMESTREAM_ROUTES)
        self.assertIn("https_server_t https_server", visible)
        self.assertIn("https_server.enable_system_auth_lease_mode", visible)
        for forbidden_plaintext_surface in (
            "http_server_t",
            "http_server.",
            "SimpleWeb::HTTP",
            '"^/pair$"',
        ):
            self.assertNotIn(forbidden_plaintext_surface, visible)
        self.assertNotRegex(visible, r"\bport_http\b")

    def test_fixed_console_state_has_no_manifest_or_host_executor(self) -> None:
        header = PROCESS_HEADER.read_text(encoding="utf-8")
        state = PROCESS_TRANSPORT.read_text(encoding="utf-8")
        targets = TRANSPORT_TARGETS.read_text(encoding="utf-8")

        self.assertIn("constexpr int qemu_console_app_id = 1255037302", header)
        self.assertIn('"QEMU Console"', state)
        self.assertIn("std::atomic_int _app_id", header)
        self.assertIn("compare_exchange_strong", state)
        self.assertIn("process_transport.cpp", targets)
        for excluded_source in (
            "display_device.cpp",
            "process.cpp",
            "system_tray.cpp",
            "upnp.cpp",
            "confighttp.cpp",
        ):
            self.assertIn(excluded_source, targets)
        for forbidden_host_control in (
            "apps.json",
            "boost::process",
            "platf::run_command",
            "platf::open_url",
            "file_handler::",
            "global_prep_cmd",
            "_process_group",
        ):
            self.assertNotIn(forbidden_host_control, state)

    def test_profile_forces_a_qemu_only_capture_graph(self) -> None:
        options = (
            SUNSHINE_ROOT / "cmake" / "prep" / "options.cmake"
        ).read_text(encoding="utf-8")
        for forced_off in (
            "SUNSHINE_ENABLE_CUDA OFF",
            "SUNSHINE_ENABLE_DRM OFF",
            "SUNSHINE_ENABLE_VAAPI OFF",
            "SUNSHINE_ENABLE_VULKAN OFF",
            "SUNSHINE_ENABLE_WAYLAND OFF",
            "SUNSHINE_ENABLE_X11 OFF",
            "SUNSHINE_ENABLE_KWIN OFF",
            "SUNSHINE_ENABLE_PORTAL OFF",
        ):
            self.assertIn(forced_off, options)
        for forced_on in (
            "SUNSHINE_ENABLE_QEMU_DBUS ON",
            "SUNSHINE_ENABLE_QEMU_DBUS_DMABUF ON",
            "SUNSHINE_ENABLE_QEMU_DBUS_AUDIO_ONLY ON",
        ):
            self.assertIn(forced_on, options)
        self.assertGreaterEqual(options.count(" FORCE)"), 10)

    def test_package_builder_enforces_the_transport_boundary(self) -> None:
        source = PACKAGE_BUILD.read_text(encoding="utf-8")
        wrapper = PACKAGE_WRAPPER.read_text(encoding="utf-8")
        self.assertIn("-DQSUNSHINE_TRANSPORT_ONLY=ON", source)
        self.assertIn("staged_loader", source)
        self.assertIn("grep -Fq '/usr/lib/q-sunshine/assets'", source)
        self.assertIn("for forbidden_transport_source in process.cpp display_device.cpp system_tray.cpp confighttp.cpp", source)
        self.assertIn('[[ ! -e "$package_root/assets/apps.json" ]]', source)
        for forbidden_binary_string in (
            "apps.json",
            "global_prep_cmd",
            "file_apps",
            "system_tray",
            "run_command",
            "open_url",
            "'^/pair$'",
            "'^/pin$'",
            "transport_route_count",
        ):
            self.assertIn(forbidden_binary_string, source)
        # Package diagnostics must not need a fabricated per-VM lease, but the
        # bypass must be limited to the two read-only Sunshine commands.
        self.assertIn('[ "$#" -eq 1 ]', wrapper)
        self.assertIn('"--version"', wrapper)
        self.assertIn('"--help"', wrapper)
        self.assertIn("complete native GameStream server material", wrapper)

    def test_requested_transport_build_graph_has_no_legacy_sources(self) -> None:
        build_directory = os.environ.get("QSUNSHINE_TRANSPORT_BUILD_DIR")
        if not build_directory:
            self.skipTest("set QSUNSHINE_TRANSPORT_BUILD_DIR to inspect a configured transport target")

        commands = subprocess.run(
            ["ninja", "-C", build_directory, "-t", "commands", "sunshine"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("/src/process_transport.cpp", commands)
        for forbidden_source in (
            "/src/process.cpp",
            "/src/display_device.cpp",
            "/src/system_tray.cpp",
            "/src/confighttp.cpp",
            "/src/upnp.cpp",
            "/src/platform/linux/publish.cpp",
        ):
            self.assertNotIn(forbidden_source, commands)

    def test_requested_transport_binary_has_only_raw_route_strings(self) -> None:
        binary = os.environ.get("QSUNSHINE_TRANSPORT_BINARY")
        if not binary:
            self.skipTest("set QSUNSHINE_TRANSPORT_BINARY to inspect a built transport artifact")

        strings = subprocess.run(
            ["strings", binary], check=True, capture_output=True, text=True
        ).stdout.splitlines()
        route_strings = {
            value for value in strings if re.fullmatch(r"\^/[A-Za-z0-9_./-]+\$", value)
        }
        self.assertSetEqual(route_strings, RAW_GAMESTREAM_ROUTES)
        self.assertIn("QEMU Console", "\n".join(strings))
        for forbidden_binary_string in (
            "^/pair$",
            "^/pin$",
            "apps.json",
            "global_prep_cmd",
            "file_apps",
            "system_tray",
            "run_command",
            "open_url",
        ):
            self.assertNotIn(forbidden_binary_string, strings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
