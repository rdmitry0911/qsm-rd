#!/usr/bin/env bash
# Build the Linux guest-tools package without any host desktop dependency.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${QSM_GUEST_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
WORK_ROOT="${QSM_GUEST_DEB_WORK_ROOT:-$ROOT_DIR/.packaging-build/qsm-guest-agent}"
VERSION="${QSM_GUEST_DEB_VERSION:-0.4.0+git$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || printf 0).guest}"

for command in gcc dpkg dpkg-deb install mktemp; do command -v "$command" >/dev/null; done
dpkg --validate-version "$VERSION" >/dev/null
mkdir -p "$OUTPUT_DIR" "$WORK_ROOT"
build_root="$(mktemp -d "$WORK_ROOT/build.XXXXXX")"
stage_root="$(mktemp -d "$WORK_ROOT/stage.XXXXXX")"
trap 'status=$?; if [ "$status" -ne 0 ]; then echo "qsm guest package build retained: $build_root $stage_root" >&2; fi; exit "$status"' EXIT

gcc -std=c11 -O2 -D_POSIX_C_SOURCE=200809L -Wall -Wextra -Wpedantic -Werror \
    "$ROOT_DIR/guest/qsf_guest_agent.c" -o "$build_root/qsm-guest-agent"
gcc -std=c11 -O2 -D_POSIX_C_SOURCE=200809L -Wall -Wextra -Wpedantic -Werror \
    "$ROOT_DIR/guest/qsf_state_watcher.c" -o "$build_root/qsm-state-watcher"
package_root="$stage_root/usr/lib/qsm-guest-agent"
install -d "$package_root" "$stage_root/usr/bin" "$stage_root/usr/lib/systemd/system" \
    "$stage_root/usr/lib/systemd/user" "$stage_root/usr/lib/udev/rules.d" \
    "$stage_root/usr/share/doc/qsm-guest-agent" "$stage_root/DEBIAN"
install -m 0755 "$build_root/qsm-guest-agent" "$package_root/qsm-guest-agent"
install -m 0755 "$build_root/qsm-state-watcher" "$package_root/qsm-state-watcher"
install -m 0755 "$ROOT_DIR/guest/qsf_wayland_clipboard_bridge.sh" "$package_root/qsm-wayland-clipboard-bridge"
install -m 0755 "$ROOT_DIR/packaging/guest/qsm-guest-agent-setup" "$stage_root/usr/bin/qsm-guest-agent-setup"
install -m 0644 "$ROOT_DIR/packaging/guest/qsm-guest-agent@.service" "$stage_root/usr/lib/systemd/system/qsm-guest-agent@.service"
install -m 0644 "$ROOT_DIR/packaging/guest/qsm-guest-clipboard.service" "$stage_root/usr/lib/systemd/user/qsm-guest-clipboard.service"
install -m 0644 "$ROOT_DIR/packaging/guest/99-qsm-guest-agent.rules" "$stage_root/usr/lib/udev/rules.d/99-qsm-guest-agent.rules"
install -m 0644 "$ROOT_DIR/packaging/guest/README.qsm-guest-agent" "$stage_root/usr/share/doc/qsm-guest-agent/README.Debian"
cat > "$stage_root/DEBIAN/control" <<EOF
Package: qsm-guest-agent
Version: $VERSION
Section: admin
Priority: optional
Architecture: amd64
Maintainer: qsm contributors <qsm@users.noreply.github.com>
Depends: libc6 (>= 2.34), systemd, udev
Recommends: wl-clipboard
Description: QSM Direct guest clipboard and file agent
 QSM Guest Agent is the optional in-VM companion for qsm-pve-direct. It uses
 a private QEMU virtio-serial port and no network listener.
EOF
printf '%s\n' '#!/bin/sh' 'set -e' 'getent group qsm-guest >/dev/null || groupadd --system qsm-guest' \
    'udevadm control --reload-rules >/dev/null 2>&1 || true' 'systemctl daemon-reload >/dev/null 2>&1 || true' > "$stage_root/DEBIAN/postinst"
chmod 0755 "$stage_root/DEBIAN/postinst"
printf '%s\n' '#!/bin/sh' 'set -e' 'systemctl daemon-reload >/dev/null 2>&1 || true' > "$stage_root/DEBIAN/postrm"
chmod 0755 "$stage_root/DEBIAN/postrm"
( cd "$stage_root"; find usr -type f -print0 | sort -z | xargs -0 md5sum ) > "$stage_root/DEBIAN/md5sums"
artifact="$OUTPUT_DIR/qsm-guest-agent_${VERSION}_amd64.deb"
dpkg-deb --root-owner-group --build "$stage_root" "$artifact"
echo "QSM_GUEST_DEB_BUILD_OK artifact=$artifact"
