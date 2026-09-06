#!/usr/bin/env bash
# Download the pinned Alpine cloud image used by the headless VirGL guest
# qualification.  It intentionally creates no mutable guest overlay: every
# E2E run must create a fresh copy-on-write image instead.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ALPINE_VERSION="${VIRGL_ALPINE_VERSION:-3.20.10}"
ALPINE_SERIES="${VIRGL_ALPINE_SERIES:-v3.20}"
ALPINE_MIRROR="${VIRGL_ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
OUTPUT_DIR="${VIRGL_VM_DIR:-$ROOT/vm/alpine-virgl-$ALPINE_VERSION}"
IMAGE_NAME="generic_alpine-${ALPINE_VERSION}-x86_64-bios-cloudinit-r0.qcow2"
IMAGE_PATH="$OUTPUT_DIR/$IMAGE_NAME"
CHECKSUM_PATH="$IMAGE_PATH.sha512"

# This is the upstream SHA-512 published for the exact image above.  Keeping
# it here makes the downloaded input reproducible even if a mirror changes its
# accompanying checksum file.
EXPECTED_SHA512='dbf008c5910e22d2c9c2268ea9ce2dfef8b12e6f5e303c7515bdc1c4540d1b01cc7452fd351910b349162af6364f183af41ff01acdc2f6cb38a3652ee7f7e56e'
IMAGE_URL="$ALPINE_MIRROR/$ALPINE_SERIES/releases/cloud/$IMAGE_NAME"

die() {
  printf 'provision Alpine VirGL guest: %s\n' "$*" >&2
  exit 1
}

for required in curl sha512sum mkdir mktemp mv chmod rm awk; do
  command -v "$required" >/dev/null || die "missing required command: $required"
done

if [[ "$ALPINE_VERSION" != '3.20.10' || "$ALPINE_SERIES" != 'v3.20' ]]; then
  die 'only the tested 3.20.10 / v3.20 image is accepted by this pinned provisioner'
fi

mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"

verify_image() {
  [[ -f "$IMAGE_PATH" ]] || return 1
  local actual
  actual="$(sha512sum "$IMAGE_PATH" | awk '{print $1}')"
  [[ "$actual" == "$EXPECTED_SHA512" ]]
}

if verify_image; then
  printf 'Alpine VirGL base already verified: %s\n' "$IMAGE_PATH"
else
  if [[ -e "$IMAGE_PATH" && "${VIRGL_FORCE_DOWNLOAD:-0}" != '1' ]]; then
    die "existing image checksum differs; inspect it or rerun with VIRGL_FORCE_DOWNLOAD=1: $IMAGE_PATH"
  fi

  temp_image="$(mktemp "$OUTPUT_DIR/.${IMAGE_NAME}.download.XXXXXX")"
  cleanup() {
    rm -f -- "$temp_image"
  }
  trap cleanup EXIT INT TERM

  printf 'Downloading pinned Alpine VirGL base: %s\n' "$IMAGE_URL"
  curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
    --output "$temp_image" "$IMAGE_URL"

  actual="$(sha512sum "$temp_image" | awk '{print $1}')"
  [[ "$actual" == "$EXPECTED_SHA512" ]] ||
    die "downloaded image SHA-512 does not match the pinned upstream value"
  mv -f -- "$temp_image" "$IMAGE_PATH"
  trap - EXIT INT TERM
fi

printf '%s\n' "$EXPECTED_SHA512" > "$CHECKSUM_PATH"
chmod 600 "$IMAGE_PATH" "$CHECKSUM_PATH"
printf 'Alpine VirGL base ready: %s\n' "$IMAGE_PATH"
