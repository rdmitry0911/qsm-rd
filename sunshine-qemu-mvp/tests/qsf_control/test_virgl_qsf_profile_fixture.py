#!/usr/bin/env python3
"""Keep the native VirGL negotiated-profile scanout gate structurally strict.

This is intentionally a fast companion to the expensive KVM/VirGL runner. It
does not pretend to render a frame; it makes regressions in the fixture's
ordering visible in normal CTest runs, while the retained native trace proves
the real Weston and Display1 evidence.
"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "virgl-qsf-wayland-cloud-init-user-data.yaml.in"
RUNNER = ROOT / "scripts" / "run-virgl-qsf-wayland-clipboard-e2e.sh"
QT_HOOK = ROOT / "scripts" / "run-qsunshine-qt-moonlight-virgl-qsf-wayland-hook.sh"


class VirglQsfProfileFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = FIXTURE.read_text(encoding="utf-8")
        cls.runner = RUNNER.read_text(encoding="utf-8")
        cls.qt_hook = QT_HOOK.read_text(encoding="utf-8")

    def test_guest_ack_is_generation_bound_and_published_after_scanout_proof(self) -> None:
        for fragment in (
            "QSUNSHINE_QSF_GUEST_REQUIRE_PROFILE_APPLY_ACK=1",
            'test "$(sed -n \'$=\' "$connection_profile_file")" = 6',
            'printf \'version=2\\n\'',
            'printf \'generation=%s\\n\' "$connection_profile_generation"',
            "wayland_current_mode_matches()",
            "timeout 2 wayland-info",
            "/flags:.*current/",
            "start_weston_for_connection_profile",
            "libseat/seatd releases its active DRM client asynchronously",
            "connection_profile_ack_tmp=$state/.connection-profile-applied.$$",
            'mv -f "$connection_profile_ack_tmp" "$state/connection-profile-applied"',
            "QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_APPLIED=version=2,generation=",
            "Keep this fixture adapter alive for both",
            'cmp -s "$connection_profile_file" "$state/connection-profile-applied"',
            "fullscreen transaction too; the compositor adapter and this",
            "observed_profile_generation=$connection_profile_generation",
        ):
            self.assertIn(fragment, self.fixture)

        publish = self.fixture.index('mv -f "$connection_profile_ack_tmp" "$state/connection-profile-applied"')
        marker = self.fixture.index("QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_APPLIED=version=2,generation=")
        self.assertLess(publish, marker)
        self.assertLess(
            self.fixture.index("wayland_current_mode_matches"),
            publish,
            "the adapter must establish a current Weston mode before publishing the ACK",
        )

    def test_native_runner_uses_negotiation_and_retains_both_ack_observers(self) -> None:
        self.assertIn(
            "qsf_client optimize-connection --resolution 1280x720 --max-fps 60 --decoder-codecs H264",
            self.runner,
        )
        self.assertIn('"guest_profile_generation"', self.runner)
        self.assertIn("QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_APPLIED=version=2", self.runner)
        self.assertIn("QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_ACK_OBSERVED=generation=", self.runner)
        self.assertNotIn('qsf_client resize 1280 720', self.runner)

    def test_qt_hook_waits_for_guest_scanout_ack_before_reconnect(self) -> None:
        self.assertIn("wait_for_guest_pattern()", self.qt_hook)
        profile_ack = self.qt_hook.index("QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_APPLIED=version=2")
        observed_ack = self.qt_hook.index("QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_ACK_OBSERVED=generation=")
        fullscreen_selection = self.qt_hook.index("write_phase_request activate-fullscreen-profile")
        fullscreen_ack = self.qt_hook.index("canonical fullscreen profile after actual Weston/VirGL scanout")
        fullscreen_reconnect = self.qt_hook.index("wait_for_phase fullscreen-process-started")
        self.assertLess(profile_ack, observed_ack)
        self.assertLess(observed_ack, fullscreen_selection)
        self.assertLess(fullscreen_selection, fullscreen_ack)
        self.assertLess(fullscreen_ack, fullscreen_reconnect)
        self.assertIn("QSUNSHINE_QT_QSF_GUEST_SCANOUT_ACK=observed", self.qt_hook)
        self.assertIn("QSUNSHINE_QT_QSF_FULLSCREEN_GUEST_SCANOUT_ACK=observed", self.qt_hook)


if __name__ == "__main__":
    unittest.main()
