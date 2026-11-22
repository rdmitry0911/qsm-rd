# SPDX-License-Identifier: AGPL-3.0-or-later
import argparse
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from extensions.standalone.discovery import discover, inspect_process
from extensions.standalone.host_server import AgentReplyError, COOKIE, HostConsoleServer


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.proc = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def process(self, pid=123, uid=1000, display="dbus,gl=off", started=456):
        path = self.proc / str(pid)
        path.mkdir(exist_ok=True)
        (path / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (path / "exe").symlink_to("/usr/bin/qemu-system-x86_64")
        (path / "cmdline").write_bytes(b"\0".join(value.encode() for value in [
            "qemu-system-x86_64", "-name", "guest=My,, guest,debug-threads=on", "-display", display]) + b"\0")
        (path / "environ").write_bytes(b"DBUS_SESSION_BUS_ADDRESS=unix:path=/private/bus\0")
        (path / "stat").write_text(f"{pid} (qemu with ) parentheses) S " + "0 " * 18 + str(started) + " 0\n")
        return path

    def test_only_owner_and_display1_are_listed(self):
        self.process()
        self.process(pid=124, uid=1001)
        self.process(pid=125, display="none")
        self.process(pid=126, display="dbus,p2p=yes")
        guests = discover(1000, self.proc)
        self.assertEqual([guest.pid for guest in guests], [123])
        self.assertEqual(guests[0].name, "My, guest")
        self.assertEqual(guests[0].address, "unix:path=/private/bus")
        self.assertEqual(guests[0].id, "123-456")

    def test_explicit_address_and_reused_pid_get_new_identity(self):
        path = self.process(display="dbus,addr=unix:path=/own/bus,gl=off")
        guest = inspect_process(path, 1000)
        self.assertEqual(guest.address, "unix:path=/own/bus")
        (path / "stat").write_text("123 (qemu) S " + "0 " * 18 + "999 0\n")
        self.assertNotEqual(inspect_process(path, 1000).id, guest.id)

    def test_mixed_uids_or_network_dbus_are_not_accepted(self):
        path = self.process()
        (path / "status").write_text("Uid: 1000 0 0 1000\n")
        self.assertIsNone(inspect_process(path, 1000))
        (path / "status").write_text("Uid: 1000 1000 1000 1000\n")
        (path / "environ").write_bytes(b"DBUS_SESSION_BUS_ADDRESS=tcp:host=example\0")
        self.assertIsNone(inspect_process(path, 1000))


class FakeAgent:
    def __init__(self, uid):
        self.uid = uid
        self.process = argparse.Namespace(returncode=None)
        self.calls = []
        self.close = AsyncMock()

    async def request(self, method, login, **values):
        self.calls.append((method, login, values))
        if method == "guests":
            return {"guests": [{"id": f"{self.uid}-123", "name": login.name, "pid": self.uid}]}
        if method == "logout":
            return None
        if values.get("guest") != f"{self.uid}-123":
            raise AgentReplyError(404, "Guest is not running under this account")
        return {"status": "running"}


class HostLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qsm-login-test-")
        args = argparse.Namespace(fps=30, bitrate=8000, encoder="libx264", worker=Path("/bin/false"),
                                  tls_cert=Path("test-cert"), pam_service="qsm-standalone", session_hours=8,
                                  vaapi_device=None, capture_render_node=None)
        self.server = HostConsoleServer(args, Path(self.directory.name), frozenset({"http://localhost:18008"}))
        self.client = TestClient(TestServer(self.server.application()))
        await self.client.start_server()
        self.headers = {"Host": "localhost:18008", "Origin": "http://localhost:18008"}
        self.fake_agents = {}

        async def agent(login):
            if login.uid not in self.fake_agents:
                self.fake_agents[login.uid] = FakeAgent(login.uid)
            self.server.agents[login.uid] = self.fake_agents[login.uid]
            return self.fake_agents[login.uid]
        self.server.agent = agent

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def login(self, uid=1000, name="alice"):
        with patch("extensions.standalone.host_server.authenticate_pam", AsyncMock(
                return_value={"uid": uid, "gid": uid, "name": name})):
            response = await self.client.post("/api/login", json={
                "username": name, "password": "test-password"}, headers=self.headers)
        self.assertEqual(response.status, 200, await response.text())
        value = (await response.json())["data"]
        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["samesite"], "Strict")
        return {**self.headers, "Cookie": f"{COOKIE}={cookie.value}", "X-QSM-CSRF": value["csrf"]}

    async def test_password_login_sets_private_session_and_lists_only_its_uid(self):
        alice = await self.login()
        bob = await self.login(1001, "bob")
        for headers, name in ((alice, "alice"), (bob, "bob")):
            response = await self.client.get("/api/guests", headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual([guest["name"] for guest in (await response.json())["data"]["guests"]], [name])
        response = await self.client.get("/api/guests/1001-123/status", headers=alice)
        self.assertEqual(response.status, 404)

    async def test_bad_password_and_missing_login_do_not_start_agent(self):
        with patch("extensions.standalone.host_server.authenticate_pam", AsyncMock(return_value=None)):
            response = await self.client.post("/api/login", json={
                "username": "alice", "password": "wrong"}, headers=self.headers)
        self.assertEqual(response.status, 401)
        self.assertFalse(self.fake_agents)
        self.assertEqual((await self.client.get("/api/guests", headers=self.headers)).status, 401)

    async def test_csrf_and_origin_are_required_for_mutations(self):
        headers = await self.login()
        for altered in ({**headers, "Origin": "https://attacker.example"},
                        {**headers, "X-QSM-CSRF": "wrong"}):
            response = await self.client.post("/api/logout", json={}, headers=altered)
            self.assertEqual(response.status, 403)
        self.assertEqual((await self.client.get("/api/guests", headers=headers)).status, 200)

    async def test_logout_revokes_cookie_and_closes_only_its_user_agent(self):
        alice = await self.login()
        bob = await self.login(1001, "bob")
        self.assertEqual((await self.client.post("/api/logout", json={}, headers=alice)).status, 204)
        self.assertEqual((await self.client.get("/api/guests", headers=alice)).status, 401)
        self.assertEqual((await self.client.get("/api/guests", headers=bob)).status, 200)
        self.fake_agents[1000].close.assert_awaited()
        self.fake_agents[1001].close.assert_not_awaited()

    async def test_expired_cookie_is_rejected(self):
        headers = await self.login()
        for login in self.server.logins.values():
            login.expires = time.monotonic() - 1
        self.assertEqual((await self.client.get("/api/guests", headers=headers)).status, 401)

    async def test_reused_unix_uid_does_not_reuse_the_old_account_agent(self):
        await self.login()
        login = next(iter(self.server.logins.values()))
        with patch("extensions.standalone.host_server.pwd.getpwuid", return_value=argparse.Namespace(
                pw_name="different-account", pw_gid=login.gid)):
            with self.assertRaises(web.HTTPUnauthorized):
                await HostConsoleServer.agent(self.server, login)

    async def test_rate_limit_bounds_failed_password_attempts(self):
        with patch("extensions.standalone.host_server.authenticate_pam", AsyncMock(return_value=None)) as auth:
            for _ in range(5):
                self.assertEqual((await self.client.post("/api/login", json={
                    "username": "alice", "password": "wrong"}, headers=self.headers)).status, 401)
            self.assertEqual((await self.client.post("/api/login", json={
                "username": "alice", "password": "wrong"}, headers=self.headers)).status, 429)
            self.assertEqual(auth.await_count, 5)


if __name__ == "__main__":
    unittest.main()
