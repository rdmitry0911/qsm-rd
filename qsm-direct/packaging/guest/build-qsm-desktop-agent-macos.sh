#!/bin/sh
# Build the macOS QSM desktop agent (clipboard-only) with clang + NSPasteboard.
# The wire protocol and all non-clipboard logic are the same portable C used on
# Linux; only guest/qsm_macos_clipboard.m is macOS-specific.
set -eu
ROOT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
OUT_DIR="${QSM_DESKTOP_MACOS_OUT_DIR:-$ROOT_DIR/.packaging-build/qsm-desktop-agent-macos}"
mkdir -p "$OUT_DIR"
clang -std=c11 -O2 -Wall -Wextra -DQSM_DESKTOP_CLIPBOARD_ONLY=1 -fobjc-arc \
  "$ROOT_DIR/guest/qsf_guest_agent.c" \
  "$ROOT_DIR/guest/qsm_macos_clipboard.m" \
  -framework Foundation -framework AppKit \
  -o "$OUT_DIR/qsm-desktop-agent"
echo "built $OUT_DIR/qsm-desktop-agent"
