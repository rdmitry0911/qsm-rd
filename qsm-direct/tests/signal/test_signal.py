#!/usr/bin/env python3
"""Unit tests for the node-local HTTPS signalling core (no sockets)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from extensions.signal.qsm_direct_signal import (  # noqa: E402
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

    def create_files(self, node, vmid, subject, rights, token, sdp):
        self.requests.append({"node": node, "vmid": vmid, "subject": subject, "rights": rights,
                              "token": token, "sdp": sdp})
        return {"type": "answer", "sdp": "v=0\r\nfiles\r\n"}


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
        body = {"user": user, "ticket": "good", "sdp": "v=0\r\n", "token": "a" * 32}
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
            self.assertEqual(data, {"type": "answer", "sdp": "v=0\r\nfiles\r\n", "rights": "rw"})
            self.assertEqual(terminal.requests[-1]["subject"], user)
            self.assertEqual(terminal.requests[-1]["token"], "a" * 32)

    def test_files_reject_invalid_tokens_and_offers(self) -> None:
        user = "alice@pve"
        allow = {(user, 200, "VM.Console"), (user, 200, "VM.GuestAgent.FileRead")}
        path = "/nodes/lab/lxc/200/qsm-direct-files"
        with tempfile.TemporaryDirectory() as directory:
            settings, terminal, _ = config(Path(directory), allow)
            for fields in ({"sdp": "v=0", "token": "short"}, {"sdp": "v=0", "token": "A" * 32},
                           {"sdp": "v=0"}, {"token": "a" * 32}, {"sdp": "\u00e9", "token": "a" * 32},
                           {"sdp": 7, "token": "a" * 32}, {"sdp": "v=0", "token": ["a" * 32]}):
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


if __name__ == "__main__":
    unittest.main()
