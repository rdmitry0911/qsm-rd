#!/usr/bin/env python3
"""Protocol tests for PVE VM.Console -> one-use .qsm -> native transport."""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
TERMINAL_DIRECTORY = ROOT / "extensions" / "terminal_server"
SYSTEM_AUTH_DIRECTORY = ROOT / "extensions" / "system_auth"
GAMESTREAM_DIRECTORY = ROOT / "extensions" / "gamestream_auth"
for directory in (TERMINAL_DIRECTORY, SYSTEM_AUTH_DIRECTORY, GAMESTREAM_DIRECTORY):
    sys.path.insert(0, str(directory))

from q_sunshine_auth import verify_ticket  # noqa: E402
from q_sunshine_gamestream_lease import LeaseIssuerConfig  # noqa: E402
from q_sunshine_terminal import (  # noqa: E402
    DESCRIPTOR_REDEEM_OPERATION,
    LAUNCH_DESCRIPTOR_KIND,
    MAX_PVE_ACL_REQUEST_BYTES,
    NodeEndpoint,
    PVE_ACL_LAUNCH_OPERATION,
    PveAclLaunchServer,
    TerminalBroker,
    TerminalError,
    TerminalServer,
    TerminalWorkerManager,
    WorkerRoute,
    WorkerSpec,
    _require_root_unix_peer,
    _validate_preprovisioned_sunshine_server_material,
    load_node_endpoints,
    terminal_tls_context,
)


NODE_ENDPOINT = NodeEndpoint(
    host="pve.example.test", port=48123, server_name="pve.example.test")


class FakeWorkerManager:
    def __init__(self) -> None:
        self.claims: list[object] = []
        self.spec = WorkerSpec(
            vmid=100,
            audience="vm-100",
            environment={},
            route=WorkerRoute(
                media_host="pve.example.test", media_port=47989,
                qsf_host="pve.example.test", qsf_port=48122,
                lease_host="pve.example.test", lease_port=48123,
            ),
            lease_config=LeaseIssuerConfig(
                issuer=Path("/issuer"),
                ca_certificate=Path("/ca.crt"),
                ca_private_key=Path("/ca.key"),
                sunshine_server_certificate=Path("/sunshine.crt"),
                audience="vm-100",
                ttl_seconds=60,
                require_root_owner=False,
            ),
        )

    def ensure_started(self, claim: object) -> WorkerSpec:
        self.claims.append(claim)
        if getattr(claim, "vmid", None) != 100 or getattr(claim, "node", None) != "pve":
            raise TerminalError("not available")
        return self.spec

    def stop_all(self) -> None:
        pass


def make_broker(worker: FakeWorkerManager | None = None) -> tuple[TerminalBroker, FakeWorkerManager]:
    manager = worker or FakeWorkerManager()
    return (
        TerminalBroker(
            worker_manager=manager,
            ticket_key=b"T" * 32,
            node_endpoints={"pve": NODE_ENDPOINT},
            endpoint_ca_pem="-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n",
            local_node="pve",
            ticket_ttl_seconds=60,
            descriptor_ttl_seconds=60,
        ),
        manager,
    )


def pve_acl_payload() -> dict[str, object]:
    return {
        "version": 1,
        "op": PVE_ACL_LAUNCH_OPERATION,
        "node": "pve",
        "vmid": 100,
        "subject": "alice@pam",
    }


def issue_descriptor(broker: TerminalBroker) -> dict[str, object]:
    return broker.launch_from_pve_acl(pve_acl_payload())


