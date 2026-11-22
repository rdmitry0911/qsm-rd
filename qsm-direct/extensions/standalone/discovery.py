# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find only this Unix user's live QEMU Display1 guests, without a VM manager.

A guest's picture comes from one of two Display1 servers:

* QEMU itself (``-display dbus``), owner-checked by QEMU's PID; or
* a ``qdev-host`` vfio-user device process (``--display-bus``), for a VM whose
  display is an out-of-process device such as an accelerated paravirtual GPU.
  Such a display is listed only when the kernel shows that this qdev-host
  holds a connected vfio-user socket whose peer is that QEMU's own socket, both
  processes belong to the user, and the D-Bus name is owned by that qdev-host.
  Names and paths the processes declare never pair them by themselves.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess

MAX_CMDLINE = 128 * 1024
# The dynamic loader run as a program: ld.so [OPTIONS] PROGRAM [ARGS]
LOADER = re.compile(r"ld-linux(?:-[a-z0-9_-]+)?\.so\.[0-9]+|ld64\.so\.[0-9]+|ld\.so")
LOADER_VALUE_OPTIONS = {"--library-path", "--preload", "--audit", "--argv0", "--inhibit-rpath",
                        "--glibc-hwcaps-prepend", "--glibc-hwcaps-mask"}
LOADER_FLAGS = {"--inhibit-cache"}
BUS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+")
DEVICE_PROGRAMS = {"qdev-host"}


@dataclass(frozen=True)
class Guest:
    pid: int
    started: str
    name: str
    address: str
    # A qdev-host display device process; zero for QEMU's own Display1.
    display_pid: int = 0
    display_started: str = ""
    bus_name: str = "org.qemu"
    display_name: str = ""

    @property
    def id(self) -> str:
        if self.display_pid:
            return f"{self.pid}-{self.started}-{self.display_pid}-{self.display_started}"
        return f"{self.pid}-{self.started}"

    def public(self) -> dict:
        value = {"id": self.id, "name": self.name, "pid": self.pid}
        if self.display_pid:
            value["display"] = {"kind": "qdev-host", "pid": self.display_pid, "name": self.display_name}
        return value


@dataclass(frozen=True)
class Process:
    pid: int
    started: str
    program: str
    arguments: list[str]


@dataclass(frozen=True)
class DeviceDisplay:
    pid: int
    started: str
    address: str
    bus_name: str
    vm_name: str
    socket: str


def qemu_options(value: str) -> list[str]:
    """QEMU legacy options escape a literal comma by doubling it."""
    fields, field, index = [], "", 0
    while index < len(value):
        if value[index:index + 2] == ",,":
            field += ","
            index += 2
        elif value[index] == ",":
            fields.append(field)
            field = ""
            index += 1
        else:
            field += value[index]
            index += 1
    return fields + [field]


def option(arguments: list[str], key: str) -> str | None:
    result = None
    for index, argument in enumerate(arguments):
        if argument == key and index + 1 < len(arguments):
            result = arguments[index + 1]
        elif argument.startswith(key + "="):
            result = argument[len(key) + 1:]
    return result


def options(arguments: list[str], key: str) -> list[str]:
    values = []
    for index, argument in enumerate(arguments):
        if argument == key and index + 1 < len(arguments):
            values.append(arguments[index + 1])
        elif argument.startswith(key + "="):
            values.append(argument[len(key) + 1:])
    return values


def program_arguments(executable: str, argv: list[str]) -> tuple[str, list[str]] | None:
    """The program name and its arguments, also for ``ld.so PROGRAM`` launches.

    Bundled runtimes start QEMU and qdev-host through their own dynamic loader,
    so the kernel's executable is the loader. Its documented options are
    skipped; any other option, or a loader mode that does not run a program
    (``--list``, ``--verify``), is not a guest.
    """
    if not LOADER.fullmatch(executable):
        return executable, argv[1:]
    index = 1
    while index < len(argv) and argv[index].startswith("--"):
        if argv[index] in LOADER_FLAGS:
            index += 1
        elif argv[index] in LOADER_VALUE_OPTIONS:
            index += 2
        elif argv[index].split("=", 1)[0] in LOADER_VALUE_OPTIONS:
            index += 1
        else:
            return None
    if index >= len(argv) or not argv[index]:
        return None
    return Path(argv[index]).name, argv[index + 1:]


