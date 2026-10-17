#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Private headless compositor for Ubuntu 24.04. No distro libraries are replaced.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${QSM_SWAY_SOURCE_DIR:?directory containing upstream Git repositories or the unpacked complete source archive}"
[[ -f "$ROOT_DIR/CMakeLists.txt" ]] || ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output="${QSM_DIRECT_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
version="${QSM_SWAY_DEB_VERSION:-1.11+qsm1}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
prefix=/opt/qsm-native-sway
mkdir -p "$work/source" "$work/stage/DEBIAN" "$output"
projects=(wayland pixman libinput wlroots sway)
commits=(a9fec8dd65977c57f4039ced34327204d9b9d779 46655e15671e1ca62d699821d75a85d341d7229c 3402f380019dc4f882e1d8627029607aca3f4be6 13a62a23a258d96f902c740310d5c7c59784a4d1 990b23c0e584114c3694447344d6bbf44b2d748f)
if [[ -f "$QSM_SWAY_SOURCE_DIR/SOURCE.SHA256" ]]; then
    (cd "$QSM_SWAY_SOURCE_DIR" && sha256sum --status -c SOURCE.SHA256)
    for i in "${!projects[@]}"; do
        grep -Fxq "${projects[$i]} ${commits[$i]}" "$QSM_SWAY_SOURCE_DIR/COMMITS"
        cp -a "$QSM_SWAY_SOURCE_DIR/${projects[$i]}" "$work/source/"
    done
else
    for i in "${!projects[@]}"; do
        name="${projects[$i]}"
        mkdir "$work/source/$name"
        git -C "$QSM_SWAY_SOURCE_DIR/$name" archive "${commits[$i]}" | tar -x -C "$work/source/$name"
    done
fi
# Compile against staged private dependencies, with their final runtime path.
export PKG_CONFIG_PATH="$work/stage$prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export PATH="$work/stage$prefix/bin:$PATH"
export LD_LIBRARY_PATH="$work/stage$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export CPPFLAGS="${CPPFLAGS:-} -I$work/stage$prefix/include"
export LDFLAGS="${LDFLAGS:-} -Wl,-rpath,$prefix/lib"
common=(--prefix="$prefix" --libdir=lib --buildtype=release --wrap-mode=nodownload -Dwerror=false)
build() {
    local name="$1"; shift
    meson setup "$work/build-$name" "$work/source/$name" "${common[@]}" "$@"
    meson compile -C "$work/build-$name" -j "${QSM_DIRECT_DEB_JOBS:-4}"
    DESTDIR="$work/stage" meson install -C "$work/build-$name"
    # pkg-config needs staged paths only during this build.
    for pc in "$work/stage$prefix/lib/pkgconfig/"*.pc; do
        sed -i "s|^prefix=$prefix$|prefix=$work/stage$prefix|" "$pc"
    done
}
build wayland -Dtests=false -Ddocumentation=false
build pixman -Dtests=disabled -Ddemos=disabled
build libinput -Dtests=false -Ddocumentation=false -Ddebug-gui=false -Dlibwacom=false -Dzshcompletiondir=no
build wlroots -Dexamples=false -Dbackends=[] -Drenderers=gles2 -Dallocators=gbm -Dsession=disabled -Dcolor-management=disabled -Dxwayland=enabled
build sway -Dswaybar=false -Dswaynag=false -Dtray=disabled -Dman-pages=disabled -Ddefault-wallpaper=false -Dzsh-completions=false -Dbash-completions=false -Dfish-completions=false
# Runtime-only package. Reproducible upstream sources are shipped separately.
rm -rf "$work/stage$prefix/include" "$work/stage$prefix/lib/pkgconfig" "$work/stage$prefix/share/wayland" "$work/stage$prefix/etc"
find "$work/stage$prefix" -name '*.a' -delete
install -d "$work/stage/usr/share/doc/qsm-console-sway"
for name in "${projects[@]}"; do
    license="$work/source/$name/COPYING"
    [[ -f "$license" ]] || license="$work/source/$name/LICENSE"
    install -m 0644 "$license" "$work/stage/usr/share/doc/qsm-console-sway/copyright.$name"
done
# Dependencies provided privately must not become dependencies on distro versions.
mkdir -p "$work/debian"
printf 'Source: qsm-console-sway\n\nPackage: qsm-console-sway\nArchitecture: any\nDescription: Private headless compositor\n' > "$work/debian/control"
binaries=()
while IFS= read -r -d '' binary; do
    if file "$binary" | grep -q 'ELF.*dynamically linked'; then binaries+=("-e$binary"); fi
done < <(find "$work/stage$prefix" -type f -print0)
dependencies="$(cd "$work" && dpkg-shlibdeps --ignore-missing-info -l"$work/stage$prefix/lib" -O "${binaries[@]}" | sed -n 's/^shlibs:Depends=//p')"
[[ -n "$dependencies" ]]
cat > "$work/stage/DEBIAN/control" <<CONTROL
Package: qsm-console-sway
Version: $version
Section: x11
Priority: optional
Architecture: $(dpkg --print-architecture)
Maintainer: Dmitry R <rdmitry0911@gmail.com>
Depends: qsm-console-guest, $dependencies
Description: Private headless Sway with independent cursor capture for QSM
 Sway 1.11 and wlroots 0.19, with private Wayland and pixman libraries.
 Installed under /opt/qsm-native-sway without replacing the distro compositor.
CONTROL
( cd "$work/stage" && find opt usr -type f -print0 | sort -z | xargs -0 -r md5sum ) > "$work/stage/DEBIAN/md5sums"
artifact="$output/qsm-console-sway_${version}_$(dpkg --print-architecture).deb"
dpkg-deb --root-owner-group --build "$work/stage" "$artifact"
cp "$0" "$work/source/build-qsm-console-sway-deb.sh"
for i in "${!projects[@]}"; do
    printf '%s %s\n' "${projects[$i]}" "${commits[$i]}"
done > "$work/source/COMMITS"
(cd "$work/source" && find "${projects[@]}" -type f -print0 | sort -z | xargs -0 -r sha256sum) > "$work/source/SOURCE.SHA256"
tar -C "$work/source" -czf "$output/qsm-console-sway_${version}_source.tar.gz" .
echo "QSM_CONSOLE_SWAY_DEB_BUILD_OK artifact=$artifact"
