#!/usr/bin/env bash
# Exercise the real GameStream path with a local, disposable Moonlight client:
# Moonlight Embedded (SDL + FFmpeg) -> Sunshine -> QEMU Display1.
#
# This intentionally creates a private session bus, XDG state and Xvfb screen.
# It never reuses a user's Sunshine or Moonlight pairing material.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SUNSHINE_BINARY="${SUNSHINE_BINARY:-$ROOT/.upstream/build-sunshine-qemu/sunshine}"
MOONLIGHT_BINARY="${MOONLIGHT_BINARY:-$ROOT/.upstream/build-moonlight-embedded/moonlight}"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
OUTPUT_PARENT="${OUTPUT_DIR:-$ROOT/artifacts/validation/moonlight-sunshine-qemu-e2e}"
ASSEMBLER="${ASSEMBLER:-as}"
LINKER="${LINKER:-ld}"
STREAM_SECONDS="${STREAM_SECONDS:-10}"
SUNSHINE_PORT="${SUNSHINE_PORT:-48189}"
PIN="${MOONLIGHT_PIN:-4242}"
DISPLAY_NUMBER="${MOONLIGHT_DISPLAY:-:93}"
WINDOW_MODE="${MOONLIGHT_WINDOW_MODE:-windowed}"
BOOT_SOURCE="$ROOT/tests/fixtures/qmdp_input_ack.S"
MESA_EGL_VENDOR="${MESA_EGL_VENDOR:-/usr/share/glvnd/egl_vendor.d/50_mesa.json}"

die() {
  echo "moonlight Sunshine QEMU e2e: $*" >&2
  exit 1
}

