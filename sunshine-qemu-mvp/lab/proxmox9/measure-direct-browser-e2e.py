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
import re
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
        if arguments.browser_ice_server:
            environment.append(f"QSM_BROWSER_E2E_ICE_SERVER={arguments.browser_ice_server}")
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


def wait_for_video(peer: BrowserPeer, timeout: float, *, width: int, height: int,
                   require_geometry: bool = False) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    nudge_sent = False
    while time.monotonic() < deadline:
        last = peer.request({"op": "status"}, timeout=5.0)
        dimensions_match = last.get("videoWidth") == width and last.get("videoHeight") == height
        if (last.get("connectionState") == "connected" and last.get("pointerReady") is True and
                last.get("controlReady") is True and last.get("readyState", 0) >= 2 and
                last.get("videoWidth", 0) > 0 and last.get("videoHeight", 0) > 0 and
                (not require_geometry or dimensions_match)):
            return last
        if (last.get("connectionState") == "connected" and last.get("pointerReady") is True and
                not nudge_sent):
            # The separate browser VM has already completed DTLS, so data
            # channels are live even if a totally idle guest has not yet
            # painted a second frame. Exercise the real pointer route rather
            # than treating a static initial scanout as a Chrome failure.
            # The QEMU Display1 mouse can legitimately expose relative mode.
            # Send a short ordered-in-time sweep rather than a lone first
            # coordinate: the first sample establishes the baseline and the
            # next samples prove that movement reaches the guest device.
            for x, y in ((40, 40), (80, 72), (120, 104)):
                peer.request({"op": "pointer", "message": {
                    "op": "mouse_position", "x": x, "y": y,
                    "width": width, "height": height,
                }}, timeout=5.0)
                time.sleep(0.03)
            nudge_sent = True
        time.sleep(0.1)
    raise RuntimeError(f"Chrome did not present video: {last}")


def wait_for_disconnect(peer: BrowserPeer, timeout: float) -> dict[str, Any]:
    """Wait for a VM/service lifecycle action to retire this WebRTC peer."""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = peer.request({"op": "status"}, timeout=5.0)
        # Chromium can keep the ICE/DTLS transport in ``connected`` briefly
        # after aiortc has closed the two server data channels.  The shipped
        # popup treats either channel's close event as the authoritative VM
        # lifecycle signal and closes itself immediately; a test peer must
        # model that same browser-visible contract instead of waiting for an
        # unrelated ICE timeout (often tens of seconds).
        if (last.get("connectionState") != "connected" or
                last.get("controlReady") is not True or last.get("pointerReady") is not True):
            return last
        time.sleep(0.1)
    raise RuntimeError(f"Chrome Console remained connected after lifecycle action: {last}")


def require_viewport(status: dict[str, Any], width: int, height: int, label: str,
                     *, require_content: bool = True) -> None:
    """Reject a decoded stream that leaves the Console viewport unused."""
    layout = status.get("layout")
    if not isinstance(layout, dict):
        raise RuntimeError(f"{label}: browser did not report video layout")
    expected = {
        "viewportWidth": width, "viewportHeight": height,
        "objectFit": "contain", "fillsViewport": True,
    }
    if require_content:
        expected["contentFillsViewport"] = True
    if any(layout.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"{label}: video does not fill {width}x{height} browser viewport: {layout}")


