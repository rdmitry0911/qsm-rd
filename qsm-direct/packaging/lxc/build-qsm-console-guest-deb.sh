#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build qsm-console-guest: the container side of the QSM Direct LXC console
# (displays, login manager, greeter, session launcher).  Architecture: all.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
LXC_DIR="$ROOT_DIR/packaging/lxc"
OUTPUT_DIR="${QSM_DIRECT_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
die() { echo "qsm-console-guest package: $*" >&2; exit 1; }

for command in dpkg-deb install find md5sum sed; do
    command -v "$command" >/dev/null 2>&1 || die "missing command: $command"
done
project_version="$(sed -n 's/^project(qsm_direct VERSION \([^ ]*\).*/\1/p' "$ROOT_DIR/CMakeLists.txt" | head -n1)"
[[ -n "$project_version" ]] || die "could not determine project version"
git_build_number="$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || printf 0)"
package_version="${QSM_DIRECT_DEB_VERSION:-${project_version}+git${git_build_number}.direct}"

package=qsm-console-guest
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
lib="$stage/usr/lib/qsm-console"
install -d "$lib" "$stage/usr/share/qsm-console" "$stage/usr/lib/systemd/system" \
    "$stage/usr/lib/systemd/user/plasma-kwin_wayland.service.d" "$stage/etc/pam.d" \
    "$stage/usr/share/doc/$package" "$stage/DEBIAN"
for script in qsm-login qsm-greeter qsm-session-launch qsm-seat-keeper qsm-consoles qsm-display-start \
              qsm-clipboard-agent; do
    install -m 0755 "$LXC_DIR/$script" "$lib/$script"
done
install -m 0644 "$LXC_DIR/qsm-display-sway.conf" "$stage/usr/share/qsm-console/sway.conf"
for unit in qsm-display@.service qsm-seat-keeper@.service qsm-login@.service qsm-consoles.service; do
    install -m 0644 "$LXC_DIR/guest/$unit" "$stage/usr/lib/systemd/system/$unit"
done
install -m 0644 "$LXC_DIR/guest/qsm-nested.conf" \
    "$stage/usr/lib/systemd/user/plasma-kwin_wayland.service.d/qsm-nested.conf"
install -m 0644 "$LXC_DIR/guest/pam-qsm-login" "$stage/etc/pam.d/qsm-login"
install -m 0644 "$LXC_DIR/guest/pam-qsm-direct-session" "$stage/etc/pam.d/qsm-direct-session"
install -m 0644 "$LXC_DIR/guest/README" "$stage/usr/share/doc/$package/README"
for script in postinst prerm postrm; do
    install -m 0755 "$LXC_DIR/guest/$script" "$stage/DEBIAN/$script"
done
printf '%s\n' /etc/pam.d/qsm-login /etc/pam.d/qsm-direct-session > "$stage/DEBIAN/conffiles"

cat > "$stage/DEBIAN/control" <<EOF
Package: $package
Version: $package_version
Section: x11
Priority: optional
Architecture: all
Maintainer: Dmitry R <rdmitry0911@gmail.com>
Depends: sway, xwayland, python3, python3-pyqt5, qtwayland5, acl, x11-xserver-utils, systemd, libpam-systemd, plasma-workspace
Recommends: kwin-wayland, plasma-workspace-wayland, kwin-x11
Description: QSM Direct graphical console inside an LXC container
 The container side of the QSM Direct LXC console for Proxmox VE: one headless
 display per tty with its own login screen, and each login a real logind
 session running KDE Plasma (Wayland or X11) inside that display.  The host's
 media worker streams it to the browser over WebRTC.  Installed by the host
 tool qsm-pve-direct-lxc.
EOF
( cd "$stage" && find usr etc -type f -print0 | sort -z | xargs -0 -r md5sum ) > "$stage/DEBIAN/md5sums"
mkdir -p "$OUTPUT_DIR"
artifact="$OUTPUT_DIR/${package}_${package_version}_all.deb"
dpkg-deb --root-owner-group --build "$stage" "$artifact" >/dev/null
echo "QSM_CONSOLE_GUEST_DEB_BUILD_OK artifact=$artifact"
