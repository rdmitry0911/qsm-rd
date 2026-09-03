#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build the q-sunshine desktop client for macOS Tahoe.  The bundle contains a
# locally built, pinned, and patched Moonlight.app.  It never embeds an
# installed Moonlight.app, because an upstream/stock binary would either reject
# --qsm-system-auth or reintroduce the PIN pairing path.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_PARENT="${QSUNSHINE_MACOS_BUILD_PARENT:-$ROOT_DIR/.packaging-build}"
DIST_DIR="${QSUNSHINE_MACOS_DIST_DIR:-$ROOT_DIR/dist}"
REQUESTED_ARCHS="${QSUNSHINE_MACOS_ARCHS:-$(uname -m)}"
CODESIGN_IDENTITY="${QSUNSHINE_MACOS_CODESIGN_IDENTITY:--}"
DEPLOYMENT_TARGET="${QSUNSHINE_MACOS_DEPLOYMENT_TARGET:-26.0}"
MOONLIGHT_REVISION="0eff3b9b4dd685e07b15a383519e1972e626c9b3"
MOONLIGHT_GIT_URL="${QSUNSHINE_MOONLIGHT_GIT_URL:-https://github.com/moonlight-stream/moonlight-qt.git}"
MOONLIGHT_PATCH="$ROOT_DIR/integration/moonlight/patches/0001-system-auth-gamestream-lease.patch"

# Homebrew intentionally does not amend PATH for non-interactive SSH shells.
# Make a freshly provisioned Intel or Apple Silicon Tahoe builder discover its
# normal tools without requiring a user-profile side effect.
if [[ -d /opt/homebrew/bin ]]; then
    PATH="/opt/homebrew/bin:$PATH"
elif [[ -d /usr/local/bin ]]; then
    PATH="/usr/local/bin:$PATH"
fi
export PATH

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
if [[ -n "${QSUNSHINE_MOONLIGHT_APP:-}" ]]; then
    die "QSUNSHINE_MOONLIGHT_APP is unsupported: this package always builds the pinned patched Moonlight source"
fi
if [[ "${QSUNSHINE_MACOS_ALLOW_OTHER:-0}" != "1" ]]; then
    product_version="$(sw_vers -productVersion)"
    [[ "$product_version" == 26.* ]] ||
        die "macOS Tahoe 26 is required (found $product_version; set QSUNSHINE_MACOS_ALLOW_OTHER=1 only for development)"
fi
[[ "$DEPLOYMENT_TARGET" =~ ^26\.[0-9]+$ ]] ||
    die "QSUNSHINE_MACOS_DEPLOYMENT_TARGET must be a macOS Tahoe 26.x version"
[[ -n "$MOONLIGHT_GIT_URL" ]] || die "QSUNSHINE_MOONLIGHT_GIT_URL is empty"
[[ -f "$MOONLIGHT_PATCH" ]] || die "missing canonical Moonlight patch: $MOONLIGHT_PATCH"

for required in cmake ninja ditto codesign hdiutil install_name_tool lipo plutil shasum git python3 make strings; do
    require_command "$required"
done

if [[ -n "${QSUNSHINE_QT_PREFIX:-}" ]]; then
    qt_prefix="$QSUNSHINE_QT_PREFIX"
else
    qt_prefix=""
    brew_binary=""
    if command -v brew >/dev/null 2>&1; then
        brew_binary="$(command -v brew)"
    elif [[ -x /opt/homebrew/bin/brew ]]; then
        brew_binary=/opt/homebrew/bin/brew
    elif [[ -x /usr/local/bin/brew ]]; then
        # Fresh Intel Homebrew installs do not modify a non-interactive SSH
        # PATH.  Resolve the standard prefix explicitly for reproducibility.
        brew_binary=/usr/local/bin/brew
    fi
    if [[ -n "$brew_binary" ]]; then
        # Homebrew's split Qt 6 packaging exposes macdeployqt from qtbase;
        # older installations may still use qt@6 or qt.
        for formula in qtbase qt@6 qt; do
            candidate="$("$brew_binary" --prefix "$formula" 2>/dev/null || true)"
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
macdeploy_libpath_args=("-libpath=$qt_prefix/lib")
macdeploy_extra_framework_paths=()

