#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK_ROOT="${QSM_QT_GEOMETRY_DEB_WORK_ROOT:-$ROOT_DIR/.packaging-build/qsm-qt-input-geometry}"
OUTPUT_DIR="${QSM_QT_GEOMETRY_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
for command in cmake dpkg dpkg-deb dpkg-shlibdeps dpkg-architecture install find md5sum; do
    command -v "$command" >/dev/null || { echo "Qt geometry build needs $command" >&2; exit 1; }
done
project_version="$(sed -n 's/^project(qsm_direct VERSION \([^ ]*\).*/\1/p' "$ROOT_DIR/CMakeLists.txt" | head -n1)"
git_build_number="$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || printf 0)"
package_version="${QSM_DIRECT_DEB_VERSION:-${QSM_QT_GEOMETRY_DEB_VERSION:-${project_version}+git${git_build_number}.direct}}"
dpkg --validate-version "$package_version" >/dev/null
package=qsm-qt-input-geometry
architecture="$(dpkg-architecture -qDEB_HOST_ARCH)"
multiarch="$(dpkg-architecture -qDEB_HOST_MULTIARCH)"
mkdir -p "$WORK_ROOT" "$OUTPUT_DIR"
build_root="$(mktemp -d "$WORK_ROOT/build.XXXXXX")"
stage_root="$(mktemp -d "$WORK_ROOT/stage.XXXXXX")"
trap 'status=$?; if [ "$status" -ne 0 ]; then echo "Qt geometry build retained: $build_root $stage_root" >&2; fi; exit "$status"' EXIT
libraries=()
for major in 5 6; do
    cmake -S "$ROOT_DIR/guest/qt-input-geometry" -B "$build_root/qt$major" \
        -DQSM_QT_MAJOR="$major" -DCMAKE_BUILD_TYPE=Release
    cmake --build "$build_root/qt$major" --parallel "${QSM_QT_GEOMETRY_DEB_JOBS:-2}"
    destination="$stage_root/usr/lib/$multiarch/qt$major/plugins/generic"
    install -d "$destination"
    install -m 0644 "$build_root/qt$major/generic/libqsm-input-geometry.so" "$destination/"
    libraries+=("-e$destination/libqsm-input-geometry.so")
done
install -d "$stage_root/DEBIAN" "$stage_root/etc/xdg/plasma-workspace/env" \
    "$stage_root/usr/share/doc/$package" "$build_root/debian"
install -m 0644 "$ROOT_DIR/packaging/guest/qsm-qt-input-geometry.sh" \
    "$stage_root/etc/xdg/plasma-workspace/env/qsm-qt-input-geometry.sh"
cat > "$build_root/debian/control" <<CONTROL
Source: $package
Section: x11
Priority: optional
Maintainer: Dmitry R <rdmitry0911@gmail.com>

Package: $package
Architecture: any
Description: Qt input geometry for QSM desktop metadata
CONTROL
depends="$(cd "$build_root" && dpkg-shlibdeps -O "${libraries[@]}" | sed -n 's/^shlibs:Depends=//p')"
[[ -n "$depends" ]] || { echo "Qt geometry dependency discovery failed" >&2; exit 1; }
cat > "$stage_root/DEBIAN/control" <<CONTROL
Package: $package
Version: $package_version
Section: x11
Priority: optional
Architecture: $architecture
Maintainer: Dmitry R <rdmitry0911@gmail.com>
Depends: $depends, qsm-desktop-agent (>= $package_version) | qsm-console-guest (>= $package_version)
Description: Qt input geometry for QSM desktop metadata
 Optional Qt 5 and Qt 6 application plugins expose focused input cursor
 bounds through the local user bus. The QSM guest agent authenticates the
 publisher against the focused compositor process and forwards bounded
 geometry on the existing guest channel.
CONTROL
cat > "$stage_root/usr/share/doc/$package/README" <<'DOC'
QSM Qt input geometry

Plasma loads the plugins for applications started after the next desktop login.
For an owned test application in a running session, set:
  QT_QPA_GENERIC_PLUGINS=qsm-input-geometry application
Existing generic plugin entries can be retained as a comma-separated list.

The plugins read input enablement and cursor bounds, without requesting text,
window titles or input-event contents. Qt 5 and Qt 6 plugins are included.
The guest metadata helper verifies the process and user of the D-Bus publisher,
checks the focused client size, and expires stale hints. Geometry is advisory;
ordinary pixel/video delivery remains available when the plugin is absent.

Build using the oldest supported Qt minor version for portable binaries.
The development baseline uses Debian 12 (Qt 5.15 and Qt 6.4), with native
Ubuntu 24.04 and Debian 13 runtime qualification.
DOC
printf '%s\n' /etc/xdg/plasma-workspace/env/qsm-qt-input-geometry.sh > "$stage_root/DEBIAN/conffiles"
(cd "$stage_root" && find usr etc -type f -print0 | sort -z | xargs -0 -r md5sum) > "$stage_root/DEBIAN/md5sums"
artifact="$OUTPUT_DIR/${package}_${package_version}_${architecture}.deb"
dpkg-deb --root-owner-group --build "$stage_root" "$artifact"
printf 'QSM_QT_INPUT_GEOMETRY_DEB_OK artifact=%s\n' "$artifact"
