"""Convert a verified legacy QSM display into the common PVE configuration."""
from __future__ import annotations

import shlex
from pathlib import Path

from .qsm_direct_terminal import (_current_section, _managed_display_profile,
                                  _pve_vga_option, DirectTerminalError)


def migration_plan(config: str, vmid: int,
                   runtime: Path = Path("/run/qsm-pve-direct")) -> dict[str, str | int]:
    profile = _managed_display_profile(config, vmid, runtime)
    if profile is None:
        raise DirectTerminalError("VM has no recognized legacy QSM Display1 profile")
    current = dict(line.partition(":")[::2] for line in _current_section(config) if ":" in line)
    args = shlex.split(current.get("args", ""))
    keep: list[str] = []
    rendernode = None
    audio = False
    index = 0
    while index < len(args):
        flag = args[index]
        value = args[index + 1] if index + 1 < len(args) else ""
        if flag == "-display":
            properties = dict(part.split("=", 1) for part in value.split(",")[1:] if "=" in part)
            if properties.get("addr") != f"unix:path={runtime}/{vmid}/qemu-display1.bus":
                raise DirectTerminalError("refusing to replace an administrator-owned display")
            rendernode = properties.get("rendernode")
            index += 2
            continue
        properties = dict(part.split("=", 1) for part in value.split(",")[1:] if "=" in part)
        if flag == "-audiodev" and value.split(",", 1)[0] == "dbus" and properties.get("id") == "qsm-direct-audio":
            audio = True
            index += 2
            continue
        if flag == "-device" and properties.get("id") in {
                "qsm-direct-gpu", "qsm-direct-hda", "qsm-direct-hda-codec"}:
            index += 2
            continue
        if profile == "virgl" and flag == "-device" and value in {
                "virtio-keyboard-pci", "virtio-mouse-pci", "virtio-tablet-pci"}:
            index += 2
            continue
        keep.append(flag)
        index += 1
    vga = _pve_vga_option(config) or {"type": "std"}
    if profile == "virgl":
        vga = {"type": "virtio-gl"}
        if rendernode:
            vga["rendernode"] = rendernode
    values: dict[str, str | int] = {
        "dbus-display": 1,
        "vga": str(vga["type"]) + "".join(f",{k}={v}" for k, v in vga.items() if k != "type"),
    }
    if keep:
        values["args"] = shlex.join(keep)
    else:
        values["delete"] = "args"
    if audio:
        if "audio0" in current:
            raise DirectTerminalError("VM also has an administrator-owned audio0; resolve it first")
        values["audio0"] = "device=ich9-intel-hda,driver=dbus"
    return values
