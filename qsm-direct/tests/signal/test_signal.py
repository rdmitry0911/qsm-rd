#!/usr/bin/env python3
"""Unit tests for the node-local HTTPS signalling core (no sockets)."""
from __future__ import annotations

import hashlib
import os
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from extensions.signal.qsm_direct_signal import (  # noqa: E402
    relay, SignalServer, websocket_accept, _build_ssl_context,
    SignalConfig, SignalError, TerminalClient, VmPolicyStore, ContainerPolicyStore, handle_request)


class FakeAuthority:
    def __init__(self, *, allow: set[tuple[str, int, str]]) -> None:
        self.allow = allow
        self.calls: list[tuple[str, int, str]] = []

    def authorize(self, username: str, ticket: str, vmid: int, privilege: str) -> str:
        self.calls.append((username, vmid, privilege))
        if ticket != "good" or (username, vmid, privilege) not in self.allow:
            raise SignalError(401, "authentication failure")
        return username


class FakeTerminal:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def create_transport(self, node, vmid, subject, sdp, width, height, fps):
        self.requests.append({"node": node, "vmid": vmid, "subject": subject,
                              "sdp": sdp, "width": width, "height": height, "fps": fps})
        return {"type": "answer", "sdp": "v=0\r\nanswer\r\n"}

    def create_files(self, node, vmid, subject, rights, token):
        self.requests.append({"node": node, "vmid": vmid, "subject": subject, "rights": rights,
                              "token": token})
        return {"type": "files", "socket": "b" * 32}


def config(tmp: Path, allow, node="lab") -> tuple[SignalConfig, FakeTerminal, FakeAuthority]:
    authority = FakeAuthority(allow=allow)
    terminal = FakeTerminal()
    return SignalConfig(authority, terminal, VmPolicyStore(tmp), node), terminal, authority


class SignalCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="qsm-signal.")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_offer_requires_vm_console_and_forwards_confirmed_subject(self) -> None:
        cfg, terminal, authority = config(self.tmp, {("u@pve", 106, "VM.Console")})
        response = handle_request(cfg, "POST", "/nodes/lab/qemu/106/qsm-direct",
                                  {"user": "u@pve", "ticket": "good", "sdp": "v=0\r\no\r\n",
                                   "width": 1280, "height": 800, "fps": 60})
        self.assertEqual(response, {"data": {"type": "answer", "sdp": "v=0\r\nanswer\r\n"}})
        self.assertEqual(authority.calls, [("u@pve", 106, "VM.Console")])
        self.assertEqual(terminal.requests[0]["subject"], "u@pve")

    def test_offer_rejected_when_ticket_invalid(self) -> None:
        cfg, terminal, _ = config(self.tmp, {("u@pve", 106, "VM.Console")})
        with self.assertRaises(SignalError) as raised:
            handle_request(cfg, "POST", "/nodes/lab/qemu/106/qsm-direct",
                           {"user": "u@pve", "ticket": "bad", "sdp": "v=0\r\n",
                            "width": 1280, "height": 800, "fps": 60})
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(terminal.requests, [])

    def test_offer_rejected_for_a_different_vm_even_with_a_valid_ticket(self) -> None:
        # A ticket good for VM 106 must not authorise VM 999.
        cfg, terminal, _ = config(self.tmp, {("u@pve", 106, "VM.Console")})
        with self.assertRaises(SignalError) as raised:
            handle_request(cfg, "POST", "/nodes/lab/qemu/999/qsm-direct",
                           {"user": "u@pve", "ticket": "good", "sdp": "v=0\r\n",
                            "width": 1280, "height": 800, "fps": 60})
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(terminal.requests, [])

    def test_odd_dimensions_are_refused_before_the_terminal(self) -> None:
        cfg, terminal, _ = config(self.tmp, {("u@pve", 106, "VM.Console")})
        with self.assertRaises(SignalError) as raised:
            handle_request(cfg, "POST", "/nodes/lab/qemu/106/qsm-direct",
                           {"user": "u@pve", "ticket": "good", "sdp": "v=0\r\n",
                            "width": 1281, "height": 800, "fps": 60})
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(terminal.requests, [])

    def test_files_need_console_and_file_read_and_write_decides_the_rights(self) -> None:
        user = "alice@pve"
        console = (user, 200, "VM.Console")
        read = (user, 200, "VM.GuestAgent.FileRead")
        write = (user, 200, "VM.GuestAgent.FileWrite")
        path = "/nodes/lab/qemu/200/qsm-direct-files"
        body = {"user": user, "ticket": "good", "token": "a" * 32}
        with tempfile.TemporaryDirectory() as directory:
            for allow, status in (({read, write}, 401), ({console}, 403), ({console, write}, 403)):
                with self.subTest(allow=sorted(item[2] for item in allow)):
                    settings, terminal, _ = config(Path(directory), allow)
                    with self.assertRaises(SignalError) as caught:
                        handle_request(settings, "POST", path, dict(body))
                    self.assertEqual(caught.exception.status, status)
                    self.assertEqual(terminal.requests, [])
            settings, terminal, _ = config(Path(directory), {console, read})
            self.assertEqual(handle_request(settings, "POST", path, dict(body))["data"]["rights"], "r")
            self.assertEqual(terminal.requests[-1]["rights"], "r")
            settings, terminal, _ = config(Path(directory), {console, read, write})
            data = handle_request(settings, "POST", path, dict(body))["data"]
            self.assertEqual(data, {"socket": "b" * 32, "rights": "rw"})
            self.assertEqual(terminal.requests[-1]["subject"], user)
            self.assertEqual(terminal.requests[-1]["token"], "a" * 32)

    def test_files_reject_invalid_tokens(self) -> None:
        user = "alice@pve"
        allow = {(user, 200, "VM.Console"), (user, 200, "VM.GuestAgent.FileRead")}
        path = "/nodes/lab/lxc/200/qsm-direct-files"
        with tempfile.TemporaryDirectory() as directory:
            settings, terminal, _ = config(Path(directory), allow)
            for fields in ({"token": "short"}, {"token": "A" * 32}, {}, {"token": ["a" * 32]}, {"token": 7}):
                with self.subTest(fields=fields), self.assertRaises(SignalError) as caught:
                    handle_request(settings, "POST", path, {"user": user, "ticket": "good", **fields})
                self.assertEqual(caught.exception.status, 400)
            with self.assertRaises(SignalError) as caught:
                handle_request(settings, "GET", path, {"user": user, "ticket": "good"})
            self.assertEqual(caught.exception.status, 405)
            self.assertEqual(terminal.requests, [])

    def test_wrong_node_is_not_found(self) -> None:
        cfg, _, _ = config(self.tmp, {("u@pve", 106, "VM.Console")}, node="lab")
        with self.assertRaises(SignalError) as raised:
            handle_request(cfg, "POST", "/nodes/other/qemu/106/qsm-direct",
                           {"user": "u@pve", "ticket": "good", "sdp": "v=0\r\n",
                            "width": 1280, "height": 800, "fps": 60})
        self.assertEqual(raised.exception.status, 404)

    def test_policy_get_defaults_and_put_requires_config_options(self) -> None:
        cfg, _, authority = config(self.tmp, {
            ("u@pve", 106, "VM.Console"), ("u@pve", 106, "VM.Config.Options")})
        got = handle_request(cfg, "GET", "/nodes/lab/qemu/106/qsm-direct-settings",
                             {"user": "u@pve", "ticket": "good"})
        self.assertEqual(got, {"data": {"codec": "auto", "encoder": "auto", "bitrate": 8000}})
        put = handle_request(cfg, "PUT", "/nodes/lab/qemu/106/qsm-direct-settings",
                             {"user": "u@pve", "ticket": "good", "codec": "hevc", "encoder": "hardware"})
        self.assertEqual(put, {"data": {"codec": "hevc", "encoder": "hardware", "bitrate": 8000}})
        conf = (self.tmp / "106.conf").read_text(encoding="utf-8")
        self.assertIn("QSM_DIRECT_CODEC=hevc\n", conf)
        self.assertIn("QSM_DIRECT_ENCODER_MODE=hardware\n", conf)
        self.assertEqual(oct(os.stat(self.tmp / "106.conf").st_mode & 0o777), "0o600")
        self.assertIn(("u@pve", 106, "VM.Config.Options"), authority.calls)

    def test_bitrate_round_trip_legacy_clients_and_invalid_values(self) -> None:
        for cls in (VmPolicyStore, ContainerPolicyStore):
            with self.subTest(store=cls.__name__):
                (self.tmp / "106.conf").write_text("QSM_DIRECT_LXC_UID=1000\nQSM_DIRECT_LXC_GRACE=300\n")
                store = cls(self.tmp)
                self.assertEqual(store.read(106)["bitrate"], 8000)
                self.assertEqual(store.write(106, "h264", "auto", 20000)["bitrate"], 20000)
                self.assertEqual(store.read(106)["bitrate"], 20000)
                self.assertEqual(store.write(106, "h264", "software")["bitrate"], 20000)
                before = (self.tmp / "106.conf").read_bytes()
                for invalid in (0, 499, 100001, True, "20000", 1.5):
                    with self.assertRaises(SignalError):
                        store.write(106, "h264", "auto", invalid)
                    self.assertEqual((self.tmp / "106.conf").read_bytes(), before)
                if cls is ContainerPolicyStore:
                    self.assertIn(b"QSM_DIRECT_LXC_UID=1000", before)
                    self.assertIn(b"QSM_DIRECT_LXC_GRACE=300", before)

    def test_legacy_json_bitrate_write_matches_the_native_policy_type(self) -> None:
        cfg, _, _ = config(self.tmp, {("u@pve", 106, "VM.Config.Options")})
        request = {"user": "u@pve", "ticket": "good", "codec": "h264", "encoder": "auto", "bitrate": 20000}
        result = handle_request(cfg, "PUT", "/nodes/lab/qemu/106/qsm-direct-settings", request)
        self.assertEqual(result["data"]["bitrate"], 20000)
        for invalid in (True, "20000", 0, 100001):
            with self.assertRaises(SignalError):
                handle_request(cfg, "PUT", "/nodes/lab/qemu/106/qsm-direct-settings", {**request, "bitrate": invalid})
        self.assertEqual(VmPolicyStore(self.tmp).read(106)["bitrate"], 20000)

    def test_policy_put_without_config_options_is_rejected(self) -> None:
        cfg, _, _ = config(self.tmp, {("u@pve", 106, "VM.Console")})
        with self.assertRaises(SignalError) as raised:
            handle_request(cfg, "PUT", "/nodes/lab/qemu/106/qsm-direct-settings",
                           {"user": "u@pve", "ticket": "good", "codec": "hevc", "encoder": "hardware"})
        self.assertEqual(raised.exception.status, 401)
        self.assertFalse((self.tmp / "106.conf").exists())

    def test_hevc_software_policy_is_rejected(self) -> None:
        cfg, _, _ = config(self.tmp, {("u@pve", 106, "VM.Config.Options")})
        with self.assertRaises(SignalError) as raised:
            handle_request(cfg, "PUT", "/nodes/lab/qemu/106/qsm-direct-settings",
                           {"user": "u@pve", "ticket": "good", "codec": "hevc", "encoder": "software"})
        self.assertEqual(raised.exception.status, 400)

    def test_unknown_route_is_not_found(self) -> None:
        cfg, _, _ = config(self.tmp, set())
        with self.assertRaises(SignalError) as raised:
            handle_request(cfg, "POST", "/nodes/lab/qemu/106/vncproxy",
                           {"user": "u@pve", "ticket": "good"})
        self.assertEqual(raised.exception.status, 404)


