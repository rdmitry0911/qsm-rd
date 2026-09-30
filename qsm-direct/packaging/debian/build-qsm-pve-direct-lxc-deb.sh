#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build qsm-pve-direct-lxc: the LXC console for a Proxmox VE node -- the setup
# tool plus the container package (qsm-console-guest) it installs into a
# container.  The media worker and service come from qsm-pve-direct.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/packaging/debian"
OUTPUT_DIR="${QSM_DIRECT_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
die() { echo "qsm-pve-direct-lxc package: $*" >&2; exit 1; }

project_version="$(sed -n 's/^project(qsm_direct VERSION \([^ ]*\).*/\1/p' "$ROOT_DIR/CMakeLists.txt" | head -n1)"
[[ -n "$project_version" ]] || die "could not determine project version"
git_build_number="$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || printf 0)"
package_version="${QSM_DIRECT_DEB_VERSION:-${project_version}+git${git_build_number}.direct}"
export QSM_DIRECT_DEB_VERSION="$package_version" QSM_DIRECT_DEB_OUTPUT_DIR="$OUTPUT_DIR"

bash "$ROOT_DIR/packaging/lxc/build-qsm-console-guest-deb.sh" >/dev/null
guest="$OUTPUT_DIR/qsm-console-guest_${package_version}_all.deb"
[[ -f "$guest" ]] || die "the container package was not built"

package=qsm-pve-direct-lxc
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
install -d "$stage/usr/sbin" "$stage/usr/share/$package" "$stage/usr/share/doc/$package" "$stage/DEBIAN"
install -m 0755 "$PACKAGE_DIR/qsm-pve-direct-lxc" "$stage/usr/sbin/qsm-pve-direct-lxc"
install -m 0644 "$guest" "$stage/usr/share/$package/qsm-console-guest.deb"
install -m 0644 "$ROOT_DIR/packaging/lxc/guest/README" "$stage/usr/share/doc/$package/README.guest"

cat > "$stage/DEBIAN/control" <<EOF
Package: $package
Version: $package_version
Section: admin
Priority: optional
Architecture: all
Maintainer: Dmitry R <rdmitry0911@gmail.com>
Depends: qsm-pve-direct (= $package_version), pve-container, python3
Breaks: qsm-pve-direct (<< $package_version)
Replaces: qsm-pve-direct (<< $package_version)
Description: QSM Direct graphical console for LXC containers on Proxmox VE
 'qsm-pve-direct-lxc enable <vmid>' prepares a Debian/Ubuntu container with a
 KDE Plasma desktop for the QSM Direct console: GPU passthrough, NVIDIA
 userspace matching the host, and the container package qsm-console-guest
 (one console with its own login screen per tty, like the terminal console).
 The container's Console button then offers "QSM Direct".
EOF
( cd "$stage" && find usr -type f -print0 | sort -z | xargs -0 -r md5sum ) > "$stage/DEBIAN/md5sums"
artifact="$OUTPUT_DIR/${package}_${package_version}_all.deb"
dpkg-deb --root-owner-group --build "$stage" "$artifact" >/dev/null
echo "QSM_PVE_DIRECT_LXC_DEB_BUILD_OK artifact=$artifact guest=$guest"
