#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build q-sunshine-pve for the Debian 13 (Trixie) userspace used by Proxmox VE
# 9.  The script intentionally builds an isolated libva and places it beside
# Sunshine, rather than changing the host's dynamic linker configuration.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/packaging/debian"
SUNSHINE_SOURCE_DIR="${SUNSHINE_SOURCE_DIR:-$ROOT_DIR/.upstream/Sunshine}"
LIBVA_SOURCE_DIR="${LIBVA_SOURCE_DIR:-$ROOT_DIR/.upstream/libva-2.21.0}"
WORK_ROOT="${QSUNSHINE_DEB_WORK_ROOT:-$ROOT_DIR/.packaging-build/proxmox9-deb}"
OUTPUT_DIR="${QSUNSHINE_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
BUILD_JOBS="${QSUNSHINE_DEB_JOBS:-4}"

# Exact build-deps gitlink used by the embedded Sunshine revision.  Pinning
# the release asset prevents an exported source tree from silently resolving
# the moving `latest` FFmpeg artifact.
readonly FFMPEG_BUILD_DEPS_TAG=v2026.724.203728
readonly FFMPEG_ARCHIVE_SHA256=2c27d4694b4ed0e734f497d4bd62f1b3662cbbc4ded2a69f2dc4b703441eebb3
readonly FFMPEG_ARCHIVE_URL="https://github.com/LizardByte/build-deps/releases/download/${FFMPEG_BUILD_DEPS_TAG}/Linux-x86_64-ffmpeg.tar.gz"
readonly SUNSHINE_LISTENER_TEARDOWN_PATCH=integration/sunshine/patches/0007-platform-linux-close-QEMU-listener-transport-before-teardown.patch
readonly SUNSHINE_LISTENER_TEARDOWN_PATCH_SHA256=bdef88093a2b2f00b51c7e3a06533e7e62ef8fb00d2eaa8c52b718a14f8b107b
readonly SUNSHINE_SYSTEM_AUTH_PATCH=integration/sunshine/patches/0008-nvhttp-require-system-auth-media-leases.patch
readonly SUNSHINE_SYSTEM_AUTH_PATCH_SHA256=35603f17e961809391d96ea155741d333b3f9d8d47d53bde4035f880c597e9ef
readonly SIMPLE_WEB_SERVER_SYSTEM_AUTH_PATCH=integration/sunshine/simple-web-server/0001-server-http-expose-request-tls-native-handle.patch
readonly SIMPLE_WEB_SERVER_SYSTEM_AUTH_PATCH_SHA256=7c978eaa72a3077fa46f03dc322abd26ac0db0e17cd8be525227cc0976cff2e9

die() {
    echo "q-sunshine Debian package: $*" >&2
    exit 1
}

