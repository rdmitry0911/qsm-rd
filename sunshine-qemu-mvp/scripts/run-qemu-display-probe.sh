#!/usr/bin/env bash
# Attach the real probe to a QEMU instance exported on a D-Bus message bus.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 DBUS_ADDRESS_FILE [OUTPUT_DIRECTORY]" >&2
  exit 2
fi

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ADDRESS_FILE=$1
OUTPUT_DIR=${2:-"$ROOT/artifacts/qemu-probe"}
BUILD_DIR="${BUILD_DIR:-$ROOT/build-no-gpu}"
PROBE="${PROBE:-$BUILD_DIR/qemu-display-probe}"
DESTINATION="${DESTINATION:-org.qemu}"
CONSOLE="${CONSOLE:-0}"
DURATION_MS="${DURATION_MS:-10000}"
# Standard VGA exposes capture/input but may reject the optional SetUIInfo
# method.  Leave it off by default; callers that know their guest supports
# resize can opt in with REQUEST_SIZE=WIDTHxHEIGHT.
REQUEST_SIZE="${REQUEST_SIZE:-}"
FPS="${FPS:-30}"

if [[ ! -x "$PROBE" ]]; then
  echo "probe not found: $PROBE; run scripts/run-no-gpu-selftest.sh first" >&2
  exit 1
fi
if [[ ! -s "$ADDRESS_FILE" ]]; then
  echo "D-Bus address file not found or empty: $ADDRESS_FILE" >&2
  exit 1
fi

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

args=(
  --dbus-address-file "$ADDRESS_FILE"
  --destination "$DESTINATION"
  --console "$CONSOLE"
  --duration-ms "$DURATION_MS"
  --fps "$FPS"
  --input-smoke
  --snapshot "$OUTPUT_DIR/final.ppm"
)

if [[ -n "$REQUEST_SIZE" ]]; then
  args+=(--request-size "$REQUEST_SIZE")
fi

if [[ "${ENCODE:-0}" == 1 ]]; then
  args+=(--encode-dir "$OUTPUT_DIR/encoded")
fi
if [[ "${AUDIO:-1}" == 0 ]]; then
  args+=(--no-audio)
elif [[ "${REQUIRE_AUDIO:-0}" == 1 ]]; then
  args+=(--require-audio)
fi

"$PROBE" "${args[@]}" | tee "$OUTPUT_DIR/probe.log"
