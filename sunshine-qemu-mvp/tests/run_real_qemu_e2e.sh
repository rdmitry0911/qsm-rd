#!/usr/bin/env bash
# Exercise the CPU Display1 path against a real QEMU process under TCG.
#
# The test deliberately builds its own tiny BIOS boot disk.  It therefore has
# no KVM, GPU, guest-OS, ISO, or network prerequisite.
set -euo pipefail

if [[ $# -ne 8 ]]; then
  echo "usage: $0 PROBE OUTPUT_DIR BOOT_SECTOR_SOURCE POINTER_MODE QEMU AS LD FFPROBE" >&2
  exit 2
fi

probe=$1
output_dir=$2
boot_source=$3
pointer_mode=$4
qemu_binary=$5
assembler=$6
linker=$7
ffprobe_binary=$8

case "$pointer_mode" in
  relative|absolute) ;;
  *)
    echo "pointer mode must be relative or absolute: $pointer_mode" >&2
    exit 2
    ;;
esac

for required in "$probe" "$boot_source" "$qemu_binary" "$assembler" "$linker" \
                "$ffprobe_binary"; do
  if [[ ! -e "$required" ]]; then
    echo "required path is unavailable: $required" >&2
    exit 1
  fi
done
command -v dbus-run-session >/dev/null
command -v busctl >/dev/null
command -v truncate >/dev/null
command -v dd >/dev/null

if ! "$qemu_binary" -display help | grep -Fxq dbus; then
  echo "QEMU lacks the required D-Bus display backend: $qemu_binary" >&2
  exit 1
fi
if ! "$qemu_binary" -audiodev help | grep -Fxq dbus; then
  echo "QEMU lacks the expected D-Bus audio module: $qemu_binary" >&2
  exit 1
fi

rm -rf "$output_dir"
mkdir -p "$output_dir"

boot_object="$output_dir/qmdp_vga_smoke.o"
boot_sector="$output_dir/qmdp_vga_smoke.bin"
floppy_image="$output_dir/qmdp-vga-smoke.img"

"$assembler" --32 -o "$boot_object" "$boot_source"
"$linker" -m elf_i386 -Ttext 0x7c00 --oformat binary -e _start \
  -o "$boot_sector" "$boot_object"

boot_bytes=$(wc -c < "$boot_sector")
boot_signature=$(od -An -tx1 -j 510 -N 2 "$boot_sector" | tr -d '[:space:]')
if [[ "$boot_bytes" != 512 || "$boot_signature" != 55aa ]]; then
  echo "invalid BIOS boot sector: bytes=$boot_bytes signature=$boot_signature" >&2
  exit 1
fi

truncate -s 1474560 "$floppy_image"
dd if="$boot_sector" of="$floppy_image" conv=notrunc status=none
floppy_bytes=$(wc -c < "$floppy_image")
if [[ "$floppy_bytes" != 1474560 ]]; then
  echo "invalid floppy image size: $floppy_bytes" >&2
  exit 1
fi

