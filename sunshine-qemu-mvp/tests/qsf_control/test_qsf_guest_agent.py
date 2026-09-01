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
        self._buffer = bytearray()
        self.state = self.path / "state"
        self.process = subprocess.Popen(
            [str(self.binary), "--device", os.ttyname(self.slave), "--state-dir", str(self.state)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
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
        self.assertEqual(self._command("FILE_PUT empty.bin -"), "OK FILE_PUT empty.bin")
        self.assertEqual((self.state / "incoming" / "empty.bin").read_bytes(), b"")

        self.assertEqual(self._command("RESIZE 1920 1080"), "OK RESIZE 1920 1080")
        self.assertEqual((self.state / "resolution").read_text(encoding="ascii"), "1920x1080\n")
        self.assertEqual(self._command("FILE_PUT ../escape QQ=="), "ERR BAD_FILE_PUT")
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


if __name__ == "__main__":
    unittest.main()
