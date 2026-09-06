#!/usr/bin/env bash
# Build the Linux desktop-session companion without any host desktop dependency.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${QSM_DESKTOP_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
WORK_ROOT="${QSM_DESKTOP_DEB_WORK_ROOT:-$ROOT_DIR/.packaging-build/qsm-desktop-agent}"
VERSION="${QSM_DESKTOP_DEB_VERSION:-0.4.0+git$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || printf 0).desktop}"

for command in gcc dpkg dpkg-deb install mktemp; do command -v "$command" >/dev/null; done
dpkg --validate-version "$VERSION" >/dev/null
mkdir -p "$OUTPUT_DIR" "$WORK_ROOT"
build_root="$(mktemp -d "$WORK_ROOT/build.XXXXXX")"
stage_root="$(mktemp -d "$WORK_ROOT/stage.XXXXXX")"
trap 'status=$?; if [ "$status" -ne 0 ]; then echo "qsm guest package build retained: $build_root $stage_root" >&2; fi; exit "$status"' EXIT

gcc -std=c11 -O2 -D_POSIX_C_SOURCE=200809L -Wall -Wextra -Wpedantic -Werror \
    -DQSM_DESKTOP_CLIPBOARD_ONLY=1 "$ROOT_DIR/guest/qsf_guest_agent.c" -o "$build_root/qsm-desktop-agent"
gcc -std=c11 -O2 -D_POSIX_C_SOURCE=200809L -Wall -Wextra -Wpedantic -Werror \
    "$ROOT_DIR/guest/qsf_state_watcher.c" -o "$build_root/qsm-desktop-state-watcher"
package_root="$stage_root/usr/lib/qsm-desktop-agent"
install -d "$package_root" "$stage_root/usr/bin" "$stage_root/usr/lib/systemd/system" \
    "$stage_root/usr/lib/systemd/user" "$stage_root/usr/lib/udev/rules.d" \
    "$stage_root/usr/share/doc/qsm-desktop-agent" "$stage_root/DEBIAN"
install -m 0755 "$build_root/qsm-desktop-agent" "$package_root/qsm-desktop-agent"
install -m 0755 "$build_root/qsm-desktop-state-watcher" "$package_root/qsm-desktop-state-watcher"
install -m 0755 "$ROOT_DIR/guest/qsf_wayland_clipboard_bridge.sh" "$package_root/qsm-desktop-clipboard-bridge"
install -m 0755 "$ROOT_DIR/packaging/guest/qsm-desktop-agent-setup" "$stage_root/usr/bin/qsm-desktop-agent-setup"
install -m 0644 "$ROOT_DIR/packaging/guest/qsm-desktop-agent@.service" "$stage_root/usr/lib/systemd/system/qsm-desktop-agent@.service"
install -m 0644 "$ROOT_DIR/packaging/guest/qsm-desktop-clipboard.service" "$stage_root/usr/lib/systemd/user/qsm-desktop-clipboard.service"
install -m 0644 "$ROOT_DIR/packaging/guest/99-qsm-desktop-agent.rules" "$stage_root/usr/lib/udev/rules.d/99-qsm-desktop-agent.rules"
install -m 0644 "$ROOT_DIR/packaging/guest/README.qsm-desktop-agent" "$stage_root/usr/share/doc/qsm-desktop-agent/README.Debian"
cat > "$stage_root/DEBIAN/control" <<EOF
Package: qsm-desktop-agent
Version: $VERSION
Section: admin
Priority: optional
Architecture: amd64
Maintainer: qsm contributors <qsm@users.noreply.github.com>
Depends: libc6 (>= 2.34), systemd, udev
Recommends: wl-clipboard
Conflicts: qsm-guest-agent
Replaces: qsm-guest-agent
Description: QSM Direct desktop clipboard companion
 QSM Desktop Agent is the optional in-VM desktop-session companion for
 qsm-pve-direct. It uses a private QEMU virtio-serial port and no network
 listener. It complements, rather than replaces, qemu-guest-agent.
EOF
cat > "$stage_root/DEBIAN/preinst" <<'EOF'
#!/bin/sh
set -e

# qsm-guest-agent was the former package name.  Record only concrete
# configured instances, stop them before dpkg removes their unit file, and
# let postinst recreate the equivalent qsm-desktop-agent instance.
case "${1:-}" in
  install|upgrade)
    migration_dir=/var/lib/qsm-desktop-agent
    migration_users="$migration_dir/legacy-users"
    install -d -m 0700 "$migration_dir"
    for directory in /etc/systemd/system/qsm-guest-agent@*.service.d; do
      [ -d "$directory" ] || continue
      unit=${directory##*/}
      user=${unit#qsm-guest-agent@}
      user=${user%.service.d}
      case "$user" in ''|*[!A-Za-z0-9._-]*) continue ;; esac
      grep -Fqx "$user" "$migration_users" 2>/dev/null || printf '%s\n' "$user" >> "$migration_users"
      systemctl disable --now "qsm-guest-agent@${user}.service" >/dev/null 2>&1 || true
    done
    ;;
esac
EOF
chmod 0755 "$stage_root/DEBIAN/preinst"
cat > "$stage_root/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e

getent group qsm-desktop >/dev/null || groupadd --system qsm-desktop
udevadm control --reload-rules >/dev/null 2>&1 || true
systemctl daemon-reload >/dev/null 2>&1 || true

migration_users=/var/lib/qsm-desktop-agent/legacy-users
if [ -f "$migration_users" ]; then
  while IFS= read -r user; do
    case "$user" in ''|*[!A-Za-z0-9._-]*) continue ;; esac
    /usr/bin/qsm-desktop-agent-setup "$user" || true
  done < "$migration_users"
  rm -f "$migration_users"
fi
EOF
chmod 0755 "$stage_root/DEBIAN/postinst"
printf '%s\n' '#!/bin/sh' 'set -e' 'systemctl daemon-reload >/dev/null 2>&1 || true' > "$stage_root/DEBIAN/postrm"
chmod 0755 "$stage_root/DEBIAN/postrm"
( cd "$stage_root"; find usr -type f -print0 | sort -z | xargs -0 md5sum ) > "$stage_root/DEBIAN/md5sums"
# Package naming must describe the delivered runtime, not merely the source
# tree from which it was built.  Audit both payload and control archive before
# producing a release artifact.
legacy_transport_pattern="$(printf '\\163\\165\\156\\163\\150\\151\\156\\145')|$(printf '\\155\\157\\157\\156\\154\\151\\147\\150\\164')"
if find "$stage_root" -type f -print0 | xargs -0 -r grep -I -n -E "$legacy_transport_pattern"; then
    echo "qsm desktop package staging unexpectedly contains a legacy transport reference" >&2
    exit 1
fi
artifact="$OUTPUT_DIR/qsm-desktop-agent_${VERSION}_amd64.deb"
dpkg-deb --root-owner-group --build "$stage_root" "$artifact"
echo "QSM_DESKTOP_DEB_BUILD_OK artifact=$artifact"
