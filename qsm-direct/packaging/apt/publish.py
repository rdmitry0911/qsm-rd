#!/usr/bin/env python3
"""Build the signed QSM APT site from explicitly approved release assets.

Requires Python 3, dpkg-dev, apt-utils and GnuPG. Never builds PVE packages.
The release manifest pins every binary by SHA256 before dpkg reads it.
"""

import argparse
import datetime as dt
import email.utils
import gzip
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.request import urlopen


HERE = Path(__file__).resolve().parent
PACKAGES = {
    "qsm-pve-direct": "amd64",
    "qsm-pve-direct-standalone": "all",
    "qsm-pve-direct-gateway": "all",
    "qsm-pve-direct-lxc": "all",
    "qsm-console-guest": "all",
}


def run(*args, **kwargs):
    return subprocess.check_output(args, **kwargs)


def digest(path, algorithm="sha256"):
    return hashlib.new(algorithm, path.read_bytes()).hexdigest()


def validate_manifest(manifest):
    if (manifest["schema"], manifest["repository"], manifest["suite"],
            manifest["component"]) != (1, "rdmitry0911/qsm-rd", "trixie", "main"):
        raise ValueError("unsupported repository or suite")
    if not re.fullmatch(r"[0-9A-F]{40}", manifest["signing_fingerprint"]):
        raise ValueError("invalid signing fingerprint")
    dt.datetime.strptime(manifest["keyring_version"], "%Y.%m.%d")
    seen = set()
    for release in manifest["releases"]:
        if not re.fullmatch(r"v[0-9A-Za-z.+-]+", release["tag"]):
            raise ValueError("unsafe release tag")
        if not re.fullmatch(r"[0-9a-f]{40}", release["source_commit"]):
            raise ValueError("invalid source commit")
        if {a["package"] for a in release["assets"]} != set(PACKAGES):
            raise ValueError("a release must contain exactly the five QSM packages")
        if len(release["assets"]) != len(PACKAGES):
            raise ValueError("duplicate package")
        if len({a["version"] for a in release["assets"]}) != 1:
            raise ValueError("QSM packages must have matching versions")
        for asset in release["assets"]:
            name, version, arch = (asset[k] for k in ("package", "version", "architecture"))
            if arch != PACKAGES[name] or not re.fullmatch(r"[0-9][0-9A-Za-z.+~:-]*", version):
                raise ValueError("unexpected architecture or version")
            if asset["filename"] != f"{name}_{version}_{arch}.deb" or ":" in version:
                raise ValueError("unsafe package filename")
            if asset["filename"] in seen:
                raise ValueError("package version already published")
            seen.add(asset["filename"])
            if not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
                raise ValueError("invalid SHA256")


def fetch_asset(release, asset, cache):
    path = cache / asset["filename"]
    if not path.exists():
        url = ("https://github.com/rdmitry0911/qsm-rd/releases/download/"
               f"{release['tag']}/{asset['filename']}")
        with urlopen(url, timeout=120) as response:
            data = response.read(40 * 1024 * 1024 + 1)
        if len(data) > 40 * 1024 * 1024:
            raise ValueError("package exceeds size limit")
        path.write_bytes(data)
    if digest(path) != asset["sha256"]:
        raise ValueError(f"SHA256 mismatch: {asset['filename']}")
    fields = run("dpkg-deb", "--show", "--showformat=${Package}\t${Version}\t${Architecture}",
                 str(path), text=True).split("\t")
    if fields != [asset[k] for k in ("package", "version", "architecture")]:
        raise ValueError(f"unexpected package metadata: {asset['filename']}")
    return path


def build_keyring(manifest, output):
    version = manifest["keyring_version"]
    epoch = int(dt.datetime.strptime(version, "%Y.%m.%d").replace(tzinfo=dt.timezone.utc).timestamp())
    with tempfile.TemporaryDirectory(prefix="qsm-keyring-") as tmp:
        stage = Path(tmp)
        (stage / "DEBIAN").mkdir()
        (stage / "DEBIAN/control").write_text(
            "Package: qsm-archive-keyring\n"
            f"Version: {version}\n"
            "Architecture: all\nSection: misc\nPriority: optional\n"
            "Maintainer: Dmitry R <rdmitry0911@gmail.com>\n"
            "Description: Signing certificate for the QSM Direct APT repository\n"
            " Installs the scoped public archive key. Does not enable a repository\n"
            " or install any console, Proxmox or guest package.\n")
        key = stage / "usr/share/keyrings/qsm-archive-keyring.gpg"
        key.parent.mkdir(parents=True)
        shutil.copyfile(HERE / key.name, key)
        copyright_file = stage / "usr/share/doc/qsm-archive-keyring/copyright"
        copyright_file.parent.mkdir(parents=True)
        copyright_file.write_text(
            "QSM Direct archive keyring\nCopyright 2026 Dmitry R\n"
            "License: GPL-3.0-or-later\n"
            "On Debian systems see /usr/share/common-licenses/GPL-3.\n")
        for path in sorted(stage.rglob("*"), reverse=True) + [stage]:
            path.chmod(0o755 if path.is_dir() else 0o644)
            os.utime(path, (epoch, epoch))
        result = output / f"qsm-archive-keyring_{version}_all.deb"
        run("dpkg-deb", "--root-owner-group", "--uniform-compression", "-Zxz",
            "--build", str(stage), str(result),
            env=dict(os.environ, SOURCE_DATE_EPOCH=str(epoch)))
    return result


