#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a NoCloud seed that turns the hover-gate fixture into a scenario guest.

The generated cloud-init user-data keeps the reviewed ``hover-gate-102``
fixture (Weston + kiosk Chrome + the input-to-pixel page) and adds:

* the scenario pages from ``lab/proxmox9/scenarios`` and a local H.264 clip,
  all switched from inside the guest by Alt+1..4 (see ``scenarios/switch.js``);
* an additional root SSH key for laboratory maintenance;
* a fresh ``instance-id`` so cloud-init applies the files on the next boot.

``packages``/``package_update`` are dropped: the fixture image already carries
Weston, Xwayland and Chrome, and the comparison guest must not depend on
Internet access during re-provisioning.
"""
from __future__ import annotations

import argparse
import base64
import pathlib
import sys

import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture-user-data", required=True, type=pathlib.Path)
    parser.add_argument("--scenarios", required=True, type=pathlib.Path,
                        help="directory with switch.js, video.html, document.html, motion.html")
    parser.add_argument("--clip", required=True, type=pathlib.Path, help="H.264 MP4 written as /opt/qsm-hover/clip.mp4")
    parser.add_argument("--ssh-public-key", required=True, type=pathlib.Path)
    parser.add_argument("--mac", required=True, help="guest NIC MAC address, lower case")
    parser.add_argument("--address", required=True, help="static IPv4 with prefix, e.g. 192.168.76.6/24")
    parser.add_argument("--gateway", default="192.168.76.1")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--hostname", default="qsm-scenario-guest")
    parser.add_argument("--output", required=True, type=pathlib.Path, help="directory for user-data, meta-data, network-config")
    arguments = parser.parse_args()

    document = yaml.safe_load(arguments.fixture_user_data.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "write_files" not in document:
        print("fixture user-data is not the expected cloud-config", file=sys.stderr)
        return 2
    document.pop("package_update", None)
    document.pop("packages", None)

    key = arguments.ssh_public_key.read_text(encoding="utf-8").strip()
    users = document.setdefault("users", [])
    root = next((user for user in users if isinstance(user, dict) and user.get("name") == "root"), None)
    if root is None:
        root = {"name": "root", "lock_passwd": True}
        users.append(root)
    keys = root.setdefault("ssh_authorized_keys", [])
    if key not in keys:
        keys.append(key)

    index = next((entry for entry in document["write_files"]
                  if isinstance(entry, dict) and entry.get("path") == "/opt/qsm-hover/index.html"), None)
    if index is None:
        print("fixture user-data has no /opt/qsm-hover/index.html", file=sys.stderr)
        return 2
    if "switch.js" not in index["content"]:
        index["content"] = index["content"].rstrip("\n") + '\n<script src="switch.js"></script>\n'

    for name in ("switch.js", "video.html", "document.html", "motion.html"):
        document["write_files"].append({
            "path": f"/opt/qsm-hover/{name}",
            "permissions": "0644",
            "content": (arguments.scenarios / name).read_text(encoding="utf-8"),
        })
    document["write_files"].append({
        "path": "/opt/qsm-hover/clip.mp4",
        "permissions": "0644",
        "encoding": "b64",
        "content": base64.b64encode(arguments.clip.read_bytes()).decode("ascii"),
    })

    arguments.output.mkdir(parents=True, exist_ok=True)
    (arguments.output / "user-data").write_text(
        "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False, width=1_000_000, allow_unicode=True),
        encoding="utf-8")
    (arguments.output / "meta-data").write_text(
        f"instance-id: {arguments.instance_id}\nlocal-hostname: {arguments.hostname}\n", encoding="utf-8")
    (arguments.output / "network-config").write_text(yaml.safe_dump({
        "version": 2,
        "ethernets": {"eth0": {
            "match": {"macaddress": arguments.mac},
            "set-name": "eth0",
            "addresses": [arguments.address],
            "routes": [{"to": "default", "via": arguments.gateway}],
            "nameservers": {"addresses": ["1.1.1.1", "8.8.8.8"]},
        }},
    }, sort_keys=False), encoding="utf-8")
    print(f"seed written to {arguments.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