def read_process(path: Path, uid: int) -> Process | None:
    try:
        status = (path / "status").read_text()
        identities = next(line.split()[1:] for line in status.splitlines() if line.startswith("Uid:"))
        if len(identities) != 4 or any(int(value) != uid for value in identities):
            return None
        executable = (path / "exe").readlink().name.removesuffix(" (deleted)")
        with (path / "cmdline").open("rb") as stream:
            raw = stream.read(MAX_CMDLINE + 1)
        if len(raw) > MAX_CMDLINE:
            return None
        argv = [value.decode("utf-8", "replace") for value in raw.split(b"\0") if value]
        program = program_arguments(executable, argv)
        if program is None:
            return None
        stat = (path / "stat").read_text()
        started = stat[stat.rfind(")") + 2:].split()[19]
        return Process(int(path.name), started, program[0], program[1])
    except (OSError, ValueError, StopIteration, IndexError, UnicodeError):
        return None


def is_qemu(process: Process) -> bool:
    return process.program.startswith("qemu-system-") or process.program in {"qemu-kvm", "kvm"}


def valid_address(address: str | None) -> bool:
    return (isinstance(address, str) and address.startswith("unix:") and len(address) <= 4096 and
            "\n" not in address)


def guest_name(process: Process) -> str:
    name = option(process.arguments, "-name") or f"QEMU {process.pid}"
    return qemu_options(name)[0].removeprefix("guest=")[:128]


def inspect_process(path: Path, uid: int, process: Process | None = None) -> Guest | None:
    """A QEMU of ``uid`` that serves Display1 itself on a private bus."""
    process = process or read_process(path, uid)
    if process is None or not is_qemu(process):
        return None
    try:
        display = option(process.arguments, "-display")
        if display is None:
            return None
        fields = qemu_options(display)
        if fields[0] != "dbus":
            return None
        settings = dict(field.split("=", 1) for field in fields[1:] if "=" in field)
        if settings.get("p2p", "off") in {"on", "yes", "true"}:
            return None
        address = settings.get("addr")
        if address is None:
            with (path / "environ").open("rb") as stream:
                environment = stream.read(1024 * 1024 + 1)
            address = next((entry.split(b"=", 1)[1].decode("utf-8") for entry in environment.split(b"\0")
                            if entry.startswith(b"DBUS_SESSION_BUS_ADDRESS=")), "")
        if not valid_address(address):
            return None
        return Guest(process.pid, process.started, guest_name(process), address)
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def inspect_device(process: Process) -> DeviceDisplay | None:
    """A qdev-host of this user that exports Display1 on a message bus."""
    if process.program not in DEVICE_PROGRAMS:
        return None
    buses, sockets = options(process.arguments, "--display-bus"), options(process.arguments, "--socket")
    names, vm_names = options(process.arguments, "--display-name"), options(process.arguments, "--vm-name")
    # qdev-host takes the last of repeated options; an ambiguous declaration is not guessed.
    if len(buses) != 1 or len(sockets) != 1 or len(names) > 1 or len(vm_names) > 1:
        return None
    bus_name = names[0] if names else "org.qemu"
    if not valid_address(buses[0]) or not BUS_NAME.fullmatch(bus_name) or len(bus_name) > 255:
        return None
    if not sockets[0].startswith("/") or len(sockets[0]) > 107:
        return None
    return DeviceDisplay(process.pid, process.started, buses[0], bus_name,
                         (vm_names[0] if vm_names else "")[:128], sockets[0])


def socket_inodes(path: Path) -> set[int]:
    inodes = set()
    for entry in (path / "fd").iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            inodes.add(int(target[8:-1]))
    return inodes


SOCK_DIAG_BY_FAMILY, NETLINK_SOCK_DIAG = 20, 4
NLM_F_REQUEST, NLM_F_DUMP, NLMSG_ERROR, NLMSG_DONE = 1, 0x300, 2, 3
UDIAG_SHOW_NAME, UDIAG_SHOW_PEER = 1, 4
UNIX_DIAG_NAME, UNIX_DIAG_PEER = 0, 2


