#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install a root-owned tree readable by the authenticated users' media agents.
set -euo pipefail
if (( EUID != 0 )); then
  echo 'Run this installer with sudo after building qsm-direct-media-worker.' >&2
  exit 1
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
QSM_INSTALL_PREFIX="${1:-/opt/qsm-standalone}"
python3 - "$ROOT" "${BUILD_DIR:-$ROOT/build}/qsm-direct-media-worker" "$QSM_INSTALL_PREFIX" <<'PY'
from pathlib import Path
import os, shutil, sys
source, worker, prefix = map(Path, sys.argv[1:])
if not prefix.is_absolute() or len(prefix.parts) < 3 or prefix.is_symlink():
    raise SystemExit('Choose an absolute installation directory, such as /opt/qsm-standalone')
if prefix.exists() and (prefix.stat().st_uid != 0 or prefix.stat().st_mode & 0o022):
    raise SystemExit('Existing installation directory must be owned by root and not writable by others')
if not worker.is_file() or not os.access(worker, os.X_OK):
    raise SystemExit('Build qsm-direct-media-worker first; set BUILD_DIR for another build')
prefix.mkdir(mode=0o755, parents=True, exist_ok=True)
prefix.chmod(0o755)
def directory(path):
    if path == prefix:
        return
    directory(path.parent)
    if path.is_symlink() or (path.exists() and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022)):
        raise SystemExit('Unsafe installation subdirectory: ' + str(path))
    path.mkdir(mode=0o755, exist_ok=True)
    path.chmod(0o755)
def copy_file(source_path, destination, mode):
    directory(destination.parent)
    if destination.is_symlink() or (destination.exists() and destination.stat().st_uid != 0):
        raise SystemExit('Unsafe installation file: ' + str(destination))
    shutil.copyfile(source_path, destination)
    os.chown(destination, 0, 0)
    destination.chmod(mode)
def install(relative, target=None, mode=0o644):
    destination = prefix / (target or relative)
    copy_file(source / relative, destination, mode)
for package in ['standalone', 'browser_bridge']:
    for path in (source / 'extensions' / package).rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            install(path.relative_to(source))
install('extensions/signal/qsm_gateway.py', 'extensions/browser_bridge/qsm_gateway.py')
install('integration/proxmox/pve9/direct_ui/qsm-direct-console.js')
install('scripts/run-qsm-standalone.sh', mode=0o755)
copy_file(worker, prefix / 'bin/qsm-direct-media-worker', 0o755)
directory(prefix / '.venv')
policy = Path('/etc/pam.d/qsm-standalone')
if not policy.exists():
    shutil.copyfile(source / 'packaging/pam.d/qsm-standalone', policy)
    policy.chmod(0o644)
elif policy.read_bytes() != (source / 'packaging/pam.d/qsm-standalone').read_bytes():
    print('Keeping the existing administrator-defined /etc/pam.d/qsm-standalone policy.')
PY
python3 -m venv "$QSM_INSTALL_PREFIX/.venv"
"$QSM_INSTALL_PREFIX/.venv/bin/pip" install --only-binary=av \
  -r "$QSM_INSTALL_PREFIX/extensions/standalone/requirements.txt"
echo "Installed QSM host login in $QSM_INSTALL_PREFIX; PAM service: qsm-standalone."
