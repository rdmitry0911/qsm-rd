#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build and install-smoke-test qsm-pve-direct in Proxmox VE 9's Debian 13 ABI.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${QSM_DIRECT_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
MIRROR="${QSM_DIRECT_DEBOOTSTRAP_MIRROR:-https://deb.debian.org/debian}"
CHROOT_DIR="${QSM_DIRECT_TRIXIE_CHROOT:-$(mktemp -d /var/tmp/qsm-direct-trixie.XXXXXX)}"
SOURCE_ARCHIVE="$(mktemp /var/tmp/qsm-direct-trixie-source.XXXXXX.tar)"
PVE_KEY="$(mktemp /var/tmp/qsm-direct-pve-key.XXXXXX)"
readonly PVE_KEY_URL='https://enterprise.proxmox.com/debian/proxmox-release-trixie.gpg'
readonly PVE_KEY_SHA256='1bcd2d5bab556076c9ea756a84fe2b7445b13f4ef6e97b2e412b68778377ba6d'

die() { echo "qsm direct Trixie driver: $*" >&2; exit 1; }
for command in sudo debootstrap tar mktemp curl sha256sum install find; do
    command -v "$command" >/dev/null 2>&1 || die "missing command: $command"
done
sudo -n true || die "passwordless sudo is required"
[[ "$CHROOT_DIR" == /var/tmp/qsm-direct-trixie.* ]] || die "chroot path must be a unique /var/tmp/qsm-direct-trixie.* directory"
for file in \
    "$ROOT_DIR/packaging/debian/build-qsm-pve-direct-deb.sh" \
    "$ROOT_DIR/scripts/proxmox-pve9-no-subscription.sources" \
    "$ROOT_DIR/tools/qsm_direct_media_worker.cpp" \
    "$ROOT_DIR/extensions/direct_terminal/qsm_direct_terminal.py" \
    "$ROOT_DIR/lab/proxmox9/qualify-qsm-direct-worker-media-e2e.py"; do
    [[ -f "$file" ]] || die "missing source: $file"
done

project_version="$(sed -n 's/^project(sunshine_qemu_mvp VERSION \([^ ]*\).*/\1/p' "$ROOT_DIR/CMakeLists.txt" | head -n1)"
build_number="$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || printf 0)"
version="${QSM_DIRECT_DEB_VERSION:-${project_version}+git${build_number}.direct}"
dpkg --validate-version "$version" >/dev/null 2>&1 || die "invalid Debian version"

sudo debootstrap --variant=minbase --arch=amd64 trixie "$CHROOT_DIR" "$MIRROR"
sudo install -d -m 0755 "$CHROOT_DIR/work/source" "$CHROOT_DIR/work/out" "$CHROOT_DIR/work/build"
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$PVE_KEY" "$PVE_KEY_URL"
[[ "$(sha256sum "$PVE_KEY" | awk '{print $1}')" == "$PVE_KEY_SHA256" ]] || die "PVE release key digest mismatch"
sudo install -D -m 0644 "$PVE_KEY" "$CHROOT_DIR/usr/share/keyrings/proxmox-release-trixie.gpg"
sudo install -D -m 0644 "$ROOT_DIR/scripts/proxmox-pve9-no-subscription.sources" \
    "$CHROOT_DIR/etc/apt/sources.list.d/qsm-direct-pve9.sources"

tar --create --file "$SOURCE_ARCHIVE" --exclude-vcs --directory "$ROOT_DIR" \
    CMakeLists.txt LICENSE src tools packaging/debian extensions/browser_bridge extensions/direct_guest extensions/direct_terminal \
    integration/proxmox/pve9/api integration/proxmox/pve9/direct_api integration/proxmox/pve9/direct_ui \
    integration/proxmox/pve9/ui \
    lab/proxmox9/qualify-qsm-direct-worker-media-e2e.py \
    scripts/proxmox-pve9-no-subscription.sources
sudo tar --extract --file "$SOURCE_ARCHIVE" --directory "$CHROOT_DIR/work/source"
sudo chmod 0755 "$CHROOT_DIR/work/source/packaging/debian/build-qsm-pve-direct-deb.sh"

sudo chroot "$CHROOT_DIR" /usr/bin/env DEBIAN_FRONTEND=noninteractive /bin/bash -ec '
  apt-get update
  apt-get install -y --no-install-recommends \
    build-essential ca-certificates cmake dbus dpkg-dev ffmpeg libdrm-dev libepoxy-dev libgbm-dev \
    libavcodec-dev libavutil-dev libswscale-dev libopus-dev libsystemd-dev ninja-build pkg-config python3 python3-aiortc python3-av \
    pve-manager qemu-server
'
sudo chroot "$CHROOT_DIR" /usr/bin/env \
    QSM_DIRECT_DEB_OUTPUT_DIR=/work/out QSM_DIRECT_DEB_WORK_ROOT=/work/build \
    QSM_DIRECT_DEB_VERSION="$version" /bin/bash -ec '
      cd /work/source
      ./packaging/debian/build-qsm-pve-direct-deb.sh
    '

mapfile -t artifacts < <(find "$CHROOT_DIR/work/out" -maxdepth 1 -type f -name 'qsm-pve-direct_*_amd64.deb' -print | sort)
[[ "${#artifacts[@]}" -eq 1 ]] || die "expected one direct package artifact"
artifact_in_chroot="${artifacts[0]#"$CHROOT_DIR"}"
sudo chroot "$CHROOT_DIR" /usr/bin/env DEBIAN_FRONTEND=noninteractive /bin/bash -ec \
    "apt-get install -y --no-install-recommends '$artifact_in_chroot' && \\
     test -x /usr/lib/qsm-pve-direct/bin/qsm-direct-media-worker && \\
     test -x /usr/bin/qsm-pve-direct-terminal && \\
     ! find /usr/lib/qsm-pve-direct -type f -print0 | xargs -0 grep -I -q -E 'sunshine|moonlight' && \\
     cmake -S /work/source -B /work/e2e -G Ninja -DCMAKE_BUILD_TYPE=Release \\
       -DQMDP_BUILD_QT_CLIENT=OFF -DQMDP_BUILD_DBUS=ON -DQMDP_BUILD_TOOLS=ON -DBUILD_TESTING=OFF && \\
     cmake --build /work/e2e --target qmdp-fake-qemu --parallel '$BUILD_JOBS' && \\
     python3 /work/source/lab/proxmox9/qualify-qsm-direct-worker-media-e2e.py \\
       --worker /usr/lib/qsm-pve-direct/bin/qsm-direct-media-worker --fake-qemu /work/e2e/qmdp-fake-qemu"
mkdir -p "$OUTPUT_DIR"
name="$(basename "${artifacts[0]}")"
sudo install -m 0644 "${artifacts[0]}" "$OUTPUT_DIR/$name"
sudo chown "$(id -u):$(id -g)" "$OUTPUT_DIR/$name"
(cd "$OUTPUT_DIR" && sha256sum "$name" | tee "$name.sha256")
echo "QSM_DIRECT_TRIXIE_BUILD_AND_INSTALL_OK artifact=$OUTPUT_DIR/$name chroot=$CHROOT_DIR"
