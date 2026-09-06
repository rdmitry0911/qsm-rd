#!/usr/bin/env bash
# Qualify the project against the pinned Alpine guest, not only the BIOS fixture.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ALPINE_VERSION="${ALPINE_VERSION:-3.24.1}"
VM_DIR="${VM_DIR:-$ROOT/vm/alpine-virt-$ALPINE_VERSION}"
ISO_PATH="$VM_DIR/alpine-virt-$ALPINE_VERSION-x86_64.iso"
OVERLAY_PATH="$VM_DIR/data.qcow2"
BUILD_DIR="${BUILD_DIR:-$ROOT/build-runtime}"
PROBE="${PROBE:-$BUILD_DIR/qemu-display-probe}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/artifacts/validation/alpine-reference-e2e}"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
BOOT_WAIT_SECONDS="${BOOT_WAIT_SECONDS:-12}"

for required in "$QEMU_BINARY" "$PROBE" dbus-daemon busctl ffprobe; do
  command -v "$required" >/dev/null || {
    echo "missing required command: $required" >&2
    exit 1
  }
done
if [[ ! -f "$ISO_PATH" || ! -f "$OVERLAY_PATH" ]]; then
  echo "reference VM is absent; run scripts/provision-alpine-reference-vm.sh first" >&2
  exit 1
fi

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"

mapfile -t bus_info < <(dbus-daemon --session --fork --print-address=1 --print-pid=1)
export DBUS_SESSION_BUS_ADDRESS="${bus_info[0]}"
dbus_pid="${bus_info[1]}"
qemu_pid=""
cleanup() {
  if [[ -n "$qemu_pid" ]]; then
    kill "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  fi
  kill "$dbus_pid" 2>/dev/null || true
  wait "$dbus_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

{
  echo "QMDP_ALPINE_REFERENCE_E2E"
  echo "qemu=$($QEMU_BINARY --version | head -n1)"
  echo "accel=tcg"
  echo "guest_iso=$(basename "$ISO_PATH")"
  echo "guest_iso_sha256=$(sha256sum "$ISO_PATH" | awk '{print $1}')"
  echo "boot_wait_seconds=$BOOT_WAIT_SECONDS"
  echo "display=virtio-vga CPU Scanout/Update"
  echo "input=usb-tablet selected by guest Linux HID stack"
} > "$OUTPUT_DIR/trace.txt"

"$QEMU_BINARY" \
  -name qmdp-alpine-reference-e2e \
  -machine q35,accel=tcg \
  -smp 2 \
  -m 768 \
  -boot order=d \
  -cdrom "$ISO_PATH" \
  -drive "file=$OVERLAY_PATH,if=virtio,format=qcow2" \
  -vga none \
  -device virtio-vga \
  -device qemu-xhci,id=xhci \
  -device usb-tablet,bus=xhci.0 \
  -display dbus,gl=off \
  -monitor none \
  -serial none \
  -nic none \
  -no-reboot \
  -no-shutdown > "$OUTPUT_DIR/qemu.log" 2>&1 &
qemu_pid=$!

for _ in $(seq 1 200); do
  if busctl --address="$DBUS_SESSION_BUS_ADDRESS" --no-pager list 2>/dev/null | \
      awk '{print $1}' | grep -Fxq org.qemu; then
    break
  fi
  if ! kill -0 "$qemu_pid" 2>/dev/null; then
    cat "$OUTPUT_DIR/qemu.log" >&2 || true
    exit 1
  fi
  sleep 0.05
done
busctl --address="$DBUS_SESSION_BUS_ADDRESS" --no-pager list | \
  awk '{print $1}' | grep -Fxq org.qemu

sleep "$BOOT_WAIT_SECONDS"
"$PROBE" \
  --dbus-address "$DBUS_SESSION_BUS_ADDRESS" \
  --destination org.qemu \
  --duration-ms 2500 \
  --no-audio \
  --input-smoke \
  --encode-dir "$OUTPUT_DIR/encoded" > "$OUTPUT_DIR/probe.log" 2>&1

grep -q '^QEMU_DISPLAY_PROBE_RESULT$' "$OUTPUT_DIR/probe.log"
grep -Eq 'frames published/encoded/dropped: [1-9][0-9]*/[1-9][0-9]*/' \
  "$OUTPUT_DIR/probe.log"
grep -q 'input pointer mode: absolute' "$OUTPUT_DIR/probe.log"
grep -q 'session errors: 0' "$OUTPUT_DIR/probe.log"

shopt -s nullglob
segments=("$OUTPUT_DIR"/encoded/*.mkv)
if (( ${#segments[@]} == 0 )); then
  echo "Alpine reference probe produced no H.264 segment" >&2
  exit 1
fi
for segment in "${segments[@]}"; do
  "$QEMU_BINARY" --version >/dev/null
  ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
    -of default=noprint_wrappers=1 "$segment" | grep -Fx 'codec_name=h264'
done

cat "$OUTPUT_DIR/probe.log" >> "$OUTPUT_DIR/trace.txt"
printf 'ALPINE_REFERENCE_E2E_OK output=%s\n' "$OUTPUT_DIR"
