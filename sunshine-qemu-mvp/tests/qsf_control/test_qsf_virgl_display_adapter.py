#!/usr/bin/env python3
"""Verify the guest-local VirGL profile acknowledgement adapter."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "guest" / "qsf_virgl_display_adapter.sh"


def canonical_profile(generation: int, width: int = 2560, height: int = 1440,
                      fps: int = 60, bitrate_kbps: int = 28000,
                      codec: str = "H264") -> str:
    return (
        "version=2\n"
        f"generation={generation}\n"
        f"resolution={width}x{height}\n"
        f"fps={fps}\n"
        f"bitrate_kbps={bitrate_kbps}\n"
        f"video_codec={codec}\n"
    )


class VirglDisplayAdapterTest(unittest.TestCase):
    def test_publishes_only_the_exact_profile_after_trusted_apply_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsf-virgl-display-adapter-") as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            profile = canonical_profile(91)
            (state / "connection-profile").write_text(profile, encoding="ascii")
            helper = Path(temporary) / "apply-profile"
            observed = Path(temporary) / "apply-observed"
            helper.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "test \"$1\" = 2560\n"
                "test \"$2\" = 1440\n"
                "test \"$3\" = 60\n"
                f"cp \"$4\" {observed}\n",
                encoding="ascii",
            )
            helper.chmod(0o700)
            process = subprocess.Popen(
                ["/bin/sh", str(ADAPTER), "--state-dir", str(state),
                 "--apply-command", str(helper), "--poll-seconds", "1", "--once"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            deadline = time.monotonic() + 8
            while not (state / "connection-profile-applied").exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=1)
                    self.fail(f"adapter exited early: stdout={stdout} stderr={stderr}")
                time.sleep(0.05)
            self.assertTrue((state / "connection-profile-applied").is_file())
            _, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(observed.read_text(encoding="ascii"), profile)
            self.assertEqual((state / "connection-profile-applied").read_text(encoding="ascii"),
                             profile)

    def test_rejects_noncanonical_profile_without_invoking_apply_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qsf-virgl-display-adapter-") as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            # Leading zero makes the field noncanonical even though a shell's
            # arithmetic parser could otherwise accept it.
            (state / "connection-profile").write_text(
                canonical_profile(7).replace("resolution=2560", "resolution=02560"),
                encoding="ascii",
            )
            helper = Path(temporary) / "must-not-run"
            marker = Path(temporary) / "unexpected"
            helper.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="ascii")
            helper.chmod(0o700)
            process = subprocess.Popen(
                ["/bin/sh", str(ADAPTER), "--state-dir", str(state),
                 "--apply-command", str(helper), "--poll-seconds", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(1.3)
            process.terminate()
            process.communicate(timeout=3)
            self.assertFalse(marker.exists())
            self.assertFalse((state / "connection-profile-applied").exists())

    def test_keeps_previous_ack_until_new_generation_is_atomically_published(self) -> None:
        """A slow compositor apply must not create an acknowledgement gap."""
        with tempfile.TemporaryDirectory(prefix="qsf-virgl-display-adapter-") as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            first_profile = canonical_profile(91)
            second_profile = canonical_profile(92, width=1920, height=1080,
                                               bitrate_kbps=12000)
            (state / "connection-profile").write_text(first_profile, encoding="ascii")
            # This mirrors an adapter which had already successfully applied
            # the previous generation before it observed a replacement.
            (state / "connection-profile-applied").write_text(first_profile, encoding="ascii")
            first_complete = Path(temporary) / "first-complete"
            second_started = Path(temporary) / "second-started"
            release_second = Path(temporary) / "release-second"
            helper = Path(temporary) / "blocking-apply-profile"
            helper.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "generation=$(sed -n '2s/^generation=//p' \"$4\")\n"
                f"if [ \"$generation\" = 91 ]; then : > {shlex.quote(str(first_complete))}; exit 0; fi\n"
                "test \"$generation\" = 92\n"
                f": > {shlex.quote(str(second_started))}\n"
                f"while [ ! -f {shlex.quote(str(release_second))} ]; do sleep 0.02; done\n",
                encoding="ascii",
            )
            helper.chmod(0o700)
            process = subprocess.Popen(
                ["/bin/sh", str(ADAPTER), "--state-dir", str(state),
                 "--apply-command", str(helper), "--poll-seconds", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.monotonic() + 8
                while (not first_complete.exists() or
                       (state / "connection-profile-applied").read_text(encoding="ascii") != first_profile):
                    self.assertIsNone(process.poll(), "adapter exited during initial apply")
                    self.assertLess(time.monotonic(), deadline, "initial generation was not applied")
                    time.sleep(0.03)

                replacement = state / ".connection-profile-replacement"
                replacement.write_text(second_profile, encoding="ascii")
                replacement.replace(state / "connection-profile")
                deadline = time.monotonic() + 8
                while not second_started.exists():
                    self.assertIsNone(process.poll(), "adapter exited during blocked replacement")
                    self.assertLess(time.monotonic(), deadline, "second apply command did not start")
                    time.sleep(0.03)

                # The old record is still a correct ACK for generation 91
                # until the helper really proves the new 1920x1080 scanout.
                self.assertEqual((state / "connection-profile-applied").read_text(encoding="ascii"),
                                 first_profile)
                release_second.touch()
                deadline = time.monotonic() + 8
                while (state / "connection-profile-applied").read_text(encoding="ascii") != second_profile:
                    self.assertIsNone(process.poll(), "adapter exited before publishing replacement")
                    self.assertLess(time.monotonic(), deadline, "new ACK was not published")
                    time.sleep(0.03)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