def unix_sockets() -> dict[int, tuple[bytes | None, int | None]]:
    """The kernel's AF_UNIX table of this network namespace: inode -> (bound name, peer inode)."""
    request = struct.pack("=BBHIIIII", socket.AF_UNIX, 0, 0, 0xFFFFFFFF, 0,
                          UDIAG_SHOW_NAME | UDIAG_SHOW_PEER, 0xFFFFFFFF, 0xFFFFFFFF)
    table: dict[int, tuple[bytes | None, int | None]] = {}
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW | socket.SOCK_CLOEXEC, NETLINK_SOCK_DIAG) as link:
        link.settimeout(2)
        link.sendto(struct.pack("=IHHII", 16 + len(request), SOCK_DIAG_BY_FAMILY,
                                NLM_F_REQUEST | NLM_F_DUMP, 1, 0) + request, (0, 0))
        while True:
            data = link.recv(1 << 20)
            offset = 0
            while offset + 16 <= len(data):
                length, kind = struct.unpack_from("=IH", data, offset)
                if length < 16 or offset + length > len(data):
                    raise OSError("malformed sock_diag reply")
                if kind == NLMSG_DONE:
                    return table
                if kind == NLMSG_ERROR:
                    raise OSError("sock_diag dump refused")
                if kind == SOCK_DIAG_BY_FAMILY and length >= 32:
                    inode = struct.unpack_from("=I", data, offset + 20)[0]
                    name = peer = None
                    attribute = offset + 32
                    while attribute + 4 <= offset + length:
                        size, tag = struct.unpack_from("=HH", data, attribute)
                        if size < 4:
                            break
                        value = data[attribute + 4:attribute + size]
                        if tag == UNIX_DIAG_NAME:
                            # A pathname address is reported with its terminating NUL.
                            name = value[:-1] if value.endswith(b"\0") and not value.startswith(b"\0") else value
                        elif tag == UNIX_DIAG_PEER and len(value) >= 4:
                            peer = struct.unpack_from("=I", value)[0]
                        attribute += (size + 3) & ~3
                    table[inode] = (name, peer)
                offset += (length + 3) & ~3


def vfio_user_peer(qemu: Path, device: Path, socket_path: str, table: dict) -> bool:
    """True when ``device`` holds an accepted connection on ``socket_path`` whose peer ``qemu`` holds."""
    device_inodes = socket_inodes(device)
    wanted = socket_path.encode()
    for inode in socket_inodes(qemu):
        peer = table.get(inode, (None, None))[1]
        if peer in device_inodes and table.get(peer, (None, None))[0] == wanted:
            return True
    return False


def device_guests(proc: Path, uid: int, qemus: list[Process], devices: list[DeviceDisplay],
                  table_source=unix_sockets) -> list[Guest]:
    if not devices or not qemus:
        return []
    try:
        table = table_source()
    except OSError:
        return []
    guests = []
    for device in devices:
        peers = []
        for qemu in qemus:
            try:
                if vfio_user_peer(proc / str(qemu.pid), proc / str(device.pid), device.socket, table):
                    peers.append(qemu)
            except (OSError, ValueError):
                continue
        if len(peers) != 1:
            continue
        qemu = peers[0]
        guests.append(Guest(qemu.pid, qemu.started, guest_name(qemu), device.address,
                            device.pid, device.started, device.bus_name, device.vm_name))
    return guests


def discover(uid: int | None = None, proc: Path = Path("/proc"), table_source=unix_sockets) -> list[Guest]:
    uid = os.geteuid() if uid is None else uid
    guests, qemus, devices = [], [], []
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        process = read_process(path, uid)
        if process is None:
            continue
        if is_qemu(process):
            qemus.append(process)
            guest = inspect_process(path, uid, process)
            if guest is not None:
                guests.append(guest)
        else:
            device = inspect_device(process)
            if device is not None:
                devices.append(device)
    guests += device_guests(proc, uid, qemus, devices, table_source)
    return sorted(guests, key=lambda guest: (guest.name.casefold(), guest.pid, guest.display_pid))[:128]


def reinspect(guest: Guest, uid: int, proc: Path = Path("/proc"), table_source=unix_sockets) -> Guest | None:
    """The same guest, re-read from /proc and the kernel socket table, or None."""
    process = read_process(proc / str(guest.pid), uid)
    if process is None:
        return None
    if not guest.display_pid:
        return inspect_process(proc / str(guest.pid), uid, process)
    if not is_qemu(process):
        return None
    device_process = read_process(proc / str(guest.display_pid), uid)
    device = inspect_device(device_process) if device_process is not None else None
    if device is None:
        return None
    # Another QEMU of this user cannot share the device connection: the peer check is per socket.
    found = device_guests(proc, uid, [process], [device], table_source)
    return found[0] if found else None


def display_owner(guest: Guest) -> str:
    """Pin the Display1 bus name to its unique connection, checking PID, UID and generation."""
    def call(method: str, name: str):
        result = subprocess.run([
            "busctl", "--address=" + guest.address, "--timeout=2", "--json=short", "call",
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", method, "s", name,
        ], capture_output=True, text=True, timeout=4, check=True)
        return json.loads(result.stdout)["data"][0]

    owner = call("GetNameOwner", guest.bus_name)
    server = guest.display_pid or guest.pid
    if (not isinstance(owner, str) or not owner.startswith(":") or
            call("GetConnectionUnixProcessID", owner) != server or
            call("GetConnectionUnixUser", owner) != os.geteuid() or
            reinspect(guest, os.geteuid()) != guest):
        raise ValueError("Display1 owner changed")
    return owner
