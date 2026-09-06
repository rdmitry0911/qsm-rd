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
    def __init__(self, path: Path, *, announce_ready: bool = True) -> None:
        self.clipboard = b""
        self.clipboard_applied = False
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(1)
        self._connection: socket.socket | None = None
        self._announce_ready = announce_ready
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
            if self._announce_ready:
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
                        self._reply("OK CLIP_SET 42" if self.clipboard_applied else "OK CLIP_SET")
                    else:
                        self._reply("ERR BAD")
        except OSError:
            return


class DirectGuestChannelTest(unittest.TestCase):
    def test_optional_guest_channel_uses_a_short_interaction_timeout(self) -> None:
        self.assertLessEqual(qsm.REQUEST_TIMEOUT_SECONDS, 5.0)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="qsm-direct-guest-test-")
        self.path = Path(self.temporary.name) / "agent.sock"
        # A QEMU socket chardev can drop the guest agent's READY line before
        # the host controller attaches. Requests must work in that normal
        # ordering and prove liveness through their own reply.
        self.agent = GuestAgent(self.path, announce_ready=False)
        self.channel = qsm.QsmGuestChannel(self.path)

    def tearDown(self) -> None:
        self.channel.close()
        self.agent.close()
        self.temporary.cleanup()

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    def test_clipboard_operations(self) -> None:
        copied = "client → guest\nПривет".encode()
        self.assertEqual(self.channel.dispatch({"op": "clipboard_set", "text_b64": self._b64(copied)}),
                         {"bytes": len(copied), "applied": False})
        self.assertEqual(self.agent.clipboard, copied)
        received = self.channel.dispatch({"op": "clipboard_get"})
        self.assertEqual(base64.b64decode(received["text_b64"]), copied)

        self.agent.clipboard_applied = True
        applied = b"event-driven acknowledgement"
        self.assertEqual(self.channel.dispatch({"op": "clipboard_set", "text_b64": self._b64(applied)}),
                         {"bytes": len(applied), "applied": True})

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

    def test_rejects_unsupported_operations_and_nul_clipboard(self) -> None:
        with self.assertRaises(qsm.GuestChannelError):
            self.channel.dispatch({"op": "file_upload", "name": "client.bin", "data_b64": ""})
        with self.assertRaises(qsm.GuestChannelError):
            self.channel.dispatch({"op": "clipboard_set", "text_b64": self._b64(b"no\x00nul")})


if __name__ == "__main__":
    unittest.main(verbosity=2)