require_file() {
    [[ -f "$1" ]] || die "missing required file: $1"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

verify_patch_digest() {
    local patch_path="$1"
    local expected_sha256="$2"
    local label="$3"
    local actual_sha256

    require_file "$patch_path"
    actual_sha256="$(sha256sum "$patch_path" | awk '{print $1}')"
    [[ "$actual_sha256" == "$expected_sha256" ]] ||
        die "$label patch digest mismatch"
}

# Verify that an exported source tree contains an exact patch, then exercise
# its reverse and forward application without mutating the caller's worktree.
# This catches both a stale source export and a malformed canonical patch.
replay_patch_roundtrip() {
    local source_dir="$1"
    local patch_path="$2"
    local label="$3"

    git -C "$source_dir" apply --no-index --reverse --check "$patch_path" >/dev/null 2>&1 ||
        die "$label is absent from the Sunshine source export"
    git -C "$source_dir" apply --no-index --reverse "$patch_path" ||
        die "cannot reverse $label in the Sunshine source export"
    if ! git -C "$source_dir" apply --no-index --check "$patch_path" >/dev/null 2>&1; then
        git -C "$source_dir" apply --no-index "$patch_path" ||
            die "cannot restore the Sunshine source export after checking $label"
        die "$label does not apply cleanly to its declared base"
    fi
    git -C "$source_dir" apply --no-index "$patch_path" ||
        die "cannot restore $label after forward application check"
    git -C "$source_dir" apply --no-index --reverse --check "$patch_path" >/dev/null 2>&1 ||
        die "$label was not restored after the replay check"
    echo "Q_SUNSHINE_PATCH_REPLAY_OK patch=$label"
}

[[ "$(dpkg --print-architecture)" == "amd64" ]] ||
    die "this package recipe currently supports amd64 only"
[[ "$BUILD_JOBS" =~ ^[1-9][0-9]*$ ]] || die "QSUNSHINE_DEB_JOBS must be a positive integer"

if [[ "${QSUNSHINE_DEB_ALLOW_NONTRIXIE:-0}" != "1" ]]; then
    source /etc/os-release
    [[ "${VERSION_CODENAME:-}" == "trixie" ]] ||
        die "refusing to build outside Debian 13/Trixie; use the clean-chroot driver"
fi

for required in cc cmake ninja meson git dpkg dpkg-deb dpkg-shlibdeps patchelf readelf ldd \
                strip strings sed grep awk find sort install cp curl gzip sha256sum tar; do
    require_command "$required"
done

require_file "$SUNSHINE_SOURCE_DIR/CMakeLists.txt"
require_file "$LIBVA_SOURCE_DIR/meson.build"
require_file "$ROOT_DIR/$SUNSHINE_LISTENER_TEARDOWN_PATCH"
require_file "$ROOT_DIR/$SUNSHINE_SYSTEM_AUTH_PATCH"
require_file "$ROOT_DIR/$SIMPLE_WEB_SERVER_SYSTEM_AUTH_PATCH"
for required_submodule_file in \
    third-party/moonlight-common-c/enet/CMakeLists.txt \
    third-party/Simple-Web-Server/CMakeLists.txt \
    third-party/lizardbyte-common/CMakeLists.txt \
    third-party/libdisplaydevice/CMakeLists.txt \
    third-party/glad/cmake/CMakeLists.txt; do
    require_file "$SUNSHINE_SOURCE_DIR/$required_submodule_file"
done
for required_package_file in control.in q-sunshine q-sunshine-preflight q-sunshine@.service \
                             q-sunshine-qsf-control q-sunshine-qsf-client q-sunshine-qsf-gateway \
                             q-sunshine-qsf-system-auth-gateway \
                             q-sunshine-qsf-control@.service q-sunshine-qsf-gateway@.service \
                             q-sunshine-qsf-system-auth-gateway@.service \
                             q-sunshine-auth q-sunshine-auth@.service q-sunshine-remote.pam \
                             postinst postrm README.Debian example-instance.conf copyright \
                             changelog.in lintian-overrides; do
    require_file "$PACKAGE_DIR/$required_package_file"
done
for required_auth_file in q_sunshine_auth.py q_sunshine_auth_gateway.py pam_auth_helper.c; do
    require_file "$ROOT_DIR/extensions/system_auth/$required_auth_file"
done
for required_gamestream_auth_file in q_sunshine_gamestream_lease.py q_sunshine_lease_issuer.c; do
    require_file "$ROOT_DIR/extensions/gamestream_auth/$required_gamestream_auth_file"
done
for required_qsf_file in qsf_control.py qsf_client.py qsf_tls_client.py qsf_tls_gateway.py; do
    require_file "$ROOT_DIR/extensions/qsf_control/$required_qsf_file"
done
require_file "$ROOT_DIR/extensions/qsf_control/README.md"
require_file "$ROOT_DIR/docs/QSF_STREAM_NEGOTIATION.md"
require_file "$ROOT_DIR/docs/SUNSHINE_QEMU_INTEGRATION.md"
require_file "$ROOT_DIR/docs/SYSTEM_AUTH.md"
require_file "$ROOT_DIR/docs/GAMESTREAM_LEASE_AUTH.md"
for required_guest_file in qsf_guest_agent.c qsf_input_watcher.c qsf_wayland_clipboard_bridge.sh \
                           qsf_virgl_display_adapter.sh; do
    require_file "$ROOT_DIR/guest/$required_guest_file"
done

# The package is built from the QEMU Display1 replay, not a stock Sunshine
# tag.  Pin the listener-retirement and native system-auth patches before
# exporting the source; the latter are replayed below from the export rather
# than trusting the caller's mutable Sunshine worktree.
listener_patch_path="$ROOT_DIR/$SUNSHINE_LISTENER_TEARDOWN_PATCH"
system_auth_patch_path="$ROOT_DIR/$SUNSHINE_SYSTEM_AUTH_PATCH"
simple_web_server_system_auth_patch_path="$ROOT_DIR/$SIMPLE_WEB_SERVER_SYSTEM_AUTH_PATCH"
verify_patch_digest "$listener_patch_path" "$SUNSHINE_LISTENER_TEARDOWN_PATCH_SHA256" \
    "Sunshine listener-retirement"
verify_patch_digest "$system_auth_patch_path" "$SUNSHINE_SYSTEM_AUTH_PATCH_SHA256" \
    "Sunshine system-auth"
verify_patch_digest "$simple_web_server_system_auth_patch_path" \
    "$SIMPLE_WEB_SERVER_SYSTEM_AUTH_PATCH_SHA256" "Simple-Web-Server system-auth"

project_version="$(sed -n 's/^project(sunshine_qemu_mvp VERSION \([^ ]*\).*/\1/p' "$ROOT_DIR/CMakeLists.txt" | head -n 1)"
[[ -n "$project_version" ]] || die "could not determine project version"
git_build_number="$(git -C "$ROOT_DIR" rev-list --count HEAD 2>/dev/null || printf '0')"
package_version="${QSUNSHINE_DEB_VERSION:-${project_version}+git${git_build_number}}"
dpkg --validate-version "$package_version" >/dev/null 2>&1 ||
    die "invalid Debian version: $package_version"
sunshine_revision="${QSUNSHINE_SUNSHINE_REVISION:-$(git -C "$SUNSHINE_SOURCE_DIR" rev-parse HEAD 2>/dev/null || printf 'source-export')}"
sunshine_build_version="${QSUNSHINE_SUNSHINE_BUILD_VERSION:-$(git -C "$SUNSHINE_SOURCE_DIR" describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || printf '0.0.0')}"
[[ "$sunshine_build_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    die "QSUNSHINE_SUNSHINE_BUILD_VERSION must be a numeric x.y.z version"

mkdir -p "$WORK_ROOT" "$OUTPUT_DIR"
build_dir="$(mktemp -d "$WORK_ROOT/build.XXXXXX")"
stage_dir="$(mktemp -d "$WORK_ROOT/stage.XXXXXX")"
sunshine_source_export_dir="$build_dir/sunshine-source-export"
libva_build_dir="$build_dir/libva"
libva_runtime_prefix=/usr/lib/q-sunshine
libva_stage_dir="$build_dir/libva-stage"
libva_staged_prefix="$libva_stage_dir$libva_runtime_prefix"
sunshine_build_dir="$build_dir/sunshine"
deb_control_dir="$build_dir/debian"
package_name=q-sunshine-pve
package_root="$stage_dir/usr/lib/q-sunshine"

# Build only from a VCS-free copy.  Apart from making the package independent
# from ambient Git metadata, this lets the gate prove that both nested patch
# series entries reverse, apply, and reverse-check against exactly the source
# bytes about to be compiled.
mkdir -p "$sunshine_source_export_dir"
tar --create --exclude-vcs --file - --directory "$SUNSHINE_SOURCE_DIR" . |
    tar --extract --file - --directory "$sunshine_source_export_dir"
for required_export_file in \
    CMakeLists.txt \
    third-party/Simple-Web-Server/CMakeLists.txt \
    third-party/Simple-Web-Server/server_http.hpp; do
    require_file "$sunshine_source_export_dir/$required_export_file"
done
git -C "$sunshine_source_export_dir" apply --no-index --reverse --check "$listener_patch_path" \
    >/dev/null 2>&1 ||
    die "Sunshine source export does not contain the required listener-retirement patch"
replay_patch_roundtrip "$sunshine_source_export_dir" "$system_auth_patch_path" \
    "Sunshine system-auth patch"
replay_patch_roundtrip "$sunshine_source_export_dir/third-party/Simple-Web-Server" \
    "$simple_web_server_system_auth_patch_path" "Simple-Web-Server system-auth patch"

ffmpeg_prepared_binaries="${QSUNSHINE_FFMPEG_PREPARED_BINARIES:-$build_dir/ffmpeg}"
if [[ -z "${QSUNSHINE_FFMPEG_PREPARED_BINARIES:-}" ]]; then
    ffmpeg_archive="$build_dir/Linux-x86_64-ffmpeg.tar.gz"
    curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
        --output "$ffmpeg_archive" "$FFMPEG_ARCHIVE_URL"
    ffmpeg_actual_sha256="$(sha256sum "$ffmpeg_archive" | awk '{print $1}')"
    [[ "$ffmpeg_actual_sha256" == "$FFMPEG_ARCHIVE_SHA256" ]] ||
        die "pinned FFmpeg archive digest mismatch: $ffmpeg_actual_sha256"
    tar --extract --gzip --file "$ffmpeg_archive" --directory "$build_dir" \
        --no-same-owner --no-same-permissions
fi
require_file "$ffmpeg_prepared_binaries/lib/libavcodec.a"
require_file "$ffmpeg_prepared_binaries/lib/libavutil.a"

echo "Q_SUNSHINE_DEB_BUILD_START version=$package_version work=$build_dir ffmpeg_tag=$FFMPEG_BUILD_DEPS_TAG ffmpeg_sha256=$FFMPEG_ARCHIVE_SHA256"

# Keep libva private to this package. Sunshine's pinned FFmpeg requires the
# 2.21 ABI even when the software encoder and qemu_dbus capture are selected.
# Configure its final prefix before staging it: libva embeds its config and
# driver search locations, so a temporary build prefix would be a broken
# runtime value as well as a build-path disclosure.
meson setup "$libva_build_dir" "$LIBVA_SOURCE_DIR" \
    --prefix "$libva_runtime_prefix" --libdir lib --buildtype release
meson compile -C "$libva_build_dir" -j "$BUILD_JOBS"
meson install -C "$libva_build_dir" --destdir "$libva_stage_dir"
for private_lib in libva.so.2 libva-drm.so.2; do
    [[ -r "$libva_staged_prefix/lib/$private_lib" ]] ||
        die "private libva build produced no $private_lib"
done

# Sunshine deliberately logs selected source locations through __FILE__. Map
# both disposable roots to relative prefixes so neither those diagnostics nor
# DWARF-style file metadata disclose the source or build chroot.
BRANCH=q-sunshine BUILD_VERSION="$sunshine_build_version" COMMIT="$sunshine_revision" \
cmake -S "$sunshine_source_export_dir" -B "$sunshine_build_dir" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/usr/lib/q-sunshine \
    -DSUNSHINE_ASSETS_DIR=assets \
    -DCMAKE_C_FLAGS:STRING="-ffile-prefix-map=$ROOT_DIR=. -ffile-prefix-map=$build_dir=." \
    -DCMAKE_CXX_FLAGS:STRING="-isystem $libva_staged_prefix/include -ffile-prefix-map=$ROOT_DIR=. -ffile-prefix-map=$build_dir=." \
    -DCMAKE_EXE_LINKER_FLAGS="-L$libva_staged_prefix/lib -Wl,-rpath,\$ORIGIN/../lib" \
    -DFFMPEG_PREPARED_BINARIES:PATH="$ffmpeg_prepared_binaries" \
    -DBUILD_DOCS=OFF \
    -DBUILD_TESTS=OFF \
    -DSUNSHINE_ENABLE_QEMU_DBUS=ON \
    -DSUNSHINE_ENABLE_QEMU_DBUS_DMABUF=ON \
    -DSUNSHINE_ENABLE_QEMU_DBUS_AUDIO_ONLY=ON \
    -DSUNSHINE_ENABLE_TRAY=OFF \
    -DSUNSHINE_ENABLE_X11=OFF \
    -DSUNSHINE_ENABLE_WAYLAND=OFF \
    -DSUNSHINE_ENABLE_KWIN=OFF \
    -DSUNSHINE_ENABLE_PORTAL=OFF \
    -DSUNSHINE_ENABLE_DRM=OFF \
    -DSUNSHINE_ENABLE_VAAPI=OFF \
    -DSUNSHINE_ENABLE_CUDA=OFF \
    -DCUDA_FAIL_ON_MISSING=OFF \
    -DSUNSHINE_ENABLE_VULKAN=OFF \
    -DLIBVIRTUALHID_ENABLE_XTEST=OFF
cmake --build "$sunshine_build_dir" --target sunshine web-ui --parallel "$BUILD_JOBS"

sunshine_binary="$sunshine_build_dir/sunshine"
[[ -x "$sunshine_binary" ]] || die "Sunshine build did not produce a runtime binary"
[[ -f "$sunshine_build_dir/assets/apps.json" ]] || die "Sunshine assets were not staged in the build tree"
[[ -f "$sunshine_build_dir/assets/web/index.html" ]] || die "Sunshine Web UI was not built"

install -d "$package_root/bin" "$package_root/lib" "$package_root/assets"
install -m 0755 "$sunshine_binary" "$package_root/bin/sunshine"
strip --strip-unneeded "$package_root/bin/sunshine"

# PAM itself stays a normal Debian runtime dependency. Keep this tiny
# conversation helper separate from Sunshine so the media binary remains
# headless and has no PAM linkage. It receives password bytes only on stdin.
auth_helper_binary="$build_dir/q-sunshine-pam-auth"
cc -std=c11 -O2 -D_FORTIFY_SOURCE=2 -fstack-protector-strong -fPIE \
    -Wall -Wextra -Werror -Wformat=2 -Werror=format-security \
    "$ROOT_DIR/extensions/system_auth/pam_auth_helper.c" -o "$auth_helper_binary" \
    -pie -Wl,-z,relro,-z,now -lpam

# The optional prepared GameStream lease issuer has its own narrow OpenSSL
# boundary. It is not linked into Sunshine and never receives a client private
# key; authd invokes it only after a valid qsa1 ticket/CSR request.
lease_issuer_binary="$build_dir/q-sunshine-lease-issuer"
cc -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -D_FORTIFY_SOURCE=2 \
    -fstack-protector-strong -fPIE -Wall -Wextra -Werror -Wformat=2 \
    -Werror=format-security \
    "$ROOT_DIR/extensions/gamestream_auth/q_sunshine_lease_issuer.c" \
    -o "$lease_issuer_binary" -pie -Wl,-z,relro,-z,now -lssl -lcrypto

# These package-owned release helpers are ELF executables just like Sunshine.
# Strip local debug sections before lintian verifies the staged package.
strip --strip-unneeded "$auth_helper_binary"
strip --strip-unneeded "$lease_issuer_binary"

# The build tree's shader directory is normally a symlink into the source
# tree. Dereference it so the package never contains a build-machine path.
cp -aL "$sunshine_build_dir/assets/." "$package_root/assets/"
[[ -f "$package_root/assets/apps.json" ]] || die "package asset copy failed"

libva_real="$(readlink -f "$libva_staged_prefix/lib/libva.so.2")"
[[ -f "$libva_real" ]] || die "cannot resolve built libva.so.2"
install -m 0644 "$libva_real" "$package_root/lib/$(basename "$libva_real")"
ln -s "$(basename "$libva_real")" "$package_root/lib/libva.so.2"
libva_drm_real="$(readlink -f "$libva_staged_prefix/lib/libva-drm.so.2")"
[[ -f "$libva_drm_real" ]] || die "cannot resolve built libva-drm.so.2"
install -m 0644 "$libva_drm_real" "$package_root/lib/$(basename "$libva_drm_real")"
ln -s "$(basename "$libva_drm_real")" "$package_root/lib/libva-drm.so.2"
strip --strip-unneeded "$package_root/lib/$(basename "$libva_real")"
strip --strip-unneeded "$package_root/lib/$(basename "$libva_drm_real")"
# libva-drm is a separately loadable VA platform library.  Its dependency on
# libva must resolve to this adjacent private ABI rather than a host copy;
# Sunshine's RUNPATH is intentionally not inherited by indirect dependencies.
patchelf --set-rpath '$ORIGIN' "$package_root/lib/$(basename "$libva_drm_real")"

install -Dm755 "$PACKAGE_DIR/q-sunshine" "$stage_dir/usr/bin/q-sunshine"
install -Dm755 "$PACKAGE_DIR/q-sunshine-preflight" "$stage_dir/usr/bin/q-sunshine-preflight"
for qsf_tool in qsf_control qsf_client qsf_tls_client qsf_tls_gateway; do
    install -Dm644 "$ROOT_DIR/extensions/qsf_control/$qsf_tool.py" \
        "$package_root/qsf/$qsf_tool.py"
done
for auth_module in q_sunshine_auth q_sunshine_auth_gateway; do
    install -Dm644 "$ROOT_DIR/extensions/system_auth/$auth_module.py" \
        "$package_root/system_auth/$auth_module.py"
done
install -Dm644 "$ROOT_DIR/extensions/gamestream_auth/q_sunshine_gamestream_lease.py" \
    "$package_root/system_auth/q_sunshine_gamestream_lease.py"
install -Dm755 "$auth_helper_binary" "$package_root/bin/q-sunshine-pam-auth"
install -Dm755 "$lease_issuer_binary" "$package_root/bin/q-sunshine-lease-issuer"
install -Dm755 "$PACKAGE_DIR/q-sunshine-auth" "$stage_dir/usr/bin/q-sunshine-auth"
install -Dm755 "$PACKAGE_DIR/q-sunshine-qsf-control" "$stage_dir/usr/bin/q-sunshine-qsf-control"
install -Dm755 "$PACKAGE_DIR/q-sunshine-qsf-client" "$stage_dir/usr/bin/q-sunshine-qsf-client"
install -Dm755 "$PACKAGE_DIR/q-sunshine-qsf-gateway" "$stage_dir/usr/bin/q-sunshine-qsf-gateway"
install -Dm755 "$PACKAGE_DIR/q-sunshine-qsf-system-auth-gateway" \
    "$stage_dir/usr/bin/q-sunshine-qsf-system-auth-gateway"
for guest_file in qsf_guest_agent.c qsf_input_watcher.c qsf_wayland_clipboard_bridge.sh; do
    install -Dm644 "$ROOT_DIR/guest/$guest_file" "$stage_dir/usr/share/q-sunshine/guest/$guest_file"
done
install -Dm755 "$ROOT_DIR/guest/qsf_virgl_display_adapter.sh" \
    "$stage_dir/usr/share/q-sunshine/guest/qsf_virgl_display_adapter.sh"
install -Dm644 "$PACKAGE_DIR/q-sunshine@.service" "$stage_dir/usr/lib/systemd/system/q-sunshine@.service"
install -Dm644 "$PACKAGE_DIR/q-sunshine-qsf-control@.service" \
    "$stage_dir/usr/lib/systemd/system/q-sunshine-qsf-control@.service"
install -Dm644 "$PACKAGE_DIR/q-sunshine-qsf-gateway@.service" \
    "$stage_dir/usr/lib/systemd/system/q-sunshine-qsf-gateway@.service"
install -Dm644 "$PACKAGE_DIR/q-sunshine-qsf-system-auth-gateway@.service" \
    "$stage_dir/usr/lib/systemd/system/q-sunshine-qsf-system-auth-gateway@.service"
install -Dm644 "$PACKAGE_DIR/q-sunshine-auth@.service" \
    "$stage_dir/usr/lib/systemd/system/q-sunshine-auth@.service"
install -Dm644 "$PACKAGE_DIR/README.Debian" "$stage_dir/usr/share/doc/$package_name/README.Debian"
install -Dm644 "$ROOT_DIR/extensions/qsf_control/README.md" "$stage_dir/usr/share/doc/$package_name/QSF.md"
install -Dm644 "$ROOT_DIR/docs/QSF_STREAM_NEGOTIATION.md" \
    "$stage_dir/usr/share/doc/$package_name/QSF_STREAM_NEGOTIATION.md"
install -Dm644 "$ROOT_DIR/docs/SUNSHINE_QEMU_INTEGRATION.md" \
    "$stage_dir/usr/share/doc/$package_name/SUNSHINE_QEMU_INTEGRATION.md"
install -Dm644 "$ROOT_DIR/docs/SYSTEM_AUTH.md" \
    "$stage_dir/usr/share/doc/$package_name/SYSTEM_AUTH.md"
install -Dm644 "$ROOT_DIR/docs/GAMESTREAM_LEASE_AUTH.md" \
    "$stage_dir/usr/share/doc/$package_name/GAMESTREAM_LEASE_AUTH.md"
install -Dm644 "$PACKAGE_DIR/q-sunshine-remote.pam" \
    "$stage_dir/usr/share/doc/$package_name/q-sunshine-remote.pam"
install -Dm644 "$PACKAGE_DIR/example-instance.conf" \
    "$stage_dir/usr/share/doc/$package_name/example-instance.conf"
install -Dm644 "$PACKAGE_DIR/copyright" "$stage_dir/usr/share/doc/$package_name/copyright"
sed "s|@VERSION@|$package_version|" "$PACKAGE_DIR/changelog.in" \
    > "$stage_dir/usr/share/doc/$package_name/changelog"
gzip -n -9 "$stage_dir/usr/share/doc/$package_name/changelog"
install -Dm644 "$PACKAGE_DIR/lintian-overrides" \
    "$stage_dir/usr/share/lintian/overrides/$package_name"

staged_binary="$package_root/bin/sunshine"
patchelf --set-rpath '$ORIGIN/../lib' "$staged_binary"
readelf -d "$staged_binary" | grep -Fq '$ORIGIN/../lib' ||
    die "package binary does not contain the private relative RPATH"
binary_strings="$build_dir/sunshine.strings"
strings "$staged_binary" > "$binary_strings"
grep -Fxq '/usr/lib/q-sunshine/assets' "$binary_strings" ||
    die "package binary was not compiled with the installed assets prefix"
for forbidden_build_path in "$ROOT_DIR" "$build_dir"; do
    if grep -Fq "$forbidden_build_path" "$binary_strings"; then
        die "package binary contains a build-machine path"
    fi
done
libva_strings="$build_dir/libva.strings"
strings "$package_root/lib/$(basename "$libva_real")" > "$libva_strings"
grep -Fq "$libva_runtime_prefix/etc/libva.conf" "$libva_strings" ||
    die "bundled libva was not configured with its final runtime prefix"
grep -Fq "$libva_runtime_prefix/lib/dri" "$libva_strings" ||
    die "bundled libva driver search path does not use its final runtime prefix"
readelf -d "$package_root/lib/$(basename "$libva_drm_real")" | grep -Fq '$ORIGIN' ||
    die "bundled libva-drm has no relative runtime search path"
for bundled_private_lib in \
    "$package_root/lib/$(basename "$libva_real")" \
    "$package_root/lib/$(basename "$libva_drm_real")"; do
    if strings "$bundled_private_lib" | grep -Fq "$build_dir"; then
        die "bundled private library contains a build-machine path"
    fi
done

ldd_output="$(ldd "$staged_binary")"
printf '%s\n' "$ldd_output" | grep -F 'not found' >/dev/null &&
    die "package binary has an unresolved runtime library"
# ldd preserves the launcher's $ORIGIN/../lib spelling, which can contain an
# intentional bin/../ component. Compare canonical paths instead of treating
# that equivalent spelling as a host-libva fallback.
ldd_libva_path="$(printf '%s\n' "$ldd_output" | awk '$1 == "libva.so.2" && $2 == "=>" { print $3; exit }')"
[[ -n "$ldd_libva_path" && "$ldd_libva_path" != "not" ]] ||
    die "package binary has no resolved libva"
[[ "$(readlink -f "$ldd_libva_path")" == "$(readlink -f "$package_root/lib/libva.so.2")" ]] ||
    die "package binary does not resolve the bundled private libva"
ldd_libva_drm_path="$(printf '%s\n' "$ldd_output" | awk '$1 == "libva-drm.so.2" && $2 == "=>" { print $3; exit }')"
if [[ -n "$ldd_libva_drm_path" ]]; then
    [[ "$ldd_libva_drm_path" != "not" ]] ||
        die "package binary has an unresolved libva-drm"
    [[ "$(readlink -f "$ldd_libva_drm_path")" == "$(readlink -f "$package_root/lib/libva-drm.so.2")" ]] ||
        die "package binary mixes the private libva with a system libva-drm"
fi
for forbidden_runtime in libX11 libXtst libXi libXext libXrender libXrandr libwayland libpulse libasound; do
    if printf '%s\n' "$ldd_output" | grep -F "$forbidden_runtime" >/dev/null; then
        die "headless package unexpectedly resolves $forbidden_runtime"
    fi
done

install -d "$stage_dir/DEBIAN" "$deb_control_dir"
# dpkg-shlibdeps requires a debian/control file in its working directory even
# when emitting substitutions to stdout.  Trixie dpkg also requires that file
# to start with a Source stanza, although the final binary package control
# must contain only the generated binary stanza below.
{
    printf 'Source: %s\n\n' "$package_name"
    sed -e "s|@VERSION@|$package_version|" -e 's|@DEPENDS@|${shlibs:Depends}|' \
        "$PACKAGE_DIR/control.in"
} > "$deb_control_dir/control"
# libva-drm is a separately shipped, loadable platform library.  Inspect it
# as well as the executable so its libdrm ABI requirement is represented in
# Depends even when the software-only Sunshine link drops it under --as-needed.
shlib_depends="$(cd "$build_dir" && dpkg-shlibdeps -O --ignore-missing-info \
    -l"$package_root/lib" "$staged_binary" \
    "$package_root/bin/q-sunshine-pam-auth" \
    "$package_root/bin/q-sunshine-lease-issuer" \
    "$package_root/lib/$(basename "$libva_drm_real")" | sed -n 's/^shlibs:Depends=//p')"
depends='python3 (>= 3.11)'
if [[ -n "$shlib_depends" ]]; then
    depends+=", $shlib_depends"
fi
sed -e "s|@VERSION@|$package_version|" -e "s|@DEPENDS@|$depends|" \
    "$PACKAGE_DIR/control.in" > "$stage_dir/DEBIAN/control"
install -m 0755 "$PACKAGE_DIR/postinst" "$stage_dir/DEBIAN/postinst"
install -m 0755 "$PACKAGE_DIR/postrm" "$stage_dir/DEBIAN/postrm"

artifact="$OUTPUT_DIR/${package_name}_${package_version}_amd64.deb"
dpkg-deb --build --root-owner-group "$stage_dir" "$artifact" >/dev/null
[[ -f "$artifact" ]] || die "dpkg-deb did not produce an artifact"

# Treat a lintian error as a packaging failure, but do not turn non-actionable
# informational tags into a failed reproducible build.
lintian --fail-on error "$artifact"

echo "Q_SUNSHINE_DEB_BUILD_OK artifact=$artifact sunshine_revision=$sunshine_revision"
echo "Q_SUNSHINE_DEB_BUILD_DIRS_RETAINED build=$build_dir stage=$stage_dir"
