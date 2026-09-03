#!/usr/bin/env python3
"""Exercise the direct browser guest channel without a desktop host."""

from __future__ import annotations

import base64
import importlib.util
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "extensions" / "direct_guest" / "qsm_guest_channel.py"
SPEC = importlib.util.spec_from_file_location("qsm_guest_channel_tested", MODULE)
assert SPEC is not None and SPEC.loader is not None
qsm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qsm)


class GuestAgent:
    def __init__(self, path: Path) -> None:
        self.clipboard = b""
        self.incoming: dict[str, bytes] = {}
        self.outgoing = {"guest.bin": b"guest\x00file"}
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(1)
        self._connection: socket.socket | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
        self._listener.close()
        self._thread.join(timeout=2)

    @staticmethod
    def _wire(data: bytes) -> str:
        return "-" if not data else base64.b64encode(data).decode("ascii")

    def event_clipboard(self, data: bytes) -> None:
        assert self._connection is not None
        self._connection.sendall(("EVENT_CLIP " + self._wire(data) + "\n").encode("ascii"))

    def _reply(self, value: str) -> None:
        assert self._connection is not None
        self._connection.sendall((value + "\n").encode("ascii"))

    def _serve(self) -> None:
        try:
            connection, _ = self._listener.accept()
            self._connection = connection
            self._reply("READY QSF1")
            buffered = bytearray()
            while True:
                block = connection.recv(65536)
                if not block:
                    return
                buffered.extend(block)
                while b"\n" in buffered:
                    raw, _, trailing = buffered.partition(b"\n")
                    buffered = bytearray(trailing)
                    fields = raw.decode("ascii").split(" ")
                    if fields == ["PING"]:
                        self._reply("OK PONG")
                    elif fields == ["CLIP_GET"]:
                        self._reply("CLIP " + self._wire(self.clipboard))
                    elif len(fields) == 2 and fields[0] == "CLIP_SET":
                        self.clipboard = b"" if fields[1] == "-" else base64.b64decode(fields[1])
                        self._reply("OK CLIP_SET")
                    elif len(fields) == 3 and fields[0] == "FILE_PUT":
                        self.incoming[fields[1]] = b"" if fields[2] == "-" else base64.b64decode(fields[2])
                        self._reply("OK FILE_PUT " + fields[1])
                    elif len(fields) == 2 and fields[0] == "FILE_GET" and fields[1] in self.outgoing:
                        self._reply("FILE " + fields[1] + " " + self._wire(self.outgoing[fields[1]]))
                    else:
                        self._reply("ERR BAD")
        except OSError:
            return


class DirectGuestChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="qsm-direct-guest-test-")
        self.path = Path(self.temporary.name) / "agent.sock"
        self.agent = GuestAgent(self.path)
        self.channel = qsm.QsmGuestChannel(self.path)

    def tearDown(self) -> None:
        self.channel.close()
        self.agent.close()
        self.temporary.cleanup()

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    def test_clipboard_and_both_file_directions(self) -> None:
        copied = "client → guest\nПривет".encode()
        self.assertEqual(self.channel.dispatch({"op": "clipboard_set", "text_b64": self._b64(copied)}),
                         {"bytes": len(copied)})
        self.assertEqual(self.agent.clipboard, copied)
        received = self.channel.dispatch({"op": "clipboard_get"})
        self.assertEqual(base64.b64decode(received["text_b64"]), copied)

        uploaded = b"\x00client-file\xff"
        self.assertEqual(self.channel.dispatch({"op": "file_upload", "name": "client.bin",
                                                 "data_b64": self._b64(uploaded)}),
                         {"name": "client.bin", "bytes": len(uploaded)})
        self.assertEqual(self.agent.incoming["client.bin"], uploaded)
        downloaded = self.channel.dispatch({"op": "file_download", "name": "guest.bin"})
        self.assertEqual(base64.b64decode(downloaded["data_b64"]), b"guest\x00file")

    def test_guest_clipboard_event_reaches_every_subscriber(self) -> None:
        received: list[str] = []
        remove = self.channel.add_clipboard_listener(received.append)
        self.channel.dispatch({"op": "status"})
        self.agent.event_clipboard("guest → client".encode())
        deadline = time.monotonic() + 2
        while not received and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(received, ["guest → client"])
        remove()

    def test_rejects_paths_and_nul_clipboard(self) -> None:
        with self.assertRaises(qsm.GuestChannelError):
            self.channel.dispatch({"op": "file_upload", "name": "../escape", "data_b64": ""})
        with self.assertRaises(qsm.GuestChannelError):
            self.channel.dispatch({"op": "clipboard_set", "text_b64": self._b64(b"no\x00nul")})


if __name__ == "__main__":
    unittest.main(verbosity=2)
