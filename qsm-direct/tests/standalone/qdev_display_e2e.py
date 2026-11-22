#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Browser gate for a running guest whose screen is a qdev-host display device.

Run it as the Unix user who owns the guest (its QEMU and qdev-host processes),
for example an accelerated macOS guest with a qdev-host GPU. It never starts,
stops or reconfigures the guest. The real host-login server, user agent,
discovery, media worker and browser page are used; only the PAM conversation
is replaced, inside this test process, by a one-time random password for the
caller's own account, because this gate must not handle the account's real
password. Real PAM is covered by host_browser_e2e.py; this gate is not a PAM
test and no shipped code contains the substitution.

The gate checks that the guest is listed as a qdev-host display, that its
video plays and that keyboard and mouse input reach the guest. Input is
observed with ``--idle-probe``, a command that prints the guest's input idle
time in seconds (for macOS: ``ioreg -c IOHIDSystem`` HIDIdleTime over SSH).
"""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import pwd
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright  # noqa: E402

from extensions.standalone import host_server, server  # noqa: E402


def screendump(control, path):
    """qdev-host's own picture (control socket ``screendump FILE``), for the before/after record."""
    if control is None:
        return None
    with socket.socket(socket.AF_UNIX) as link:
        link.settimeout(8)
        link.connect(str(control))
        link.sendall(f"screendump {path}\n".encode())
        return link.recv(4096).decode(errors="replace").strip()


def idle(command):
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return float(result.stdout.strip().split()[-1])


