#!/bin/bash
set -euo pipefail
mkdir -p /tmp/qsm-intel-lab
repo=/home/dima/Projects/q-sunshine
builder=/var/tmp/qsm-dev-trixie/work/qsm-shared24
manifest=$(mktemp)
trap 'rm -f "$manifest"' EXIT
git -C "$repo" ls-files -z qsm-direct | python3 -c 'import sys; data=sys.stdin.buffer.read().split(b"\0"); sys.stdout.buffer.write(b"\0".join(p[len(b"qsm-direct/"):] for p in data if p)+b"\0")' > "$manifest"
sudo -n mkdir -p "$builder"
sudo -n rsync -a --from0 --files-from="$manifest" "$repo/qsm-direct/" "$builder/"
sudo -n chroot /var/tmp/qsm-dev-trixie /bin/bash -c 'cd /work/qsm-shared24 && QSM_DIRECT_DEB_VERSION=0.4.0+shared24 QSM_DIRECT_DEB_JOBS=4 bash packaging/debian/build-qsm-pve-direct-deb.sh && QSM_DIRECT_DEB_VERSION=0.4.0+shared24 bash packaging/debian/build-qsm-pve-direct-lxc-deb.sh' > /tmp/qsm-intel-lab/build24.log 2>&1
