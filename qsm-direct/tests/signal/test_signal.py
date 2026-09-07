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
    SignalConfig, SignalError, TerminalClient, VmPolicyStore, handle_request)


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
        self.assertEqual(got, {"data": {"codec": "auto", "encoder": "auto"}})
        put = handle_request(cfg, "PUT", "/nodes/lab/qemu/106/qsm-direct-settings",
                             {"user": "u@pve", "ticket": "good", "codec": "hevc", "encoder": "hardware"})
        self.assertEqual(put, {"data": {"codec": "hevc", "encoder": "hardware"}})
        conf = (self.tmp / "106.conf").read_text(encoding="utf-8")
        self.assertIn("QSM_DIRECT_CODEC=hevc\n", conf)
        self.assertIn("QSM_DIRECT_ENCODER_MODE=hardware\n", conf)
        self.assertEqual(oct(os.stat(self.tmp / "106.conf").st_mode & 0o777), "0o600")
        self.assertIn(("u@pve", 106, "VM.Config.Options"), authority.calls)

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