class WebSocketHandshakeTests(unittest.TestCase):
    def test_the_answer_of_the_rfc_example(self) -> None:
        self.assertEqual(websocket_accept("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_a_key_that_is_not_sixteen_bytes_of_base64_is_refused(self) -> None:
        for key in ("", "short", "!!!!", "dGhlIHNhbXBsZSBub25jZQ", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"):
            self.assertIsNone(websocket_accept(key), key)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class TlsRelayTests(unittest.TestCase):
    """The real server, TLS and relay; only the terminal behind them is a stand-in."""

    KEY = "dGhlIHNhbXBsZSBub25jZQ=="

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
                        "-keyout", str(root / "key.pem"), "-out", str(root / "cert.pem")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.terminal = FakeTerminal()
        self.peers: dict[str, socket.socket] = {}
        self.terminal.attach_files = lambda token: self.peers.pop(token, None)
        config_value = SignalConfig(FakeAuthority(allow=set()), self.terminal, VmPolicyStore(root), "lab")
        self.server = SignalServer(("127.0.0.1", 0), config_value, _build_ssl_context(root / "cert.pem", root / "key.pem"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE

    def connect(self, token: str, headers: str | None = None) -> tuple[ssl.SSLSocket, bytes]:
        raw = socket.create_connection(self.server.server_address[:2])
        client = self.context.wrap_socket(raw)
        self.addCleanup(client.close)
        if headers is None:
            headers = (f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {self.KEY}\r\n"
                       "Sec-WebSocket-Version: 13\r\n")
        client.sendall(f"GET /qsm-direct-files/socket/{token} HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            block = client.recv(4096)
            if not block:
                break
            reply += block
        return client, reply

    def test_a_silent_client_keeps_nobody_else_out(self) -> None:
        silent = socket.create_connection(self.server.server_address[:2])
        self.addCleanup(silent.close)
        raw = socket.create_connection(self.server.server_address[:2], timeout=5)
        client = self.context.wrap_socket(raw)
        self.addCleanup(client.close)
        client.sendall(b"GET /nodes/lab/qemu/1/none HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertTrue(client.recv(4096).startswith(b"HTTP/1.1"))

    def test_a_token_it_does_not_know_gets_no_upgrade(self) -> None:
        _client, reply = self.connect("a" * 32)
        self.assertTrue(reply.startswith(b"HTTP/1.1 404"), reply[:40])

    def test_a_request_that_is_no_websocket_gets_no_peer(self) -> None:
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        self.peers["a" * 32] = theirs
        _client, reply = self.connect("a" * 32, "Connection: close\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply[:40])
        self.assertIn("a" * 32, self.peers, "the token is still good")

    def test_bytes_cross_both_ways_at_once_and_arrive_whole(self) -> None:
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.peers["a" * 32] = theirs
        client, reply = self.connect("a" * 32)
        self.assertTrue(reply.startswith(b"HTTP/1.1 101"), reply[:40])
        self.assertIn(b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", reply)
        size = 24 * 1024 * 1024
        pattern = bytes(range(256)) * 1024

        def peer_side() -> None:
            # Far more than any buffer holds, in both directions, read and written in turn.
            digest, received, sent = hashlib.sha256(), 0, 0
            ours.setblocking(False)
            while received < size or sent < size:
                readable, writable, _ = select.select([ours] if received < size else [],
                                                      [ours] if sent < size else [], [], 20)
                self.assertTrue(readable or writable)
                if readable:
                    block = ours.recv(1 << 18)
                    digest.update(block)
                    received += len(block)
                if writable:
                    sent += ours.send(pattern[sent % len(pattern):][:size - sent])
            self.peer_digest = digest.hexdigest()
        thread = threading.Thread(target=peer_side)
        thread.start()
        client.setblocking(False)
        digest, received, sent = hashlib.sha256(), 0, 0
        while received < size or sent < size:
            progress = False
            if sent < size:
                try:
                    sent += client.send(pattern[sent % len(pattern):][:min(size - sent, 1 << 16)])
                    progress = True
                except (ssl.SSLWantWriteError, ssl.SSLWantReadError):
                    pass
            if received < size:
                try:
                    block = client.recv(1 << 18)
                    digest.update(block)
                    received += len(block)
                    progress = True
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    pass
            if not progress:
                select.select([client], [client] if sent < size else [], [], .05)
        thread.join(30)
        expected = hashlib.sha256()
        for start in range(0, size, len(pattern)):
            expected.update(pattern[:min(len(pattern), size - start)])
        self.assertEqual(digest.hexdigest(), expected.hexdigest())
        self.assertEqual(self.peer_digest, expected.hexdigest())

    def test_either_end_going_away_ends_the_relay(self) -> None:
        for end in ("peer", "client"):
            with self.subTest(end):
                ours, theirs = socket.socketpair()
                self.addCleanup(ours.close)
                self.peers["a" * 32] = theirs
                client, _reply = self.connect("a" * 32)
                if end == "peer":
                    ours.close()
                    client.settimeout(10)
                    self.assertEqual(client.recv(10), b"")
                else:
                    client.close()
                    ours.settimeout(10)
                    self.assertEqual(ours.recv(10), b"")


class TerminalClientTokenTests(unittest.TestCase):
    def client(self) -> TerminalClient:
        # Nothing listens there, so a request that gets that far says so.
        client = TerminalClient(Path("/nonexistent/qsm-terminal.sock"))
        client._request = lambda request: {"type": "files", "socket": "b" * 32}
        return client

    def test_an_unknown_token_never_reaches_the_terminal(self) -> None:
        self.assertIsNone(self.client().attach_files("a" * 32))

    def test_a_token_it_handed_out_reaches_the_terminal_once(self) -> None:
        client = self.client()
        client.create_files("lab", 200, "u@pve", "r", "c" * 32)
        with self.assertRaises(SignalError) as caught:
            client.attach_files("b" * 32)
        self.assertEqual(caught.exception.status, 503)
        self.assertIsNone(client.attach_files("b" * 32))

    def test_an_old_token_is_forgotten(self) -> None:
        client = self.client()
        client.create_files("lab", 200, "u@pve", "r", "c" * 32)
        client._file_tokens["b" * 32] = 0.
        self.assertIsNone(client.attach_files("b" * 32))


class RelayPlainTests(unittest.TestCase):
    def test_plain_tcp_relays_too(self) -> None:
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        client = socket.create_connection(listener.getsockname())
        self.addCleanup(client.close)
        accepted, _address = listener.accept()
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        thread = threading.Thread(target=relay, args=(accepted, theirs), daemon=True)
        thread.start()
        client.sendall(b"up")
        self.assertEqual(ours.recv(2), b"up")
        ours.sendall(b"down")
        self.assertEqual(client.recv(4), b"down")
        client.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
