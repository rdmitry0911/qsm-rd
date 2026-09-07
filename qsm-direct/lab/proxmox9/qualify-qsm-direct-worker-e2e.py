#!/usr/bin/env python3
"""Real browser E2E gate for the standalone QSM direct worker.

It uses a private D-Bus session and the repository's separate fake QEMU
Display1 service, but no compatibility media server or native client.  The
browser produces an offer and control data-channel, the direct worker receives
Display1 frames, FFmpeg produces H.264, and the QSM bridge packetizes it for
an ordinary Chrome WebRTC decoder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import select
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extensions.browser_bridge.qsm_browser_bridge import BrowserWebRtcBridge, BridgeError  # noqa: E402


class QualificationError(RuntimeError):
    """The independently executing direct path did not complete."""


class BrowserPeer:
    def __init__(self, command: str | None = None) -> None:
        if command is not None:
            parsed = shlex.split(command)
            if not parsed:
                raise QualificationError("remote browser command is empty")
            self._process = subprocess.Popen(
                parsed, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
            )
            return
        chrome = Path("/usr/bin/google-chrome")
        if not chrome.is_file() or not os.access(chrome, os.X_OK):
            raise QualificationError("ordinary Google Chrome is required")
        environment = {**os.environ, "QSM_BROWSER_E2E_EXECUTABLE": os.fspath(chrome)}
        self._process = subprocess.Popen(
            ["/usr/bin/node", os.fspath(ROOT / "lab/proxmox9/browser-webrtc-receiver.cjs")],
            cwd=os.fspath(ROOT / "lab/proxmox9"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1, env=environment,
        )

    def request(self, payload: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
        if self._process.stdin is None or self._process.stdout is None:
            raise QualificationError("browser helper has no control pipes")
        self._process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._process.stdin.flush()
        readable, _writeable, _exceptional = select.select([self._process.stdout], [], [], timeout)
        if not readable:
            raise QualificationError("browser helper timed out")
        try:
            response = json.loads(self._process.stdout.readline())
        except json.JSONDecodeError as error:
            raise QualificationError("browser helper returned invalid JSON") from error
        if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), dict):
            raise QualificationError(f"browser helper failed: {response.get('error', 'unknown error')}")
        return response["result"]

    def close(self) -> None:
        try:
            if self._process.poll() is None:
                self.request({"op": "close"}, timeout=5)
        except QualificationError:
            pass
        finally:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=3)


def wait_socket(path: Path, process: subprocess.Popen[bytes], label: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise QualificationError(f"{label} exited before its socket appeared")
        try:
            if path.stat().st_mode:
                return
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    raise QualificationError(f"{label} did not create its socket")


def terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


async def qualify(worker_binary: Path, fake_qemu_binary: Path,
                  browser_command: str | None = None) -> dict[str, object]:
    if not worker_binary.is_file() or not os.access(worker_binary, os.X_OK):
        raise QualificationError("direct worker binary is unavailable")
    if not fake_qemu_binary.is_file() or not os.access(fake_qemu_binary, os.X_OK):
        raise QualificationError("fake QEMU binary is unavailable")
    with tempfile.TemporaryDirectory(prefix="qsm-direct-e2e.") as temporary:
        root = Path(temporary)
        bus_path = root / "display.bus"
        address = f"unix:path={bus_path}"
        daemon = subprocess.Popen(
            ["/usr/bin/dbus-daemon", "--session", "--nofork", "--nopidfile", f"--address={address}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        fake: subprocess.Popen[bytes] | None = None
        worker: subprocess.Popen[bytes] | None = None
        browser: BrowserPeer | None = None
        bridge: BrowserWebRtcBridge | None = None
        try:
            wait_socket(bus_path, daemon, "private D-Bus daemon")
            fake = subprocess.Popen(
                [os.fspath(fake_qemu_binary), "--bus-address", address, "--width", "320", "--height", "180",
                 "--frames", "120", "--fps", "30", "--inline"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            await asyncio.sleep(0.15)
            if fake.poll() is not None:
                raise QualificationError("fake QEMU did not start")
            bridge = BrowserWebRtcBridge(root, fps=30)
            video_socket, audio_socket = bridge.start_taps()
            worker = subprocess.Popen(
                [os.fspath(worker_binary), "--dbus-address", address, "--video-socket", video_socket,
                 "--audio-socket", audio_socket, "--input-socket", bridge.input_context,
                 "--encoder", "libx264", "--fps", "30", "--initial-size", "320x180"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            await asyncio.sleep(0.25)
            if worker.poll() is not None:
                raise QualificationError("direct worker did not start")
            browser = BrowserPeer(browser_command)
            offer = browser.request({"op": "offer"})
            answer = await bridge.answer_offer(str(offer["sdp"]), str(offer["type"]))
            browser.request({"op": "answer", "answer": answer})
            deadline = time.monotonic() + 12
            status: dict[str, Any] = {}
            frame_stats: dict[str, Any] = {}
            controls_sent = False
            while time.monotonic() < deadline:
                status = browser.request({"op": "status"}, timeout=3)
                if status.get("connectionState") == "connected":
                    if not controls_sent:
                        for message in (
                            {"op": "resize", "width": 320, "height": 180, "fps": 30},
                            {"op": "mouse_button", "button": 1, "down": True},
                            {"op": "mouse_button", "button": 1, "down": False},
                            {"op": "keyboard", "key": 30, "down": True, "modifiers": 0},
                            {"op": "keyboard", "key": 30, "down": False, "modifiers": 0},
                            # A browser scroll-down (positive deltaY) must reach
                            # Display1 as one QEMU wheel-down click, button 4.
                            {"op": "scroll", "vertical": 120, "horizontal": 0},
                        ):
                            browser.request({"op": "control", "message": message}, timeout=3)
                        # Cursor positions must traverse the lossy latest-
                        # state channel, never queue in front of clicks/keys.
                        for message in (
                            {"op": "mouse_position", "x": 20, "y": 10, "width": 320, "height": 180},
                            {"op": "mouse_position", "x": 25, "y": 14, "width": 320, "height": 180},
                        ):
                            browser.request({"op": "pointer", "message": message}, timeout=3)
                        controls_sent = True
                    if (status.get("videoWidth") == 320 and status.get("videoHeight") == 180 and
                            float(status.get("currentTime", 0)) > 0):
                        frame_stats = browser.request({"op": "frame_stats"}, timeout=3)
                        # The fake QEMU producer deliberately draws a colour
                        # gradient and moving rectangle.  A connected WebRTC
                        # peer with an all-black decoded frame is therefore a
                        # capture failure, not a passing media test.
                        if (int(frame_stats.get("nonBlack", 0)) < 8 or
                                int(frame_stats.get("lumaMax", 0)) - int(frame_stats.get("lumaMin", 0)) < 8):
                            raise QualificationError(
                                f"Chrome decoded an empty/black direct-worker frame: {frame_stats}")
                        bridge.ingress.raise_if_failed()
                        bridge.input.raise_if_failed()
                        break
                await asyncio.sleep(0.1)
            else:
                raise QualificationError(f"Chrome did not decode direct worker H.264: {status}")
            # Let the independently running Display1 producer finish before
            # closing its listener.  Stopping the worker here would turn a
            # deliberate test teardown into a spurious QEMU peer failure.
            try:
                fake_stdout, fake_stderr = fake.communicate(timeout=8)
            except subprocess.TimeoutExpired as error:
                raise QualificationError("fake QEMU did not finish") from error
            if fake.returncode != 0:
                raise QualificationError(f"fake QEMU failed: {fake_stderr.decode('utf-8', 'replace')}")
            trace = fake_stdout.decode("utf-8", "replace")
            # mouse=5: the click's press and release, one RelMotion (the fake
            # QEMU reports IsAbsolute=false, so the first pointer sample only
            # seeds the relative baseline and the second becomes the motion),
            # and the wheel click's press and release.  Markers are anchored
            # with spaces so "wheel_up=0" cannot match inside another field.
            if not all(marker in f" {trace.strip()} " for marker in (
                    "FAKE_QEMU_RESULT", " requested=320x180 ", " keyboard=2 ", " mouse=5 ",
                    " wheel_up=0 ", " wheel_down=1 ", " absolute=0:", " relative=1:")):
                raise QualificationError(f"direct input/resize did not reach Display1: {trace}")
            return {"browser": status, "frame_stats": frame_stats, "fake_qemu": trace.strip()}
        finally:
            if browser is not None:
                browser.close()
            terminate(worker)
            terminate(fake)
            if bridge is not None:
                await bridge.close()
            terminate(daemon)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--fake-qemu", type=Path, required=True)
    parser.add_argument(
        "--browser-command",
        help="optional JSON-lines browser-peer command, for example an SSH command to a browser VM",
    )
    arguments = parser.parse_args()
    try:
        result = asyncio.run(qualify(arguments.worker, arguments.fake_qemu, arguments.browser_command))
    except (OSError, QualificationError, subprocess.SubprocessError, BridgeError) as error:
        print(f"QSM_DIRECT_WORKER_E2E_FAILED: {error}", file=sys.stderr)
        return 1
    print("QSM_DIRECT_WORKER_E2E_OK " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
