#!/usr/bin/env python3
"""Protocol tests for the local QSF control boundary.

The fake agent intentionally speaks the same constrained virtio-serial wire
protocol as the guest C agent.  It lets this test cover authentication, UTF-8
clipboard round-trips, both file directions, and resize forwarding without a
desktop host or a QEMU process.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "extensions" / "qsf_control" / "qsf_control.py"


class FakeAgent:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clipboard = b""
        self.incoming: dict[str, bytes] = {}
        self.outgoing: dict[str, bytes] = {"guest.txt": b"guest-to-client\n", "empty.bin": b""}
        self.received_resize: tuple[int, int] | None = None
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

    def _reply(self, value: str) -> None:
        assert self._connection is not None
        self._connection.sendall(value.encode("ascii") + b"\n")

    @staticmethod
    def _wire_encode(data: bytes) -> str:
        return "-" if not data else base64.b64encode(data).decode("ascii")

    @staticmethod
    def _wire_decode(data: str) -> bytes:
        return b"" if data == "-" else base64.b64decode(data.encode("ascii"), validate=True)

    def _serve(self) -> None:
        connection, _ = self._listener.accept()
        self._connection = connection
        self._reply("READY QSF1")
        buffer = bytearray()
        try:
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, trailing = buffer.partition(b"\n")
                    buffer = bytearray(trailing)
                    parts = raw.decode("ascii").split(" ")
                    if parts == ["PING"]:
                        self._reply("OK PONG")
                    elif parts == ["CLIP_GET"]:
                        self._reply("CLIP " + self._wire_encode(self.clipboard))
                    elif len(parts) == 2 and parts[0] == "CLIP_SET":
                        self.clipboard = self._wire_decode(parts[1])
                        self._reply("OK CLIP_SET")
                    elif len(parts) == 3 and parts[0] == "FILE_PUT":
                        self.incoming[parts[1]] = self._wire_decode(parts[2])
                        self._reply("OK FILE_PUT " + parts[1])
                    elif len(parts) == 2 and parts[0] == "FILE_GET":
                        if parts[1] not in self.outgoing:
                            self._reply("ERR FILE_NOT_FOUND")
                        else:
                            self._reply("FILE " + parts[1] + " " + self._wire_encode(self.outgoing[parts[1]]))
                    elif len(parts) == 3 and parts[0] == "RESIZE":
                        self.received_resize = (int(parts[1]), int(parts[2]))
                        self._reply("OK RESIZE " + parts[1] + " " + parts[2])
                    else:
                        self._reply("ERR UNKNOWN_COMMAND")
        except (OSError, ValueError):
            return


class QsfControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="qsf-control-test-")
        self.path = Path(self.directory.name)
        self.agent = FakeAgent(self.path / "agent.sock")
        self.control_socket = self.path / "control.sock"
        self.token_file = self.path / "token"
        self.process = subprocess.Popen(
            [
                sys.executable, str(CONTROL),
                "--agent-socket", str(self.path / "agent.sock"),
                "--control-socket", str(self.control_socket),
                "--token-file", str(self.token_file),
                "--no-qemu-resize",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(100):
            if self.control_socket.exists() and self.token_file.exists():
                break
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate(timeout=1)
                self.fail(f"control process exited: stdout={stdout} stderr={stderr}")
            time.sleep(0.02)
        else:
            self.fail("control socket did not appear")
        self.token = self.token_file.read_text(encoding="ascii").strip()

    def tearDown(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        self.agent.close()
        self.directory.cleanup()

    def request(self, payload: dict[str, object], *, token: str | None = None) -> dict[str, object]:
        payload = dict(payload)
        payload["token"] = self.token if token is None else token
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(self.control_socket))
            client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            result = client.recv(4 * 1024 * 1024)
        return json.loads(result.decode("utf-8"))

    def test_end_to_end_operations_and_authentication(self) -> None:
        status = self.request({"op": "status"})
        self.assertTrue(status["ok"])
        self.assertEqual(status["result"]["agent"], "ready")

        text = "client → guest\nПривет"
        set_result = self.request({"op": "clipboard_set", "text_b64": base64.b64encode(text.encode()).decode()})
        self.assertTrue(set_result["ok"])
        self.assertEqual(self.agent.clipboard.decode(), text)
        get_result = self.request({"op": "clipboard_get"})
        self.assertEqual(base64.b64decode(get_result["result"]["text_b64"]), text.encode())

        uploaded = b"\x00client-file\xff"
        file_result = self.request({"op": "upload", "name": "client.bin", "data_b64": base64.b64encode(uploaded).decode()})
        self.assertTrue(file_result["ok"])
        self.assertEqual(self.agent.incoming["client.bin"], uploaded)
        downloaded = self.request({"op": "download", "name": "guest.txt"})
        self.assertEqual(base64.b64decode(downloaded["result"]["data_b64"]), b"guest-to-client\n")
        empty = self.request({"op": "download", "name": "empty.bin"})
        self.assertEqual(base64.b64decode(empty["result"]["data_b64"]), b"")

        resize = self.request({"op": "resize", "width": 1920, "height": 1080})
        self.assertTrue(resize["ok"])
        self.assertEqual(self.agent.received_resize, (1920, 1080))
        self.assertEqual(resize["result"]["qemu_set_ui_info"], "disabled")

        rejected = self.request({"op": "download", "name": "../escape"})
        self.assertFalse(rejected["ok"])
        unauthenticated = self.request({"op": "status"}, token="0" * 64)
        self.assertFalse(unauthenticated["ok"])
        self.assertIn("authentication", unauthenticated["error"])


if __name__ == "__main__":
    unittest.main()
