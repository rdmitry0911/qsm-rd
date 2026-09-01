#!/usr/bin/env python3
"""Protocol tests for the local QSF control boundary.

The fake agent intentionally speaks the same constrained virtio-serial wire
protocol as the guest C agent.  It lets this test cover authentication, UTF-8
clipboard round-trips, both file directions, and resize forwarding without a
desktop host or a QEMU process.
"""

from __future__ import annotations

import base64
import importlib.util
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
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "extensions" / "qsf_control" / "qsf_control.py"
SPEC = importlib.util.spec_from_file_location("qsf_control_transaction_under_test", CONTROL)
assert SPEC is not None and SPEC.loader is not None
qsf_control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qsf_control)


class FakeAgent:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.clipboard = b""
        self.incoming: dict[str, bytes] = {}
        self.outgoing: dict[str, bytes] = {"guest.txt": b"guest-to-client\n", "empty.bin": b""}
        self.received_resize: tuple[int, int] | None = None
        self.optimization_requested = False
        self.received_pair_capabilities: tuple[int, int, int, int, str] | None = None
        self.profile_apply_delay_seconds = 0.0
        self._pending_profile: tuple[int, int, int, int, int, str] | None = None
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
                    elif parts == ["CONNECTION_OPTIMIZE"]:
                        self.optimization_requested = True
                        self._reply("GUEST_CAPABILITIES_REQUEST 2 2560 1440 60")
                    elif len(parts) == 7 and parts[0] == "PAIR_CAPABILITIES" and parts[1] == "2":
                        width, height, fps, bitrate = (int(parts[2]), int(parts[3]),
                                                       int(parts[4]), int(parts[5]))
                        codec = parts[6]
                        self.received_pair_capabilities = (width, height, fps, bitrate, codec)
                        self._pending_profile = (42, width, height, fps, bitrate, codec)
                        self._reply("CONNECTION_PROFILE_ACCEPTED 2 42 %d %d %d %d %s" %
                                    (width, height, fps, bitrate, codec))
                    elif len(parts) == 8 and parts[0] in {
                            "COMMIT_CONNECTION_PROFILE", "AWAIT_CONNECTION_PROFILE"} and \
                            parts[1] == "2" and self._pending_profile is not None:
                        generation, width, height, fps, bitrate, codec = self._pending_profile
                        if parts[2:] != [str(generation), str(width), str(height), str(fps),
                                         str(bitrate), codec]:
                            self._reply("ERR BAD_CONNECTION_PROFILE")
                        elif parts[0] == "COMMIT_CONNECTION_PROFILE":
                            self._reply("CONNECTION_PROFILE_PENDING 2 %d %d %d %d %d %s" %
                                        self._pending_profile)
                        else:
                            if self.profile_apply_delay_seconds:
                                time.sleep(self.profile_apply_delay_seconds)
                            self._reply("CONNECTION_PROFILE 2 %d %d %d %d %d %s" %
                                        self._pending_profile)
                    else:
                        self._reply("ERR UNKNOWN_COMMAND")
        except (OSError, ValueError):
            return


class RecordingProfileAgent:
    """In-process agent used to inspect a broker transaction's deadline."""

    def __init__(self, *, block_on_apply: bool = False) -> None:
        self.requests: list[tuple[str, str, float | None]] = []
        self.pending: tuple[int, int, int, int, int, str] | None = None
        self.block_on_apply = block_on_apply
        self.apply_started = threading.Event()
        self.allow_apply = threading.Event()

    def request(self, command: str, expected_prefix: str,
                *, deadline: float | None = None) -> str:
        self.requests.append((command, expected_prefix, deadline))
        parts = command.split(" ")
        if parts == ["CONNECTION_OPTIMIZE"]:
            return "GUEST_CAPABILITIES_REQUEST 2 2560 1440 60"
        if len(parts) == 7 and parts[0] == "PAIR_CAPABILITIES":
            self.pending = (42, int(parts[2]), int(parts[3]), int(parts[4]),
                            int(parts[5]), parts[6])
            return "CONNECTION_PROFILE_ACCEPTED 2 %d %d %d %d %d %s" % self.pending
        if self.pending is not None and len(parts) == 8 and \
                parts[0] == "COMMIT_CONNECTION_PROFILE":
            return "CONNECTION_PROFILE_PENDING 2 %d %d %d %d %d %s" % self.pending
        if self.pending is not None and len(parts) == 8 and \
                parts[0] == "AWAIT_CONNECTION_PROFILE":
            self.apply_started.set()
            if self.block_on_apply and not self.allow_apply.wait(timeout=2):
                raise AssertionError("test did not release the profile apply")
            return "CONNECTION_PROFILE 2 %d %d %d %d %d %s" % self.pending
        if len(parts) == 3 and parts[0] == "RESIZE":
            return "OK RESIZE " + parts[1] + " " + parts[2]
        raise AssertionError(f"unexpected agent command: {command}")


