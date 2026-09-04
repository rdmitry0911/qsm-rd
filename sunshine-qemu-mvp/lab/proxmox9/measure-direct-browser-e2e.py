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
import base64
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
        environment = ["env", f"QSM_BROWSER_E2E_EXECUTABLE={arguments.browser_executable}"]
        if arguments.browser_headful:
            environment.append("QSM_BROWSER_E2E_HEADFUL=1")
        if arguments.browser_display:
            environment.append(f"DISPLAY={arguments.browser_display}")
        command = [
            "ssh", "-i", arguments.browser_key,
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{arguments.browser_user}@{arguments.browser_host}",
            *environment,
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
        if (last.get("connectionState") == "connected" and last.get("pointerReady") is True and
                last.get("readyState", 0) >= 2 and
                last.get("videoWidth", 0) > 0 and last.get("videoHeight", 0) > 0):
            return last
        if (last.get("connectionState") == "connected" and last.get("pointerReady") is True and
                not nudge_sent):
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
    parser.add_argument("--browser-headful", action="store_true",
                        help="run Chrome in an isolated visible X display")
    parser.add_argument("--browser-display", default="",
                        help="X display for --browser-headful, for example :97")
    parser.add_argument("--socket", default="/run/qsm-pve-direct-terminal/pve-webrtc.sock")
    parser.add_argument("--node", default=socket.gethostname())
    parser.add_argument("--vmid", type=int, default=100)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--warmup-seconds", type=float, default=3.0)
    parser.add_argument("--hover-runs", type=int, default=0,
                        help="run the 1280x800 laboratory hover-popover measurement N times")
    parser.add_argument("--guest-transfer", action="store_true",
                        help="exercise browser-to-guest clipboard and both file directions")
    parser.add_argument("--guest-file-bytes", type=int, default=22,
                        help="bytes uploaded during --guest-transfer (0..2097152)")
    parser.add_argument("--guest-download-bytes", type=int,
                        help="require this many bytes from the guest download during --guest-transfer")
    arguments = parser.parse_args()
    if not 0 <= arguments.guest_file_bytes <= 2 * 1024 * 1024:
        parser.error("--guest-file-bytes must be in 0..2097152")
    if arguments.guest_download_bytes is not None and not 0 <= arguments.guest_download_bytes <= 2 * 1024 * 1024:
        parser.error("--guest-download-bytes must be in 0..2097152")

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
        after_video = peer.request({"op": "status"})
        pixels = peer.request({"op": "frame_stats"})
        result = {
            "firstVideoMs": first_video_ms,
            "video": video,
            "before": before,
            "after": after,
            "afterVideo": after_video,
            "deltaFramesDecoded": after.get("framesDecoded", 0) - before.get("framesDecoded", 0),
            "pixels": pixels,
        }
        if arguments.guest_transfer:
            clipboard = "browser direct clipboard → guest\nПривет".encode("utf-8")
            set_result = peer.request({"op": "guest", "message": {
                "op": "qsm_guest_clipboard_set",
                "text_b64": base64.b64encode(clipboard).decode("ascii"),
            }}, timeout=15.0)
            if set_result.get("bytes") != len(clipboard):
                raise RuntimeError("guest clipboard set returned an invalid byte count")
            get_result = peer.request({"op": "guest", "message": {
                "op": "qsm_guest_clipboard_get",
            }}, timeout=15.0)
            if base64.b64decode(get_result.get("text_b64", ""), validate=True) != clipboard:
                raise RuntimeError("guest clipboard round trip did not preserve UTF-8 bytes")
            upload_seed = b"\x00browser-direct-file\xff\n"
            upload = (upload_seed * ((arguments.guest_file_bytes + len(upload_seed) - 1) //
                                     len(upload_seed)))[:arguments.guest_file_bytes]
            upload_result = peer.request({"op": "guest", "message": {
                "op": "qsm_guest_file_upload", "name": "browser-direct.bin",
                "data_b64": base64.b64encode(upload).decode("ascii"),
            }}, timeout=15.0)
            if upload_result.get("name") != "browser-direct.bin" or upload_result.get("bytes") != len(upload):
                raise RuntimeError("guest file upload returned an invalid result")
            incoming_list = peer.request({"op": "guest", "message": {
                "op": "qsm_guest_file_list", "area": "incoming",
            }}, timeout=15.0)
            if not any(entry.get("name") == "browser-direct.bin" and entry.get("bytes") == len(upload)
                       for entry in incoming_list.get("files", [])):
                raise RuntimeError("guest incoming file manifest did not include browser upload")
            outgoing_list = peer.request({"op": "guest", "message": {
                "op": "qsm_guest_file_list", "area": "outgoing",
            }}, timeout=15.0)
            if not any(entry.get("name") == "guest-download.txt" for entry in outgoing_list.get("files", [])):
                raise RuntimeError("guest outgoing file manifest did not include download fixture")
            download_result = peer.request({"op": "guest", "message": {
                "op": "qsm_guest_file_download", "name": "guest-download.txt",
            }}, timeout=15.0)
            downloaded = base64.b64decode(download_result.get("data_b64", ""), validate=True)
            if download_result.get("name") != "guest-download.txt" or not downloaded:
                raise RuntimeError("guest file download returned no guest data")
            if arguments.guest_download_bytes is not None and len(downloaded) != arguments.guest_download_bytes:
                raise RuntimeError("guest file download returned an unexpected byte count")
            result["guestTransfer"] = {
                "clipboardBytes": len(clipboard), "uploadBytes": len(upload),
                "downloadBytes": len(downloaded),
                "incomingFiles": len(incoming_list["files"]), "outgoingFiles": len(outgoing_list["files"]),
            }
        if arguments.hover_runs:
            if arguments.width != 1280 or arguments.height < 480:
                raise RuntimeError("the hover target requires a 1280-pixel-wide desktop at least 480 pixels high")
            hover: list[dict[str, Any]] = []
            for _ in range(arguments.hover_runs):
                hover.append(peer.request({"op": "measure_hover", "message": {
                    # The guest's blue application icon is 560..720 x 290..450.
                    # Its magenta popover contains pixel (780, 350).
                    "width": arguments.width, "height": arguments.height,
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
