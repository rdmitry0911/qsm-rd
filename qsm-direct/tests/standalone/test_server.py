# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise the non-PVE trust boundary and failed-worker cleanup."""
import argparse
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCPeerConnection

from extensions.standalone.server import ConsoleServer, network_policy, read_token

TOKEN = "qsm_test_token_" + "x" * 32


class PolicyTests(unittest.TestCase):
    def test_token_file_rejects_public_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text(TOKEN + "\n")
            path.chmod(0o600)
            self.assertEqual(read_token(path), TOKEN)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_token(link)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_token(path)

    def test_nonlocal_http_and_wildcard_without_public_url_are_refused(self):
        args = argparse.Namespace(listen="0.0.0.0", port=18008, public_url=None,
                                  tls_cert=None, tls_key=None)
        with self.assertRaisesRegex(ValueError, "non-loopback"):
            network_policy(args)
        args.tls_cert, args.tls_key = Path("cert"), Path("key")
        with self.assertRaisesRegex(ValueError, "wildcard"):
            network_policy(args)
        args.public_url = "https://qemu.example:18008"
        self.assertEqual(network_policy(args)[0], args.public_url)
        args.public_url = "https://qemu.example:18008/console"
        with self.assertRaises(ValueError):
            network_policy(args)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qsm-test-")
        args = argparse.Namespace(worker=Path("/bin/false"), dbus_address="unix:path=/missing",
                                  fps=30, bitrate=8000, encoder="libx264", name="test guest",
                                  vaapi_device=None, capture_render_node=None)
        self.console = ConsoleServer(args, Path(self.directory.name), TOKEN,
                                     frozenset({"http://localhost:18008"}))
        self.client = TestClient(TestServer(self.console.application()))
        await self.client.start_server()
        self.headers = {"Host": "localhost:18008", "Authorization": f"Bearer {TOKEN}"}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_api_requires_token_and_does_not_spawn_a_worker(self):
        with patch("asyncio.create_subprocess_exec") as spawn:
            for authorization in ("", "Bearer wrong", "Bearer é", "Bearer " + "x" * 300):
                response = await self.client.post("/api/offer", json={}, headers={
                    "Host": "localhost:18008", "Authorization": authorization})
                self.assertEqual(response.status, 401)
            spawn.assert_not_called()

    async def test_host_and_origin_cannot_be_rebound(self):
        response = await self.client.get("/api/status", headers={**self.headers, "Host": "attacker.example"})
        self.assertEqual(response.status, 403)
        response = await self.client.get("/api/status", headers={**self.headers, "Origin": "https://attacker.example"})
        self.assertEqual(response.status, 403)
        response = await self.client.get("/api/status", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["data"]["name"], "test guest")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_login_page_does_not_disclose_token_or_qemu_address(self):
        response = await self.client.get("/", headers={"Host": "localhost:18008"})
        body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertNotIn(TOKEN, body)
        self.assertNotIn("unix:path", body)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    async def test_malformed_and_oversized_offers_are_rejected_before_spawn(self):
        with patch("asyncio.create_subprocess_exec") as spawn:
            for body in (None, [], {}, {"sdp": "test", "width": True, "height": 100, "fps": 30}):
                response = await self.client.post("/api/offer", json=body, headers=self.headers)
                self.assertEqual(response.status, 400)
            response = await self.client.post("/api/offer", data="x" * (129 * 1024), headers=self.headers)
            self.assertEqual(response.status, 413)
            spawn.assert_not_called()

    async def test_worker_exit_frees_console_for_another_offer(self):
        peer = RTCPeerConnection()
        try:
            peer.addTransceiver("video", direction="recvonly")
            peer.addTransceiver("audio", direction="recvonly")
            peer.createDataChannel("qsm-control")
            sdp = (await peer.createOffer()).sdp
            for _ in range(2):
                response = await self.client.post("/api/offer", json={
                    "sdp": sdp, "width": 640, "height": 480, "fps": 30}, headers=self.headers)
                self.assertEqual(response.status, 502, await response.text())
                self.assertIsNone(self.console.session)
                self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        finally:
            await peer.close()

    async def test_shutdown_during_offer_reaps_worker_and_concurrent_cleanup_waits(self):
        script = Path(self.directory.name) / "sleep-worker"
        script.write_text("#!/bin/sh\nexec sleep 60\n")
        script.chmod(0o700)
        self.console.args.worker = script
        started = asyncio.Event()

        async def pending_offer(_bridge, _sdp):
            started.set()
            await asyncio.Event().wait()

        async def request_offer():
            return await self.client.post("/api/offer", json={
                "sdp": "test", "width": 640, "height": 480, "fps": 30}, headers=self.headers)

        with patch("extensions.standalone.server.BrowserWebRtcBridge.answer_offer", pending_offer):
            request = asyncio.create_task(request_offer())
            try:
                await asyncio.wait_for(started.wait(), 3)
                worker = self.console.session.worker
                await asyncio.wait_for(asyncio.gather(
                    self.console.close(None), self.console.close(None)), 5)
                self.assertIsNotNone(worker.returncode)
                self.assertIsNone(self.console.session)
                self.assertEqual([p.name for p in Path(self.directory.name).iterdir()], ["sleep-worker"])
            finally:
                await asyncio.gather(request, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
