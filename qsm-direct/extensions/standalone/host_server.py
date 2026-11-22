# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host-account login, short-lived web sessions and user-owned QEMU agents."""
import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import socket
import sys
import time

from aiohttp import web

from . import rpc
from .server import ConsoleServer, ROOT

COOKIE = "qsm_host_session"


class AgentReplyError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


async def authenticate_pam(service, payload, remote):
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("pam_auth.py")), service,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        data, _ = await asyncio.wait_for(process.communicate(json.dumps({
            **payload, "remote": remote}).encode("utf-8")), 15)
        return json.loads(data) if process.returncode == 0 and len(data) <= 4096 else None
    except (OSError, ValueError, asyncio.TimeoutError):
        return None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


@dataclass
class Login:
    id: str
    uid: int
    gid: int
    name: str
    csrf: str
    expires: float


class AgentConnection:
    def __init__(self, process, reader, writer):
        self.process, self.reader, self.writer = process, reader, writer
        self.lock = asyncio.Lock()

    async def request(self, method, login, **values):
        async with self.lock:
            try:
                await rpc.send(self.writer, {"method": method, "login": login.id, **values})
                reply = await asyncio.wait_for(rpc.receive(self.reader), 40)
            except asyncio.CancelledError:
                # A late reply must never become the next request's response.
                await self.close()
                raise
            except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                await self.close()
                raise web.HTTPServiceUnavailable(text="User console service is unavailable") from None
            if "error" in reply:
                raise AgentReplyError(reply["status"], reply["error"])
            return reply["data"]

    async def close(self):
        self.writer.close()
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.process.wait()


