#!/usr/bin/env bash
# Build the standalone project and retain a real QEMU/TCG E2E trace.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT/build-real-qemu}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/artifacts/validation/real-qemu}"
BUILD_TYPE="${BUILD_TYPE:-RelWithDebInfo}"
detected_jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
if (( detected_jobs > 4 )); then detected_jobs=4; fi
JOBS="${JOBS:-$detected_jobs}"

for required in cmake ninja qemu-system-x86_64 dbus-run-session busctl as ld ffmpeg ffprobe; do
  command -v "$required" >/dev/null || {
    echo "missing required command: $required" >&2
    exit 1
  }
done

rm -rf "$BUILD_DIR" "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

{
  echo "root=$ROOT"
  echo "build_dir=$BUILD_DIR"
  echo "build_type=$BUILD_TYPE"
  echo "date_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  uname -a
  qemu-system-x86_64 --version | head -n1
  echo "display_backends:"
  qemu-system-x86_64 -display help
  echo "audio_backends:"
  qemu-system-x86_64 -audiodev help
  if [[ -e /dev/kvm ]]; then
    echo "kvm_device=present (the test still forces TCG)"
  else
    echo "kvm_device=absent (the test uses TCG)"
  fi
} > "$OUTPUT_DIR/environment.txt"

cmake -S "$ROOT" -B "$BUILD_DIR" -G Ninja \
  -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
  -DQMDP_BUILD_DBUS=ON \
  -DQMDP_BUILD_TOOLS=ON \
  -DQMDP_ENABLE_REAL_QEMU_E2E=ON
cmake --build "$BUILD_DIR" -j"$JOBS"

ctest --test-dir "$BUILD_DIR" --output-on-failure -V \
  -R '^qmdp_real_qemu(_absolute)?_e2e$' | tee "$OUTPUT_DIR/ctest.log"

if [[ ! -f "$BUILD_DIR/real-qemu-e2e/trace.txt" ||
      ! -f "$BUILD_DIR/real-qemu-absolute-e2e/trace.txt" ]]; then
  echo "real-QEMU CTest passed without producing both pointer-mode traces" >&2
  exit 1
fi
cp -a "$BUILD_DIR/real-qemu-e2e/." "$OUTPUT_DIR/"
mkdir -p "$OUTPUT_DIR/absolute"
cp -a "$BUILD_DIR/real-qemu-absolute-e2e/." "$OUTPUT_DIR/absolute/"

printf 'REAL_QEMU_SELFTEST_OK output=%s\n' "$OUTPUT_DIR"
