#!/usr/bin/env bash
# Build and validate the complete CPU-only development path.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT/build-no-gpu}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/artifacts/validation/no-gpu-selftest}"
BUILD_TYPE="${BUILD_TYPE:-RelWithDebInfo}"
detected_jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
if (( detected_jobs > 4 )); then detected_jobs=4; fi
JOBS="${JOBS:-$detected_jobs}"

command -v cmake >/dev/null
command -v ffmpeg >/dev/null

rm -rf "$BUILD_DIR" "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

{
  echo "root=$ROOT"
  echo "build_dir=$BUILD_DIR"
  echo "build_type=$BUILD_TYPE"
  echo "date_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cmake --version | head -n1
  "${CXX:-c++}" --version | head -n1
  ffmpeg -version | head -n1
} | tee "$OUTPUT_DIR/environment.txt"

cmake -S "$ROOT" -B "$BUILD_DIR" -G Ninja \
  -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
  -DQMDP_BUILD_DBUS=ON \
  -DQMDP_BUILD_TOOLS=ON
cmake --build "$BUILD_DIR" -j"$JOBS"
ctest --test-dir "$BUILD_DIR" --output-on-failure -V \
  | tee "$OUTPUT_DIR/ctest.log"

"$BUILD_DIR/qmdp_dbus_selftest" \
  --output "$OUTPUT_DIR/encoded" \
  --duration-ms 1200 \
  | tee "$OUTPUT_DIR/selftest.log"

if command -v ffprobe >/dev/null; then
  : > "$OUTPUT_DIR/ffprobe.txt"
  shopt -s nullglob
  for segment in "$OUTPUT_DIR"/encoded/*.mkv; do
    {
      echo "=== $(basename "$segment") ==="
      ffprobe -v error \
        -select_streams v:0 \
        -show_entries stream=codec_name,width,height,pix_fmt,avg_frame_rate \
        -show_entries format=duration,size \
        -of default=noprint_wrappers=1 \
        "$segment"
    } >> "$OUTPUT_DIR/ffprobe.txt"
  done
fi

cp -f "$BUILD_DIR/dbus-integration-last.ppm" \
      "$OUTPUT_DIR/dbus-integration-last.ppm"
cp -f "$BUILD_DIR/message-bus-map-e2e/final.ppm" \
      "$OUTPUT_DIR/message-bus-map-final.ppm"
cp -f "$BUILD_DIR/message-bus-inline-e2e/final.ppm" \
      "$OUTPUT_DIR/message-bus-inline-final.ppm"

printf 'NO_GPU_SELFTEST_OK output=%s\n' "$OUTPUT_DIR"
