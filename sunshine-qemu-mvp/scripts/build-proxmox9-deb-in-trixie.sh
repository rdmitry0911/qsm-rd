#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build and install-test the Debian package in a disposable Debian 13 userspace.
# Proxmox VE 9 is based on Debian Trixie, so this prevents an Ubuntu-built ELF
# or Ubuntu dependency name from leaking into the release artifact.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${QSUNSHINE_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
DEBOOTSTRAP_MIRROR="${QSUNSHINE_DEBOOTSTRAP_MIRROR:-https://deb.debian.org/debian}"
CHROOT_DIR="${QSUNSHINE_TRIXIE_CHROOT:-$(mktemp -d /var/tmp/q-sunshine-trixie.XXXXXX)}"
SOURCE_ARCHIVE="$(mktemp /var/tmp/q-sunshine-trixie-source.XXXXXX.tar)"

die() {
    echo "q-sunshine Trixie package driver: $*" >&2
    exit 1
}

for required in sudo debootstrap tar mktemp find; do
    command -v "$required" >/dev/null 2>&1 || die "missing required command: $required"
done
sudo -n true || die "passwordless sudo is required"
[[ "$(dpkg --print-architecture)" == "amd64" ]] || die "amd64 build host required"
[[ "$CHROOT_DIR" == /var/tmp/q-sunshine-trixie.* ]] ||
    die "QSUNSHINE_TRIXIE_CHROOT must be a unique /var/tmp/q-sunshine-trixie.* directory"

for required_path in \
    "$ROOT_DIR/.upstream/Sunshine/CMakeLists.txt" \
    "$ROOT_DIR/.upstream/libva-2.21.0/meson.build" \
    "$ROOT_DIR/integration/sunshine/patches/0007-platform-linux-close-QEMU-listener-transport-before-teardown.patch" \
    "$ROOT_DIR/packaging/debian/build-proxmox9-deb.sh"; do
    [[ -f "$required_path" ]] || die "missing required source: $required_path"
done

sunshine_revision="$(git -C "$ROOT_DIR/.upstream/Sunshine" rev-parse HEAD 2>/dev/null || printf 'source-export')"
project_version="$(sed -nE 's/^project\([^)]* VERSION ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' "$ROOT_DIR/CMakeLists.txt" | head -n1)"
[[ -n "$project_version" ]] || die "could not determine project version from CMakeLists.txt"
git_build_number="$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || printf '0')"
package_version="${QSUNSHINE_DEB_VERSION:-${project_version}+git${git_build_number}}"
sunshine_build_version="${QSUNSHINE_SUNSHINE_BUILD_VERSION:-$(git -C "$ROOT_DIR/.upstream/Sunshine" describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || printf '0.0.0')}"
[[ "$package_version" =~ ^[0-9][0-9A-Za-z.+~:-]*$ ]] || die "invalid Debian package version: $package_version"
[[ "$sunshine_build_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid Sunshine build version: $sunshine_build_version"
echo "Q_SUNSHINE_TRIXIE_BOOTSTRAP_START root=$CHROOT_DIR sunshine_revision=$sunshine_revision"

sudo debootstrap --variant=minbase --arch=amd64 trixie "$CHROOT_DIR" "$DEBOOTSTRAP_MIRROR"
sudo install -d -m 0755 "$CHROOT_DIR/work/source" "$CHROOT_DIR/work/out" "$CHROOT_DIR/work/build"

# The external Sunshine worktree is deliberately exported without .git object
# stores.  All initialized submodule contents remain in the source archive.
tar --create --file "$SOURCE_ARCHIVE" --exclude-vcs --directory "$ROOT_DIR" \
    CMakeLists.txt LICENSE \
    .upstream/Sunshine .upstream/libva-2.21.0 \
    packaging/debian extensions/qsf_control guest integration/sunshine \
    docs/QSF_STREAM_NEGOTIATION.md docs/SUNSHINE_QEMU_INTEGRATION.md
sudo tar --extract --file "$SOURCE_ARCHIVE" --directory "$CHROOT_DIR/work/source"
sudo chmod 0755 "$CHROOT_DIR/work/source/packaging/debian/build-proxmox9-deb.sh"

sudo chroot "$CHROOT_DIR" /usr/bin/env DEBIAN_FRONTEND=noninteractive /bin/bash -ec '
    apt-get update
    apt-get install -y --no-install-recommends \
        build-essential binutils ca-certificates cmake curl dpkg-dev git \
        libboost-dev libcap-dev libcurl4-openssl-dev libdrm-dev libevdev-dev \
        libgbm-dev libglib2.0-dev libicu-dev libminiupnpc-dev libnuma-dev \
        libopus-dev libssl-dev lintian meson ninja-build nlohmann-json3-dev \
        nodejs npm patchelf pkg-config python3 python3-jinja2
'

sudo chroot "$CHROOT_DIR" /usr/bin/env \
    QSUNSHINE_DEB_OUTPUT_DIR=/work/out \
    QSUNSHINE_DEB_WORK_ROOT=/work/build \
    QSUNSHINE_SUNSHINE_REVISION="$sunshine_revision" \
    QSUNSHINE_SUNSHINE_BUILD_VERSION="$sunshine_build_version" \
    QSUNSHINE_DEB_VERSION="$package_version" \
    /bin/bash -ec '
        cd /work/source
        ./packaging/debian/build-proxmox9-deb.sh
    '

mapfile -t artifacts < <(find "$CHROOT_DIR/work/out" -maxdepth 1 -type f -name 'q-sunshine-pve_*_amd64.deb' -print | sort)
[[ "${#artifacts[@]}" -eq 1 ]] || die "expected exactly one package artifact, found ${#artifacts[@]}"

# This uses the clean target userspace and exercises maintainer scripts.  The
# package intentionally leaves every instance disabled, so it cannot start a
# service or mutate a VM configuration during the test.
artifact_in_chroot="${artifacts[0]#"$CHROOT_DIR"}"
[[ "$artifact_in_chroot" == /work/out/q-sunshine-pve_*_amd64.deb ]] ||
    die "package artifact is outside the expected chroot output directory"
sudo chroot "$CHROOT_DIR" /usr/bin/env DEBIAN_FRONTEND=noninteractive \
    /bin/bash -ec "apt-get install -y --no-install-recommends '$artifact_in_chroot' && q-sunshine --version"

mkdir -p "$OUTPUT_DIR"
artifact_name="$(basename "${artifacts[0]}")"
sudo install -m 0644 "${artifacts[0]}" "$OUTPUT_DIR/$artifact_name"
sudo chown "$(id -u):$(id -g)" "$OUTPUT_DIR/$artifact_name"
sha256sum "$OUTPUT_DIR/$artifact_name" | tee "$OUTPUT_DIR/$artifact_name.sha256"

echo "Q_SUNSHINE_TRIXIE_BUILD_OK artifact=$OUTPUT_DIR/$artifact_name"
echo "Q_SUNSHINE_TRIXIE_CHROOT_RETAINED root=$CHROOT_DIR source_archive=$SOURCE_ARCHIVE"
