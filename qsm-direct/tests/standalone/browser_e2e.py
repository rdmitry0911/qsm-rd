#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real Chrome -> shipped viewer -> WebRTC -> worker -> QEMU guest gate.

Needs a built worker, qemu-system-x86_64, dbus-daemon, GNU as/ld, Google
Chrome, and the standalone Python requirements plus playwright and pillow.
Runs entirely under TCG; no Proxmox, guest OS image, KVM or GPU is needed.
"""
import argparse
import asyncio
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import aiohttp
from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]


async def until(check, message, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(message)


def children(pid):
    return Path(f"/proc/{pid}/task/{pid}/children").read_text().split()


async def qualify(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    processes, logs = [], []
    errors, console_errors = [], []
    # D-Bus and QMP sockets must stay below sockaddr_un's pathname limit.
    with tempfile.TemporaryDirectory(prefix="qsm-e2e-") as temporary:
        runtime = Path(temporary)
        token = secrets.token_urlsafe(32)
        token_file = runtime / "token"
        token_file.write_text(token)
        token_file.chmod(0o600)
        async def launch(command, name):
            log = (output / f"{name}.log").open("wb")
            logs.append(log)
            proc = await asyncio.create_subprocess_exec(*command, stdout=log, stderr=log)
            processes.append(proc)
            return proc

        try:
            for command in (
                ["as", "--32", "-o", str(runtime / "guest.o"),
                 str(ROOT / "tests/fixtures/qsm_standalone_input.S")],
                ["ld", "-m", "elf_i386", "-Ttext", "0x7c00", "--oformat", "binary",
                 "-e", "_start", "-o", str(runtime / "guest.bin"), str(runtime / "guest.o")],
            ):
                subprocess.run(command, check=True, capture_output=True)
            boot = (runtime / "guest.bin").read_bytes()
            assert len(boot) == 512 and boot[-2:] == b"\x55\xaa"
            (runtime / "guest.img").write_bytes(boot + bytes(1474560 - len(boot)))
            address = f"unix:path={runtime}/bus"
            await launch(["dbus-daemon", "--session", "--nofork", f"--address={address}"], "dbus")
            async def bus_ready():
                return (runtime / "bus").exists()
            await until(bus_ready, "private D-Bus did not start")
            qemu = await launch([
                args.qemu, "-accel", "tcg", "-machine", "pc", "-m", "128", "-vga", "std",
                "-display", f"dbus,addr={address},gl=off", "-monitor", "none", "-serial", "none",
                "-no-reboot", "-no-shutdown", "-drive",
                f"file={runtime}/guest.img,if=floppy,format=raw,readonly=on",
            ], "qemu")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            command = ["bash", str(ROOT / "scripts/run-qsm-standalone.sh"),
                       "--auth", "token",
                       "--worker", str(args.worker.resolve()), "--dbus-address", address,
                       "--port", str(port), "--fps", "30",
                       "--name", "Standalone E2E QEMU"]
            if args.tls:
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                                "-keyout", str(runtime / "key.pem"), "-out", str(runtime / "cert.pem"),
                                "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
                command.extend(["--tls-cert", str(runtime / "cert.pem"),
                                "--tls-key", str(runtime / "key.pem"),
                                "--listen", "0.0.0.0", "--public-url", f"https://127.0.0.1:{port}",
                                "--token-file", str(token_file)])
            # The launcher must use this very environment, including aiortc.
            os.environ["PYTHON"] = sys.executable
            server = await launch(command, "server")
            origin = f"{'https' if args.tls else 'http'}://127.0.0.1:{port}"
            async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"},
                                             connector=aiohttp.TCPConnector(ssl=False)) as http:
                generated_token = None
                async def server_ready():
                    nonlocal token, generated_token
                    if server.returncode is not None:
                        raise AssertionError("standalone server exited; see server.log")
                    if not args.tls:
                        lines = (output / "server.log").read_text().splitlines()
                        paths = [line.removeprefix("Access token file: ") for line in lines
                                 if line.startswith("Access token file: ")]
                        if not paths:
                            return False
                        generated_token = Path(paths[-1])
                        token = generated_token.read_text().strip()
                        http.headers["Authorization"] = f"Bearer {token}"
                    try:
                        async with http.get(origin + "/api/status") as response:
                            return response.status == 200
                    except aiohttp.ClientError:
                        return False
                await until(server_ready, "standalone server did not start")
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        executable_path=args.browser, headless=True,
                        args=["--no-sandbox", "--disable-dev-shm-usage"])
                    try:
                        context = await browser.new_context(viewport={"width": 1280, "height": 800},
                                                            ignore_https_errors=args.tls)
                        page = await context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("console", lambda message: console_errors.append(message.text)
                                if message.type == "error" and "401" not in message.text else None)

                        async def login(target):
                            await target.goto(origin)
                            await target.locator("#token").fill(token)
                            await target.get_by_role("button", name="Connect", exact=True).click()

                        async def colour(target, side, channel):
                            image = Image.open(io.BytesIO(await target.screenshot())).convert("RGB")
                            pixel = image.getpixel((image.width // 4 * (1 if side == "left" else 3), image.height // 2))
                            return pixel[channel] > 150 and all(pixel[channel] > pixel[i] + 70 for i in range(3) if i != channel)

                        await login(page)
                        await page.wait_for_function("""() => {
                            const v = document.querySelector('video');
                            return v && v.videoWidth > 0 && v.readyState >= 2 && v.currentTime > 0;
                        }""", timeout=25000)
                        assert await page.title() == "Standalone E2E QEMU — QSM Console"
                        assert await page.evaluate("typeof Ext === 'undefined' && typeof Proxmox === 'undefined'")
                        await until(lambda: colour(page, "left", 2), "guest did not paint the initial blue picture")
                        await page.screenshot(path=str(output / "before.png"))
                        await page.locator("video").focus()
                        await page.keyboard.press("a")
                        await until(lambda: colour(page, "left", 1), "browser keyboard did not reach the guest")
                        await page.screenshot(path=str(output / "keyboard.png"))
                        await until(lambda: colour(page, "right", 2), "mouse marker changed before browser input")
                        await page.mouse.move(850, 450)
                        await page.mouse.move(900, 470, steps=4)
                        await page.mouse.click(900, 470)
                        await until(lambda: colour(page, "right", 0), "browser mouse did not reach the guest")
                        await page.screenshot(path=str(output / "mouse.png"))
                        await page.set_viewport_size({"width": 1024, "height": 768})
                        await until(lambda: colour(page, "left", 1), "fixed-size VGA disconnected on browser resize")
                        await page.screenshot(path=str(output / "resized.png"))
                        print("REAL_GUEST_VIDEO_KEYBOARD_MOUSE_RESIZE_OK", flush=True)

                        other = await context.new_page()
                        await login(other)
                        await other.get_by_text("This guest console is in use.", exact=False).wait_for(timeout=15000)
                        await other.close()
                        assert len(children(server.pid)) == 1
                        # Match a user's close-tab action, including unload.
                        await page.close(run_before_unload=True)
                        async def no_worker():
                            return not children(server.pid)
                        await until(no_worker, "closing the browser left its worker running")
                        assert qemu.returncode is None
                        reopened = await context.new_page()
                        await login(reopened)
                        await until(lambda: colour(reopened, "left", 1), "reopened console did not show the running guest", 25)
                        await until(lambda: colour(reopened, "right", 0), "guest mouse state was lost on reconnect")
                        await reopened.screenshot(path=str(output / "reconnected.png"))
                        worker_pids = children(server.pid)
                        assert len(worker_pids) == 1
                        server.terminate()
                        await asyncio.wait_for(server.wait(), 10)
                        assert all(not Path(f"/proc/{pid}").exists() for pid in worker_pids)
                        assert generated_token is None or not generated_token.exists()
                        assert qemu.returncode is None, "server shutdown killed the attached QEMU"
                        # A 409 from the deliberate second-tab test is expected.
                        console_errors = [e for e in console_errors if "409" not in e]
                        assert not errors, errors
                        assert not console_errors, console_errors
                        report = {"result": "PASS", "tls": args.tls,
                                  "token_mode": "provided" if args.tls else "generated_and_removed",
                                  "qemu": subprocess.check_output([args.qemu, "--version"], text=True).splitlines()[0],
                                  "browser": browser.version, "accel": "tcg", "real_guest_input": True,
                                  "keyboard_visible_in_guest": True, "mouse_visible_in_guest": True,
                                  "fixed_vga_resize": True, "second_tab_refused": True,
                                  "reconnect": True, "worker_cleanup": True, "qemu_left_running": True,
                                  "no_proxmox_or_extjs": True, "page_errors": errors}
                        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
                        print(json.dumps(report, indent=2))
                    finally:
                        await browser.close()
        finally:
            for process in reversed(processes):
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 10)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
            for log in logs:
                log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory")
    parser.add_argument("--qemu", default=shutil.which("qemu-system-x86_64"))
    parser.add_argument("--browser", default=shutil.which("google-chrome"))
    parser.add_argument("--tls", action="store_true", help="exercise HTTPS with a throwaway certificate")
    asyncio.run(qualify(parser.parse_args()))
