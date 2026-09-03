#!/usr/bin/env python3
"""Measure the real Chrome WebRTC receive path of a QSM Direct lab VM.

Run this *inside* the nested PVE laboratory.  The browser is deliberately a
separate VM: it exercises Chrome's actual H.264 decoder, RTP jitter buffer and
the QSM data channels instead of accepting an aiortc-only result as browser
evidence.  Output is a single machine-readable JSON record suitable for a
qualification log.
"""

from __future__ import annotations

import argparse
import json
import select
import socket
import subprocess
import sys
import time
from typing import Any


class BrowserPeer:
    """Small JSON-lines driver for the disposable Chrome peer over SSH."""

    def __init__(self, arguments: argparse.Namespace) -> None:
        command = [
            "ssh", "-i", arguments.browser_key,
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{arguments.browser_user}@{arguments.browser_host}",
            "env", f"QSM_BROWSER_E2E_EXECUTABLE={arguments.browser_executable}",
            "node", arguments.browser_script,
        ]
        self._process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        assert self._process.stdin is not None
        assert self._process.stdout is not None

    def request(self, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("browser peer pipes are unavailable")
        self._process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._process.stdin.flush()
        ready, _, _ = select.select([self._process.stdout], [], [], timeout)
        if not ready:
            raise RuntimeError(f"browser peer did not answer {payload.get('op')!r}")
        line = self._process.stdout.readline()
        if not line:
            raise RuntimeError("browser peer closed stdout")
        response = json.loads(line)
        if response.get("ok") is not True or not isinstance(response.get("result"), dict):
            raise RuntimeError(f"browser peer rejected {payload.get('op')!r}: {response.get('error')}")
        return response["result"]

    def close(self) -> None:
        if self._process.poll() is not None:
            return
        try:
            self.request({"op": "close"}, timeout=5.0)
        except (OSError, RuntimeError, ValueError):
            pass
        try:
            self._process.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()


def terminal_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30.0)
        connection.connect(path)
        connection.sendall(encoded)
        response = bytearray()
        while not response.endswith(b"\n"):
            block = connection.recv(65536)
            if not block:
                raise RuntimeError("direct-terminal closed its response")
            response.extend(block)
            if len(response) > 300000:
                raise RuntimeError("direct-terminal response is excessive")
    decoded = json.loads(response.decode("ascii"))
    if decoded.get("ok") is not True or not isinstance(decoded.get("result"), dict):
        raise RuntimeError("direct-terminal rejected the browser offer")
    return decoded["result"]


def wait_for_video(peer: BrowserPeer, timeout: float, *, width: int, height: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    nudge_sent = False
    while time.monotonic() < deadline:
        last = peer.request({"op": "status"}, timeout=5.0)
        if (last.get("connectionState") == "connected" and last.get("readyState", 0) >= 2 and
                last.get("videoWidth", 0) > 0 and last.get("videoHeight", 0) > 0):
            return last
        if last.get("connectionState") == "connected" and not nudge_sent:
            # The separate browser VM has already completed DTLS, so data
            # channels are live even if a totally idle guest has not yet
            # painted a second frame. Exercise the real pointer route rather
            # than treating a static initial scanout as a Chrome failure.
            peer.request({"op": "pointer", "message": {
                "op": "mouse_position", "x": 40, "y": 40,
                "width": width, "height": height,
            }}, timeout=5.0)
            nudge_sent = True
        time.sleep(0.1)
    raise RuntimeError(f"Chrome did not present video: {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-host", default="192.168.76.2")
    parser.add_argument("--browser-user", default="root")
    parser.add_argument("--browser-key", default="/root/.ssh/qsm-browser-gate")
    parser.add_argument("--browser-script", default="/opt/qsm-browser/browser-webrtc-receiver.cjs")
    parser.add_argument("--browser-executable", default="/usr/bin/google-chrome")
    parser.add_argument("--socket", default="/run/qsm-pve-direct-terminal/pve-webrtc.sock")
    parser.add_argument("--node", default=socket.gethostname())
    parser.add_argument("--vmid", type=int, default=100)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--warmup-seconds", type=float, default=3.0)
    parser.add_argument("--hover-runs", type=int, default=0,
                        help="run the 1280x800 laboratory hover-popover measurement N times")
    arguments = parser.parse_args()

    peer = BrowserPeer(arguments)
    started = time.monotonic()
    try:
        offer = peer.request({"op": "offer"})
        if "H264/90000" not in offer.get("sdp", ""):
            raise RuntimeError("Chrome test peer did not offer H.264")
        answer = terminal_request(arguments.socket, {
            "version": 1, "op": "pve_acl_webrtc", "node": arguments.node,
            "vmid": arguments.vmid, "subject": "root@pam", "sdp": offer["sdp"],
            "sdp_type": "offer", "width": arguments.width, "height": arguments.height,
            "fps": arguments.fps,
        })
        peer.request({"op": "answer", "answer": answer})
        video = wait_for_video(peer, timeout=30.0, width=arguments.width, height=arguments.height)
        first_video_ms = (time.monotonic() - started) * 1000.0
        before = peer.request({"op": "webrtc_stats"})
        time.sleep(arguments.warmup_seconds)
        after = peer.request({"op": "webrtc_stats"})
        pixels = peer.request({"op": "frame_stats"})
        result = {
            "firstVideoMs": first_video_ms,
            "video": video,
            "before": before,
            "after": after,
            "deltaFramesDecoded": after.get("framesDecoded", 0) - before.get("framesDecoded", 0),
            "pixels": pixels,
        }
        if arguments.hover_runs:
            if (arguments.width, arguments.height) != (1280, 800):
                raise RuntimeError("the hover target requires --width 1280 --height 800")
            hover: list[dict[str, Any]] = []
            for _ in range(arguments.hover_runs):
                hover.append(peer.request({"op": "measure_hover", "message": {
                    # The guest's blue application icon is 560..720 x 290..450.
                    # Its magenta popover contains pixel (780, 350).
                    "width": 1280, "height": 800,
                    "resetX": 40, "resetY": 40,
                    "targetX": 640, "targetY": 370,
                    "probeX": 780, "probeY": 350,
                    "timeoutMs": 8000,
                }}, timeout=15.0))
            result["hover"] = hover
        print("QSM_LAB_DIRECT_CHROME_E2E " + json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0
    finally:
        peer.close()


if __name__ == "__main__":
    raise SystemExit(main())
