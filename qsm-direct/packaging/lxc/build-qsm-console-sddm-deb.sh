#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the optional, privately installed native SDDM for Ubuntu 24.04 / Qt 5.
# QSM_SDDM_SOURCE_DIR must contain the upstream Git repository. Its working
# tree is not modified; only the pinned, verified commit is exported.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE_COMMIT=63780fcd79f1dbf81a30eef48c28c699ab15aded
: "${QSM_SDDM_SOURCE_DIR:?set QSM_SDDM_SOURCE_DIR to an upstream sddm Git checkout}"
output="${QSM_DIRECT_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
version="${QSM_SDDM_DEB_VERSION:-0.21.0+qsm2}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/source" "$work/stage/DEBIAN" "$work/debian" "$output"
git -C "$QSM_SDDM_SOURCE_DIR" archive "$SOURCE_COMMIT" | tar -x -C "$work/source"
for patch in "$ROOT_DIR"/packaging/lxc/sddm/*.patch; do
    git -C "$work/source" apply "$patch"
done
cmake -S "$work/source" -B "$work/build" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/qsm-native-sddm -DBUILD_WITH_QT6=OFF \
    -DINSTALL_PAM_CONFIGURATION=OFF -DBUILD_MAN_PAGES=OFF \
    -DDATA_INSTALL_DIR=/usr/share/sddm \
    -DQML_INSTALL_DIR=/opt/qsm-native-sddm/share/qt5/qml \
    -DCONFIG_FILE=/etc/qsm-direct/sddm.conf -DCONFIG_DIR=/etc/qsm-direct/sddm.conf.d
cmake --build "$work/build" -j"${QSM_DIRECT_DEB_JOBS:-4}"
DESTDIR="$work/stage" cmake --install "$work/build"
# Keep only the private installation. Distro SDDM owns themes, scripts, PAM,
# D-Bus policy and public units; activation is performed by qsm-native-profile.
rm -rf "$work/stage/etc" "$work/stage/usr" "$work/stage/var"
install -d "$work/stage/usr/share/doc/qsm-console-sddm"
install -m 0644 "$work/source/LICENSE" "$work/stage/usr/share/doc/qsm-console-sddm/copyright"
cp -a "$ROOT_DIR/packaging/lxc/sddm" "$work/stage/usr/share/doc/qsm-console-sddm/patches"
cat > "$work/debian/control" <<'CONTROL'
Source: qsm-console-sddm
Section: x11
Priority: optional
Maintainer: Dmitry R <rdmitry0911@gmail.com>

Package: qsm-console-sddm
Architecture: any
Description: Native SDDM for QSM virtual seats
CONTROL
libraries=()
while IFS= read -r -d '' binary; do
    if file "$binary" | grep -q 'ELF.*dynamically linked'; then libraries+=("-e$binary"); fi
done < <(find "$work/stage/opt" -type f -perm /111 -print0)
dependencies="$(cd "$work" && dpkg-shlibdeps -O "${libraries[@]}" | sed -n 's/^shlibs:Depends=//p')"
[[ -n "$dependencies" ]]
cat > "$work/stage/DEBIAN/control" <<CONTROL
Package: qsm-console-sddm
Version: $version
Section: x11
Priority: optional
Architecture: $(dpkg --print-architecture)
Maintainer: Dmitry R <rdmitry0911@gmail.com>
Depends: sddm, qsm-console-guest (>= 0.4.0+shared12), $dependencies
Description: Native SDDM on virtual graphical seats for QSM LXC consoles
 Full SDDM daemon and greeter, privately installed under /opt, with the
 upstream VT-less seat change and native greeter reset/session integration.
 Authentication uses the distribution's PAM stack. Activation is opt-in.
CONTROL
( cd "$work/stage" && find opt usr -type f -print0 | sort -z | xargs -0 -r md5sum ) > "$work/stage/DEBIAN/md5sums"
artifact="$output/qsm-console-sddm_${version}_$(dpkg --print-architecture).deb"
dpkg-deb --root-owner-group --build "$work/stage" "$artifact"
echo "QSM_CONSOLE_SDDM_DEB_BUILD_OK artifact=$artifact source=$SOURCE_COMMIT"
