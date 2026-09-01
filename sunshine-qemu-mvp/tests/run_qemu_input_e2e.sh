#!/usr/bin/env bash
# Prove that Display1 Keyboard Press/Release reaches raw guest hardware.
#
# The fixture observes both set-1 bytes itself and writes an acknowledgement to
# QEMU debugcon.  This is intentionally independent of a host GUI, KVM, guest
# OS, or network so it is usable as the first E2E gate for the Sunshine route.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
ASSEMBLER="${ASSEMBLER:-as}"
LINKER="${LINKER:-ld}"
OUTPUT_PARENT="${OUTPUT_DIR:-$ROOT/artifacts/validation/qemu-input-e2e}"
BOOT_SOURCE="$ROOT/tests/fixtures/qmdp_input_ack.S"

for required in "$QEMU_BINARY" "$ASSEMBLER" "$LINKER" "$BOOT_SOURCE" \
                dbus-run-session busctl gdbus truncate dd; do
  if [[ -e "$required" ]]; then
    continue
  fi
  command -v "$required" >/dev/null || {
    echo "missing required command or path: $required" >&2
    exit 1
  }
done

if ! "$QEMU_BINARY" -display help | grep -Fxq dbus; then
  echo "QEMU lacks the required D-Bus display backend: $QEMU_BINARY" >&2
  exit 1
fi

mkdir -p "$OUTPUT_PARENT"
chmod 700 "$OUTPUT_PARENT"
OUTPUT_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
BOOT_OBJECT="$OUTPUT_DIR/qmdp_input_ack.o"
BOOT_SECTOR="$OUTPUT_DIR/qmdp_input_ack.bin"
FLOPPY_IMAGE="$OUTPUT_DIR/qmdp-input-ack.img"

"$ASSEMBLER" --32 -o "$BOOT_OBJECT" "$BOOT_SOURCE"
"$LINKER" -m elf_i386 -Ttext 0x7c00 --oformat binary -e _start \
  -o "$BOOT_SECTOR" "$BOOT_OBJECT"

boot_bytes=$(wc -c < "$BOOT_SECTOR")
boot_signature=$(od -An -tx1 -j 510 -N 2 "$BOOT_SECTOR" | tr -d '[:space:]')
if [[ "$boot_bytes" != 512 || "$boot_signature" != 55aa ]]; then
  echo "invalid BIOS boot sector: bytes=$boot_bytes signature=$boot_signature" >&2
  exit 1
fi

truncate -s 1474560 "$FLOPPY_IMAGE"
dd if="$BOOT_SECTOR" of="$FLOPPY_IMAGE" conv=notrunc status=none

export QMDP_INPUT_E2E_OUTPUT_DIR="$OUTPUT_DIR"
dbus-run-session -- bash -euo pipefail -c '
  qemu_binary=$1
  output_dir=$2
  floppy_image=$3
  qemu_log="$output_dir/qemu.log"
  guest_debug="$output_dir/guest-debug.log"

  "$qemu_binary" \
    -name qmdp-input-e2e \
    -accel tcg \
    -machine pc \
    -m 128 \
    -vga std \
    -display dbus,gl=off \
    -monitor none \
    -serial none \
    -debugcon "file:$guest_debug" \
    -global isa-debugcon.iobase=0xe9 \
    -no-reboot \
    -no-shutdown \
    -drive "file=$floppy_image,if=floppy,format=raw,readonly=on" \
    >"$qemu_log" 2>&1 &
  qemu_pid=$!

  cleanup() {
    kill "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  for _ in $(seq 1 100); do
    if busctl --user --no-pager list 2>/dev/null | awk "{print \$1}" | grep -Fxq org.qemu; then
      break
    fi
    if ! kill -0 "$qemu_pid" 2>/dev/null; then
      cat "$qemu_log" >&2 || true
      exit 1
    fi
    sleep 0.05
  done
  busctl --user --no-pager list | awk "{print \$1}" | grep -Fxq org.qemu

  # qnum 0x1e is physical A.  Use the public Display1 methods that the
  # Sunshine adapter invokes after translating Moonlight VK_A.
  sleep 0.5
  gdbus call --session --dest org.qemu \
    --object-path /org/qemu/Display1/Console_0 \
    --method org.qemu.Display1.Keyboard.Press 30 >/dev/null
  gdbus call --session --dest org.qemu \
    --object-path /org/qemu/Display1/Console_0 \
    --method org.qemu.Display1.Keyboard.Release 30 >/dev/null

  for _ in $(seq 1 100); do
    if [[ -f "$guest_debug" ]] && grep -Fqx INPUT_PRESS_RELEASE_OK "$guest_debug"; then
      exit 0
    fi
    sleep 0.05
  done
  cat "$qemu_log" >&2 || true
  cat "$guest_debug" >&2 || true
  echo "guest did not observe QEMU Display1 A make/break" >&2
  exit 1
' bash "$QEMU_BINARY" "$OUTPUT_DIR" "$FLOPPY_IMAGE"

grep -Fqx INPUT_PRESS_RELEASE_OK "$OUTPUT_DIR/guest-debug.log"
{
  echo "QMDP_QEMU_INPUT_E2E"
  echo "qemu=$($QEMU_BINARY --version | head -n1)"
  echo "accel=tcg"
  echo "transport=private-session-dbus + org.qemu.Display1.Keyboard"
  echo "moonlight_virtual_key=VK_A (0x41)"
  echo "qemu_qnum=0x1e"
  echo "guest_ack=INPUT_PRESS_RELEASE_OK"
  echo
  cat "$OUTPUT_DIR/guest-debug.log"
} > "$OUTPUT_DIR/trace.txt"

printf 'QEMU_INPUT_E2E_OK output=%s\n' "$OUTPUT_DIR"
