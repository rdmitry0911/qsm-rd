#!/usr/bin/env bash
# Build the pinned Sunshine worktree with the QEMU Display1 patch series.
# This software-only deployment intentionally has no host X11/Wayland runtime
# dependency. The bundled static FFmpeg still needs core libva, so the helper
# uses an isolated compatible prefix and never changes the system linker setup.
# Guest PCM comes from QEMU AudioOutListener; host PulseAudio is not linked.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${SUNSHINE_SOURCE_DIR:-$ROOT/.upstream/Sunshine}"
BUILD_DIR="${SUNSHINE_BUILD_DIR:-$ROOT/.upstream/build-sunshine-qemu}"
LIBVA_PREFIX="${ISOLATED_LIBVA_PREFIX:-$ROOT/.upstream/prefix-libva-2.21.0}"
BUILD_JOBS="${BUILD_JOBS:-2}"

die() {
  echo "build upstream Sunshine QEMU: $*" >&2
  exit 1
}

[[ -f "$SOURCE_DIR/CMakeLists.txt" ]] ||
  die "pinned Sunshine worktree is missing: $SOURCE_DIR"
for required_submodule_file in \
  third-party/moonlight-common-c/enet/CMakeLists.txt \
  third-party/Simple-Web-Server/CMakeLists.txt \
  third-party/lizardbyte-common/CMakeLists.txt \
  third-party/libdisplaydevice/CMakeLists.txt \
  third-party/glad/cmake/CMakeLists.txt; do
  [[ -f "$SOURCE_DIR/$required_submodule_file" ]] ||
    die "Sunshine submodules are missing; run: git -C $SOURCE_DIR submodule update --init --recursive"
done
[[ -f "$LIBVA_PREFIX/lib/libva.so.2" ]] ||
  die "isolated libva is missing; run $ROOT/scripts/build-isolated-libva-2.21.sh first"
[[ "$BUILD_JOBS" =~ ^[1-9][0-9]*$ ]] || die "BUILD_JOBS must be a positive integer"
command -v cmake >/dev/null || die "cmake is required"
command -v ninja >/dev/null || die "ninja is required"
command -v readelf >/dev/null || die "readelf is required for the runtime-dependency gate"

cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$BUILD_DIR" \
  -DCMAKE_EXE_LINKER_FLAGS="-L$LIBVA_PREFIX/lib -Wl,-rpath,$LIBVA_PREFIX/lib" \
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
  -DSUNSHINE_ENABLE_VULKAN=OFF \
  -DLIBVIRTUALHID_ENABLE_XTEST=OFF
cmake --build "$BUILD_DIR" --target sunshine --parallel "$BUILD_JOBS"

[[ -x "$BUILD_DIR/sunshine" ]] || die "Sunshine build did not produce $BUILD_DIR/sunshine"
[[ -f "$BUILD_DIR/assets/apps.json" ]] || die "Sunshine assets were not staged beside build binary"
# Do not use a pipe here: with pipefail, grep -q can exit early and make
# strings report SIGPIPE even after it has found the expected path.
grep -Fxq "$BUILD_DIR/assets" < <(strings "$BUILD_DIR/sunshine") ||
  die "Sunshine binary was not compiled with its reproducible assets prefix"

# The QEMU Display1 route talks to the VM over private D-Bus and uses GBM/EGL
# directly.  The deployed Sunshine process must not gain a host-desktop
# dependency merely because the disposable Moonlight test client uses Xvfb.
for forbidden_needed in libX11.so libXtst.so libXi.so libXext.so libXrender.so libXrandr.so libwayland; do
  if grep -F "[$forbidden_needed" < <(readelf -d "$BUILD_DIR/sunshine") >/dev/null; then
    die "software QEMU build unexpectedly needs $forbidden_needed"
  fi
done
# Do not stop at direct ELF entries: distro PulseAudio builds can themselves
# pull X11 helpers. This profile omits the host audio backend entirely, so the
# whole resolved dependency closure must remain desktop-stack free.
for forbidden_runtime in libX11 libXtst libXi libXext libXrender libXrandr libwayland libpulse libasound; do
  if grep -F "$forbidden_runtime" < <(ldd "$BUILD_DIR/sunshine") >/dev/null; then
    die "QEMU guest-audio-only build transitively resolves $forbidden_runtime"
  fi
done
ldd "$BUILD_DIR/sunshine" | grep -Fq "$LIBVA_PREFIX/lib/libva.so.2" ||
  die "Sunshine binary does not resolve libva from isolated prefix"

printf 'SUNSHINE_QEMU_BUILD_OK binary=%s desktop_runtime=none host_audio=none vaapi=off libva_prefix=%s\n' \
  "$BUILD_DIR/sunshine" "$LIBVA_PREFIX"