export QMDP_REAL_QEMU_OUTPUT_DIR="$output_dir"
dbus-run-session -- bash -euo pipefail -c '
  qemu_binary=$1
  probe=$2
  output_dir=$3
  floppy_image=$4
  pointer_mode=$5
  qemu_log="$output_dir/qemu.log"
  probe_log="$output_dir/probe.log"

  machine_args=(-machine pc)
  input_args=()
  if [[ "$pointer_mode" == absolute ]]; then
    # The default PS/2 mouse stays the active relative handler even when a USB
    # tablet is attached.  Disabling i8042 makes the tablet the one input
    # route, so Display1 reports IsAbsolute=true without a guest OS driver.
    machine_args=(-machine pc,i8042=off,usb=on)
    input_args=(-usbdevice tablet)
  fi

  "$qemu_binary" \
    -accel tcg \
    "${machine_args[@]}" \
    -m 128 \
    -vga std \
    -display dbus,gl=off \
    -monitor none \
    -serial none \
    -no-reboot \
    -no-shutdown \
    -drive "file=$floppy_image,if=floppy,format=raw,readonly=on" \
    "${input_args[@]}" \
    >"$qemu_log" 2>&1 &
  qemu_pid=$!

  cleanup() {
    kill "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  for _ in $(seq 1 100); do
    if busctl --user --no-pager list 2>/dev/null | \
        awk "{print \$1}" | grep -Fxq org.qemu; then
      break
    fi
    if ! kill -0 "$qemu_pid" 2>/dev/null; then
      cat "$qemu_log" >&2 || true
      exit 1
    fi
    sleep 0.05
  done
  busctl --user --no-pager list | awk "{print \$1}" | grep -Fxq org.qemu

  # Let SeaBIOS hand control to the fixture before registering the listener.
  # Standard VGA does not implement Console.SetUIInfo, so this intentionally
  # proves capture/input without treating an optional UI resize as mandatory.
  sleep 0.35
  "$probe" \
    --dbus-address "$DBUS_SESSION_BUS_ADDRESS" \
    --destination org.qemu \
    --duration-ms 1200 \
    --no-audio \
    --input-smoke \
    --encode-dir "$output_dir/encoded" \
    >"$probe_log" 2>&1
' bash "$qemu_binary" "$probe" "$output_dir" "$floppy_image" "$pointer_mode"

grep -q '^QEMU_DISPLAY_PROBE_RESULT$' "$output_dir/probe.log"
grep -Eq 'frames published/encoded/dropped: [1-9][0-9]*/[1-9][0-9]*/' \
  "$output_dir/probe.log"
grep -Eq 'scanout inline/map: [1-9][0-9]*/[0-9]+' "$output_dir/probe.log"
grep -q "input pointer mode: $pointer_mode" "$output_dir/probe.log"
grep -q 'session errors: 0' "$output_dir/probe.log"

shopt -s nullglob
segments=("$output_dir"/encoded/*.mkv)
if (( ${#segments[@]} == 0 )); then
  echo "real-QEMU probe produced no H.264 segment" >&2
  exit 1
fi

: > "$output_dir/ffprobe.txt"
for segment in "${segments[@]}"; do
  if [[ ! -s "$segment" ]]; then
    echo "empty H.264 segment: $segment" >&2
    exit 1
  fi
  {
    echo "=== $(basename "$segment") ==="
    "$ffprobe_binary" -v error -select_streams v:0 \
      -show_entries stream=codec_name,width,height,pix_fmt,avg_frame_rate \
      -show_entries format=duration,size \
      -of default=noprint_wrappers=1 "$segment"
  } >> "$output_dir/ffprobe.txt"
done
grep -q '^codec_name=h264$' "$output_dir/ffprobe.txt"

{
  echo "QMDP_REAL_QEMU_E2E"
  echo "qemu=$($qemu_binary --version | head -n1)"
  echo "accel=tcg"
  echo "boot_sector_bytes=$boot_bytes"
  echo "boot_signature=$boot_signature"
  echo "floppy_image_bytes=$floppy_bytes"
  echo "transport=private-session-dbus + RegisterListener peer socket"
  echo "capture=real-qemu-inline-cpu-framebuffer"
  echo "input_pointer_mode=$pointer_mode"
  if [[ "$pointer_mode" == absolute ]]; then
    echo "input_device=usb-tablet; ps2=i8042-off"
  else
    echo "input_device=default-ps2"
  fi
  echo "audio=not-generated-by-bios-fixture"
  echo "resize=not-supported-by-standard-vga-fixture"
  echo
  cat "$output_dir/probe.log"
  echo
  cat "$output_dir/ffprobe.txt"
} > "$output_dir/trace.txt"

printf 'REAL_QEMU_E2E_OK output=%s\n' "$output_dir"