# Homebrew splits optional Qt frameworks across formulae.  They can be
# present as keg-only dependencies, so macdeployqt cannot find them through
# qtbase's normal rpaths.  Resolve their formulae explicitly instead of
# requiring a global `brew link` (which would mutate the builder).
if [[ -n "$brew_binary" ]]; then
    for qt_formula in qtscxml qtvirtualkeyboard; do
        qt_optional_prefix="$("$brew_binary" --prefix "$qt_formula" 2>/dev/null || true)"
        if [[ -d "$qt_optional_prefix/lib" ]]; then
            macdeploy_libpath_args+=("-libpath=$qt_optional_prefix/lib")
            macdeploy_extra_framework_paths+=("$qt_optional_prefix/lib")
        fi
    done
fi
if [[ -x "$qt_prefix/bin/qmake" ]]; then
    moonlight_qmake="$qt_prefix/bin/qmake"
elif [[ -x "$qt_prefix/bin/qmake6" ]]; then
    moonlight_qmake="$qt_prefix/bin/qmake6"
else
    die "Qt prefix has no qmake or qmake6: $qt_prefix"
fi
if [[ -n "${QSUNSHINE_QT_CMAKE_PREFIX:-}" ]]; then
    qt_cmake_prefix="$QSUNSHINE_QT_CMAKE_PREFIX"
else
    qt_cmake_prefix="$("$moonlight_qmake" -query QT_INSTALL_PREFIX)"
fi
[[ -d "$qt_cmake_prefix" ]] ||
    die "Qt CMake prefix does not exist: $qt_cmake_prefix"

IFS=';' read -r -a requested_arch_array <<< "$REQUESTED_ARCHS"
[[ "${#requested_arch_array[@]}" -gt 0 ]] || die "QSUNSHINE_MACOS_ARCHS is empty"
moonlight_archs=""
for architecture in "${requested_arch_array[@]}"; do
    case "$architecture" in
        x86_64|arm64) ;;
        *) die "unsupported macOS architecture: $architecture" ;;
    esac
    [[ " $moonlight_archs " != *" $architecture "* ]] ||
        die "QSUNSHINE_MACOS_ARCHS contains $architecture more than once"
    moonlight_archs+="${moonlight_archs:+ }$architecture"
done

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
moonlight_source="$build_dir/moonlight-source"
moonlight_build="$build_dir/moonlight-build"
mkdir -p "$stage_dir"

# The commit check plus --index verifies the exact pinned source preimage; it
# cannot be silently applied to a different Moonlight source revision.
git clone --quiet --recursive "$MOONLIGHT_GIT_URL" "$moonlight_source"
git -C "$moonlight_source" checkout --quiet --detach "$MOONLIGHT_REVISION"
git -C "$moonlight_source" submodule update --init --recursive
[[ "$(git -C "$moonlight_source" rev-parse HEAD)" == "$MOONLIGHT_REVISION" ]] ||
    die "Moonlight checkout is not the pinned revision"
[[ -z "$(git -C "$moonlight_source" status --porcelain --untracked-files=all)" ]] ||
    die "fresh Moonlight checkout is unexpectedly dirty"
git -C "$moonlight_source" apply --check --index "$MOONLIGHT_PATCH"
git -C "$moonlight_source" apply --index "$MOONLIGHT_PATCH"
git -C "$moonlight_source" diff --check

# Moonlight upstream distributes a pinned universal macOS dependency archive
# through setup-deps.py.  Build it from the patched checkout, then deploy its
# own Qt runtime before copying it into the q-sunshine shell bundle.
(
    cd "$moonlight_source"
    python3 ./setup-deps.py
)
mkdir -p "$moonlight_build"
(
    cd "$moonlight_build"
    "$moonlight_qmake" "$moonlight_source/moonlight-qt.pro" \
        "QMAKE_APPLE_DEVICE_ARCHS=$moonlight_archs"
    make -j"$(sysctl -n hw.logicalcpu)" release
)
moonlight_bundle="$moonlight_build/app/Moonlight.app"
embedded_moonlight="$moonlight_bundle/Contents/MacOS/Moonlight"
[[ -x "$embedded_moonlight" ]] || die "patched Moonlight.app was not produced"
assert_architectures "$embedded_moonlight"
strings -a "$embedded_moonlight" | grep -F -- "qsm-system-auth" >/dev/null ||
    die "built Moonlight is missing the required q-sunshine system-auth marker"

# macdeployqt follows an executable's LC_RPATH entries when resolving
# frameworks.  `-libpath` is insufficient for several keg-only Homebrew Qt
# modules, so add their absolute builder rpaths before deployment; macdeployqt
# then rewrites the copied frameworks to the bundle-relative locations.
for framework_path in "${macdeploy_extra_framework_paths[@]}"; do
    install_name_tool -add_rpath "$framework_path" "$embedded_moonlight"
