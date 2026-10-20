#!/usr/bin/env python3
"""Use an isolated real APT client; never change the host's APT configuration."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from publish import HERE, PACKAGES


def apt_environment(root, uri, key, preferences=""):
    root.mkdir(parents=True)
    for name in ("lists/partial", "archives/partial", "empty", "downloads"):
        (root / name).mkdir(parents=True)
    (root / "status").touch()
    (root / "sources.list").write_text(f"deb [arch=amd64 signed-by={key}] {uri} trixie main\n")
    (root / "preferences").write_text(preferences)
    config = root / "apt.conf"
    config.write_text(
        f'Dir::Etc::main "/dev/null"; Dir::Etc::parts "{root}/empty";\n'
        f'Dir::Etc::sourcelist "{root}/sources.list"; Dir::Etc::sourceparts "{root}/empty";\n'
        f'Dir::Etc::preferences "{root}/preferences"; Dir::Etc::preferencesparts "{root}/empty";\n'
        f'Dir::State "{root}"; Dir::State::status "{root}/status";\n'
        f'Dir::State::lists "{root}/lists"; Dir::Cache "{root}";\n'
        f'Dir::Cache::archives "{root}/archives"; Dir::Log "{root}";\n'
        'APT::Architecture "amd64"; APT::Get::List-Cleanup "0";\n'
        'Acquire::Languages "none"; Acquire::AllowInsecureRepositories "false";\n'
        'Acquire::AllowDowngradeToInsecureRepositories "false";\n'
        'Acquire::Check-Valid-Until "true"; Acquire::Retries "0";\n'
        'APT::Update::Error-Mode "any";\n')
    return dict(os.environ, APT_CONFIG=str(config), LC_ALL="C")


def command(env, *args, cwd=None, must_pass=True):
    result = subprocess.run(args, env=env, cwd=cwd, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if must_pass and result.returncode:
        raise RuntimeError(result.stdout)
    return result


def main(site, url):
    manifest = json.loads((site / "approved-releases.json").read_text())
    key = (site / "qsm-archive-keyring.gpg").resolve()
    with tempfile.TemporaryDirectory(prefix="qsm-apt-verify-") as tmp:
        root = Path(tmp)
        env = apt_environment(root / "valid", url or site.resolve().as_uri(), key)
        update = command(env, "apt-get", "update")
        if "WARNING:" in update.stdout or "W:" in update.stdout:
            raise RuntimeError(update.stdout)
        print("PASS: signed APT update, no authentication warnings")
        expected = {}
        for release in manifest["releases"]:
            for asset in release["assets"]:
                expected[asset["filename"]] = asset["sha256"]
                command(env, "apt-get", "download", f"{asset['package']}={asset['version']}",
                        cwd=root / "valid/downloads")
        command(env, "apt-get", "download", "qsm-archive-keyring", cwd=root / "valid/downloads")
        for path in (root / "valid/downloads").glob("*.deb"):
            if path.name in expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected[path.name]:
                raise RuntimeError(f"download checksum mismatch: {path.name}")
        if len(list((root / "valid/downloads").glob("*.deb"))) != len(expected) + 1:
            raise RuntimeError("missing package download")
        print(f"PASS: real APT download and SHA256 verification of all {len(expected) + 1} packages")
        names = command(env, "apt-cache", "pkgnames").stdout.splitlines()
        if set(names) != set(PACKAGES) | {"qsm-archive-keyring"}:
            raise RuntimeError(f"unexpected archive packages: {names}")
        print("PASS: only six allowlisted QSM packages, no Proxmox replacements")

        changed = root / "changed"
        shutil.copytree(site, changed)
        signed = changed / "dists/trixie/InRelease"
        data = signed.read_text()
        if "Origin: QSM Direct" not in data:
            raise RuntimeError("test fixture does not contain expected signed field")
        signed.write_text(data.replace("Origin: QSM Direct", "Origin: TAMPERED!!", 1))
        bad_env = apt_environment(root / "bad-signature", changed.as_uri(), key)
        result = command(bad_env, "apt-get", "update", must_pass=False)
        if result.returncode == 0 or not any(s in result.stdout.lower()
                                           for s in ("signature", "badsig", "message has been manipulated")):
            raise RuntimeError("APT did not reject tampered metadata:\n" + result.stdout)
        print("PASS: APT rejects tampered InRelease")

        shutil.copyfile(site / "dists/trixie/InRelease", signed)
        bad_env = apt_environment(root / "bad-package", changed.as_uri(), key)
        command(bad_env, "apt-get", "update")
        asset = manifest["releases"][-1]["assets"][0]
        deb = changed / "pool/main/q/qsm-direct" / asset["filename"]
        data = bytearray(deb.read_bytes())
        data[-1] ^= 1
        deb.write_bytes(data)
        result = command(bad_env, "apt-get", "download", asset["package"],
                         cwd=root / "bad-package/downloads", must_pass=False)
        if result.returncode == 0 or "hash sum mismatch" not in result.stdout.lower():
            raise RuntimeError("APT did not reject changed package:\n" + result.stdout)
        print("PASS: APT rejects a modified deb with signed indexes unchanged")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--url", help="Also test the live HTTPS repository instead of the local valid site")
    args = parser.parse_args()
    main(args.site.resolve(), args.url)
