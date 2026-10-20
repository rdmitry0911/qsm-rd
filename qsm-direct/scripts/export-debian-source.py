#!/usr/bin/env python3
"""Export a pinned, source-only standalone tree from the QSM monorepository."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
TOP = {"CMakeLists.txt", "LICENSE", "LICENSE.spdx", "README.md"}
PREFIXES = ("src/", "tools/", "extensions/", "protocols/", "tests/", "guest/",
            "packaging/debian/", "packaging/guest/", "packaging/lxc/",
            "packaging/webrtc-viewer/", "integration/proxmox/pve9/direct_ui/", "debian/")
EXCLUDE = ("packaging/lxc/sddm/", "packaging/lxc/build-qsm-console-sddm-deb.sh",
           "packaging/lxc/build-qsm-console-sway-deb.sh")


def export(revision, output, version):
    if output.exists():
        raise ValueError("choose a new output directory")
    commit = subprocess.check_output(["git", "rev-parse", revision], cwd=ROOT, text=True).strip()
    epoch = int(subprocess.check_output(["git", "show", "-s", "--format=%ct", commit], cwd=ROOT))
    data = subprocess.check_output(["git", "archive", f"{commit}:qsm-direct"], cwd=ROOT)
    parent = f"qsm-pve-direct-{version}"
    output.mkdir(parents=True)
    orig = output / f"qsm-pve-direct_{version}.orig.tar.xz"
    packaging = []
    inventory = []
    with tarfile.open(fileobj=io.BytesIO(data)) as source, tarfile.open(orig, "w:xz") as target:
        for member in source:
            name = member.name
            if not member.isfile() or name.startswith(EXCLUDE):
                continue
            if name not in TOP and not name.startswith(PREFIXES):
                continue
            if name.endswith((".deb", ".pyc", ".so", ".o", ".a")) or "__pycache__" in name:
                raise ValueError(f"unexpected prebuilt file: {name}")
            payload = source.extractfile(member).read()
            mode = 0o755 if member.mode & 0o111 else 0o644
            if name.startswith("debian/"):
                packaging.append((name, payload, mode))
                continue
            entry = tarfile.TarInfo(f"{parent}/{name}")
            entry.size = len(payload)
            entry.mode = mode
            entry.uid = entry.gid = 0
            entry.mtime = epoch
            target.addfile(entry, io.BytesIO(payload))
            inventory.append({"path": name, "sha256": hashlib.sha256(payload).hexdigest()})
    if not packaging:
        raise ValueError("revision has no Debian source packaging")
    tree = output / parent
    with tarfile.open(orig) as archive:
        archive.extractall(output, filter="data")
    for name, payload, mode in packaging:
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)
    (output / "export-provenance.json").write_text(json.dumps({
        "git_commit": commit, "upstream_version": version,
        "orig_sha256": hashlib.sha256(orig.read_bytes()).hexdigest(),
        "upstream_files": inventory,
    }, indent=2) + "\n")
    print(tree)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--version", default="0.4.0+20261020")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.revision, args.output.resolve(), args.version)
