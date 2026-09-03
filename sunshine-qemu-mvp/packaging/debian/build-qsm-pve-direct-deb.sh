#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build the browser-only QSM direct transport for Proxmox VE 9 / Debian 13.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/packaging/debian"
WORK_ROOT="${QSM_DIRECT_DEB_WORK_ROOT:-$ROOT_DIR/.packaging-build/qsm-pve-direct}"
OUTPUT_DIR="${QSM_DIRECT_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
BUILD_JOBS="${QSM_DIRECT_DEB_JOBS:-4}"

die() { echo "qsm-pve-direct Debian package: $*" >&2; exit 1; }
require_file() { [[ -f "$1" ]] || die "missing required file: $1"; }

[[ "$(dpkg --print-architecture)" == amd64 ]] || die "amd64 is required"
[[ "$BUILD_JOBS" =~ ^[1-9][0-9]*$ ]] || die "QSM_DIRECT_DEB_JOBS must be positive"
if [[ "${QSM_DIRECT_DEB_ALLOW_NONTRIXIE:-0}" != 1 ]]; then
    source /etc/os-release
    [[ "${VERSION_CODENAME:-}" == trixie ]] || die "build in Debian 13/Trixie or use the clean-chroot driver"
fi
for command in cmake ninja dpkg dpkg-deb install find sed tar sha256sum strings; do
    command -v "$command" >/dev/null 2>&1 || die "missing command: $command"
done
for required in \
    "$ROOT_DIR/CMakeLists.txt" \
    "$ROOT_DIR/tools/qsm_direct_media_worker.cpp" \
    "$ROOT_DIR/extensions/browser_bridge/qsm_browser_bridge.py" \
    "$ROOT_DIR/extensions/direct_terminal/qsm_direct_terminal.py" \
    "$ROOT_DIR/integration/proxmox/pve9/direct_api/PVE/API2/QsmDirect.pm" \
    "$ROOT_DIR/integration/proxmox/pve9/direct_ui/qsm-direct-console.js" \
    "$PACKAGE_DIR/qsm-pve-direct-terminal" \
    "$PACKAGE_DIR/qsm-pve-direct-terminal.service" \
    "$PACKAGE_DIR/qsm-pve-direct-ui" \
    "$PACKAGE_DIR/qsm-pve-direct-postinst" \
    "$PACKAGE_DIR/qsm-pve-direct-prerm" \
    "$PACKAGE_DIR/README.qsm-pve-direct"; do
    require_file "$required"
done

project_version="$(sed -n 's/^project(sunshine_qemu_mvp VERSION \([^ ]*\).*/\1/p' "$ROOT_DIR/CMakeLists.txt" | head -n1)"
[[ -n "$project_version" ]] || die "could not determine project version"
git_build_number="$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || printf 0)"
package_version="${QSM_DIRECT_DEB_VERSION:-${project_version}+git${git_build_number}.direct}"
dpkg --validate-version "$package_version" >/dev/null 2>&1 || die "invalid Debian version"

mkdir -p "$WORK_ROOT" "$OUTPUT_DIR"
build_root="$(mktemp -d "$WORK_ROOT/build.XXXXXX")"
stage_root="$(mktemp -d "$WORK_ROOT/stage.XXXXXX")"
trap 'status=$?; if [[ $status -ne 0 ]]; then echo "qsm-pve-direct build retained: $build_root $stage_root" >&2; fi; exit $status' EXIT
cmake -S "$ROOT_DIR" -B "$build_root/cmake" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DQMDP_BUILD_QT_CLIENT=OFF -DQMDP_BUILD_DBUS=ON \
    -DQMDP_BUILD_TOOLS=ON -DBUILD_TESTING=OFF
cmake --build "$build_root/cmake" --target qsm-direct-media-worker --parallel "$BUILD_JOBS"
worker="$build_root/cmake/qsm-direct-media-worker"
[[ -x "$worker" ]] || die "direct media worker was not built"

package_name=qsm-pve-direct
package_root="$stage_root/usr/lib/$package_name"
install -d "$package_root/bin" "$package_root/browser_bridge" "$package_root/direct_terminal" \
    "$package_root/pve9-api/PVE/API2" "$package_root/pve9-api/PVE/QsmDirect" "$package_root/pve9-ui" \
    "$stage_root/usr/bin" "$stage_root/usr/sbin" "$stage_root/usr/share/doc/$package_name" \
    "$stage_root/usr/share/pve-manager/js" "$stage_root/usr/share/$package_name/pve9-ui" \
    "$stage_root/usr/lib/systemd/system/pveproxy.service.d" \
    "$stage_root/usr/lib/systemd/system/pvedaemon.service.d" \
    "$stage_root/DEBIAN"
install -m 0755 "$worker" "$package_root/bin/qsm-direct-media-worker"
install -m 0644 "$ROOT_DIR/extensions/browser_bridge/__init__.py" "$package_root/browser_bridge/__init__.py"
install -m 0644 "$ROOT_DIR/extensions/browser_bridge/qsm_browser_bridge.py" "$package_root/browser_bridge/qsm_browser_bridge.py"
install -m 0755 "$ROOT_DIR/extensions/direct_terminal/qsm_direct_terminal.py" "$package_root/direct_terminal/qsm_direct_terminal.py"
install -m 0755 "$PACKAGE_DIR/qsm-pve-direct-terminal" "$stage_root/usr/bin/qsm-pve-direct-terminal"
install -m 0644 "$PACKAGE_DIR/qsm-pve-direct-terminal.service" \
    "$stage_root/usr/lib/systemd/system/qsm-pve-direct-terminal.service"
