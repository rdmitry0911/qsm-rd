#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authenticated browser console for an already running, non-PVE QEMU."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import ssl
import stat
import tempfile
from urllib.parse import urlsplit

from aiohttp import web

from extensions.browser_bridge.qsm_browser_bridge import BrowserWebRtcBridge, BridgeError

ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent
VIEWER = ROOT / "integration/proxmox/pve9/direct_ui/qsm-direct-console.js"
LOG = logging.getLogger("qsm-standalone")
MAX_BODY = 128 * 1024


def read_token(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077):
            raise ValueError("token file must be a private regular file owned by this user (mode 0600)")
        token = os.read(fd, 258).decode("ascii").strip()
    finally:
        os.close(fd)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("token must contain 32..256 URL-safe ASCII characters")
    return token


def network_policy(args: argparse.Namespace) -> tuple[str, frozenset[str]]:
    address = ipaddress.ip_address(args.listen)
    tls = args.tls_cert is not None
    if tls != (args.tls_key is not None):
        raise ValueError("--tls-cert and --tls-key must be supplied together")
    if not address.is_loopback and not tls:
        raise ValueError("a non-loopback listener requires --tls-cert and --tls-key")
    host = f"[{address}]" if address.version == 6 else str(address)
    origin = args.public_url or f"{'https' if tls else 'http'}://{host}:{args.port}"
    parts = urlsplit(origin)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or
            parts.username is not None or parts.password is not None or
            parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("--public-url must be an HTTP(S) origin without a path or credentials")
    # This listener terminates TLS itself; do not infer trust from proxy headers.
    if parts.scheme != ("https" if tls else "http"):
        raise ValueError("--public-url scheme must match the listener's TLS setting")
    if address.is_unspecified and not args.public_url:
        raise ValueError("a wildcard listener requires --public-url with its browser-facing hostname")
    if not tls and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("HTTP is supported only on localhost; use HTTPS for remote browsers")
    origin = f"{parts.scheme}://{parts.netloc.lower()}"
    origins = {origin}
    if address.is_loopback and not args.public_url:
        origins.add(f"{parts.scheme}://localhost:{args.port}")
    return origin, frozenset(origins)


class ConsoleServer:
    """One guest and one browser at a time; QEMU's lifecycle belongs to its owner."""

    def __init__(self, args: argparse.Namespace, runtime: Path, token: str,
                 origins: frozenset[str]) -> None:
        self.args = args
        self.runtime = runtime
        self.token = token
        self.origins = origins
        self.hosts = {urlsplit(origin).netloc for origin in origins}
        self.session: ConsoleSession | None = None
        self.lock = asyncio.Lock()
        self.closing = False

    @web.middleware
    async def boundary(self, request: web.Request, handler):
        if request.host.lower() not in self.hosts:
            raise web.HTTPForbidden(text="Unrecognised console host")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in self.origins:
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed")
        if request.path.startswith("/api/"):
            bearer = request.headers.get("Authorization", "")
            if (len(bearer) > 264 or not bearer.isascii() or
                    not hmac.compare_digest(bearer, f"Bearer {self.token}")):
                raise web.HTTPUnauthorized(text="Console access token required")
        try:
            response = await handler(request)
        except web.HTTPException as error:
            response = error
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data: blob:; media-src 'self' blob:; worker-src blob:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        if isinstance(response, web.HTTPException):
            raise response
        return response

    async def asset(self, request: web.Request) -> web.Response:
        files = {"/": (ASSETS / "index.html", "text/html"),
                 "/standalone.js": (ASSETS / "standalone.js", "text/javascript"),
                 "/console.js": (VIEWER, "text/javascript")}
        path, kind = files[request.path]
        return web.Response(body=path.read_bytes(), content_type=kind)

    async def status(self, _request: web.Request) -> web.Response:
        # There is no VM manager to poll: attachment verifies Display1 during
        # the offer. A failed attachment never controls or restarts QEMU.
        return web.json_response({"data": {"status": "running", "name": self.args.name,
                                          "fps": self.args.fps}})

    async def offer(self, request: web.Request) -> web.Response:
        try:
            offer = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Invalid WebRTC offer") from None
        if (not isinstance(offer, dict) or set(offer) != {"sdp", "width", "height", "fps"} or
                not isinstance(offer["sdp"], str) or not offer["sdp"].isascii() or
                not 1 <= len(offer["sdp"]) <= 96 * 1024 or "\x00" in offer["sdp"] or
                any(type(offer[k]) is not int or not low <= offer[k] <= high
                    for k, low, high in (("width", 64, 16384), ("height", 64, 16384), ("fps", 10, 240)))):
            raise web.HTTPBadRequest(text="Invalid WebRTC offer")
        # Reserve before the first await; a second tab cannot start another
        # encoder or consume the first browser's input authority.
        async with self.lock:
            if self.closing:
                raise web.HTTPServiceUnavailable(text="Console server is stopping")
            if self.session is not None:
                raise web.HTTPConflict(text="This guest console is in use. Close its other browser tab first.")
            session = ConsoleSession(self)
            self.session = session
        try:
            answer = await session.start(offer)
            return web.json_response({"data": {**answer, "session": session.id}})
        except BridgeError as error:
            LOG.warning("Console negotiation failed: %s", error)
            await session.close()
            raise web.HTTPBadRequest(text=str(error)) from None
        except (OSError, TimeoutError, RuntimeError) as error:
            LOG.error("Could not attach the QEMU display worker: %s", error)
            await session.close()
            raise web.HTTPBadGateway(text="Could not attach to QEMU Display1. Check the D-Bus address and server log.") from None
        except asyncio.CancelledError:
            # Shutdown's cleanup task may be waiting for this request to end.
            if not session.closed:
                await session.close()
            raise
        except BaseException:
            await session.close()
            raise

    async def disconnect(self, request: web.Request) -> web.Response:
        session = self.session
        if session is not None and hmac.compare_digest(request.match_info["session"], session.id):
            await session.close()
        return web.Response(status=204)

    async def close(self, _app: web.Application) -> None:
        self.closing = True
        if self.session is not None:
            await self.session.close()

    def application(self) -> web.Application:
        app = web.Application(middlewares=[self.boundary], client_max_size=MAX_BODY)
        for path in ("/", "/standalone.js", "/console.js"):
            app.router.add_get(path, self.asset)
        app.router.add_get("/api/status", self.status)
        app.router.add_post("/api/offer", self.offer)
        app.router.add_delete("/api/console/{session:[0-9a-f]{32}}", self.disconnect)
        app.on_shutdown.append(self.close)
        return app