if [[ "${1:-}" != "--inside-private-bus" ]]; then
  for required in "$SUNSHINE_BINARY" "$MOONLIGHT_BINARY" "$QEMU_BINARY" "$ASSEMBLER" \
                  "$LINKER" "$BOOT_SOURCE" dbus-run-session busctl timeout truncate dd \
                  Xvfb xdpyinfo xdotool xwd curl ffmpeg ffprobe stdbuf; do
    if [[ -e "$required" ]]; then
      continue
    fi
    command -v "$required" >/dev/null || die "missing required command or path: $required"
  done
  [[ -f "$(dirname -- "$SUNSHINE_BINARY")/assets/apps.json" ]] ||
    die "Sunshine assets are missing beside $SUNSHINE_BINARY"
  [[ -f "$MESA_EGL_VENDOR" ]] || die "Mesa EGL vendor file not found: $MESA_EGL_VENDOR"
  "$QEMU_BINARY" -display help | grep -Fxq dbus ||
    die "QEMU lacks the D-Bus display backend: $QEMU_BINARY"
  [[ "$STREAM_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "STREAM_SECONDS must be a positive integer"
  [[ "$SUNSHINE_PORT" =~ ^[1-9][0-9]*$ ]] || die "SUNSHINE_PORT must be a positive integer"
  (( SUNSHINE_PORT >= 1029 && SUNSHINE_PORT <= 65500 )) ||
    die "SUNSHINE_PORT is outside Sunshine's safe base-port range"
  [[ "$PIN" =~ ^[0-9]{4}$ ]] || die "MOONLIGHT_PIN must have exactly four digits"
  [[ "$WINDOW_MODE" == "windowed" || "$WINDOW_MODE" == "fullscreen" ]] ||
    die "MOONLIGHT_WINDOW_MODE must be windowed or fullscreen"

  # A fresh child directory is essential: pairing state and an old guest ACK
  # must not turn a later invocation into a false positive.  Keep all run
  # evidence below an ignored parent so callers can retain several attempts.
  mkdir -p "$OUTPUT_PARENT"
  chmod 700 "$OUTPUT_PARENT"
  OUTPUT_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
  chmod 700 "$OUTPUT_DIR"
  printf 'moonlight Sunshine QEMU e2e evidence=%s\n' "$OUTPUT_DIR"

  boot_object="$OUTPUT_DIR/qmdp_input_ack.o"
  boot_sector="$OUTPUT_DIR/qmdp_input_ack.bin"
  floppy_image="$OUTPUT_DIR/qmdp-input-ack.img"
  "$ASSEMBLER" --32 -o "$boot_object" "$BOOT_SOURCE"
  "$LINKER" -m elf_i386 -Ttext 0x7c00 --oformat binary -e _start \
    -o "$boot_sector" "$boot_object"
  [[ "$(wc -c < "$boot_sector")" == "512" ]] || die "fixture must be one BIOS sector"
  [[ "$(od -An -tx1 -j 510 -N 2 "$boot_sector" | tr -d '[:space:]')" == "55aa" ]] ||
    die "fixture has no BIOS signature"
  truncate -s 1474560 "$floppy_image"
  dd if="$boot_sector" of="$floppy_image" conv=notrunc status=none

  dbus-run-session -- "$SCRIPT_PATH" --inside-private-bus \
    "$SUNSHINE_BINARY" "$MOONLIGHT_BINARY" "$OUTPUT_DIR" "$floppy_image" \
    "$STREAM_SECONDS" "$SUNSHINE_PORT" "$PIN" "$DISPLAY_NUMBER" "$MESA_EGL_VENDOR"

  grep -Fq 'Succesfully paired' "$OUTPUT_DIR/moonlight-pair.log"
  grep -Fq 'Desktop' "$OUTPUT_DIR/moonlight-list.log"
  grep -Fq 'Starting RTSP handshake...' "$OUTPUT_DIR/moonlight-stream.log"
  grep -Fq 'Starting video stream...' "$OUTPUT_DIR/moonlight-stream.log"
  grep -Fq 'Received first video packet after' "$OUTPUT_DIR/moonlight-stream.log"
  ! grep -Fq 'Audio stream start failed' "$OUTPUT_DIR/moonlight-stream.log"
  ! grep -Fq 'No video traffic was ever received from the host!' "$OUTPUT_DIR/moonlight-stream.log"
  [[ -s "$OUTPUT_DIR/moonlight-client.png" ]]
  [[ -s "$OUTPUT_DIR/moonlight-client-center-rgb.txt" ]]
  grep -Fq 'codec_name=png' "$OUTPUT_DIR/moonlight-client.ffprobe"
  grep -Fq 'width=1280' "$OUTPUT_DIR/moonlight-client.ffprobe"
  grep -Fq 'height=720' "$OUTPUT_DIR/moonlight-client.ffprobe"
  [[ -s "$OUTPUT_DIR/moonlight-root.png" ]]
  grep -Fq 'codec_name=png' "$OUTPUT_DIR/moonlight-root.ffprobe"
  if [[ "$WINDOW_MODE" == "windowed" ]]; then
    grep -Fq 'Width: 1280' "$OUTPUT_DIR/moonlight-window.xwininfo"
    grep -Fq 'Height: 720' "$OUTPUT_DIR/moonlight-window.xwininfo"
    grep -Fq 'Width: 1600' "$OUTPUT_DIR/moonlight-root.xwininfo"
    grep -Fq 'Height: 900' "$OUTPUT_DIR/moonlight-root.xwininfo"
  else
    # SDL's exclusive-fullscreen mode uses the requested GameStream output
    # mode.  Give the disposable X server that exact mode and require the
    # decoded Moonlight window to occupy its entire root drawable, rather than
    # merely accepting an undecorated 1280x720 window centered on a larger
    # virtual desktop.
    grep -Fq 'Width: 1280' "$OUTPUT_DIR/moonlight-window.xwininfo"
    grep -Fq 'Height: 720' "$OUTPUT_DIR/moonlight-window.xwininfo"
    grep -Eq 'Absolute upper-left X:[[:space:]]+0$' "$OUTPUT_DIR/moonlight-window.xwininfo"
    grep -Eq 'Absolute upper-left Y:[[:space:]]+0$' "$OUTPUT_DIR/moonlight-window.xwininfo"
    grep -Fq 'Width: 1280' "$OUTPUT_DIR/moonlight-root.xwininfo"
    grep -Fq 'Height: 720' "$OUTPUT_DIR/moonlight-root.xwininfo"
    grep -Fq 'width=1280' "$OUTPUT_DIR/moonlight-root.ffprobe"
    grep -Fq 'height=720' "$OUTPUT_DIR/moonlight-root.ffprobe"
  fi
  grep -Fq 'Screencasting with QEMU Display1 D-Bus' "$OUTPUT_DIR/sunshine.log"
  grep -Fq 'Found H.264 encoder: libx264 [software]' "$OUTPUT_DIR/sunshine.log"
  grep -Fq 'New streaming session started' "$OUTPUT_DIR/sunshine.log"
  grep -Fxq 'INPUT_PRESS_RELEASE_OK' "$OUTPUT_DIR/guest-debugcon.log"
  grep -Fq 'moonlight_timeout_status=124' "$OUTPUT_DIR/trace.txt"
  printf 'MOONLIGHT_SUNSHINE_QEMU_E2E_OK output=%s\n' "$OUTPUT_DIR"
  exit 0
fi

[[ $# -eq 10 ]] || die "internal invocation has invalid arguments"
sunshine_binary=$2
moonlight_binary=$3
output_dir=$4
floppy_image=$5
stream_seconds=$6
sunshine_port=$7
pin=$8
display_number=$9
mesa_egl_vendor=${10}

# Moonlight Embedded asks SDL for exclusive fullscreen at the negotiated
# GameStream mode. On a bare Xvfb server, SDL cannot switch an arbitrary
# 1600x900 root to 1280x720, so configure a matching disposable display for
# the fullscreen gate. The normal windowed gate deliberately keeps a larger
# root to distinguish it from fullscreen.
xvfb_screen='1600x900x24'
if [[ "$WINDOW_MODE" == "fullscreen" ]]; then
  xvfb_screen='1280x720x24'
fi

qemu_log="$output_dir/qemu.log"
guest_debugcon_log="$output_dir/guest-debugcon.log"
sunshine_log="$output_dir/sunshine.log"
pair_log="$output_dir/moonlight-pair.log"
list_log="$output_dir/moonlight-list.log"
stream_log="$output_dir/moonlight-stream.log"
xvfb_log="$output_dir/xvfb.log"
xdg_config_dir="$output_dir/xdg-config"
moonlight_key_dir="$output_dir/moonlight-keys"
mkdir -p "$xdg_config_dir" "$moonlight_key_dir"
chmod 700 "$xdg_config_dir" "$moonlight_key_dir"

"$QEMU_BINARY" \
  -name qmdp-moonlight-sunshine-e2e \
  -accel tcg \
  -machine pc \
  -m 128 \
  -vga std \
  -display dbus,gl=off \
  -monitor none \
  -serial none \
  -chardev "file,id=qmdp_debugcon,path=$guest_debugcon_log" \
  -device isa-debugcon,iobase=0xe9,chardev=qmdp_debugcon \
  -no-reboot \
  -no-shutdown \
  -drive "file=$floppy_image,if=floppy,format=raw,readonly=on" \
  >"$qemu_log" 2>&1 &
qemu_pid=$!

__EGL_VENDOR_LIBRARY_FILENAMES="$mesa_egl_vendor" LIBGL_ALWAYS_SOFTWARE=1 \
  Xvfb "$display_number" -screen 0 "$xvfb_screen" +extension GLX >"$xvfb_log" 2>&1 &
xvfb_pid=$!
sunshine_pid=''

cleanup() {
  if [[ -n "$sunshine_pid" ]]; then
    kill "$sunshine_pid" 2>/dev/null || true
    wait "$sunshine_pid" 2>/dev/null || true
  fi
  kill "$xvfb_pid" "$qemu_pid" 2>/dev/null || true
  wait "$xvfb_pid" 2>/dev/null || true
  wait "$qemu_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 100); do
  if busctl --user --no-pager list 2>/dev/null | awk '{print $1}' | grep -Fxq org.qemu; then
    break
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { cat "$qemu_log" >&2 || true; die "QEMU exited early"; }
  sleep 0.05
done
busctl --user --no-pager list | awk '{print $1}' | grep -Fxq org.qemu || die "QEMU did not expose org.qemu"

for _ in $(seq 1 100); do
  DISPLAY="$display_number" xdpyinfo >/dev/null 2>&1 && break
  kill -0 "$xvfb_pid" 2>/dev/null || { cat "$xvfb_log" >&2 || true; die "Xvfb exited early"; }
  sleep 0.05
done
DISPLAY="$display_number" xdpyinfo >/dev/null || die "Xvfb did not become ready"

# -0 makes Sunshine consume this deterministic PIN from its private stdin when
# Moonlight begins pairing.  The client keys and Sunshine state stay below the
# ignored output directory, so this script cannot alter normal user pairing.
printf '%s\n' "$pin" | \
  XDG_CONFIG_HOME="$xdg_config_dir" \
  SUNSHINE_QEMU_DBUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
  "$sunshine_binary" -0 \
    capture=qemu_dbus encoder=software stream_audio=false \
    system_tray=false bind_address=127.0.0.1 port="$sunshine_port" \
    >"$sunshine_log" 2>&1 &
sunshine_pid=$!

for _ in $(seq 1 200); do
  if curl --silent --show-error --fail --max-time 1 \
      "http://127.0.0.1:${sunshine_port}/serverinfo?uniqueid=0123456789ABCDEF" \
      >"$output_dir/serverinfo-before-pair.xml" 2>/dev/null; then
    break
  fi
  kill -0 "$sunshine_pid" 2>/dev/null || { cat "$sunshine_log" >&2 || true; die "Sunshine exited early"; }
  sleep 0.05
done
[[ -s "$output_dir/serverinfo-before-pair.xml" ]] || die "Sunshine HTTP server did not become ready"

"$moonlight_binary" -debug -pin "$pin" -keydir "$moonlight_key_dir" -port "$sunshine_port" \
  pair 127.0.0.1 >"$pair_log" 2>&1
"$moonlight_binary" -debug -keydir "$moonlight_key_dir" -port "$sunshine_port" \
  list 127.0.0.1 >"$list_log" 2>&1

set +e
moonlight_window_argument=()
if [[ "$WINDOW_MODE" == "windowed" ]]; then
  moonlight_window_argument=(-windowed)
fi
DISPLAY="$display_number" SDL_AUDIODRIVER=dummy \
  __EGL_VENDOR_LIBRARY_FILENAMES="$mesa_egl_vendor" LIBGL_ALWAYS_SOFTWARE=1 \
  stdbuf -oL -eL timeout --signal=TERM --kill-after=5s "${stream_seconds}s" \
  "$moonlight_binary" -debug -keydir "$moonlight_key_dir" -port "$sunshine_port" \
    -platform sdl "${moonlight_window_argument[@]}" -app Desktop -codec h264 -width 1280 -height 720 -fps 30 -bitrate 4000 \
    stream 127.0.0.1 >"$stream_log" 2>&1 &
moonlight_pid=$!
set -e

for _ in $(seq 1 200); do
  if grep -Fq 'Received first video packet after' "$stream_log"; then
    break
  fi
  kill -0 "$moonlight_pid" 2>/dev/null || { cat "$stream_log" >&2 || true; die "Moonlight exited before receiving video"; }
  sleep 0.05
done
grep -Fq 'Received first video packet after' "$stream_log" || die "Moonlight did not receive video"

for _ in $(seq 1 100); do
  moonlight_window=$(DISPLAY="$display_number" xdotool search --name 'Moonlight' 2>/dev/null | head -n1 || true)
  [[ -n "$moonlight_window" ]] && break
  sleep 0.05
done
if [[ -z "${moonlight_window:-}" ]]; then
  DISPLAY="$display_number" xwininfo -root -tree > "$output_dir/xvfb-window-tree.txt" 2>&1 || true
  die "Moonlight SDL window was not found on Xvfb"
fi
# Let the software decoder present at least one complete frame after the first
# RTP packet. xdotool delivers a normal SDL key down/up pair to the Moonlight
# window; Moonlight common-c carries it through the GameStream input stream,
# Sunshine uses its QEMU Display1 keyboard route, and the BIOS fixture writes
# this exact proof only after it sees both raw PS/2 set-1 bytes.
sleep 1
DISPLAY="$display_number" xdotool key --window "$moonlight_window" --clearmodifiers a
for _ in $(seq 1 100); do
  grep -Fxq 'INPUT_PRESS_RELEASE_OK' "$guest_debugcon_log" 2>/dev/null && break
  kill -0 "$moonlight_pid" 2>/dev/null || { cat "$stream_log" >&2 || true; die "Moonlight exited before guest input acknowledgement"; }
  sleep 0.05
done
grep -Fxq 'INPUT_PRESS_RELEASE_OK' "$guest_debugcon_log" || {
  cat "$guest_debugcon_log" >&2 || true
  die "guest did not acknowledge Moonlight key press/release"
}

# Retain an ordinary PNG after the guest changes its framebuffer to green, so
# one artifact captures the decoded result of the same end-to-end input path.
# Display1 delivers the screen update asynchronously from the input method
# reply and the BIOS acknowledgement, so give the SDL decoder one frame period
# before sampling its drawable.
sleep 1
DISPLAY="$display_number" xwininfo -id "$moonlight_window" > "$output_dir/moonlight-window.xwininfo"
DISPLAY="$display_number" xwd -silent -id "$moonlight_window" -out "$output_dir/moonlight-client.xwd"
ffmpeg -hide_banner -loglevel error -y -f xwd_pipe -i "$output_dir/moonlight-client.xwd" \
  "$output_dir/moonlight-client.png"
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
  "$output_dir/moonlight-client.png" > "$output_dir/moonlight-client.ffprobe"
# The fixture begins red and turns its entire VGA framebuffer green only after
# it has consumed the key make and break.  Sample a decoded center pixel so
# the retained client screenshot is asserted as a post-input video result, not
# merely an image that happened to be present on the X server.
ffmpeg -hide_banner -loglevel error -i "$output_dir/moonlight-client.png" \
  -vf 'crop=1:1:640:360,format=rgb24' -f rawvideo - | \
  od -An -tu1 -N3 > "$output_dir/moonlight-client-center-rgb.txt"
read -r decoded_red decoded_green decoded_blue < "$output_dir/moonlight-client-center-rgb.txt" ||
  die "could not sample decoded Moonlight screenshot"
[[ "$decoded_red" =~ ^[0-9]+$ && "$decoded_green" =~ ^[0-9]+$ && "$decoded_blue" =~ ^[0-9]+$ ]] ||
  die "decoded Moonlight screenshot has invalid RGB sample"
(( decoded_green > decoded_red + 40 && decoded_green > decoded_blue + 40 )) ||
  die "decoded Moonlight screenshot did not show guest's post-input green framebuffer"
DISPLAY="$display_number" xwininfo -root > "$output_dir/moonlight-root.xwininfo"
DISPLAY="$display_number" xwd -silent -root -out "$output_dir/moonlight-root.xwd"
ffmpeg -hide_banner -loglevel error -y -f xwd_pipe -i "$output_dir/moonlight-root.xwd" \
  "$output_dir/moonlight-root.png"
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
  "$output_dir/moonlight-root.png" > "$output_dir/moonlight-root.ffprobe"

set +e
wait "$moonlight_pid"
moonlight_status=$?
set -e
if [[ "$moonlight_status" != "124" ]]; then
  cat "$stream_log" >&2 || true
  die "Moonlight must stay active until bounded shutdown; status=$moonlight_status"
fi

{
  echo "QMDP_MOONLIGHT_SUNSHINE_QEMU_E2E"
  echo "qemu=$($QEMU_BINARY --version | head -n1)"
  echo "sunshine_binary=$sunshine_binary"
  echo "moonlight_binary=$moonlight_binary"
  echo "moonlight_version=$($moonlight_binary help | head -n1)"
  echo "accel=tcg"
  echo "display=standard-vga + dbus,gl=off"
  echo "client=Xvfb + Moonlight Embedded SDL/FFmpeg software H.264 decode"
  echo "client_window_mode=$WINDOW_MODE"
  echo "decoded_center_rgb=$decoded_red,$decoded_green,$decoded_blue"
  echo "transport=Moonlight GameStream pairing + HTTPS launch + RTSP + RTP"
  echo "sunshine_config=capture=qemu_dbus encoder=software stream_audio=false"
  echo "pairing=private deterministic PIN and disposable client keys"
  echo "moonlight_timeout_status=$moonlight_status"
  echo "input_audio_clipboard_file_transfer=not-qualified-by-this-gate"
  echo
  cat "$stream_log"
} > "$output_dir/trace.txt"
