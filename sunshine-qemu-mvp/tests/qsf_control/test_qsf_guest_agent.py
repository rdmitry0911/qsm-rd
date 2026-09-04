#!/usr/bin/env python3
"""Exercise the production C guest endpoint over a real serial-like PTY.

This is deliberately below QEMU: it proves framing, binary file payloads and
guest-originated clipboard notifications against the compiled agent before the
larger VM gate adds virtio-serial and a desktop image to the equation.
"""

from __future__ import annotations

import base64
import os
import pty
import select
import subprocess
import tempfile
import time
import tty
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "guest" / "qsf_guest_agent.c"


class GuestAgentPtyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="qsf-guest-agent-test-")
        self.path = Path(self.directory.name)
        self.binary = self.path / "qsf-guest-agent"
        subprocess.run(
            ["gcc", "-std=c11", "-Wall", "-Wextra", "-Wpedantic", "-Werror", str(SOURCE), "-o", str(self.binary)],
            check=True,
        )
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.device = self.path / "virtio-port"
        os.symlink(os.ttyname(self.slave), self.device)
        self._buffer = bytearray()
        self.state = self.path / "state"
        self.process = subprocess.Popen(
            [str(self.binary), "--device", str(self.device), "--state-dir", str(self.state)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "QSUNSHINE_QSF_GUEST_MAX_WIDTH": "2560",
                "QSUNSHINE_QSF_GUEST_MAX_HEIGHT": "1440",
                "QSUNSHINE_QSF_GUEST_MAX_FPS": "60",
            },
        )
        self.assertEqual(self._line(), "READY QSF1")

    def tearDown(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        if self.process.stderr is not None:
            self.process.stderr.close()
        os.close(self.master)
        os.close(self.slave)
        self.directory.cleanup()

    def _line(self, timeout: float = 3.0) -> str:
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail("timed out waiting for the guest agent")
            readable, _, _ = select.select([self.master], [], [], remaining)
            if not readable:
                continue
            self._buffer.extend(os.read(self.master, 65536))
        line, _, trailing = self._buffer.partition(b"\n")
        self._buffer = bytearray(trailing)
        return line.decode("ascii")

    def _command(self, value: str) -> str:
        os.write(self.master, value.encode("ascii") + b"\n")
        return self._line()

    def _assert_no_line(self, timeout: float) -> None:
        if b"\n" in self._buffer:
            self.fail("guest agent unexpectedly emitted a line")
        readable, _, _ = select.select([self.master], [], [], timeout)
        if readable:
            self._buffer.extend(os.read(self.master, 65536))
            self.fail("guest agent unexpectedly emitted a line")

    def _replace_transport(self) -> None:
        """Model a QEMU socket-chardev peer going away and returning.

        The agent is deliberately pointed at a stable pathname while the
        underlying PTY changes.  Closing the old master gives its open slave
        the same HUP/EOF class that virtio-serial reports when qsf-control is
        retired; atomically retargeting the path lets the production retry
        loop attach to the replacement transport.
        """
        replacement_master, replacement_slave = pty.openpty()
        tty.setraw(replacement_slave)
        replacement_link = self.path / "virtio-port.next"
        os.symlink(os.ttyname(replacement_slave), replacement_link)
        os.replace(replacement_link, self.device)

        previous_master, previous_slave = self.master, self.slave
        self.master, self.slave = replacement_master, replacement_slave
        os.close(previous_master)
        os.close(previous_slave)

    @staticmethod
    def _wire(data: bytes) -> str:
        return "-" if not data else base64.b64encode(data).decode("ascii")

    def _write_guest_clipboard(self, data: bytes) -> None:
        temporary = self.state / "clipboard.next"
        temporary.write_bytes(data)
        os.replace(temporary, self.state / "qsf-clipboard.txt")

    def test_clipboard_files_resize_and_guest_notification(self) -> None:
        self.assertEqual(self._command("PING"), "OK PONG")

        client_clipboard = "client → guest\nПривет".encode("utf-8")
        self.assertEqual(self._command("CLIP_SET " + self._wire(client_clipboard)), "OK CLIP_SET")
        self.assertEqual((self.state / "qsf-clipboard.txt").read_bytes(), client_clipboard)
        self.assertEqual(self._command("CLIP_GET"), "CLIP " + self._wire(client_clipboard))

        guest_clipboard = "guest → client\nЗдравствуйте".encode("utf-8")
        self._write_guest_clipboard(guest_clipboard)
        self.assertEqual(self._line(timeout=4), "EVENT_CLIP " + self._wire(guest_clipboard))

        client_file = b"\x00client-file\xff\n"
        self.assertEqual(
            self._command("FILE_PUT client.bin " + self._wire(client_file)),
            "OK FILE_PUT client.bin",
        )
        self.assertEqual((self.state / "incoming" / "client.bin").read_bytes(), client_file)

        guest_file = b"guest-file\x00\xff"
        (self.state / "outgoing" / "guest.bin").write_bytes(guest_file)
        self.assertEqual(
            self._command("FILE_GET guest.bin"),
            "FILE guest.bin " + self._wire(guest_file),
        )
        self.assertEqual(
            self._command("FILE_LIST incoming"),
            "FILES incoming " + self._wire(f"client.bin\t{len(client_file)}\n".encode("ascii")),
        )
        self.assertEqual(
            self._command("FILE_LIST outgoing"),
            "FILES outgoing " + self._wire(f"guest.bin\t{len(guest_file)}\n".encode("ascii")),
        )
        self.assertEqual(self._command("FILE_PUT empty.bin -"), "OK FILE_PUT empty.bin")
        self.assertEqual((self.state / "incoming" / "empty.bin").read_bytes(), b"")

        self.assertEqual(self._command("RESIZE 1920 1080"), "OK RESIZE 1920 1080")
        self.assertEqual((self.state / "resolution").read_text(encoding="ascii"), "1920x1080\n")
        self.assertEqual(self._command("FILE_PUT ../escape QQ=="), "ERR BAD_FILE_PUT")
        self.assertEqual(self._command("FILE_PUT .hidden QQ=="), "ERR BAD_FILE_PUT")
        self.assertEqual(self._command("FILE_LIST home"), "ERR BAD_FILE_LIST")
        self.assertEqual(self._command("CLIP_SET not-base64"), "ERR BAD_CLIPBOARD")

    def test_clip_set_requires_strict_utf8_and_preserves_state(self) -> None:
        original = "known-good\\nПривет".encode("utf-8")
        self.assertEqual(self._command("CLIP_SET " + self._wire(original)), "OK CLIP_SET")

        invalid = {
            "nul": b"before\x00after",
            "lone_continuation": b"\x80",
            "overlong_two_byte": b"\xc0\x80",
            "truncated_two_byte": b"\xc2",
            "bad_three_byte_continuation": b"\xe2\x28\xa1",
            "overlong_three_byte": b"\xe0\x80\x80",
            "surrogate": b"\xed\xa0\x80",
            "overlong_four_byte": b"\xf0\x80\x80\x80",
            "above_unicode_range": b"\xf4\x90\x80\x80",
            "illegal_lead": b"\xf5\x80\x80\x80",
        }
        for name, payload in invalid.items():
            with self.subTest(name=name):
                self.assertEqual(self._command("CLIP_SET " + self._wire(payload)), "ERR BAD_CLIPBOARD")
                self.assertEqual((self.state / "qsf-clipboard.txt").read_bytes(), original)
                self.assertEqual(self._command("CLIP_GET"), "CLIP " + self._wire(original))

        self._write_guest_clipboard(b"\xff")
        time.sleep(0.6)
        self._assert_no_line(0.4)
        self.assertEqual(self._command("CLIP_GET"), "ERR CLIPBOARD_NOT_UTF8")

    def test_clip_set_accepts_utf8_boundaries_and_empty(self) -> None:
        valid = {
            "empty": b"",
            "two_byte_minimum": b"\xc2\x80",
            "three_byte_minimum": b"\xe0\xa0\x80",
            "four_byte_minimum": b"\xf0\x90\x80\x80",
            "maximum_scalar": b"\xf4\x8f\xbf\xbf",
        }
        for name, payload in valid.items():
            with self.subTest(name=name):
                self.assertEqual(self._command("CLIP_SET " + self._wire(payload)), "OK CLIP_SET")
                self.assertEqual((self.state / "qsf-clipboard.txt").read_bytes(), payload)

    def test_reopens_the_virtio_transport_after_peer_disconnect(self) -> None:
        self.assertEqual(self._command("PING"), "OK PONG")
        self._replace_transport()
        self.assertEqual(self._line(timeout=4), "READY QSF1")
        self.assertIsNone(self.process.poll(), "guest agent exited after transport HUP")
        self.assertEqual(self._command("PING"), "OK PONG")

    def test_guest_applies_only_pair_configuration_within_its_display_capabilities(self) -> None:
        self.assertEqual(self._command("CONNECTION_OPTIMIZE"),
                         "GUEST_CAPABILITIES_REQUEST 2 2560 1440 60")
        accepted = self._command("PAIR_CAPABILITIES 2 2560 1440 60 30000 H264").split(" ")
        self.assertEqual(accepted[0], "CONNECTION_PROFILE_ACCEPTED")
        self.assertEqual(accepted[1], "2")
        self.assertEqual(accepted[3:], ["2560", "1440", "60", "30000", "H264"])
        generation = accepted[2]
        command_tail = f"2 {generation} 2560 1440 60 30000 H264"
        self.assertEqual(
            self._command("COMMIT_CONNECTION_PROFILE " + command_tail),
            "CONNECTION_PROFILE_PENDING " + command_tail,
        )
        self.assertEqual(
            (self.state / "connection-profile").read_text(encoding="ascii"),
            "version=2\ngeneration=" + generation + "\nresolution=2560x1440\n"
            "fps=60\nbitrate_kbps=30000\nvideo_codec=H264\n",
        )
        self.assertEqual((self.state / "resolution").read_text(encoding="ascii"), "2560x1440\n")
        # The strict default must not release a reconnect merely because the
        # profile state exists.  A desktop adapter writes the exact canonical
        # acknowledgement only after it has observed the new scanout.
        os.write(self.master, ("AWAIT_CONNECTION_PROFILE " + command_tail + "\n").encode("ascii"))
        self._assert_no_line(0.2)
        (self.state / "connection-profile-applied").write_text(
            "version=2\ngeneration=1\nresolution=2560x1440\n"
            "fps=60\nbitrate_kbps=30000\nvideo_codec=H264\n",
            encoding="ascii",
        )
        self._assert_no_line(0.2)
        expected_profile = (self.state / "connection-profile").read_text(encoding="ascii")
        temporary = self.state / "connection-profile-applied.new"
        temporary.write_text(expected_profile, encoding="ascii")
        os.replace(temporary, self.state / "connection-profile-applied")
        self.assertEqual(self._line(timeout=3), "CONNECTION_PROFILE " + command_tail)
        self.assertEqual(self._command("PAIR_CAPABILITIES 2 3840 2160 60 30000 H264"),
                         "ERR BAD_PAIR_CAPABILITIES")
        self.assertEqual(self._command("PAIR_CAPABILITIES 2 1920 1080 60 18000 H265"),
                         "ERR BAD_PAIR_CAPABILITIES")

    def test_commit_keeps_previous_ack_until_adapter_publishes_new_generation(self) -> None:
        def pair_and_commit(width: int, height: int, bitrate_kbps: int) -> tuple[str, str]:
            accepted = self._command(
                f"PAIR_CAPABILITIES 2 {width} {height} 60 {bitrate_kbps} H264"
            ).split(" ")
            self.assertEqual(accepted[0], "CONNECTION_PROFILE_ACCEPTED")
            command_tail = f"2 {accepted[2]} {width} {height} 60 {bitrate_kbps} H264"
            self.assertEqual(
                self._command("COMMIT_CONNECTION_PROFILE " + command_tail),
                "CONNECTION_PROFILE_PENDING " + command_tail,
            )
            return command_tail, (self.state / "connection-profile").read_text(encoding="ascii")

        first_tail, first_profile = pair_and_commit(1280, 720, 8000)
        first_ack_temporary = self.state / "connection-profile-applied.first.new"
        first_ack_temporary.write_text(first_profile, encoding="ascii")
        os.replace(first_ack_temporary, self.state / "connection-profile-applied")
        self.assertEqual(
            self._command("AWAIT_CONNECTION_PROFILE " + first_tail),
            "CONNECTION_PROFILE " + first_tail,
        )

        second_tail, second_profile = pair_and_commit(1920, 1080, 18000)
        # An adapter sees either the fully acknowledged previous profile or
        # the atomically published new profile.  The guest must never create
        # a transient absence of this ACK while committing a new generation.
        self.assertEqual(
            (self.state / "connection-profile-applied").read_text(encoding="ascii"),
            first_profile,
        )
        os.write(self.master, ("AWAIT_CONNECTION_PROFILE " + second_tail + "\n").encode("ascii"))
        self._assert_no_line(0.2)

        second_ack_temporary = self.state / "connection-profile-applied.second.new"
        second_ack_temporary.write_text(second_profile, encoding="ascii")
        os.replace(second_ack_temporary, self.state / "connection-profile-applied")
        self.assertEqual(self._line(timeout=3), "CONNECTION_PROFILE " + second_tail)


if __name__ == "__main__":
    unittest.main()