class ConsoleSession:
    def __init__(self, server: ConsoleServer) -> None:
        self.server = server
        self.id = secrets.token_hex(16)
        self.directory = Path(tempfile.mkdtemp(prefix="peer-", dir=server.runtime))
        self.bridge: BrowserWebRtcBridge | None = None
        self.worker: asyncio.subprocess.Process | None = None
        self.watch: asyncio.Task | None = None
        self.starting: asyncio.Task | None = None
        self.cleanup: asyncio.Task | None = None
        self.closed = False

    async def start(self, offer: dict) -> dict[str, str]:
        self.starting = asyncio.current_task()
        try:
            return await self._start(offer)
        finally:
            self.starting = None

    async def _start(self, offer: dict) -> dict[str, str]:
        args = self.server.args
        self.bridge = BrowserWebRtcBridge(
            self.directory, fps=args.fps, video_codec="h264", video_bitrate_kbps=args.bitrate)
        video, audio = self.bridge.start_taps()
        command = [str(args.worker), "--dbus-address", args.dbus_address,
                   "--video-socket", video, "--audio-socket", audio,
                   "--input-socket", self.bridge.input_context,
                   "--codec", "h264", "--encoder", args.encoder,
                   "--video-bitrate-kbps", str(args.bitrate), "--fps", str(args.fps),
                   "--initial-size", f"{offer['width'] // 2 * 2}x{offer['height'] // 2 * 2}",
                   "--allow-unsupported-ui-info"]
        for option in ("vaapi_device", "capture_render_node"):
            value = getattr(args, option)
            if value is not None:
                command.extend(["--" + option.replace("_", "-"), str(value)])
        # The worker is the only child we own. Never signal the attached QEMU.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(*command))
        try:
            self.worker = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # Retain the child handle even when shutdown interrupts spawn.
            result = (await asyncio.gather(spawn, return_exceptions=True))[0]
            if isinstance(result, asyncio.subprocess.Process):
                self.worker = result
            raise
        answer_task = asyncio.create_task(self.bridge.answer_offer(offer["sdp"]))
        exit_task = asyncio.create_task(self.worker.wait())
        try:
            done, _ = await asyncio.wait((answer_task, exit_task), timeout=25,
                                         return_when=asyncio.FIRST_COMPLETED)
            if exit_task in done:
                raise RuntimeError("QEMU display worker exited during attachment")
            if answer_task not in done:
                raise TimeoutError("WebRTC offer timed out")
            answer = answer_task.result()
        finally:
            for task in (answer_task, exit_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(answer_task, exit_task, return_exceptions=True)
        if self.closed:
            raise RuntimeError("console closed during negotiation")
        self.watch = asyncio.create_task(self.monitor())
        return answer

    async def monitor(self) -> None:
        connected = False
        deadline = asyncio.get_running_loop().time() + 30
        try:
            while not self.closed:
                assert self.bridge is not None and self.worker is not None
                state = self.bridge.connection_state
                connected = connected or state == "connected"
                if (self.worker.returncode is not None or self.bridge.terminal or
                        (not connected and asyncio.get_running_loop().time() > deadline)):
                    break
                await asyncio.sleep(0.5)
        finally:
            if not self.closed:
                await self.close()

    async def close(self) -> None:
        if self.cleanup is None:
            self.closed = True
            self.cleanup = asyncio.create_task(self._close(asyncio.current_task()))
        # Every caller waits for the same cleanup; cancellation of a closing
        # browser request cannot abandon the worker during server shutdown.
        await asyncio.shield(self.cleanup)

    async def _close(self, caller: asyncio.Task | None) -> None:
        if self.starting is not None and self.starting is not caller:
            self.starting.cancel()
            await asyncio.gather(self.starting, return_exceptions=True)
        if self.watch is not None and self.watch is not caller:
            self.watch.cancel()
            await asyncio.gather(self.watch, return_exceptions=True)
        try:
            if self.bridge is not None:
                await self.bridge.close()
        finally:
            if self.worker is not None and self.worker.returncode is None:
                try:
                    self.worker.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.worker.wait(), 5)
                except asyncio.TimeoutError:
                    try:
                        self.worker.kill()
                    except ProcessLookupError:
                        pass
                    await self.worker.wait()
            shutil.rmtree(self.directory)
            async with self.server.lock:
                if self.server.session is self:
                    self.server.session = None


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dbus-address", help="QEMU's local Unix D-Bus address")
    source.add_argument("--dbus-address-file", type=Path, help="file containing that address")
    parser.add_argument("--worker", type=Path, default=shutil.which("qsm-direct-media-worker"),
                        help="path to qsm-direct-media-worker (or put it on PATH)")
    parser.add_argument("--listen", default="127.0.0.1", help="bind IP (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=18008)
    parser.add_argument("--public-url", help="browser-facing origin; required for wildcard binds")
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--token-file", type=Path, help="private file with an existing access token")
    parser.add_argument("--name", default="QEMU", help="guest name shown in the browser")
    parser.add_argument("--encoder", default="libx264", choices=("libx264", "h264_vaapi", "h264_qsv", "h264_nvenc"))
    parser.add_argument("--vaapi-device", type=Path)
    parser.add_argument("--capture-render-node", type=Path)
    parser.add_argument("--bitrate", type=int, default=8000, help="video ceiling in kbit/s (default: 8000)")
    parser.add_argument("--fps", type=int, default=60)
    args = parser.parse_args(argv)
    try:
        if args.worker is None or not os.access(args.worker, os.X_OK) or not args.worker.is_file():
            raise ValueError("build qsm-direct-media-worker and pass its path with --worker")
        args.worker = args.worker.resolve()
        if not 1 <= args.port <= 65535 or not 10 <= args.fps <= 240 or not 500 <= args.bitrate <= 100000:
            raise ValueError("port must be 1..65535, fps 10..240, bitrate 500..100000")
        if args.encoder == "h264_vaapi" and args.vaapi_device is None:
            raise ValueError("h264_vaapi requires --vaapi-device")
        if args.dbus_address_file is not None:
            args.dbus_address = args.dbus_address_file.read_text().strip()
        if not args.dbus_address.startswith("unix:") or len(args.dbus_address) > 4096 or "\n" in args.dbus_address:
            raise ValueError("Display1 requires a local unix: D-Bus address on the QEMU host")
        args.origin, args.origins = network_policy(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return args


async def run(args: argparse.Namespace) -> None:
    # Short paths also leave room for the bridge's Unix socket names.
    with tempfile.TemporaryDirectory(prefix="qsm-") as directory:
        runtime = Path(directory)
        token_file = args.token_file
        if token_file is None:
            token_file = runtime / "access-token"
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32) + "\n")
        token = read_token(token_file)
        server = ConsoleServer(args, runtime, token, args.origins)
        runner = web.AppRunner(server.application(), access_log=None)
        await runner.setup()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stopped.set)
        context = None
        try:
            if args.tls_cert is not None:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(args.tls_cert, args.tls_key)
            site = web.TCPSite(runner, args.listen, args.port, ssl_context=context)
            await site.start()
            print(f"QSM console: {args.origin}/", flush=True)
            print(f"Access token file: {token_file}", flush=True)
            await stopped.wait()
        finally:
            await runner.cleanup()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signum)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    try:
        asyncio.run(run(arguments()))
    except (OSError, ValueError) as error:
        raise SystemExit(f"qsm-standalone: {error}") from None


if __name__ == "__main__":
    main()
