# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find only this Unix user's live QEMU Display1 guests, without a VM manager."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class Guest:
    pid: int
    started: str
    name: str
    address: str

    @property
    def id(self) -> str:
        return f"{self.pid}-{self.started}"

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "pid": self.pid}


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


def inspect_process(path: Path, uid: int) -> Guest | None:
    try:
        status = (path / "status").read_text()
        identities = next(line.split()[1:] for line in status.splitlines() if line.startswith("Uid:"))
        if len(identities) != 4 or any(int(value) != uid for value in identities):
            return None
        executable = (path / "exe").readlink().name.removesuffix(" (deleted)")
        if not executable.startswith("qemu-system-") and executable not in {"qemu-kvm", "kvm"}:
            return None
        with (path / "cmdline").open("rb") as stream:
            raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            return None
        arguments = [value.decode("utf-8", "replace") for value in raw.split(b"\0") if value]
        display = option(arguments, "-display")
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
        if not address.startswith("unix:") or len(address) > 4096 or "\n" in address:
            return None
        stat = (path / "stat").read_text()
        started = stat[stat.rfind(")") + 2:].split()[19]
        name = option(arguments, "-name") or f"QEMU {path.name}"
        name = qemu_options(name)[0].removeprefix("guest=")
        return Guest(int(path.name), started, name[:128], address)
    except (OSError, ValueError, StopIteration, IndexError, UnicodeError):
        return None


def discover(uid: int | None = None, proc: Path = Path("/proc")) -> list[Guest]:
    uid = os.geteuid() if uid is None else uid
    guests = []
    for path in proc.iterdir():
        if path.name.isdigit():
            guest = inspect_process(path, uid)
            if guest is not None:
                guests.append(guest)
    return sorted(guests, key=lambda guest: (guest.name.casefold(), guest.pid))[:128]


def display_owner(guest: Guest) -> str:
    """Pin org.qemu to its unique connection, checking PID, UID and generation."""
    def call(method: str, name: str):
        result = subprocess.run([
            "busctl", "--address=" + guest.address, "--timeout=2", "--json=short", "call",
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", method, "s", name,
        ], capture_output=True, text=True, timeout=4, check=True)
        return json.loads(result.stdout)["data"][0]

    owner = call("GetNameOwner", "org.qemu")
    if (not isinstance(owner, str) or not owner.startswith(":") or
            call("GetConnectionUnixProcessID", owner) != guest.pid or
            call("GetConnectionUnixUser", owner) != os.geteuid() or
            inspect_process(Path("/proc") / str(guest.pid), os.geteuid()) != guest):
        raise ValueError("QEMU Display1 owner changed")
    return owner