class HostConsoleServer(ConsoleServer):
    def __init__(self, args, runtime, origins):
        super().__init__(args, runtime, "", origins)
        self.logins = {}
        self.agents = {}
        self.agent_lock = asyncio.Lock()
        self.failures = defaultdict(deque)
        self.auth_slots = asyncio.Semaphore(4)
        self.reaper = None

    @staticmethod
    def key(token):
        return hashlib.sha256(token.encode("utf-8")).digest()

    def identity(self, request):
        token = request.cookies.get(COOKIE, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        login = self.logins.get(self.key(token))
        return login if login is not None and login.expires > time.monotonic() else None

    @web.middleware
    async def boundary(self, request, handler):
        if request.host.lower() not in self.hosts:
            raise web.HTTPForbidden(text="Unrecognised console host")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in self.origins:
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed")
        if request.path.startswith("/api/"):
            if request.method not in {"GET", "HEAD"} and origin not in self.origins:
                raise web.HTTPForbidden(text="Console origin required")
            if request.path != "/api/login":
                login = self.identity(request)
                if login is None:
                    raise web.HTTPUnauthorized(text="Host account login required")
                if request.method not in {"GET", "HEAD"}:
                    csrf = request.headers.get("X-QSM-CSRF", "")
                    if not csrf.isascii() or not hmac.compare_digest(csrf, login.csrf):
                        raise web.HTTPForbidden(text="Invalid console session")
                request["login"] = login
        try:
            response = await handler(request)
        except AgentReplyError as error:
            response = web.Response(status=error.status, text=error.message)
        except web.HTTPException as error:
            response = web.Response(status=error.status, text=error.text)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; "
                "media-src 'self' blob:; worker-src blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    async def agent(self, login):
        async with self.agent_lock:
            try:
                account = pwd.getpwuid(login.uid)
            except KeyError:
                raise web.HTTPUnauthorized(text="Host account changed; sign in again") from None
            if account.pw_name != login.name or account.pw_gid != login.gid:
                raise web.HTTPUnauthorized(text="Host account changed; sign in again")
            existing = self.agents.get(login.uid)
            if existing is not None and existing.process.returncode is None:
                return existing
            if existing is not None:
                await existing.close()
                self.agents.pop(login.uid, None)
            if len(self.agents) >= 16:
                raise web.HTTPServiceUnavailable(text="Too many active user console services")
            parent, child = socket.socketpair()
            command = [sys.executable, "-m", "extensions.standalone.user_agent", "--fd", str(child.fileno()),
                       "--worker", str(self.args.worker), "--fps", str(self.args.fps),
                       "--bitrate", str(self.args.bitrate), "--encoder", self.args.encoder]
            for option in ("vaapi_device", "capture_render_node"):
                value = getattr(self.args, option)
                if value is not None:
                    command.extend(["--" + option.replace("_", "-"), str(value)])
            credentials = {}
            if os.geteuid() == 0:
                credentials = {"user": login.uid, "group": login.gid,
                               "extra_groups": os.getgrouplist(login.name, login.gid)}
            elif login.uid != os.geteuid():
                parent.close()
                child.close()
                raise web.HTTPForbidden(text="This server only accepts its own host account")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, pass_fds=(child.fileno(),), cwd=ROOT,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": account.pw_dir,
                         "USER": account.pw_name, "LOGNAME": account.pw_name,
                         "PYTHONPATH": str(ROOT), "PYTHONNOUSERSITE": "1"},
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    start_new_session=True, **credentials)
                parent.setblocking(False)
                reader, writer = await asyncio.open_connection(sock=parent)
                hello = await asyncio.wait_for(rpc.receive(reader), 5)
                if hello != {"uid": login.uid, "gid": login.gid}:
                    raise ValueError("user console identity mismatch")
            except BaseException:
                parent.close()
                if 'process' in locals() and process.returncode is None:
                    process.terminate()
                    await process.wait()
                raise
            finally:
                child.close()
            connection = AgentConnection(process, reader, writer)
            self.agents[login.uid] = connection
            return connection

    async def config(self, _request):
        return web.json_response({"data": {"auth": "pam"}})

    @staticmethod
    def public_login(login):
        return {"user": login.name, "csrf": login.csrf}

    async def login(self, request):
        address = request.remote or "unknown"
        now = time.monotonic()
        if len(self.failures) >= 1024 and address not in self.failures:
            self.failures = defaultdict(deque, {key: value for key, value in self.failures.items()
                                                if value and value[-1] > now - 60})
            if len(self.failures) >= 1024:
                raise web.HTTPTooManyRequests(text="Please wait before signing in again")
        attempts = self.failures[address]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            raise web.HTTPTooManyRequests(text="Please wait before signing in again")
        attempts.append(now)
        if request.content_length is None or request.content_length > 8192:
            raise web.HTTPBadRequest(text="Invalid login request")
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Invalid login request") from None
        if (not isinstance(payload, dict) or set(payload) != {"username", "password"} or
                not all(isinstance(value, str) for value in payload.values()) or
                not 1 <= len(payload["username"]) <= 128 or not 1 <= len(payload["password"]) <= 1024):
            raise web.HTTPBadRequest(text="Invalid login request")
        try:
            await asyncio.wait_for(self.auth_slots.acquire(), .1)
        except asyncio.TimeoutError:
            raise web.HTTPTooManyRequests(text="Please wait before signing in again") from None
        try:
            # A short-lived isolated interpreter owns the PAM conversation.
            # Neither the argv/environment nor any agent receives a password.
            identity = await authenticate_pam(self.args.pam_service, payload, address)
        finally:
            self.auth_slots.release()
        if identity is None:
            raise web.HTTPUnauthorized(text="Invalid username or password")
        if len(self.logins) >= 128:
            raise web.HTTPServiceUnavailable(text="Too many active console logins")
        previous = self.identity(request)
        if previous is not None:
            await self.revoke(previous)
        login = Login(secrets.token_hex(16), identity["uid"], identity["gid"], identity["name"],
                      secrets.token_urlsafe(24), time.monotonic() + self.args.session_hours * 3600)
        await self.agent(login)
        token = secrets.token_urlsafe(32)
        self.logins[self.key(token)] = login
        response = web.json_response({"data": self.public_login(login)})
        response.set_cookie(COOKIE, token, path="/", httponly=True,
                            secure=self.args.tls_cert is not None, samesite="Strict",
                            max_age=self.args.session_hours * 3600)
        return response

    async def session_info(self, request):
        return web.json_response({"data": self.public_login(request["login"])})

    async def revoke(self, login):
        for key, value in list(self.logins.items()):
            if value.id == login.id:
                del self.logins[key]
        agent = self.agents.get(login.uid)
        if agent is not None and agent.process.returncode is None:
            try:
                await agent.request("logout", login)
            except (web.HTTPException, AgentReplyError):
                pass
        if not any(value.uid == login.uid for value in self.logins.values()):
            self.agents.pop(login.uid, None)
            if agent is not None:
                await agent.close()

    async def logout(self, request):
        await self.revoke(request["login"])
        response = web.Response(status=204)
        response.del_cookie(COOKIE, path="/")
        return response

    async def guests(self, request):
        return await self.forward(request, "guests")

    async def guest_status(self, request):
        return await self.forward(request, "status", guest=request.match_info["guest"])

    async def guest_offer(self, request):
        try:
            offer = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Invalid WebRTC offer") from None
        if (not isinstance(offer, dict) or set(offer) != {"sdp", "width", "height", "fps"} or
                not isinstance(offer["sdp"], str) or not offer["sdp"].isascii() or
                not 1 <= len(offer["sdp"]) <= 96 * 1024 or "\0" in offer["sdp"] or
                any(type(offer[key]) is not int or not low <= offer[key] <= high
                    for key, low, high in (("width", 64, 16384), ("height", 64, 16384), ("fps", 10, 240)))):
            raise web.HTTPBadRequest(text="Invalid WebRTC offer")
        return await self.forward(request, "offer", guest=request.match_info["guest"], offer=offer)

    async def guest_disconnect(self, request):
        await self.forward(request, "disconnect", guest=request.match_info["guest"],
                           session=request.match_info["session"])
        return web.Response(status=204)

    async def forward(self, request, method, **values):
        login = request["login"]
        agent = await self.agent(login)
        result = await agent.request(method, login, **values)
        if login not in self.logins.values() or login.expires <= time.monotonic():
            await self.revoke(login)
            raise web.HTTPUnauthorized(text="Host account login expired")
        return web.json_response({"data": result})

    async def reap(self):
        while True:
            await asyncio.sleep(10)
            for login in list(self.logins.values()):
                if login.expires <= time.monotonic():
                    await self.revoke(login)

    async def startup(self, _app):
        self.reaper = asyncio.create_task(self.reap())

    async def close(self, _app):
        self.closing = True
        self.logins.clear()
        if self.reaper is not None:
            self.reaper.cancel()
            await asyncio.gather(self.reaper, return_exceptions=True)
        await asyncio.gather(*(agent.close() for agent in self.agents.values()))

    def application(self):
        app = web.Application(middlewares=[self.boundary], client_max_size=128 * 1024)
        for path in ("/", "/console", "/standalone.js", "/console.js"):
            app.router.add_get(path, self.asset)
        app.router.add_get("/config", self.config)
        app.router.add_post("/api/login", self.login)
        app.router.add_get("/api/session", self.session_info)
        app.router.add_post("/api/logout", self.logout)
        app.router.add_get("/api/guests", self.guests)
        prefix = "/api/guests/{guest:[0-9]+-[0-9]+}"
        app.router.add_get(prefix + "/status", self.guest_status)
        app.router.add_post(prefix + "/offer", self.guest_offer)
        app.router.add_delete(prefix + "/console/{session:[0-9a-f]{32}}", self.guest_disconnect)
        app.on_startup.append(self.startup)
        app.on_shutdown.append(self.close)
        return app
