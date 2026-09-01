#!/usr/bin/env bash
# Rebuild and execute the no-GPU test matrix with ASan, UBSan and leak checks.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT/build-sanitize}"
OUTPUT_LOG="${OUTPUT_LOG:-$ROOT/artifacts/validation/sanitizers-ctest.log}"
detected_jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
if (( detected_jobs > 4 )); then detected_jobs=4; fi
JOBS="${JOBS:-$detected_jobs}"

rm -rf "$BUILD_DIR"
mkdir -p "$(dirname -- "$OUTPUT_LOG")"

cmake -S "$ROOT" -B "$BUILD_DIR" -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug \
  -DQMDP_BUILD_DBUS=ON \
  -DQMDP_BUILD_TOOLS=ON \
  -DQMDP_ENABLE_SANITIZERS=ON
cmake --build "$BUILD_DIR" -j"$JOBS"

ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=1:halt_on_error=1}" \
UBSAN_OPTIONS="${UBSAN_OPTIONS:-halt_on_error=1:print_stacktrace=1}" \
ctest --test-dir "$BUILD_DIR" --output-on-failure -V | tee "$OUTPUT_LOG"