def build(manifest_path, output, cache):
    manifest = json.loads(manifest_path.read_text())
    validate_manifest(manifest)
    fingerprint = manifest["signing_fingerprint"]
    if (HERE / "archive-key-fingerprint.txt").read_text().strip() != fingerprint:
        raise ValueError("fingerprint file does not match manifest")
    key_info = run("gpg", "--batch", "--with-colons", "--show-keys",
                   str(HERE / "qsm-archive-keyring.gpg"), text=True)
    if next(line.split(":")[9] for line in key_info.splitlines()
            if line.startswith("fpr:")) != fingerprint:
        raise ValueError("public key does not match manifest")
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    pool = output / "pool/main/q/qsm-direct"
    pool.mkdir(parents=True)
    for release in manifest["releases"]:
        for asset in release["assets"]:
            shutil.copyfile(fetch_asset(release, asset, cache), pool / asset["filename"])
    build_keyring(manifest, pool)
    packages = run("dpkg-scanpackages", "--multiversion", "pool", "/dev/null", cwd=output)
    dist = output / "dists/trixie"
    for arch in ("amd64", "all"):
        index = dist / f"main/binary-{arch}"
        index.mkdir(parents=True)
        filtered = packages
        if arch == "all":
            filtered = b"\n\n".join(p for p in packages.strip().split(b"\n\n")
                                       if b"\nArchitecture: all\n" in p) + b"\n\n"
        (index / "Packages").write_bytes(filtered)
        (index / "Packages.gz").write_bytes(gzip.compress(filtered, mtime=0))
        for name in ("Packages", "Packages.gz"):
            for algorithm in ("SHA256", "SHA512"):
                target = index / "by-hash" / algorithm
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(index / name, target / digest(index / name, algorithm.lower()))
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    options = {
        "Origin": "QSM Direct", "Label": "QSM Direct preview",
        "Suite": "trixie", "Codename": "trixie", "Components": "main",
        "Architectures": "amd64 all", "Description": "Standalone QSM Direct for Proxmox VE 9",
        "Date": email.utils.format_datetime(now, usegmt=True),
        "NotAutomatic": "yes", "ButAutomaticUpgrades": "yes", "Acquire-By-Hash": "yes",
    }
    args = ["apt-ftparchive"]
    for key, value in options.items():
        args.extend(["-o", f"APT::FTPArchive::Release::{key}={value}"])
    # apt-ftparchive excludes by-hash when calculating Release checksums.
    release_file = dist / "Release"
    # Several apt-ftparchive versions silently ignore a Valid-Until option.
    # Write this authenticated header explicitly before signing the result.
    expires = email.utils.format_datetime(now + dt.timedelta(days=30), usegmt=True)
    release_file.write_bytes(f"Valid-Until: {expires}\n".encode()
                             + run(*args, "release", "dists/trixie", cwd=output))
    for mode, name in (("--clearsign", "InRelease"), ("--detach-sign", "Release.gpg")):
        run("gpg", "--batch", "--yes", "--local-user", fingerprint,
            "--digest-algo", "SHA512", "--armor", "--output", str(dist / name),
            mode, str(release_file))
    for name in ("qsm-archive-keyring.gpg", "qsm-archive-keyring.asc", "archive-key-fingerprint.txt"):
        shutil.copyfile(HERE / name, output / name)
    (output / "approved-releases.json").write_text(json.dumps(manifest, indent=2) + "\n")
    readme = (HERE / "README.md").read_text()
    (output / "README.txt").write_text(readme)
    (output / ".nojekyll").touch()
    (output / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>QSM Direct APT repository</title>'
        '<style>body{max-width:76ch;margin:2rem auto;padding:0 1rem;line-height:1.5}'
        'pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
        '<h1>QSM Direct APT repository</h1><p>'
        '<a href="https://github.com/rdmitry0911/qsm-rd">Source and documentation</a> · '
        '<a href="dists/trixie/InRelease">Signed metadata</a> · '
        '<a href="qsm-archive-keyring.gpg">Public signing key</a>'
        '</p><pre>' + html.escape(readme) + '</pre></html>')
    print(f"APT site ready: {output}; {len(manifest['releases'])} approved release(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "releases.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    build(args.manifest, args.output.resolve(), args.cache.resolve())