async def qualify(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    account = pwd.getpwuid(os.geteuid())
    secret = secrets.token_urlsafe(24)
    used = []

    async def one_time_password(_service, payload, _remote):
        # Test-only stand-in for the PAM helper: the caller's own account and a
        # random password that exists only in this process, accepted once.
        if used or payload.get("username") != account.pw_name or payload.get("password") != secret:
            return None
        used.append(True)
        return {"uid": account.pw_uid, "gid": account.pw_gid, "name": account.pw_name}
    host_server.authenticate_pam = one_time_password

    report = {"result": "FAIL", "account": account.pw_name, "guest": args.guest,
              "pam": "replaced by a one-time in-process password (not a PAM test)"}
    with tempfile.TemporaryDirectory(prefix="qsm-qdev-e2e-") as temporary:
        runtime = Path(temporary)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(runtime / "key.pem"), "-out", str(runtime / "cert.pem"),
                        "-subj", "/CN=localhost"], check=True, capture_output=True)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        options = server.arguments(["--auth", "pam", "--pam-service", args.pam_service, "--worker", str(args.worker), "--port", str(port),
                                    "--fps", str(args.fps), "--tls-cert", str(runtime / "cert.pem"),
                                    "--tls-key", str(runtime / "key.pem")])
        serving = asyncio.create_task(server.run(options))
        origin = f"https://127.0.0.1:{port}"
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=args.browser, headless=True,
                                                           args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    context = await browser.new_context(ignore_https_errors=True,
                                                        viewport={"width": 1600, "height": 960})
                    page = await context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    for _ in range(100):
                        try:
                            await page.goto(origin)
                            break
                        except Exception:
                            await asyncio.sleep(.1)
                    await page.locator("#username").fill(account.pw_name)
                    await page.locator("#password").fill(secret)
                    await page.get_by_role("button", name="Sign in", exact=True).click()
                    await page.get_by_role("heading", name="Your guests", exact=True).wait_for()
                    link = page.get_by_role("link", name="Open console: " + args.guest, exact=True)
                    await link.first.wait_for(timeout=20000)
                    rows = await page.locator("#guest-list li").all_text_contents()
                    report["guest_list"] = rows
                    await page.screenshot(path=str(output / "guest-list.png"))
                    device_rows = [row for row in rows if args.guest in row and "qdev-host display" in row]
                    assert len(device_rows) == 1, f"expected one qdev-host display row for {args.guest}: {rows}"
                    hrefs = [await item.get_attribute("href") for item in await link.all()]
                    device_href = next(href for href in hrefs if href.count("-") == 3)
                    report["guest_id"] = device_href.split("guest=", 1)[1]
                    started = time.monotonic()
                    await page.goto(origin + device_href)
                    await page.wait_for_function("""() => {
                        const video = document.querySelector('video');
                        return video && video.readyState >= 2 && video.videoWidth > 0 && video.currentTime > 0;
                    }""", timeout=45000)
                    report["first_video_s"] = round(time.monotonic() - started, 2)
                    await asyncio.sleep(2)
                    frame = await page.evaluate("""() => {
                        const video = document.querySelector('video');
                        const canvas = document.createElement('canvas');
                        canvas.width = video.videoWidth; canvas.height = video.videoHeight;
                        canvas.getContext('2d').drawImage(video, 0, 0);
                        return {width: video.videoWidth, height: video.videoHeight,
                                png: canvas.toDataURL('image/png'),
                                frames: video.getVideoPlaybackQuality?.().totalVideoFrames ?? null};
                    }""")
                    (output / "video-frame.png").write_bytes(base64.b64decode(frame["png"].split(",", 1)[1]))
                    report["video"] = {"width": frame["width"], "height": frame["height"],
                                       "decoded_frames": frame["frames"]}
                    await page.screenshot(path=str(output / "console.png"))
                    if args.idle_probe:
                        deadline = time.monotonic() + 30
                        while (before := idle(args.idle_probe)) < 4 and time.monotonic() < deadline:
                            await asyncio.sleep(1)
                        report["idle_before_keyboard_s"] = before
                        report["screendump_before_keyboard"] = screendump(args.control, output / "guest-before-key.ppm")
                        await page.locator("video").focus()
                        # A printable key: a lone modifier is a viewer shortcut gesture, not guest input.
                        await page.keyboard.press(args.key)
                        await asyncio.sleep(.7)
                        report["screendump_after_keyboard"] = screendump(args.control, output / "guest-after-key.ppm")
                        after = idle(args.idle_probe)
                        report["idle_after_keyboard_s"] = after
                        assert before >= 4 and after < 2, "keyboard input did not reach the guest"
                        deadline = time.monotonic() + 30
                        while (before := idle(args.idle_probe)) < 4 and time.monotonic() < deadline:
                            await asyncio.sleep(1)
                        report["idle_before_mouse_s"] = before
                        box = await page.locator("video").bounding_box()
                        for step in range(8):
                            await page.mouse.move(box["x"] + box["width"] * (.05 + step * .01),
                                                  box["y"] + box["height"] * .5)
                            await asyncio.sleep(.05)
                        await page.mouse.click(box["x"] + box["width"] * .12, box["y"] + box["height"] * .5)
                        await asyncio.sleep(.7)
                        report["screendump_after_mouse"] = screendump(args.control, output / "guest-after-mouse.ppm")
                        after = idle(args.idle_probe)
                        report["idle_after_mouse_s"] = after
                        assert before >= 4 and after < 2, "mouse input did not reach the guest"
                        report["input_path"] = "browser -> QSM worker -> qdev-host Display1 Console_0 -> QMP input-send-event"
                    frames = await page.evaluate(
                        "() => document.querySelector('video').getVideoPlaybackQuality?.().totalVideoFrames ?? null")
                    report["video"]["decoded_frames_end"] = frames
                    await page.screenshot(path=str(output / "console-after-input.png"))
                    assert not errors, errors
                    report["page_errors"] = errors
                    report["browser"] = browser.version
                    report["result"] = "PASS"
                except Exception as error:
                    report["error"] = repr(error)
                finally:
                    await browser.close()
        finally:
            os.kill(os.getpid(), signal.SIGTERM)  # server.run stops on its SIGTERM handler
            await asyncio.wait_for(serving, 20)
            (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)
    if report["result"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--guest", required=True, help="the guest's QEMU -name")
    parser.add_argument("--idle-probe", help="shell command printing the guest's input idle seconds")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--key", default="a", help="printable key typed into the guest's focused field")
    parser.add_argument("--control", type=Path, help="qdev-host --control socket for before/after screendumps")
    parser.add_argument("--pam-service", default="qsm-standalone",
                        help="an installed root-owned PAM policy name (checked by the server, not used here)")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(qualify(parser.parse_args()))