install -m 0755 "$PACKAGE_DIR/qsm-pve-direct-ui" "$stage_root/usr/sbin/qsm-pve-direct-ui"
install -m 0644 "$PACKAGE_DIR/README.qsm-pve-direct" "$stage_root/usr/share/doc/$package_name/README.Debian"
install -m 0644 "$ROOT_DIR/integration/proxmox/pve9/direct_api/PVE/API2/QsmDirect.pm" \
    "$package_root/pve9-api/PVE/API2/QsmDirect.pm"

# The compatibility predicate is intentionally the same reviewed PVE ABI as
# the native package, but the direct package owns different Perl namespaces,
# launcher paths and service drop-ins. Generate that isolated copy while
# staging; no q-sunshine-named file or runtime path enters this package.
sed -e 's/QSunshine/QsmDirect/g' -e 's/q-sunshine/qsm-pve-direct/g' \
    "$ROOT_DIR/integration/proxmox/pve9/api/PVE/QSunshine/Compatibility.pm" > \
    "$package_root/pve9-api/PVE/QsmDirect/Compatibility.pm"
for role in pveproxy pvedaemon pvesh; do
    sed -e 's/QSunshine/QsmDirect/g' -e 's/q-sunshine/qsm-pve-direct/g' \
        "$ROOT_DIR/integration/proxmox/pve9/api/q-sunshine-$role" > \
        "$package_root/pve9-api/qsm-pve-direct-$role"
    chmod 0755 "$package_root/pve9-api/qsm-pve-direct-$role"
done
for role in pveproxy pvedaemon; do
    sed -e 's#q-sunshine-api#qsm-pve-direct-api#g' -e 's#q-sunshine#qsm-pve-direct#g' \
        "$ROOT_DIR/integration/proxmox/pve9/api/systemd/$role.service.d/q-sunshine-api.conf" > \
        "$stage_root/usr/lib/systemd/system/$role.service.d/qsm-pve-direct-api.conf"
done

# The guarded diversion manager is transport neutral. Its staged names are
# direct-specific, so either mutually-exclusive package restores only its own
# template diversion and assets.
sed -e 's/q-sunshine-pve/qsm-pve-direct/g' -e 's/q-sunshine/qsm-pve-direct/g' \
    "$ROOT_DIR/integration/proxmox/pve9/ui/q_sunshine_pve9_ui.py" > \
    "$package_root/pve9-ui/qsm_pve_direct_ui.py"
chmod 0755 "$package_root/pve9-ui/qsm_pve_direct_ui.py"
install -m 0644 "$ROOT_DIR/integration/proxmox/pve9/direct_ui/qsm-direct-console.js" \
    "$stage_root/usr/share/pve-manager/js/qsm-pve-direct-console.js"
sed -e 's/q-sunshine/qsm-pve-direct/g' \
    "$ROOT_DIR/integration/proxmox/pve9/ui/pve-manager-index-template.sha256" > \
    "$stage_root/usr/share/$package_name/pve9-ui/pve-manager-index-template.sha256"

cat > "$stage_root/DEBIAN/control" <<EOF
Package: $package_name
Version: $package_version
Section: net
Priority: optional
Architecture: amd64
Maintainer: qsm contributors <qsm@users.noreply.github.com>
Depends: libc6 (>= 2.38), libopus0 (>= 1.3), libstdc++6 (>= 13), libsystemd0, dbus, ffmpeg, python3, python3-aiortc, python3-av, pve-manager, qemu-server
Conflicts: q-sunshine-pve
Replaces: q-sunshine-pve
Description: browser-only direct QEMU Display1 console for Proxmox VE 9
 qsm-pve-direct adds one PVE VM.Console-protected browser WebRTC Console
 transport. It runs a node-local QEMU Display1 H.264/Opus worker and has no
 pairing, native client, external transport listener, host desktop server, or
 legacy compatibility transport implementation.
EOF
install -m 0755 "$PACKAGE_DIR/qsm-pve-direct-postinst" "$stage_root/DEBIAN/postinst"
install -m 0755 "$PACKAGE_DIR/qsm-pve-direct-prerm" "$stage_root/DEBIAN/prerm"
printf '%s\n' '#!/bin/sh' 'set -e' 'systemctl daemon-reload >/dev/null 2>&1 || true' > "$stage_root/DEBIAN/postrm"
chmod 0755 "$stage_root/DEBIAN/postrm"
printf '%s\n' 'interest-noawait /usr/share/pve-manager/index.html.tpl' \
    > "$stage_root/DEBIAN/triggers"

# A direct build must be semantically independent as well as dependency-free.
# This catches accidental staging of compatibility transport code or old UI
# names before a host ever sees the archive.
if find "$stage_root/usr" -type f -print0 | xargs -0 -r grep -I -n -E 'sunshine|moonlight|Sunshine|Moonlight'; then
    die "direct package staging unexpectedly contains a compatibility transport reference"
fi
(
    cd "$stage_root"
    find usr -type f -print0 | sort -z | xargs -0 -r md5sum
) > "$stage_root/DEBIAN/md5sums"
artifact="$OUTPUT_DIR/${package_name}_${package_version}_amd64.deb"
dpkg-deb --root-owner-group --build "$stage_root" "$artifact"
dpkg-deb -I "$artifact" >/dev/null
echo "QSM_DIRECT_DEB_BUILD_OK artifact=$artifact"
