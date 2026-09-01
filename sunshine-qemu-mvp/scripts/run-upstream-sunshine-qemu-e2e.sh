#!/usr/bin/env bash
# Qualify the exported Sunshine CPU-display patch against a real QEMU Display1
# server.  This is intentionally a Sunshine video-probe gate, not a Moonlight
# session or an audio/input acceptance test.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SUNSHINE_BINARY="${SUNSHINE_BINARY:-$ROOT/.upstream/build-sunshine-qemu/sunshine}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/artifacts/validation/upstream-sunshine-qemu-e2e}"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
ASSEMBLER="${ASSEMBLER:-as}"
LINKER="${LINKER:-ld}"
TIMEOUT_SECONDS="${SUNSHINE_TIMEOUT_SECONDS:-15}"
QEMU_VIDEO="${QEMU_VIDEO:-std}"
BOOT_SOURCE="$ROOT/tests/fixtures/qmdp_vga_smoke.S"

if [[ "${1:-}" != "--inside-private-bus" ]]; then
  for required in "$SUNSHINE_BINARY" "$QEMU_BINARY" "$ASSEMBLER" "$LINKER" \
                  "$BOOT_SOURCE" dbus-run-session busctl timeout truncate dd; do
    if [[ -e "$required" ]]; then
      continue
    fi
    command -v "$required" >/dev/null || {
      echo "missing required command or path: $required" >&2
      exit 1
    }
  done

  if [[ ! -f "$(dirname -- "$SUNSHINE_BINARY")/assets/apps.json" ]]; then
    echo "Sunshine assets are missing beside $SUNSHINE_BINARY" >&2
    echo "configure its build with CMAKE_INSTALL_PREFIX set to the build directory" >&2
    exit 1
  fi
  if ! "$QEMU_BINARY" -display help | grep -Fxq dbus; then
    echo "QEMU lacks the D-Bus display backend: $QEMU_BINARY" >&2
    exit 1
  fi
  if [[ ! "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "SUNSHINE_TIMEOUT_SECONDS must be a positive integer" >&2
    exit 2
  fi
  if [[ "$QEMU_VIDEO" != "std" && "$QEMU_VIDEO" != "virtio-vga" ]]; then
    echo "QEMU_VIDEO must be std or virtio-vga" >&2
    exit 2
  fi

  mkdir -p "$OUTPUT_DIR"
  chmod 700 "$OUTPUT_DIR"
fi

if [[ "${1:-}" != "--inside-private-bus" ]]; then
  boot_object="$OUTPUT_DIR/qmdp_vga_smoke.o"
  boot_sector="$OUTPUT_DIR/qmdp_vga_smoke.bin"
  floppy_image="$OUTPUT_DIR/qmdp-vga-smoke.img"

  "$ASSEMBLER" --32 -o "$boot_object" "$BOOT_SOURCE"
  "$LINKER" -m elf_i386 -Ttext 0x7c00 --oformat binary -e _start \
    -o "$boot_sector" "$boot_object"
  boot_bytes=$(wc -c < "$boot_sector")
  boot_signature=$(od -An -tx1 -j 510 -N 2 "$boot_sector" | tr -d '[:space:]')
  if [[ "$boot_bytes" != "512" || "$boot_signature" != "55aa" ]]; then
    echo "invalid BIOS boot sector: bytes=$boot_bytes signature=$boot_signature" >&2
    exit 1
  fi
  truncate -s 1474560 "$floppy_image"
  dd if="$boot_sector" of="$floppy_image" conv=notrunc status=none

  dbus-run-session -- "$SCRIPT_PATH" --inside-private-bus \
    "$SUNSHINE_BINARY" "$OUTPUT_DIR" "$floppy_image" "$TIMEOUT_SECONDS" "$QEMU_VIDEO"

  grep -Fq 'Screencasting with QEMU Display1 D-Bus' "$OUTPUT_DIR/sunshine.log"
  grep -Fq 'Creating encoder [libx264]' "$OUTPUT_DIR/sunshine.log"
  grep -Fq 'Found H.264 encoder: libx264 [software]' "$OUTPUT_DIR/sunshine.log"
  if [[ "$QEMU_VIDEO" == "virtio-vga" ]]; then
    grep -Fq 'Console.SetUIInfo accepted ' "$OUTPUT_DIR/sunshine.log"
  else
    grep -Fq 'Console.SetUIInfo is unavailable for this VM' "$OUTPUT_DIR/sunshine.log"
  fi
  grep -Fq 'sunshine_timeout_status=124' "$OUTPUT_DIR/trace.txt"
  printf 'UPSTREAM_SUNSHINE_QEMU_E2E_OK output=%s\n' "$OUTPUT_DIR"
  exit 0
fi

if [[ $# -ne 6 ]]; then
  echo "internal invocation has invalid arguments" >&2
  exit 2
fi

sunshine_binary=$2
output_dir=$3
floppy_image=$4
timeout_seconds=$5
qemu_video=$6
qemu_log="$output_dir/qemu.log"
sunshine_log="$output_dir/sunshine.log"
xdg_config_dir="$output_dir/xdg-config"
mkdir -p "$xdg_config_dir"

case "$qemu_video" in
  std)
    qemu_video_args=(-vga std)
    ;;
  virtio-vga)
    qemu_video_args=(-device virtio-vga)
    ;;
  *)
    echo "invalid QEMU video selector: $qemu_video" >&2
    exit 2
    ;;
esac

"$QEMU_BINARY" \
  -name qmdp-upstream-sunshine-e2e \
  -accel tcg \
  -machine pc \
  -m 128 \
  "${qemu_video_args[@]}" \
  -display dbus,gl=off \
  -monitor none \
  -serial none \
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
  if busctl --user --no-pager list 2>/dev/null | awk '{print $1}' | grep -Fxq org.qemu; then
    break
  fi
  if ! kill -0 "$qemu_pid" 2>/dev/null; then
    cat "$qemu_log" >&2 || true
    exit 1
  fi
  sleep 0.05
done
busctl --user --no-pager list | awk '{print $1}' | grep -Fxq org.qemu

# Let SeaBIOS give the fixture control before Sunshine registers its listener.
sleep 0.35
set +e
XDG_CONFIG_HOME="$xdg_config_dir" \
SUNSHINE_QEMU_DBUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
  timeout --signal=TERM --kill-after=5s "${timeout_seconds}s" \
  "$sunshine_binary" -1 capture=qemu_dbus encoder=software >"$sunshine_log" 2>&1
sunshine_status=$?
set -e
if [[ "$sunshine_status" != "124" ]]; then
  cat "$sunshine_log" >&2 || true
  echo "Sunshine must remain alive until the bounded test shutdown; status=$sunshine_status" >&2
  exit 1
fi

{
  echo "QMDP_UPSTREAM_SUNSHINE_QEMU_E2E"
  echo "qemu=$($QEMU_BINARY --version | head -n1)"
  echo "sunshine_binary=$sunshine_binary"
  echo "accel=tcg"
  echo "display=$qemu_video + dbus,gl=off"
  echo "transport=private-session-dbus + RegisterListener peer socket"
  echo "sunshine_config=capture=qemu_dbus encoder=software"
  echo "scope=Sunshine Display1 CPU capture plus libx264 encoder probing"
  echo "sunshine_timeout_status=$sunshine_status"
  echo "audio_input_moonlight=not-qualified-by-this-gate"
  echo
  cat "$sunshine_log"
} > "$output_dir/trace.txt"
