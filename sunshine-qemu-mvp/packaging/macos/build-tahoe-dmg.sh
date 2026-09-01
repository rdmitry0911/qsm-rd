#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build a self-contained q-sunshine Qt desktop client for macOS Tahoe.  The
# graphical transport remains an unmodified Moonlight.app embedded in the
# launcher bundle; q-sunshine owns its own Qt UI and QSF companion protocol.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_PARENT="${QSUNSHINE_MACOS_BUILD_PARENT:-$ROOT_DIR/.packaging-build}"
DIST_DIR="${QSUNSHINE_MACOS_DIST_DIR:-$ROOT_DIR/dist}"
REQUESTED_ARCHS="${QSUNSHINE_MACOS_ARCHS:-$(uname -m)}"
CODESIGN_IDENTITY="${QSUNSHINE_MACOS_CODESIGN_IDENTITY:--}"
DEPLOYMENT_TARGET="${QSUNSHINE_MACOS_DEPLOYMENT_TARGET:-26.0}"

die() {
    echo "q-sunshine macOS package: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

if [[ "$(uname -s)" != "Darwin" ]]; then
    die "this builder must run on macOS"
fi
if [[ "${QSUNSHINE_MACOS_ALLOW_OTHER:-0}" != "1" ]]; then
    product_version="$(sw_vers -productVersion)"
    [[ "$product_version" == 26.* ]] ||
        die "macOS Tahoe 26 is required (found $product_version; set QSUNSHINE_MACOS_ALLOW_OTHER=1 only for development)"
fi
[[ "$DEPLOYMENT_TARGET" =~ ^26\.[0-9]+$ ]] ||
    die "QSUNSHINE_MACOS_DEPLOYMENT_TARGET must be a macOS Tahoe 26.x version"
for required in cmake ninja ditto codesign hdiutil lipo shasum; do
    require_command "$required"
done

if [[ -n "${QSUNSHINE_QT_PREFIX:-}" ]]; then
    qt_prefix="$QSUNSHINE_QT_PREFIX"
else
    qt_prefix=""
    if command -v brew >/dev/null 2>&1; then
        # Homebrew's split Qt 6 packaging exposes macdeployqt from qtbase;
        # older installations may still use qt@6 or qt.
        for formula in qtbase qt@6 qt; do
            candidate="$(brew --prefix "$formula" 2>/dev/null || true)"
            if [[ -x "$candidate/bin/macdeployqt" ]]; then
                qt_prefix="$candidate"
                break
            fi
        done
    fi
fi
[[ -n "$qt_prefix" && -x "$qt_prefix/bin/macdeployqt" ]] ||
    die "set QSUNSHINE_QT_PREFIX to a Qt 6 prefix containing bin/macdeployqt"
macdeployqt="$qt_prefix/bin/macdeployqt"
if [[ -n "${QSUNSHINE_QT_CMAKE_PREFIX:-}" ]]; then
    qt_cmake_prefix="$QSUNSHINE_QT_CMAKE_PREFIX"
elif [[ -x "$qt_prefix/bin/qmake" ]]; then
    qt_cmake_prefix="$("$qt_prefix/bin/qmake" -query QT_INSTALL_PREFIX)"
else
    qt_cmake_prefix="$qt_prefix"
fi
[[ -d "$qt_cmake_prefix" ]] ||
    die "Qt CMake prefix does not exist: $qt_cmake_prefix"

if [[ -n "${QSUNSHINE_MOONLIGHT_APP:-}" ]]; then
    moonlight_app="$QSUNSHINE_MOONLIGHT_APP"
else
    moonlight_app=""
    for candidate in /Applications/Moonlight.app "$HOME/Applications/Moonlight.app"; do
        if [[ -d "$candidate" ]]; then
            moonlight_app="$candidate"
            break
        fi
    done
fi
[[ -d "$moonlight_app/Contents/MacOS" ]] ||
    die "set QSUNSHINE_MOONLIGHT_APP to an installed Moonlight.app"
moonlight_binary="$(find "$moonlight_app/Contents/MacOS" -maxdepth 1 -type f -perm -111 -print -quit)"
[[ -n "$moonlight_binary" ]] || die "Moonlight.app has no executable in Contents/MacOS"

IFS=';' read -r -a requested_arch_array <<< "$REQUESTED_ARCHS"
[[ "${#requested_arch_array[@]}" -gt 0 ]] || die "QSUNSHINE_MACOS_ARCHS is empty"
assert_architectures() {
    local file="$1"
    local available
    available="$(lipo -archs "$file")"
    local architecture
    for architecture in "${requested_arch_array[@]}"; do
        [[ " $available " == *" $architecture "* ]] ||
            die "$file does not contain requested architecture $architecture (has: $available)"
    done
}
assert_architectures "$moonlight_binary"

project_version="$(sed -n 's/^project(sunshine_qemu_mvp VERSION \([^ ]*\).*/\1/p' "$ROOT_DIR/CMakeLists.txt" | head -n 1)"
[[ -n "$project_version" ]] || die "could not determine project version"
git_revision="${QSUNSHINE_MACOS_GIT_REVISION:-$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || printf 'source')}"
[[ "$git_revision" =~ ^[0-9A-Za-z._-]+$ ]] || die "invalid macOS artifact revision: $git_revision"
arch_label="${REQUESTED_ARCHS//;/+}"
artifact="$DIST_DIR/q-sunshine-${project_version}+g${git_revision}-macos-tahoe-${arch_label}.dmg"
[[ ! -e "$artifact" ]] || die "refusing to overwrite existing artifact: $artifact"

mkdir -p "$BUILD_PARENT" "$DIST_DIR"
build_dir="$(mktemp -d "$BUILD_PARENT/macos-tahoe.XXXXXX")"
stage_dir="$build_dir/stage"
mkdir -p "$stage_dir"

cmake -S "$ROOT_DIR" -B "$build_dir" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="$qt_cmake_prefix;$qt_prefix" \
    -DCMAKE_OSX_ARCHITECTURES="$REQUESTED_ARCHS" \
    -DCMAKE_OSX_DEPLOYMENT_TARGET="$DEPLOYMENT_TARGET" \
    -DBUILD_TESTING=OFF \
    -DQMDP_BUILD_DBUS=OFF \
    -DQMDP_BUILD_TOOLS=OFF \
    -DQMDP_BUILD_QT_CLIENT=ON
cmake --build "$build_dir" --target qsunshine-client --parallel
cmake --install "$build_dir" --prefix "$stage_dir"

app_bundle="$stage_dir/qsunshine-client.app"
[[ -d "$app_bundle/Contents/MacOS" ]] ||
    die "CMake did not produce qsunshine-client.app"
launcher_binary="$app_bundle/Contents/MacOS/qsunshine-client"
[[ -x "$launcher_binary" ]] || die "bundle launcher is missing"
assert_architectures "$launcher_binary"

# Deploy the QML runtime before adding the nested Moonlight bundle.  This keeps
# Qt's deployment scanner focused on q-sunshine and lets the final signing pass
# cover both applications.
"$macdeployqt" "$app_bundle" \
    -qmldir="$ROOT_DIR/clients/qsunshine-qt/qml" \
    -always-overwrite
ditto "$moonlight_app" "$app_bundle/Contents/Resources/Moonlight.app"
cp -p "$ROOT_DIR/LICENSE" "$app_bundle/Contents/Resources/LICENSE"

embedded_moonlight="$(find "$app_bundle/Contents/Resources/Moonlight.app/Contents/MacOS" \
    -maxdepth 1 -type f -perm -111 -print -quit)"
[[ -n "$embedded_moonlight" ]] || die "embedded Moonlight.app is incomplete"
assert_architectures "$embedded_moonlight"

if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
    codesign --force --deep --sign - "$app_bundle"
else
    codesign --force --deep --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$app_bundle"
fi
codesign --verify --deep --strict --verbose=2 "$app_bundle"

hdiutil create -volname q-sunshine -srcfolder "$app_bundle" -format UDZO "$artifact" >/dev/null
hdiutil verify "$artifact" >/dev/null
shasum -a 256 "$artifact" | tee "$artifact.sha256"

echo "Q_SUNSHINE_MACOS_DMG_OK artifact=$artifact bundle=$app_bundle moonlight=$moonlight_app deployment_target=$DEPLOYMENT_TARGET"
echo "Q_SUNSHINE_MACOS_BUILD_DIR_RETAINED build=$build_dir"
