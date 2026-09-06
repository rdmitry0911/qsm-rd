#!/usr/bin/env bash
# CPU-only QEMU D-Bus display example for development and CI hosts without a GPU.
# This script was syntax-checked in the build container, but QEMU itself was not
# installed there. Verify device names against the QEMU version on the target.
set -euo pipefail

VM_NAME="${VM_NAME:-qmdp-cpu-demo}"
DISK_IMAGE="${DISK_IMAGE:?Set DISK_IMAGE to a bootable qcow2/raw image}"
DISK_FORMAT="${DISK_FORMAT:-qcow2}"
MEMORY_MB="${MEMORY_MB:-4096}"
CPUS="${CPUS:-4}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/qmdp-${VM_NAME}}"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"

mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"

mapfile -t bus_info < <(
  dbus-daemon --session --fork --print-address=1 --print-pid=1
)
export DBUS_SESSION_BUS_ADDRESS="${bus_info[0]}"
dbus_pid="${bus_info[1]}"
printf '%s\n' "$DBUS_SESSION_BUS_ADDRESS" > "$RUNTIME_DIR/dbus.address"
chmod 600 "$RUNTIME_DIR/dbus.address"

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

printf 'D-Bus address file: %s\n' "$RUNTIME_DIR/dbus.address"
printf 'Attach with: scripts/run-qemu-display-probe.sh %q\n' \
       "$RUNTIME_DIR/dbus.address"

"$QEMU_BINARY" \
  -name "$VM_NAME" \
  -machine q35,accel="${ACCEL:-tcg}" \
  -smp "$CPUS" \
  -m "$MEMORY_MB" \
  -drive "file=$DISK_IMAGE,if=virtio,format=$DISK_FORMAT" \
  -device virtio-vga \
  -display dbus,audiodev=dbus0 \
  -audiodev dbus,id=dbus0,out.frequency=48000,out.channels=2 \
  -device ich9-intel-hda \
  -device hda-duplex,audiodev=dbus0 \
  -device qemu-xhci,id=xhci \
  -device usb-tablet,bus=xhci.0 \
  -device usb-kbd,bus=xhci.0 &
qemu_pid=$!
wait "$qemu_pid"
qemu_pid=""
