#!/usr/bin/env python3
"""Prove that an unmodified Chromium decodes the browser bridge's H.264.

This is deliberately a local media-transport gate, before the PVE API adapter
is wired in.  Chromium creates the recv-only SDP offer; the bridge answers it
and gets valid H.264 access units through the same owner-private Unix tap that
a headless patched Moonlight process uses in production.  No browser
extension, native browser helper, PVE login, or decoded video frame appears in
the test path.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extensions.browser_bridge.qsm_browser_bridge import (  # noqa: E402
    BrowserWebRtcBridge,
    PACKET_END,
    PACKET_FIRST,
    PACKET_HEADER,
    PACKET_IDR,
    PACKET_MAGIC,
)


class QualificationError(RuntimeError):
    """The actual browser did not receive the expected encoded video."""


def _run_ffmpeg_h264(destination: Path) -> bytes:
    """Generate deterministic 64x48 H.264 access units with AUD boundaries."""
    subprocess.run([
        "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=30",
        "-frames:v", "120", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-x264-params", "aud=1:keyint=12:min-keyint=12:scenecut=0:repeat-headers=1",
        "-f", "h264", os.fspath(destination),
    ], check=True)
    return destination.read_bytes()


def _annex_b_nals(encoded: bytes) -> list[tuple[int, bytes]]:
    """Return start-code-prefixed NALs; strict enough for this test fixture."""
    starts: list[tuple[int, int]] = []
    index = 0
    while index + 3 < len(encoded):
        if encoded[index:index + 4] == b"\x00\x00\x00\x01":
            starts.append((index, 4))
            index += 4
        elif encoded[index:index + 3] == b"\x00\x00\x01":
            starts.append((index, 3))
            index += 3
        else:
            index += 1
    if not starts or starts[0][0] != 0:
        raise QualificationError("ffmpeg did not produce Annex-B H.264")
    result: list[tuple[int, bytes]] = []
    for position, (start, prefix) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(encoded)
        if start + prefix >= end:
            raise QualificationError("empty H.264 NAL in fixture")
        result.append((encoded[start + prefix] & 0x1f, encoded[start:end]))
    return result


def _access_units(encoded: bytes) -> list[tuple[bool, bytes]]:
    """Group a stream containing AUD NALs into complete Annex-B access units."""
    pending: list[bytes] = []
    leading: list[bytes] = []
    result: list[tuple[bool, bytes]] = []

    def finish(parts: list[bytes]) -> None:
        if not parts:
            return
        joined = b"".join(parts)
        keyframe = any((part[4] if part.startswith(b"\x00\x00\x00\x01") else part[3]) & 0x1f == 5
                       for part in parts)
        result.append((keyframe, joined))

    for nal_type, nal in _annex_b_nals(encoded):
        if nal_type == 9:  # Access Unit Delimiter, emitted by libx264 above.
            finish(pending)
            pending = [*leading, nal]
            leading = []
        elif not pending:
            leading.append(nal)
        else:
            pending.append(nal)
    finish(pending)
    if len(result) < 100 or not any(keyframe for keyframe, _data in result):
        raise QualificationError("incomplete H.264 access-unit fixture")
    return result


class _BrowserPeer:
    """Strict JSON-lines wrapper around the local Playwright peer helper."""

    def __init__(self) -> None:
        chrome = Path("/usr/bin/google-chrome")
        if not chrome.is_file() or not os.access(chrome, os.X_OK):
            raise QualificationError("ordinary Google Chrome is required for the H.264 browser gate")
        environment = {**os.environ, "QSM_BROWSER_E2E_EXECUTABLE": os.fspath(chrome)}
        self._process = subprocess.Popen(
            ["/usr/bin/node", os.fspath(ROOT / "lab/proxmox9/browser-webrtc-receiver.cjs")],
            cwd=os.fspath(ROOT / "lab/proxmox9"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=environment,
        )

    def request(self, payload: dict[str, Any], *, timeout: float = 12.0) -> dict[str, Any]:
        if self._process.stdin is None or self._process.stdout is None:
            raise QualificationError("browser test peer has no control pipes")
        self._process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._process.stdin.flush()
        readable, _writable, _exceptional = select.select([self._process.stdout], [], [], timeout)
        if not readable:
            raise QualificationError("browser test peer timed out")
        response = self._process.stdout.readline()
        try:
            message = json.loads(response)
        except json.JSONDecodeError as error:
            raise QualificationError("browser test peer returned invalid JSON") from error
        if not isinstance(message, dict) or message.get("ok") is not True or \
                not isinstance(message.get("result"), dict):
            raise QualificationError(f"browser test peer failed: {message.get('error', 'unknown error')}")
        return message["result"]

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


def _send_access_unit(path: str, frame_number: int, keyframe: bool, payload: bytes) -> None:
    flags = PACKET_FIRST | PACKET_END | (PACKET_IDR if keyframe else 0)
    packet = PACKET_HEADER.pack(PACKET_MAGIC, frame_number, 0, flags, len(payload)) + payload
    producer = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        producer.connect(path)
        sent = producer.sendmsg([packet])
        if sent != len(packet):
            raise QualificationError("short local encoded-video tap send")
    finally:
        producer.close()


async def qualify() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="qsm-browser-chromium.") as temporary:
        runtime_directory = Path(temporary)
        bridge = BrowserWebRtcBridge(runtime_directory, fps=30)
        browser = _BrowserPeer()
        try:
            video_context, _audio_context = bridge.start_taps()
            offer = browser.request({"op": "offer"})
            answer = await bridge.answer_offer(str(offer["sdp"]), str(offer["type"]))
            browser.request({"op": "answer", "answer": answer})

            access_units = _access_units(_run_ffmpeg_h264(runtime_directory / "fixture.h264"))
            deadline = time.monotonic() + 12.0
            sent = 0
            observed: dict[str, Any] = {}
            while time.monotonic() < deadline:
                keyframe, payload = access_units[sent % len(access_units)]
                _send_access_unit(video_context.removeprefix("unix:"), sent + 1, keyframe, payload)
                sent += 1
                if sent % 6 == 0:
                    observed = browser.request({"op": "status"}, timeout=3)
                    if (observed.get("connectionState") == "connected" and
                            observed.get("videoWidth") == 64 and
                            observed.get("videoHeight") == 48 and
                            float(observed.get("currentTime", 0)) > 0):
                        bridge.ingress.raise_if_failed()
                        return {"sent_access_units": sent, "browser": observed}
                await asyncio.sleep(1 / 30)
            raise QualificationError(f"Chromium did not decode bridge H.264: {observed}")
        finally:
            browser.close()
            await bridge.close()


def main() -> int:
    try:
        evidence = asyncio.run(qualify())
    except (OSError, subprocess.SubprocessError, QualificationError) as error:
        print(f"QSM_BROWSER_WEBRTC_ENCODED_E2E_FAILED: {error}", file=sys.stderr)
        return 1
    print("QSM_BROWSER_WEBRTC_ENCODED_E2E_OK " + json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