def _host_capabilities(*, deadline: float | None = None) -> dict[str, object]:
    del deadline
    return {
        "max_width": 2560,
        "max_height": 1440,
        "max_fps": 60,
        "max_bitrate_kbps": 30000,
        "encoder_codecs": ("H264",),
    }


def _broker_for_transaction_test(agent: RecordingProfileAgent) -> object:
    # Avoid opening a real Unix connection: this class tests only Broker's
    # transaction boundaries, while QsfControlTest below covers the wire.
    broker = qsf_control.Broker.__new__(qsf_control.Broker)
    broker._agent = agent
    broker._enable_qemu_resize = False
    broker._display_transaction_lock = threading.Lock()
    return broker


class BrokerDisplayTransactionTest(unittest.TestCase):
    payload = {"op": "connection_optimize", "client": {
        "requested_width": 2560,
        "requested_height": 1440,
        "max_fps": 60,
        "decoder_codecs": ["H264"],
    }}

    def test_profile_stages_share_one_absolute_deadline(self) -> None:
        agent = RecordingProfileAgent()
        broker = _broker_for_transaction_test(agent)
        qemu_calls: list[tuple[int, int, float | None]] = []
        host_deadlines: list[float | None] = []

        def host(*, deadline: float | None = None) -> dict[str, object]:
            host_deadlines.append(deadline)
            return _host_capabilities()

        def set_qemu(width: int, height: int, *, deadline: float | None = None) -> str:
            qemu_calls.append((width, height, deadline))
            return "disabled"

        broker._set_qemu_ui_info = set_qemu
        with patch.object(qsf_control.time, "monotonic", return_value=1000.0), \
                patch.object(qsf_control, "detected_host_encoder_capabilities", side_effect=host):
            profile = broker.optimize_connection(self.payload)

        self.assertEqual(profile["guest_profile_generation"], "42")
        deadlines = [request[2] for request in agent.requests]
        self.assertEqual(deadlines, [1075.0] * 4)
        self.assertEqual(host_deadlines, [1075.0])
        self.assertEqual(qemu_calls, [(2560, 1440, 1075.0)])

    def test_resize_cannot_interleave_a_profile_apply(self) -> None:
        agent = RecordingProfileAgent(block_on_apply=True)
        broker = _broker_for_transaction_test(agent)
        broker._set_qemu_ui_info = lambda width, height, *, deadline=None: "disabled"
        errors: list[BaseException] = []

        with patch.object(qsf_control, "detected_host_encoder_capabilities",
                          side_effect=_host_capabilities):
            worker = threading.Thread(
                target=lambda: self._run_profile(broker, errors), daemon=True)
            worker.start()
            self.assertTrue(agent.apply_started.wait(timeout=1),
                            "profile transaction did not reach its guest acknowledgement")
            with self.assertRaisesRegex(qsf_control.ControlError,
                                        "another display profile transaction"):
                broker.dispatch({"op": "resize", "width": 1920, "height": 1080})
            self.assertFalse(any(command.startswith("RESIZE ")
                                 for command, _, _ in agent.requests))
            agent.allow_apply.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def _run_profile(self, broker: object, errors: list[BaseException]) -> None:
        try:
            broker.optimize_connection(self.payload)
        except BaseException as error:  # pragma: no cover - failure assertion below
            errors.append(error)


class QsfControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="qsf-control-test-")
        self.path = Path(self.directory.name)
        self.agent = FakeAgent(self.path / "agent.sock")
        self.control_socket = self.path / "control.sock"
        self.token_file = self.path / "token"
        environment = os.environ.copy()
        environment.update({
            "QSUNSHINE_QSF_HOST_MAX_WIDTH": "2560",
            "QSUNSHINE_QSF_HOST_MAX_HEIGHT": "1440",
            "QSUNSHINE_QSF_HOST_MAX_FPS": "60",
            "QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS": "30000",
            "QSUNSHINE_QSF_HOST_ENCODER_CODECS": "H264",
        })
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
            env=environment,
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

        optimized = self.request({"op": "connection_optimize", "client": {
            "requested_width": 2560,
            "requested_height": 1440,
            "max_fps": 60,
            "decoder_codecs": ["H264"],
        }})
        self.assertTrue(optimized["ok"])
        self.assertTrue(self.agent.optimization_requested)
        self.assertEqual(self.agent.received_pair_capabilities, (2560, 1440, 60, 28000, "H264"))
        self.assertEqual(optimized["result"], {
            "version": 2,
            "width": 2560,
            "height": 1440,
            "fps": 60,
            "bitrate_kbps": 28000,
            "video_codec": "H.264",
            "requested_width": 2560,
            "requested_height": 1440,
            "guest_profile_generation": "42",
            "qemu_set_ui_info": "disabled",
        })

        rejected = self.request({"op": "download", "name": "../escape"})
        self.assertFalse(rejected["ok"])
        unauthenticated = self.request({"op": "status"}, token="0" * 64)
        self.assertFalse(unauthenticated["ok"])
        self.assertIn("authentication", unauthenticated["error"])


if __name__ == "__main__":
    unittest.main()