class TerminalBrokerTests(unittest.TestCase):
    def test_pve_acl_launch_envelope_is_strict_and_has_no_pve_bearer_artifact(self) -> None:
        broker, manager = make_broker()
        envelope = issue_descriptor(broker)
        self.assertEqual(set(envelope), {
            "version", "kind", "endpoint", "claim", "expires_at_utc_ms",
        })
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(envelope["kind"], LAUNCH_DESCRIPTOR_KIND)
        self.assertEqual(envelope["endpoint"], {
            "host": "pve.example.test",
            "port": 48123,
            "server_name": "pve.example.test",
            "ca_pem": "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n",
        })
        self.assertRegex(str(envelope["claim"]), r"\Aqsd1\.[A-Za-z0-9_-]{43}\Z")
        self.assertNotIn("session_token", envelope)
        self.assertEqual(len(manager.claims), 1)

    def test_redeem_is_one_use_vm_scoped_and_returns_only_expected_fields(self) -> None:
        broker, _manager = make_broker()
        envelope = issue_descriptor(broker)
        result = broker.redeem_descriptor({
            "version": 1,
            "op": DESCRIPTOR_REDEEM_OPERATION,
            "claim": envelope["claim"],
        })
        self.assertEqual(set(result), {
            "version", "session_token", "subject", "audience", "expires_at_utc_ms",
            "routes", "server_name", "ca_pem",
        })
        self.assertEqual(result["subject"], "alice@pam")
        self.assertEqual(result["audience"], "vm-100")
        self.assertEqual(result["routes"], {
            "media": {"host": "pve.example.test", "port": 47989},
            "qsf": {"host": "pve.example.test", "port": 48122},
            "lease": {"host": "pve.example.test", "port": 48123},
        })
        subject, _expiry = verify_ticket(b"T" * 32, str(result["session_token"]), audience="vm-100")
        self.assertEqual(subject, "alice@pam")
        with self.assertRaises(TerminalError):
            broker.redeem_descriptor({
                "version": 1,
                "op": DESCRIPTOR_REDEEM_OPERATION,
                "claim": envelope["claim"],
            })

    def test_pve_acl_handoff_issues_descriptor_without_pve_bearer_artifact(self) -> None:
        broker, manager = make_broker()
        envelope = broker.launch_from_pve_acl(pve_acl_payload())
        self.assertEqual(set(envelope), {
            "version", "kind", "endpoint", "claim", "expires_at_utc_ms",
        })
        self.assertEqual(envelope["kind"], LAUNCH_DESCRIPTOR_KIND)
        self.assertEqual(len(manager.claims), 1)
        self.assertEqual(getattr(manager.claims[0], "subject"), "alice@pam")
        serialized = json.dumps(envelope, sort_keys=True)
        self.assertNotIn("ticket", serialized.lower())

    def test_pve_acl_handoff_requires_exact_local_mapped_request(self) -> None:
        broker, manager = make_broker()
        for payload in (
                {**pve_acl_payload(), "ticket": "forbidden"},
                {**pve_acl_payload(), "op": "redeem_launch"},
                {**pve_acl_payload(), "version": True},
                {**pve_acl_payload(), "version": 1.0},
                {**pve_acl_payload(), "subject": "alice"},
                {**pve_acl_payload(), "vmid": 99},
                {**pve_acl_payload(), "node": "remote"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(TerminalError):
                    broker.launch_from_pve_acl(payload)
        self.assertEqual(manager.claims, [])

    def test_pve_acl_handoff_rejects_mapped_nonlocal_node(self) -> None:
        manager = FakeWorkerManager()
        broker = TerminalBroker(
            worker_manager=manager,
            ticket_key=b"T" * 32,
            node_endpoints={
                "pve": NODE_ENDPOINT,
                "remote": NodeEndpoint(
                    host="remote.example.test", port=48123,
                    server_name="remote.example.test"),
            },
            endpoint_ca_pem="-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n",
            local_node="pve",
            ticket_ttl_seconds=60,
            descriptor_ttl_seconds=60,
        )
        with self.assertRaises(TerminalError):
            broker.launch_from_pve_acl({**pve_acl_payload(), "node": "remote"})
        self.assertEqual(manager.claims, [])

    def test_node_endpoint_policy_is_strict(self) -> None:
        with tempfile.TemporaryDirectory(prefix="q-sunshine-node-map.") as temporary:
            path = Path(temporary) / "endpoints.json"
            path.write_text(json.dumps({
                "version": 1,
                "nodes": {
                    "pve": {"host": "127.0.0.1", "port": 48123, "server_name": "pve.example.test"},
                },
            }), encoding="utf-8")
            os.chmod(path, 0o644)
            self.assertEqual(load_node_endpoints(path)["pve"].host, "127.0.0.1")
            path.write_text(json.dumps({
                "version": 1,
                "nodes": {"pve": {"host": "https://evil.example", "port": 48123,
                                  "server_name": "pve.example.test"}},
            }), encoding="utf-8")
            with self.assertRaises(TerminalError):
                load_node_endpoints(path)

    def test_private_dbus_bus_is_owned_and_address_is_not_inherited(self) -> None:
        if shutil.which("dbus-daemon") is None:
            self.skipTest("dbus-daemon is unavailable")
        with tempfile.TemporaryDirectory(prefix="q-sunshine-vm-bus.") as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            manager = TerminalWorkerManager(
                instance_directory=root / "instances",
                runtime_directory=root / "workers",
                vm_runtime_directory=runtime_root,
                state_directory=root / "state",
                ticket_key_path=root / "ticket.key",
                terminal_certificate=root / "server.crt",
                terminal_key=root / "server.key",
                lease_host="pve.example.test",
                lease_port=48123,
                transport_bind_host="127.0.0.1",
                advertised_host="pve.example.test",
                local_node="pve",
            )
            environment = {
                "SUNSHINE_QEMU_DBUS_ADDRESS":
                    f"unix:path={runtime_root}/100/qemu-display1.bus",
                "QSUNSHINE_QSF_AGENT_SOCKET": f"{runtime_root}/100/qsf-agent.sock",
            }
            try:
                manager._ensure_dbus_bus_locked(100, environment)
                bus_socket = runtime_root / "100" / "qemu-display1.bus"
                self.assertTrue(stat.S_ISSOCK(os.lstat(bus_socket).st_mode))
                self.assertEqual(os.lstat(bus_socket).st_mode & 0o077, 0)
                with self.assertRaises(TerminalError):
                    manager._dbus_socket_path({
                        **environment,
                        "SUNSHINE_QEMU_DBUS_ADDRESS": "unix:path=/tmp/not-a-vm-bus",
                    }, 100)
            finally:
                manager.stop_all()

    def test_preprovisioned_gamestream_server_pair_is_required_before_worker_start(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl is unavailable")
        with tempfile.TemporaryDirectory(prefix="q-sunshine-server-pair.") as temporary:
            directory = Path(temporary)
            certificate = directory / "sunshine-server.crt"
            private_key = directory / "sunshine-server.key"
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
                "-subj", "/CN=q-sunshine-vm-100",
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.chmod(private_key, 0o600)
            environment = {
                "QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_CERT": str(certificate),
                "QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_KEY": str(private_key),
            }
            self.assertEqual(
                _validate_preprovisioned_sunshine_server_material(environment),
                (certificate, private_key))
            with self.assertRaises(TerminalError):
                _validate_preprovisioned_sunshine_server_material({
                    **environment,
                    "QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_KEY": str(directory / "missing.key"),
                })

    def test_worker_child_environment_excludes_terminal_only_lease_authority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="q-sunshine-child-env.") as temporary:
            root = Path(temporary)
            manager = TerminalWorkerManager(
                instance_directory=root / "instances",
                runtime_directory=root / "workers",
                vm_runtime_directory=root / "vm-runtime",
                state_directory=root / "state",
                ticket_key_path=root / "ticket.key",
                terminal_certificate=root / "terminal.crt",
                terminal_key=root / "terminal.key",
                lease_host="pve.example.test",
                lease_port=48123,
                transport_bind_host="127.0.0.1",
                advertised_host="pve.example.test",
                local_node="pve",
            )
            spec = WorkerSpec(
                vmid=100,
                audience="vm-100",
                environment={
                    "QSUNSHINE_GAMESTREAM_LEASE_ISSUER": "/root/issuer",
                    "QSUNSHINE_GAMESTREAM_LEASE_CA_KEY": "/root/ca.key",
                    "QSUNSHINE_GAMESTREAM_LEASE_TTL_SECONDS": "300",
                    "QSUNSHINE_GAMESTREAM_LEASE_CA_CERT": "/root/ca.crt",
                    "QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_CERT": "/root/server.crt",
                    "QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_KEY": "/root/server.key",
                },
                route=WorkerRoute(
                    media_host="pve.example.test", media_port=47989,
                    qsf_host="pve.example.test", qsf_port=48122,
                    lease_host="pve.example.test", lease_port=48123),
                lease_config=LeaseIssuerConfig(
                    issuer=Path("/root/issuer"), ca_certificate=Path("/root/ca.crt"),
                    ca_private_key=Path("/root/ca.key"),
                    sunshine_server_certificate=Path("/root/server.crt"),
                    audience="vm-100", ttl_seconds=60, require_root_owner=False),
            )
            sunshine_environment = manager._make_child_environment(spec)
            self.assertNotIn("QSUNSHINE_GAMESTREAM_LEASE_ISSUER", sunshine_environment)
            self.assertNotIn("QSUNSHINE_GAMESTREAM_LEASE_CA_KEY", sunshine_environment)
            self.assertNotIn("QSUNSHINE_GAMESTREAM_LEASE_TTL_SECONDS", sunshine_environment)
            self.assertIn("QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_KEY", sunshine_environment)


class TerminalSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = Path(tempfile.mkdtemp(prefix="q-sunshine-terminal-test."))
        self.addCleanup(lambda: shutil.rmtree(self.temporary_directory, ignore_errors=True))

    @staticmethod
    def _read_line(connection: socket.socket) -> dict[str, object]:
        data = bytearray()
        while b"\n" not in data:
            block = connection.recv(65536)
            if not block:
                break
            data.extend(block)
        return json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))

    @staticmethod
    def _stop_server(server: object, thread: threading.Thread) -> None:
        getattr(server, "shutdown")()
        thread.join(timeout=3.0)
        getattr(server, "server_close")()

    def _start_tls_server(self) -> tuple[TerminalBroker, FakeWorkerManager,
                                         TerminalServer, threading.Thread, ssl.SSLContext]:
        if shutil.which("openssl") is None:
            self.skipTest("openssl is unavailable")
        certificate = self.temporary_directory / "server.crt"
        private_key = self.temporary_directory / "server.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
            "-subj", "/CN=localhost",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(private_key, 0o600)
        broker, manager = make_broker()
        server = TerminalServer(
            ("127.0.0.1", 0), terminal_tls_context(certificate, private_key), broker,
            max_concurrent_requests=2)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop_server, server, thread)
        context = ssl.create_default_context(cafile=str(certificate))
        context.check_hostname = False
        return broker, manager, server, thread, context

    def _start_pve_acl_server(self) -> tuple[TerminalBroker, FakeWorkerManager,
                                              PveAclLaunchServer, threading.Thread, Path]:
        broker, manager = make_broker()
        socket_path = self.temporary_directory / "pve-launch.sock"
        server = PveAclLaunchServer(socket_path, broker, max_concurrent_requests=2)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop_server, server, thread)
        return broker, manager, server, thread, socket_path

    def _pve_acl_request(self, socket_path: Path, payload: bytes) -> dict[str, object]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5.0)
            connection.connect(str(socket_path))
            connection.sendall(payload)
            return self._read_line(connection)

    def test_pve_acl_socket_is_private_and_removes_its_own_path(self) -> None:
        _broker, _manager, server, _thread, socket_path = self._start_pve_acl_server()
        metadata = os.lstat(socket_path)
        self.assertTrue(stat.S_ISSOCK(metadata.st_mode))
        self.assertEqual(metadata.st_mode & 0o077, 0)
        self.assertEqual(metadata.st_uid, os.geteuid())
        self._stop_server(server, _thread)
        self.assertFalse(socket_path.exists())

    def test_pve_acl_socket_never_replaces_a_non_socket_path(self) -> None:
        socket_path = self.temporary_directory / "pve-launch.sock"
        socket_path.write_text("do not replace", encoding="utf-8")
        with self.assertRaises(TerminalError):
            PveAclLaunchServer(socket_path, make_broker()[0], max_concurrent_requests=2)
        self.assertEqual(socket_path.read_text(encoding="utf-8"), "do not replace")

    def test_pve_acl_socket_never_unlinks_a_symlink(self) -> None:
        socket_path = self.temporary_directory / "pve-launch.sock"
        target = self.temporary_directory / "target"
        target.write_text("do not follow", encoding="utf-8")
        socket_path.symlink_to(target)
        with self.assertRaises(TerminalError):
            PveAclLaunchServer(socket_path, make_broker()[0], max_concurrent_requests=2)
        self.assertTrue(socket_path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "do not follow")

    def test_pve_acl_peer_credential_gate_requires_uid_zero(self) -> None:
        class FakePeer:
            def __init__(self, uid: int) -> None:
                self.uid = uid

            def getsockopt(self, _level: int, _option: int, _size: int) -> bytes:
                return struct.pack("3i", 1234, self.uid, 1234)

        _require_root_unix_peer(FakePeer(0))  # type: ignore[arg-type]
        with self.assertRaises(TerminalError):
            _require_root_unix_peer(FakePeer(1000))  # type: ignore[arg-type]
        with self.assertRaises(TerminalError):
            _require_root_unix_peer(FakePeer(-1))  # type: ignore[arg-type]

    def test_pve_acl_socket_returns_descriptor_only_for_the_internal_schema(self) -> None:
        _broker, manager, server, _thread, socket_path = self._start_pve_acl_server()
        with mock.patch("q_sunshine_terminal._require_root_unix_peer"):
            result = self._pve_acl_request(
                socket_path,
                json.dumps(pve_acl_payload(), separators=(",", ":")).encode("ascii") + b"\n")
        self.assertEqual(set(result), {
            "version", "kind", "endpoint", "claim", "expires_at_utc_ms",
        })
        self.assertEqual(result["kind"], LAUNCH_DESCRIPTOR_KIND)
        self.assertEqual(len(manager.claims), 1)
        self.assertNotIn("ticket", json.dumps(result, sort_keys=True).lower())

    def test_pve_acl_socket_accepts_a_real_root_peer(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("a real root SO_PEERCRED peer requires a root test process")
        _broker, manager, _server, _thread, socket_path = self._start_pve_acl_server()
        result = self._pve_acl_request(
            socket_path,
            json.dumps(pve_acl_payload(), separators=(",", ":")).encode("ascii") + b"\n")
        self.assertEqual(result["kind"], LAUNCH_DESCRIPTOR_KIND)
        self.assertEqual(len(manager.claims), 1)

    def test_pve_acl_socket_rejects_nonroot_peer_and_malformed_frames(self) -> None:
        _broker, manager, _server, _thread, socket_path = self._start_pve_acl_server()
        # This test process is deliberately non-root.  Its real SO_PEERCRED
        # must fail before the request can select a VM.
        if os.geteuid() != 0:
            result = self._pve_acl_request(
                socket_path,
                json.dumps(pve_acl_payload(), separators=(",", ":")).encode("ascii") + b"\n")
            self.assertEqual(result, {"ok": False, "error": "authentication failed"})
            self.assertEqual(manager.claims, [])

        with mock.patch("q_sunshine_terminal._require_root_unix_peer"):
            duplicate_subject = (
                b'{"version":1,"op":"pve_acl_launch","node":"pve",'
                b'"vmid":100,"subject":"alice@pam","subject":"root@pam"}\n')
            result = self._pve_acl_request(socket_path, duplicate_subject)
            self.assertEqual(result, {"ok": False, "error": "authentication failed"})
            oversized = b"{" + b"x" * MAX_PVE_ACL_REQUEST_BYTES + b"\n"
            result = self._pve_acl_request(socket_path, oversized)
            self.assertEqual(result, {"ok": False, "error": "authentication failed"})
        self.assertEqual(manager.claims, [])

    def test_cli_starts_from_authoritative_node_map_without_fallback_arguments(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl is unavailable")
        certificate = self.temporary_directory / "server.crt"
        private_key = self.temporary_directory / "server.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
            "-subj", "/CN=localhost",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(private_key, 0o600)
        ticket_key = self.temporary_directory / "ticket.key"
        ticket_key.write_bytes(b"T" * 32)
        os.chmod(ticket_key, 0o600)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        endpoint_map = self.temporary_directory / "node-endpoints.json"
        endpoint_map.write_text(json.dumps({
            "version": 1,
            "nodes": {"pve": {"host": "127.0.0.1", "port": port,
                              "server_name": "localhost"}},
        }), encoding="utf-8")
        os.chmod(endpoint_map, 0o644)
        (self.temporary_directory / "instances").mkdir()
        pve_launch_socket = self.temporary_directory / "pve-launch.sock"
        process = subprocess.Popen([
            sys.executable, str(TERMINAL_DIRECTORY / "q_sunshine_terminal.py"),
            "--server-cert", str(certificate),
            "--server-key", str(private_key),
            "--ticket-key", str(ticket_key),
            "--descriptor-ca-file", str(certificate),
            "--node-endpoints-file", str(endpoint_map),
            "--listen-host", "127.0.0.1",
            "--listen-port", str(port),
            "--pve-launch-socket", str(pve_launch_socket),
            "--transport-bind-host", "127.0.0.1",
            "--local-node", "pve",
            "--instance-directory", str(self.temporary_directory / "instances"),
            "--runtime-directory", str(self.temporary_directory / "runtime"),
            "--vm-runtime-directory", str(self.temporary_directory / "vm-runtime"),
            "--state-directory", str(self.temporary_directory / "state"),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert process.stdout is not None
            ready = ""
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                readable, _writable, _exceptional = select.select([process.stdout], [], [], 0.1)
                if readable:
                    ready = process.stdout.readline()
                    break
                if process.poll() is not None:
                    break
            self.assertIn(f"Q_SUNSHINE_TERMINAL_READY host=127.0.0.1 port={port}", ready)
            metadata = os.lstat(pve_launch_socket)
            self.assertTrue(stat.S_ISSOCK(metadata.st_mode))
            self.assertEqual(metadata.st_mode & 0o077, 0)
            self.assertEqual(metadata.st_uid, os.geteuid())
        finally:
            if process.poll() is None:
                process.terminate()
            _stdout, stderr = process.communicate(timeout=8.0)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertFalse(pve_launch_socket.exists())

    def test_tls_redeem_accepts_claim_once(self) -> None:
        broker, _manager, server, _thread, context = self._start_tls_server()
        envelope = issue_descriptor(broker)
        port = server.server_address[1]

        def redeem() -> dict[str, object]:
            with socket.create_connection(("127.0.0.1", port), timeout=5.0) as raw:
                with context.wrap_socket(raw, server_hostname="localhost") as connection:
                    connection.sendall(json.dumps({
                        "version": 1,
                        "op": DESCRIPTOR_REDEEM_OPERATION,
                        "claim": envelope["claim"],
                    }, separators=(",", ":")).encode("ascii") + b"\n")
                    return self._read_line(connection)

        first = redeem()
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"]["audience"], "vm-100")  # type: ignore[index]
        second = redeem()
        self.assertFalse(second["ok"])

    def test_tls_rejects_obsolete_direct_issue_launch_operation(self) -> None:
        _broker, _manager, server, _thread, context = self._start_tls_server()
        port = server.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                connection.sendall(json.dumps({
                    "version": 1,
                    "op": "issue_launch",
                    "subject": "alice@pam",
                    "node": "pve",
                    "vmid": 100,
                }, separators=(",", ":")).encode("ascii") + b"\n")
                response = self._read_line(connection)
        self.assertEqual(response, {"ok": False, "error": "authentication failed"})

    def test_tls_rejects_the_removed_http_launch_path(self) -> None:
        _broker, manager, server, _thread, context = self._start_tls_server()
        port = server.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as connection:
                connection.sendall(
                    b"POST /pve/v1/launch HTTP/1.1\r\n"
                    b"Host: pve.example.test:48123\r\n\r\n")
                response = self._read_line(connection)
        self.assertEqual(response, {"ok": False, "error": "authentication failed"})
        self.assertEqual(manager.claims, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
