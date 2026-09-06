#!/usr/bin/env bash
# Launch the pinned Alpine guest on a private Unix D-Bus for interactive probe work.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ALPINE_VERSION="${ALPINE_VERSION:-3.24.1}"
VM_DIR="${VM_DIR:-$ROOT/vm/alpine-virt-$ALPINE_VERSION}"
ISO_PATH="$VM_DIR/alpine-virt-$ALPINE_VERSION-x86_64.iso"
OVERLAY_PATH="$VM_DIR/data.qcow2"
RUNTIME_DIR="${RUNTIME_DIR:-$VM_DIR/runtime}"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
ACCEL="${ACCEL:-tcg}"
MEMORY_MB="${MEMORY_MB:-768}"
CPUS="${CPUS:-2}"

for required in "$QEMU_BINARY" dbus-daemon; do
  command -v "$required" >/dev/null || {
    echo "missing required command: $required" >&2
    exit 1
  }
done
if [[ ! -f "$ISO_PATH" || ! -f "$OVERLAY_PATH" ]]; then
  echo "reference VM is absent; run scripts/provision-alpine-reference-vm.sh first" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"
mapfile -t bus_info < <(dbus-daemon --session --fork --print-address=1 --print-pid=1)
export DBUS_SESSION_BUS_ADDRESS="${bus_info[0]}"
dbus_pid="${bus_info[1]}"
address_path="$RUNTIME_DIR/dbus.address"
umask 077
printf '%s\n' "$DBUS_SESSION_BUS_ADDRESS" > "$address_path"

qemu_pid=""
cleanup() {
  if [[ -n "$qemu_pid" ]]; then
    kill "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  fi
  kill "$dbus_pid" 2>/dev/null || true
  wait "$dbus_pid" 2>/dev/null || true
  rm -f "$address_path"
}
trap cleanup EXIT INT TERM

printf 'D-Bus address file: %s\n' "$address_path"
printf 'Attach with: %s --dbus-address "$(cat "$address_path")" --destination org.qemu --no-audio --input-smoke\n' \
  "$ROOT/build/qemu-display-probe"

"$QEMU_BINARY" \
  -name qmdp-alpine-reference \
  -machine "q35,accel=$ACCEL" \
  -smp "$CPUS" \
  -m "$MEMORY_MB" \
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
  -no-shutdown &
qemu_pid=$!
wait "$qemu_pid"
qemu_pid=""
