#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real PAM + two Unix owners + QEMU + browser gate. Run explicitly as root.

Creates unique temporary accounts, assigns random test-only passwords through
chpasswd's stdin, and removes those accounts and their processes in finally.
Use a root-owned readable installation so the user agents can import QSM.
"""
import argparse
import asyncio
import io
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

import aiohttp
from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]


async def until(check, message, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(.1)
    raise AssertionError(message)


def children(pid):
    return [int(value) for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]


def uid(pid):
    return Path(f"/proc/{pid}").stat().st_uid


async def qualify(args):
    if os.geteuid() != 0:
        raise SystemExit("This explicit multiuser PAM gate needs root for temporary accounts")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    accounts, processes, logs = [], [], []
    errors = []
    with tempfile.TemporaryDirectory(prefix="qsm-pam-") as temporary:
        runtime = Path(temporary)
        runtime.chmod(0o711)
        async def launch(name, command, account=None):
            log = (output / f"{name}.log").open("wb")
            logs.append(log)
            credentials = {} if account is None else {
                "user": account.pw_uid, "group": account.pw_gid, "extra_groups": []}
            process = await asyncio.create_subprocess_exec(
                *command, stdout=log, stderr=log, cwd=args.server_root, start_new_session=True,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": str(args.server_root),
                     "PYTHONNOUSERSITE": "1"}, **credentials)
            processes.append(process)
            return process

        try:
            subprocess.run(["as", "--32", "-o", str(runtime / "boot.o"),
                            str(ROOT / "tests/fixtures/qsm_standalone_input.S")], check=True, capture_output=True)
            subprocess.run(["ld", "-m", "elf_i386", "-Ttext", "0x7c00", "--oformat", "binary",
                            "-e", "_start", "-o", str(runtime / "boot.bin"), str(runtime / "boot.o")],
                           check=True, capture_output=True)
            boot = (runtime / "boot.bin").read_bytes()
            assert len(boot) == 512 and boot[-2:] == b"\x55\xaa"
            guests = []
            passwords = {}
            for suffix in ("a", "b"):
                username = "qsmtest" + secrets.token_hex(4) + suffix
                subprocess.run(["useradd", "--no-create-home", "--user-group", "--shell", "/bin/sh",
                                "--home-dir", str(runtime / username), username], check=True, capture_output=True)
                accounts.append(username)
                password = secrets.token_urlsafe(24)
                passwords[username] = password
                subprocess.run(["chpasswd"], input=f"{username}:{password}\n", text=True,
                               check=True, capture_output=True)
                account = pwd.getpwnam(username)
                home = runtime / username
                home.mkdir(mode=0o700)
                os.chown(home, account.pw_uid, account.pw_gid)
                image = home / "boot.img"
                image.write_bytes(boot + bytes(1474560 - len(boot)))
                image.chmod(0o600)
                os.chown(image, account.pw_uid, account.pw_gid)
                address = f"unix:path={home}/bus"
                await launch("dbus-" + suffix, ["dbus-daemon", "--session", "--nofork", "--address=" + address], account)
                async def bus_ready():
                    return (home / "bus").exists()
                await until(bus_ready, "test user's D-Bus did not start")
                qemu = await launch("qemu-" + suffix, [
                    "qemu-system-x86_64", "-name", "Guest " + suffix.upper(), "-accel", "tcg",
                    "-machine", "pc", "-m", "128", "-vga", "std", "-display", "dbus,addr=" + address + ",gl=off",
                    "-monitor", "none", "-serial", "none", "-no-reboot", "-no-shutdown",
                    "-drive", f"file={image},if=floppy,format=raw,readonly=on"], account)
                guests.append((account, qemu))
            await launch("hidden-qemu", ["qemu-system-x86_64", "-name", "Hidden guest", "-S", "-machine", "none",
                                         "-display", "none", "-monitor", "none", "-serial", "none"], guests[0][0])
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(runtime / "key.pem"), "-out", str(runtime / "cert.pem"),
                            "-subj", "/CN=localhost"], check=True, capture_output=True)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            origin = f"https://127.0.0.1:{port}"
            server = await launch("server", [
                str(args.python), "-m", "extensions.standalone.server", "--auth", "pam",
                "--worker", str(args.worker), "--port", str(port), "--fps", "30",
                "--tls-cert", str(runtime / "cert.pem"), "--tls-key", str(runtime / "key.pem")])
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
                async def ready():
                    if server.returncode is not None:
                        raise AssertionError("PAM console server stopped; see server.log")
                    try:
                        async with http.get(origin + "/config") as response:
                            return response.status == 200
                    except aiohttp.ClientError:
                        return False
                await until(ready, "PAM server did not start")
                async with http.post(origin + "/api/login", json={"username": accounts[0], "password": "wrong"},
                                     headers={"Origin": origin}) as response:
                    assert response.status == 401
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=args.browser, headless=True,
                                                           args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    contexts = []
                    pages = []
                    identifiers = []
                    for index, (account, qemu) in enumerate(guests):
                        context = await browser.new_context(ignore_https_errors=True,
                                                            viewport={"width": 1280, "height": 800})
                        contexts.append(context)
                        page = await context.new_page()
                        pages.append(page)
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(origin)
                        await page.locator("#username").fill(account.pw_name)
                        await page.locator("#password").fill(passwords[account.pw_name])
                        await page.get_by_role("button", name="Sign in", exact=True).click()
                        await page.get_by_role("heading", name="Your guests", exact=True).wait_for()
                        await page.get_by_role("link", name="Open console: Guest " + "AB"[index], exact=True).wait_for()
                        rows = await page.locator("#guest-list li").all_text_contents()
                        assert len(rows) == 1 and "Guest " + "AB"[index] in rows[0], "guest owner isolation failed"
                        assert "Hidden guest" not in rows[0]
                        cookies = await context.cookies()
                        cookie = next(value for value in cookies if value["name"] == "qsm_host_session")
                        assert cookie["httpOnly"] and cookie["secure"] and cookie["sameSite"] == "Strict"
                        assert "qsm_host_session" not in await page.evaluate("document.cookie")
                        await page.reload()
                        link = page.get_by_role("link", name="Open console: Guest " + "AB"[index], exact=True)
                        await link.wait_for()
                        identifiers.append((await link.get_attribute("href")).split("guest=", 1)[1])
                        await page.screenshot(path=str(output / f"guest-list-{index}.png"))
                        await link.click()
                        await page.wait_for_function("""() => {
                            const video = document.querySelector('video');
                            return video && video.readyState >= 2 && video.videoWidth > 0 && video.currentTime > 0;
                        }""", timeout=30000)
                        assert await page.evaluate("typeof Ext === 'undefined' && typeof Proxmox === 'undefined'")
                        async def colour(side, channel):
                            image = Image.open(io.BytesIO(await page.screenshot())).convert("RGB")
                            pixel = image.getpixel((image.width // 4 * side, image.height // 2))
                            return pixel[channel] > 150 and all(pixel[channel] > pixel[i] + 70
                                                                for i in range(3) if i != channel)
                        await page.locator("video").focus()
                        await page.keyboard.press("a")
                        await until(lambda: colour(1, 1), "authenticated keyboard did not reach the guest")
                        await page.mouse.move(850, 450)
                        await page.mouse.click(900, 470)
                        await until(lambda: colour(3, 0), "authenticated mouse did not reach the guest")
                        await page.screenshot(path=str(output / f"console-{index}.png"))
                        agents = [pid for pid in children(server.pid) if uid(pid) == account.pw_uid]
                        assert len(agents) == 1
                        workers = children(agents[0])
                        assert len(workers) == 1 and uid(workers[0]) == account.pw_uid
                        print(f"REAL_PAM_GUEST_{index}_OWNER_AND_INPUT_OK", flush=True)

                    alice, bob = contexts
                    response = await alice.request.get(origin + f"/api/guests/{identifiers[1]}/status")
                    assert response.status == 404
                    response = await bob.request.get(origin + f"/api/guests/{identifiers[0]}/status")
                    assert response.status == 404
                    response = await alice.request.post(origin + "/api/logout", data={}, headers={"Origin": origin})
                    assert response.status == 403  # cookie alone cannot log out or control a guest
                    cookie = next(value for value in await alice.cookies() if value["name"] == "qsm_host_session")
                    saved_cookie = cookie["value"]
                    listing = await alice.new_page()
                    await listing.goto(origin)
                    await listing.get_by_role("button", name="Sign out", exact=True).click()
                    await listing.get_by_role("button", name="Sign in", exact=True).wait_for()
                    async def alice_gone():
                        return not any(uid(pid) == guests[0][0].pw_uid for pid in children(server.pid))
                    await until(alice_gone, "logout left the user's media agent running")
                    response = await alice.request.get(origin + "/api/guests", headers={
                        "Cookie": "qsm_host_session=" + saved_cookie})
                    assert response.status == 401
                    assert any(uid(pid) == guests[1][0].pw_uid for pid in children(server.pid))
                    assert all(qemu.returncode is None for _, qemu in guests)
                    subprocess.run(["chage", "--expiredate", "1970-01-02", accounts[0]], check=True, capture_output=True)
                    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
                        async with http.post(origin + "/api/login", json={"username": accounts[0],
                                "password": passwords[accounts[0]]}, headers={"Origin": origin}) as response:
                            assert response.status == 401
                    await pages[1].get_by_role("button", name="Guests", exact=True).evaluate("button => button.click()")
                    await pages[1].get_by_role("heading", name="Your guests", exact=True).wait_for()
                    await pages[1].get_by_role("link", name="Open console: Guest B", exact=True).click()
                    await pages[1].wait_for_function("() => document.querySelector('video')?.readyState >= 2", timeout=30000)
                    agents = children(server.pid)
                    workers = [pid for agent in agents for pid in children(agent)]
                    server.terminate()
                    await asyncio.wait_for(server.wait(), 12)
                    assert all(not Path(f"/proc/{pid}").exists() for pid in agents + workers)
                    assert all(qemu.returncode is None for _, qemu in guests)
                    assert not errors, errors
                    report = {"result": "PASS", "real_pam_password_login": True,
                              "wrong_password_rejected": True, "expired_account_rejected": True,
                              "own_guests_only": True, "guest_without_display1_hidden": True,
                              "foreign_guest_rejected": True, "session_cookie_http_only_secure_strict": True,
                              "csrf_required": True, "reload_keeps_login": True,
                              "media_bridge_and_encoder_run_as_owner": True,
                              "keyboard_and_mouse_visible_in_two_real_guests": True,
                              "logout_closes_only_its_owner": True, "revoked_cookie_rejected": True,
                              "return_to_guests_and_reconnect": True,
                              "server_shutdown_reaps_agents_and_workers": True, "qemu_left_running": True,
                              "browser": browser.version, "page_errors": errors}
                    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
                    print(json.dumps(report, indent=2))
                finally:
                    await browser.close()
        finally:
            for process in reversed(processes):
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 12)
                    except asyncio.TimeoutError:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
            for log in logs:
                log.close()
            for username in reversed(accounts):
                subprocess.run(["userdel", username], check=True, capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=sys.executable)
    parser.add_argument("--server-root", type=Path, default=ROOT)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(qualify(parser.parse_args()))
