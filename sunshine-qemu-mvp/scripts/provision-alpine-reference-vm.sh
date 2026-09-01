#!/usr/bin/env bash
# Fetch a small, pinned Linux guest used for real-QEMU qualification.
# The ISO and writable overlay are intentionally ignored by Git.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ALPINE_VERSION="${ALPINE_VERSION:-3.24.1}"
ALPINE_SERIES="${ALPINE_SERIES:-v3.24}"
ALPINE_MIRROR="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
VM_DIR="${VM_DIR:-$ROOT/vm/alpine-virt-$ALPINE_VERSION}"
ISO_NAME="alpine-virt-$ALPINE_VERSION-x86_64.iso"
CHECKSUM_NAME="$ISO_NAME.sha256"
ISO_PATH="$VM_DIR/$ISO_NAME"
CHECKSUM_PATH="$VM_DIR/$CHECKSUM_NAME"
OVERLAY_PATH="$VM_DIR/data.qcow2"
BASE_URL="$ALPINE_MIRROR/$ALPINE_SERIES/releases/x86_64"

for required in curl sha256sum qemu-img; do
  command -v "$required" >/dev/null || {
    echo "missing required command: $required" >&2
    exit 1
  }
done

mkdir -p "$VM_DIR"
chmod 700 "$VM_DIR"

checksum_tmp="$(mktemp "$VM_DIR/.${CHECKSUM_NAME}.XXXXXX")"
iso_tmp=""
cleanup() {
  rm -f "$checksum_tmp"
  if [[ -n "$iso_tmp" ]]; then
    rm -f "$iso_tmp"
  fi
}
trap cleanup EXIT INT TERM

curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
  --output "$checksum_tmp" "$BASE_URL/$CHECKSUM_NAME"

need_download=1
if [[ -f "$ISO_PATH" ]] &&
   (cd "$VM_DIR" && sha256sum --check --status "$checksum_tmp"); then
  need_download=0
fi

if (( need_download )); then
  iso_tmp="$(mktemp "$VM_DIR/.${ISO_NAME}.XXXXXX")"
  curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
    --output "$iso_tmp" "$BASE_URL/$ISO_NAME"
  mv -f "$iso_tmp" "$ISO_PATH"
  iso_tmp=""
fi

mv -f "$checksum_tmp" "$CHECKSUM_PATH"
checksum_tmp=""
(cd "$VM_DIR" && sha256sum --check "$CHECKSUM_NAME")

if [[ ! -f "$OVERLAY_PATH" ]]; then
  qemu-img create -f qcow2 "$OVERLAY_PATH" 2G >/dev/null
fi

printf 'ALPINE_REFERENCE_VM_READY\n'
printf 'iso=%s\n' "$ISO_PATH"
printf 'overlay=%s\n' "$OVERLAY_PATH"
printf 'sha256=%s\n' "$(awk '{print $1}' "$CHECKSUM_PATH")"