def geometry(value: str) -> tuple[int, int]:
    width, separator, height = value.partition("x")
    if separator != "x" or not width.isdecimal() or not height.isdecimal():
        raise argparse.ArgumentTypeError("geometry must be WIDTHxHEIGHT")
    result = int(width), int(height)
    if not (64 <= result[0] <= 16384 and 64 <= result[1] <= 16384 and
            result[0] % 2 == 0 and result[1] % 2 == 0):
        raise argparse.ArgumentTypeError("geometry must be even and within 64..16384")
    return result


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
    parser.add_argument("--browser-ice-server", default="",
                        help="optional laboratory-only STUN/TURN URL for the remote Chrome peer")
    parser.add_argument("--socket", default="/run/qsm-pve-direct-terminal/pve-webrtc.sock")
    parser.add_argument("--node", default=socket.gethostname())
    parser.add_argument("--vmid", type=int, default=100)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--warmup-seconds", type=float, default=3.0)
    parser.add_argument("--hold-seconds", type=float, default=0.0,
                        help="keep the live browser peer open before recording final state (0..120)")
    parser.add_argument("--expect-disconnect", action="store_true",
                        help="require a lifecycle action to disconnect the live browser peer within --hold-seconds")
    parser.add_argument("--hover-runs", type=int, default=0,
                        help="run the 1280x800 laboratory hover-popover measurement N times")
    parser.add_argument("--guest-transfer", action="store_true",
                        help="exercise browser-to-guest clipboard and both file directions")
    parser.add_argument("--guest-file-bytes", type=int, default=22,
                        help="bytes uploaded during --guest-transfer (0..2097152)")
    parser.add_argument("--guest-download-bytes", type=int,
                        help="require this many bytes from the guest download during --guest-transfer")
    parser.add_argument("--viewport-resize", type=geometry, action="append", default=[],
                        help="resize the actual Chrome viewport and Display1 in this live session (WIDTHxHEIGHT)")
    parser.add_argument("--screenshot", default="",
                        help="optional /tmp/qsm-browser-e2e-*.png path on the disposable Chrome host")
    arguments = parser.parse_args()
    if not 0 <= arguments.guest_file_bytes <= 2 * 1024 * 1024:
        parser.error("--guest-file-bytes must be in 0..2097152")
    if arguments.guest_download_bytes is not None and not 0 <= arguments.guest_download_bytes <= 2 * 1024 * 1024:
        parser.error("--guest-download-bytes must be in 0..2097152")
    if not 0 <= arguments.hold_seconds <= 120:
        parser.error("--hold-seconds must be in 0..120")
    if arguments.expect_disconnect and arguments.hold_seconds < 1:
        parser.error("--expect-disconnect requires --hold-seconds of at least one second")
    if not (64 <= arguments.width <= 16384 and 64 <= arguments.height <= 16384 and
            arguments.width % 2 == 0 and arguments.height % 2 == 0):
        parser.error("--width/--height must be even and within 64..16384")
    if arguments.screenshot and not re.fullmatch(r"/tmp/qsm-browser-e2e-[A-Za-z0-9._-]{1,80}\.png", arguments.screenshot):
        parser.error("--screenshot must be a bounded /tmp/qsm-browser-e2e-*.png path")

    peer = BrowserPeer(arguments)
    started = time.monotonic()
    try:
        offer = peer.request({"op": "offer"})
        if "H264/90000" not in offer.get("sdp", ""):
            raise RuntimeError("Chrome test peer did not offer H.264")
        browser_viewport = peer.request({"op": "viewport", "width": arguments.width, "height": arguments.height})
        require_viewport(browser_viewport, arguments.width, arguments.height, "initial viewport")
        answer = terminal_request(arguments.socket, {
            "version": 1, "op": "pve_acl_webrtc", "node": arguments.node,
            "vmid": arguments.vmid, "subject": "root@pam", "sdp": offer["sdp"],
            "sdp_type": "offer", "width": arguments.width, "height": arguments.height,
            "fps": arguments.fps,
        })
        peer.request({"op": "answer", "answer": answer})
        initial_video = wait_for_video(peer, timeout=30.0, width=arguments.width, height=arguments.height)
        # Match the real Console UI: the SDP request supplies an initial
        # worker size, but the browser's control channel is the authoritative
        # resize path after the WebRTC channels become live.
        peer.request({"op": "control", "message": {
            "op": "resize", "width": arguments.width, "height": arguments.height,
            "fps": arguments.fps,
        }}, timeout=5.0)
        video = wait_for_video(peer, timeout=30.0, width=arguments.width, height=arguments.height,
                               require_geometry=True)
        require_viewport(video, arguments.width, arguments.height, "initial Display1 frame")
        # Exercise the reliable input lane too. Shift (set-1 42) changes no
        # text in a greeter or desktop, while still proving key press and
        # release traverse Chrome -> SCTP -> Display1 -> guest USB keyboard.
        # The matching button click proves that a direct Console has both its
        # lossy pointer samples and ordered button events after a reconnect.
        peer.request({"op": "control", "message": {
            "op": "mouse_button", "button": 1, "down": True,
        }}, timeout=5.0)
        peer.request({"op": "control", "message": {
            "op": "mouse_button", "button": 1, "down": False,
        }}, timeout=5.0)
        peer.request({"op": "control", "message": {
            "op": "keyboard", "key": 42, "down": True, "modifiers": 0,
        }}, timeout=5.0)
        peer.request({"op": "control", "message": {
            "op": "keyboard", "key": 42, "down": False, "modifiers": 0,
        }}, timeout=5.0)
        viewport_resizes: list[dict[str, Any]] = []
        for index, (width, height) in enumerate(arguments.viewport_resize, start=1):
            resized_viewport = peer.request({"op": "viewport", "width": width, "height": height})
            # The old frame deliberately remains aspect-correct during this
            # short transition. Only the post-ack Display1 frame is required
            # to occupy the whole viewport.
            require_viewport(resized_viewport, width, height, f"viewport resize {index}",
                             require_content=False)
            peer.request({"op": "control", "message": {
                "op": "resize", "width": width, "height": height, "fps": arguments.fps,
            }}, timeout=5.0)
            resized_video = wait_for_video(peer, timeout=30.0, width=width, height=height,
                                           require_geometry=True)
            require_viewport(resized_video, width, height, f"Display1 resize {index}")
            viewport_resizes.append({"width": width, "height": height, "video": resized_video})
        first_video_ms = (time.monotonic() - started) * 1000.0
        before = peer.request({"op": "webrtc_stats"})
        time.sleep(arguments.warmup_seconds)
        after = peer.request({"op": "webrtc_stats"})
        after_video = peer.request({"op": "status"})
        pixels = peer.request({"op": "frame_stats"})
        if arguments.screenshot:
            peer.request({"op": "screenshot", "path": arguments.screenshot})
        result = {
            "firstVideoMs": first_video_ms,
            "initialVideo": initial_video,
            "video": video,
            "before": before,
            "after": after,
            "afterVideo": after_video,
            "deltaFramesDecoded": after.get("framesDecoded", 0) - before.get("framesDecoded", 0),
            "pixels": pixels,
            "inputExercise": {"pointerSweep": 3, "mouseClick": True, "keyboard": "left-shift"},
            "viewportResizes": viewport_resizes,
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
            outgoing_files = outgoing_list.get("files")
            if not isinstance(outgoing_files, list):
                raise RuntimeError("guest outgoing file manifest is invalid")
            # The agent owns this directory.  A real desktop may expose a
            # user-selected file rather than the laboratory's historical
            # ``guest-download.txt`` fixture; any safe manifest entry is a
            # valid guest-to-browser transfer candidate.
            download_name = next((entry.get("name") for entry in outgoing_files
                                  if isinstance(entry, dict) and isinstance(entry.get("name"), str)), None)
            if download_name is None:
                raise RuntimeError("guest outgoing file manifest is empty")
            download_result = peer.request({"op": "guest", "message": {
                "op": "qsm_guest_file_download", "name": download_name,
            }}, timeout=15.0)
            downloaded = base64.b64decode(download_result.get("data_b64", ""), validate=True)
            if download_result.get("name") != download_name or not downloaded:
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
        if arguments.expect_disconnect:
            result["afterHold"] = wait_for_disconnect(peer, arguments.hold_seconds)
        elif arguments.hold_seconds:
            time.sleep(arguments.hold_seconds)
            result["afterHold"] = peer.request({"op": "status"}, timeout=10.0)
        print("QSM_LAB_DIRECT_CHROME_E2E " + json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0
    finally:
        peer.close()


if __name__ == "__main__":
    raise SystemExit(main())