done
"$macdeployqt" "$moonlight_bundle" "${macdeploy_libpath_args[@]}" \
    -qmldir="$moonlight_source/app/gui" -appstore-compliant

cmake -S "$ROOT_DIR" -B "$build_dir/qsunshine-build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="$qt_cmake_prefix;$qt_prefix" \
    -DCMAKE_OSX_ARCHITECTURES="$REQUESTED_ARCHS" \
    -DCMAKE_OSX_DEPLOYMENT_TARGET="$DEPLOYMENT_TARGET" \
    -DBUILD_TESTING=OFF \
    -DQMDP_BUILD_DBUS=OFF \
    -DQMDP_BUILD_TOOLS=OFF \
    -DQMDP_BUILD_QT_CLIENT=ON
cmake --build "$build_dir/qsunshine-build" --target qsunshine-client --parallel
cmake --install "$build_dir/qsunshine-build" --prefix "$stage_dir"

app_bundle="$stage_dir/qsunshine-client.app"
[[ -d "$app_bundle/Contents/MacOS" ]] ||
    die "CMake did not produce qsunshine-client.app"
launcher_binary="$app_bundle/Contents/MacOS/qsunshine-client"
[[ -x "$launcher_binary" ]] || die "bundle launcher is missing"
assert_architectures "$launcher_binary"

# Deploy the QML runtime before adding the nested Moonlight bundle.  This
# keeps Qt's deployment scanner scoped to q-sunshine and makes the final
# signing pass cover both applications.
"$macdeployqt" "$app_bundle" "${macdeploy_libpath_args[@]}" \
    -qmldir="$ROOT_DIR/clients/qsunshine-qt/qml" \
    -always-overwrite
info_plist="$app_bundle/Contents/Info.plist"
[[ -f "$info_plist" ]] || die "q-sunshine bundle has no Info.plist"
plutil -lint "$info_plist" >/dev/null
plutil -extract CFBundleDocumentTypes xml1 -o - "$info_plist" |
    grep -F '<string>qsm</string>' >/dev/null ||
    die "q-sunshine bundle does not register .qsm document opens"
plutil -extract UTExportedTypeDeclarations xml1 -o - "$info_plist" |
    grep -F '<string>io.qsunshine.pve-launch</string>' >/dev/null ||
    die "q-sunshine bundle does not export its .qsm UTI"
if plutil -extract CFBundleURLTypes raw -o - "$info_plist" >/dev/null 2>&1; then
    die "q-sunshine bundle must not register a launch URL scheme"
fi
ditto "$moonlight_bundle" "$app_bundle/Contents/Resources/Moonlight.app"
cp -p "$ROOT_DIR/LICENSE" "$app_bundle/Contents/Resources/LICENSE"

embedded_moonlight="$app_bundle/Contents/Resources/Moonlight.app/Contents/MacOS/Moonlight"
[[ -x "$embedded_moonlight" ]] || die "embedded patched Moonlight.app is incomplete"
assert_architectures "$embedded_moonlight"
strings -a "$embedded_moonlight" | grep -F -- "qsm-system-auth" >/dev/null ||
    die "packaged Moonlight is not the patched system-auth binary"

if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
    codesign --force --deep --sign - "$embedded_moonlight"
    codesign --force --deep --sign - "$app_bundle"
else
    codesign --force --deep --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$embedded_moonlight"
    codesign --force --deep --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$app_bundle"
fi
codesign --verify --deep --strict --verbose=2 "$app_bundle"

hdiutil create -volname q-sunshine -srcfolder "$app_bundle" -format UDZO "$artifact" >/dev/null
hdiutil verify "$artifact" >/dev/null
# Run shasum from the artifact directory so the sidecar names the portable
# basename, rather than an absolute path that only exists on this builder.
artifact_basename="$(basename "$artifact")"
(
    cd "$DIST_DIR"
    shasum -a 256 "$artifact_basename" | tee "$artifact_basename.sha256"
)

echo "Q_SUNSHINE_MACOS_DMG_OK artifact=$artifact bundle=$app_bundle moonlight_revision=$MOONLIGHT_REVISION deployment_target=$DEPLOYMENT_TARGET"
echo "Q_SUNSHINE_MACOS_BUILD_DIR_RETAINED build=$build_dir"
