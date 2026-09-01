#!/usr/bin/env bash
# Illustrative bring-up script for one private D-Bus + one QEMU VM.
# It is not a production service unit and does not include the unfinished
# Sunshine qemu_dbus backend.
set -euo pipefail

VM_NAME="${VM_NAME:-qmdp-demo}"
DISK_IMAGE="${DISK_IMAGE:?Set DISK_IMAGE to a bootable qcow2/raw image}"
MEMORY_MB="${MEMORY_MB:-8192}"
CPUS="${CPUS:-8}"
DISK_FORMAT="${DISK_FORMAT:-qcow2}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/qmdp-${VM_NAME}}"
mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"

mapfile -t BUS_INFO < <(dbus-daemon --session --fork --print-address=1 --print-pid=1)
export DBUS_SESSION_BUS_ADDRESS="${BUS_INFO[0]}"
DBUS_PID="${BUS_INFO[1]}"
printf '%s\n' "$DBUS_SESSION_BUS_ADDRESS" > "$RUNTIME_DIR/dbus.address"
chmod 600 "$RUNTIME_DIR/dbus.address"

cleanup() {
  kill "$DBUS_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

qemu-system-x86_64 \
  -name "$VM_NAME" \
  -machine q35,accel=kvm \
  -cpu host \
  -smp "$CPUS" \
  -m "$MEMORY_MB" \
  -drive "file=$DISK_IMAGE,if=virtio,format=$DISK_FORMAT" \
  -device virtio-vga-gl \
  -display dbus,gl=on,audiodev=dbus0 \
  -audiodev dbus,id=dbus0,nsamples=480,out.frequency=48000,out.channels=2 \
  -device ich9-intel-hda \
  -device hda-duplex,audiodev=dbus0 \
  -device qemu-xhci,id=xhci \
  -device usb-tablet,bus=xhci.0 \
  -device usb-kbd,bus=xhci.0
