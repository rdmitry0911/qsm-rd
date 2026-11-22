# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host-login discovery of guests whose Display1 is served by a qdev-host device."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from extensions.standalone import discovery
from extensions.standalone.discovery import (Guest, discover, display_owner, program_arguments,
                                             reinspect, unix_sockets, vfio_user_peer)
from extensions.standalone.host_server import COOKIE, HostConsoleServer

LOADER = "/opt/runtime/ld-linux-x86-64.so.2"
SOCKET = "/dev/shm/guest/pvg.sock"


class FakeProc:
    """A /proc tree; sockets are fd links resolved through a fake kernel AF_UNIX table."""

    def __init__(self, root: Path):
        self.root = root
        self.table = {}

    def process(self, pid, argv, uid=1000, exe=LOADER, started=None, sockets=()):
        path = self.root / str(pid)
        path.mkdir()
        (path / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (path / "exe").symlink_to(exe)
        (path / "cmdline").write_bytes(b"\0".join(value.encode() for value in argv) + b"\0")
        (path / "environ").write_bytes(b"")
        (path / "stat").write_text(f"{pid} (proc) S " + "0 " * 18 + str(started or pid * 10) + " 0\n")
        (path / "fd").mkdir()
        for number, inode in enumerate(sockets):
            (path / "fd" / str(number + 3)).symlink_to(f"socket:[{inode}]")
        return path

    def connect(self, client, accepted, name=SOCKET):
        self.table[client] = (None, accepted)
        self.table[accepted] = (name.encode(), client)

    def qemu(self, pid=200, name="warm-4080", sockets=(10,), uid=1000, display="none"):
        return self.process(pid, [LOADER, "--library-path", "/opt/runtime/lib", "--preload", "/opt/jemalloc.so",
                                  "/opt/pkg/qemu-system-x86_64", "-name", name, "-vga", "none",
                                  "-display", display, "-device",
                                  json.dumps({"driver": "vfio-user-pci", "id": "gfx",
                                              "socket": {"type": "unix", "path": SOCKET}})],
                            uid=uid, sockets=sockets)

    def qdev(self, pid=100, sockets=(20,), uid=1000, extra=(), socket_path=SOCKET,
             bus="unix:path=/run/user/1000/guest/display1.bus"):
        argv = [LOADER, "--library-path", "/opt/pkg/qdev/lib", "/opt/pkg/qdev/qdev-host",
                "--plugin", "/opt/pkg/qdev/plugins/uniq-vgpu.so", "--socket", socket_path,
                "--control", "/dev/shm/guest/ctl.sock"]
        if bus is not None:
            argv += ["--display-bus", bus]
        return self.process(pid, argv + ["--vm-name", "GPU"] + list(extra), uid=uid, sockets=sockets)


class DeviceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.proc = FakeProc(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    def discover(self, uid=1000):
        return discover(uid, self.proc.root, lambda: self.proc.table)

    def test_loader_launch_is_parsed_as_its_program(self):
        self.assertEqual(program_arguments("ld-linux-x86-64.so.2", [
            LOADER, "--library-path", "/l", "--inhibit-cache", "--argv0=x", "/q/qemu-system-x86_64", "-name", "g"]),
            ("qemu-system-x86_64", ["-name", "g"]))
        self.assertEqual(program_arguments("qemu-system-x86_64", ["qemu", "-m", "1"]),
                         ("qemu-system-x86_64", ["-m", "1"]))
        for mode in ("--list", "--verify", "--unknown"):
            self.assertIsNone(program_arguments("ld-linux-x86-64.so.2", [LOADER, mode, "/q/qemu-system-x86_64"]))
        self.assertIsNone(program_arguments("ld-linux-x86-64.so.2", [LOADER, "--library-path", "/l"]))

    def test_connected_qdev_host_display_is_listed_under_its_qemu(self):
        self.proc.qemu()
        self.proc.qdev()
        self.proc.connect(10, 20)
        guests = self.discover()
        self.assertEqual(len(guests), 1)
        guest = guests[0]
        self.assertEqual((guest.pid, guest.name, guest.display_pid, guest.bus_name, guest.display_name),
                         (200, "warm-4080", 100, "org.qemu", "GPU"))
        self.assertEqual(guest.address, "unix:path=/run/user/1000/guest/display1.bus")
        self.assertEqual(guest.id, "200-2000-100-1000")
        self.assertEqual(guest.public(), {"id": "200-2000-100-1000", "name": "warm-4080", "pid": 200,
                                          "display": {"kind": "qdev-host", "pid": 100, "name": "GPU"}})

    def test_qemu_own_display_and_device_display_are_separate_guests(self):
        self.proc.qemu(display="dbus,addr=unix:path=/own/bus")
        self.proc.qdev()
        self.proc.connect(10, 20)
        self.assertEqual(sorted(guest.id for guest in self.discover()), ["200-2000", "200-2000-100-1000"])

    def test_declared_names_and_paths_alone_do_not_pair(self):
        # The QEMU names the device's socket path and the device names the VM, but the
        # kernel shows no connection between them.
        self.proc.qemu(sockets=(10,))
        self.proc.qdev(sockets=(20,))
        self.assertEqual(self.discover(), [])
        # The QEMU is connected to a socket that another process holds.
        self.proc.process(300, ["/usr/bin/other"], exe="/usr/bin/other", sockets=(30,))
        self.proc.connect(10, 30)
        self.assertEqual(self.discover(), [])

    def test_connection_on_another_listener_does_not_pair(self):
        self.proc.qemu()
        self.proc.qdev()
        self.proc.connect(10, 20, name="/dev/shm/guest/other.sock")
        self.assertEqual(self.discover(), [])

    def test_foreign_uid_device_or_qemu_is_not_listed(self):
        self.proc.qemu()
        self.proc.qdev(uid=1001)
        self.proc.connect(10, 20)
        self.assertEqual(self.discover(), [])
        self.assertEqual(self.discover(1001), [])

    def test_device_without_bus_or_with_ambiguous_options_is_not_listed(self):
        self.proc.qemu(sockets=(10, 11, 12, 13))
        self.proc.qdev(pid=101, sockets=(21,), bus=None, extra=["--display-p2p", "/dev/shm/guest/p2p"])
        self.proc.qdev(pid=102, sockets=(22,), extra=["--display-bus", "unix:path=/second"])
        self.proc.qdev(pid=103, sockets=(23,), extra=["--display-name", "bad name"])
        self.proc.qdev(pid=104, sockets=(24,), bus="tcp:host=example,port=1")
        for client, accepted in ((10, 21), (11, 22), (12, 23), (13, 24)):
            self.proc.connect(client, accepted)
        self.assertEqual(self.discover(), [])

    def test_only_the_qdev_host_program_serves_device_displays(self):
        self.proc.qemu()
        path = self.proc.qdev()
        (path / "cmdline").write_bytes((path / "cmdline").read_bytes().replace(b"qdev/qdev-host", b"qdev/fake-host"))
        self.proc.connect(10, 20)
        self.assertEqual(self.discover(), [])

    def test_custom_display_name_is_used_as_the_destination_name(self):
        self.proc.qemu()
        self.proc.qdev(extra=["--display-name", "org.example.Gpu"])
        self.proc.connect(10, 20)
        self.assertEqual(self.discover()[0].bus_name, "org.example.Gpu")

    def test_socket_table_failure_keeps_qemu_display1_guests(self):
        self.proc.qemu(display="dbus,addr=unix:path=/own/bus")
        self.proc.qdev()

        def refused():
            raise OSError("no sock_diag")
        self.assertEqual([guest.id for guest in discover(1000, self.proc.root, refused)], ["200-2000"])

    def test_reinspect_detects_a_restarted_device_or_lost_connection(self):
        self.proc.qemu()
        device = self.proc.qdev()
        self.proc.connect(10, 20)
        guest = self.discover()[0]
        table = lambda: self.proc.table
        self.assertEqual(reinspect(guest, 1000, self.proc.root, table), guest)
        (device / "stat").write_text("100 (proc) S " + "0 " * 18 + "999 0\n")
        self.assertNotEqual(reinspect(guest, 1000, self.proc.root, table), guest)
        (device / "stat").write_text("100 (proc) S " + "0 " * 18 + "1000 0\n")
        self.proc.table.clear()
        self.assertIsNone(reinspect(guest, 1000, self.proc.root, table))


class DisplayOwnerTests(unittest.TestCase):
    def guest(self):
        return Guest(200, "2000", "warm-4080", "unix:path=/bus", 100, "1000", "org.example.Gpu", "GPU")

    def busctl(self, pid, uid):
        def run(command, **_kwargs):
            method, name = command[-3], command[-1]
            value = {"GetNameOwner": ":1.7", "GetConnectionUnixProcessID": pid,
                     "GetConnectionUnixUser": uid}[method]
            self.calls.append((method, name))
            return subprocess.CompletedProcess(command, 0, json.dumps({"type": "s", "data": [value]}), "")
        return run

    def setUp(self):
        self.calls = []

    def test_device_display_owner_must_be_the_qdev_host_process(self):
        guest = self.guest()
        with patch.object(discovery, "reinspect", return_value=guest), \
                patch.object(discovery.subprocess, "run", self.busctl(100, os.geteuid())):
            self.assertEqual(display_owner(guest), ":1.7")
        self.assertEqual(self.calls[0], ("GetNameOwner", "org.example.Gpu"))
        for pid, uid in ((200, os.geteuid()), (100, os.geteuid() + 1)):
            with patch.object(discovery, "reinspect", return_value=guest), \
                    patch.object(discovery.subprocess, "run", self.busctl(pid, uid)):
                with self.assertRaises(ValueError):
                    display_owner(guest)

    def test_changed_device_is_refused(self):
        guest = self.guest()
        with patch.object(discovery, "reinspect", return_value=None), \
                patch.object(discovery.subprocess, "run", self.busctl(100, os.geteuid())):
            with self.assertRaises(ValueError):
                display_owner(guest)


CHILD = r"""
import socket, sys, time
role, path = sys.argv[1], sys.argv[2]
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
if role == "listen":
    sock.bind(path); sock.listen(1); print("ready", flush=True)
    connection, _ = sock.accept()
else:
    sock.connect(path)
print("connected", flush=True)
sys.stdin.read()
"""


class KernelSocketTableTests(unittest.TestCase):
    """The real sock_diag table and /proc fd links of two processes connected on a path."""

    def test_accepted_connection_identifies_its_peer_process(self):
        try:
            unix_sockets()
        except OSError as error:
            self.skipTest(f"sock_diag unavailable: {error}")
        with tempfile.TemporaryDirectory(prefix="qsm-diag-") as directory:
            path = os.path.join(directory, "vfio.sock")
            children = []
            try:
                server = subprocess.Popen([sys.executable, "-I", "-c", CHILD, "listen", path],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                children.append(server)
                self.assertEqual(server.stdout.readline().strip(), "ready")
                client = subprocess.Popen([sys.executable, "-I", "-c", CHILD, "connect", path],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                children.append(client)
                self.assertEqual(client.stdout.readline().strip(), "connected")
                self.assertEqual(server.stdout.readline().strip(), "connected")
                proc = Path("/proc")
                for _ in range(20):
                    table = unix_sockets()
                    if vfio_user_peer(proc / str(client.pid), proc / str(server.pid), path, table):
                        break
                    time.sleep(0.05)
                self.assertTrue(vfio_user_peer(proc / str(client.pid), proc / str(server.pid), path, table))
                self.assertFalse(vfio_user_peer(proc / str(client.pid), proc / str(server.pid), path + "x", table))
                self.assertFalse(vfio_user_peer(proc / str(client.pid), proc / str(os.getpid()), path, table))
                self.assertFalse(vfio_user_peer(proc / str(server.pid), proc / str(client.pid), path, table))
            finally:
                for child in children:
                    child.stdin.close()
                    child.wait(5)
                    child.stdout.close()


class DeviceGuestRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_guest_ids_reach_the_user_agent(self):
        directory = tempfile.TemporaryDirectory(prefix="qsm-login-test-")
        self.addCleanup(directory.cleanup)
        args = argparse.Namespace(fps=30, bitrate=8000, encoder="libx264", worker=Path("/bin/false"),
                                  tls_cert=Path("test-cert"), pam_service="qsm-standalone", session_hours=8,
                                  vaapi_device=None, capture_render_node=None)
        server = HostConsoleServer(args, Path(directory.name), frozenset({"http://localhost:18008"}))
        client = TestClient(TestServer(server.application()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        agent = argparse.Namespace(process=argparse.Namespace(returncode=None), close=AsyncMock(),
                                   request=AsyncMock(return_value={"status": "running"}))

        async def make_agent(login):
            server.agents[login.uid] = agent
            return agent
        server.agent = make_agent
        headers = {"Host": "localhost:18008", "Origin": "http://localhost:18008"}
        with patch("extensions.standalone.host_server.authenticate_pam", AsyncMock(
                return_value={"uid": 1000, "gid": 1000, "name": "alice"})):
            response = await client.post("/api/login", json={"username": "alice", "password": "pw"},
                                         headers=headers)
        cookie = response.cookies[COOKIE].value
        headers["Cookie"] = f"{COOKIE}={cookie}"
        response = await client.get("/api/guests/200-2000-100-1000/status", headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(agent.request.await_args.kwargs, {"guest": "200-2000-100-1000"})
        self.assertEqual((await client.get("/api/guests/200-2000-100/status", headers=headers)).status, 404)


if __name__ == "__main__":
    unittest.main()
