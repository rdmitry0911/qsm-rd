#!/usr/bin/env bash
# Exercise the native headless graphics route end-to-end:
#
#   Moonlight Embedded (disposable client-side Xvfb)
#     -> Sunshine GameStream / software H.264
#     -> QEMU Display1 ScanoutDMABUF
#     -> native KVM Alpine guest, virtio-vga-gl, VirGL/NVIDIA
#
# The only X11 server in this file is the isolated Moonlight SDL test client.
# QEMU, Sunshine, the QEMU Display1 observer, and the guest use no host X11,
# Wayland, GTK, or compositor.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"

SUNSHINE_BINARY="${SUNSHINE_BINARY:-$ROOT/.upstream/build-sunshine-qemu/sunshine}"
MOONLIGHT_BINARY="${MOONLIGHT_BINARY:-$ROOT/.upstream/build-moonlight-embedded/moonlight}"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
QEMU_IMG_BINARY="${QEMU_IMG_BINARY:-qemu-img}"
# The native observer must be the GBM/EGL-enabled project build.  It is a
# second, read-only Display1 listener; QEMU explicitly supports many listeners
# on one console, so it cannot replace or fake Sunshine's listener.
DISPLAY_PROBE="${QEMU_DISPLAY_PROBE:-$ROOT/.build-dmabuf/qemu-display-probe}"
PROVISIONER="$ROOT/scripts/provision-alpine-virgl-guest.sh"
# This is a narrowly extended version of the ordinary VirGL fixture.  It adds
# a root guest evdev check for the real Moonlight `A` key press/release while
# preserving the same pinned Alpine image, DRM, renderer, and kmscube checks.
USER_DATA="${VIRGL_MOONLIGHT_USER_DATA:-$ROOT/tests/fixtures/virgl-moonlight-cloud-init-user-data.yaml}"
META_DATA="$ROOT/tests/fixtures/virgl-cloud-init-meta-data.yaml"

ALPINE_VERSION="${VIRGL_ALPINE_VERSION:-3.20.10}"
VM_DIR="${VIRGL_VM_DIR:-$ROOT/vm/alpine-virgl-$ALPINE_VERSION}"
BASE_IMAGE="${VIRGL_BASE_IMAGE:-$VM_DIR/generic_alpine-${ALPINE_VERSION}-x86_64-bios-cloudinit-r0.qcow2}"
OUTPUT_PARENT="${OUTPUT_DIR:-$ROOT/artifacts/validation/moonlight-sunshine-virgl-e2e}"

ACCEL="${VIRGL_ACCEL:-kvm}"
QEMU_RUN_AS="${VIRGL_QEMU_RUN_AS:-$(id -un)}"
QEMU_USE_SUDO="${VIRGL_QEMU_USE_SUDO:-1}"
RENDER_NODE="${VIRGL_RENDER_NODE:-/dev/dri/renderD128}"
BOOT_TIMEOUT_SECONDS="${VIRGL_BOOT_TIMEOUT_SECONDS:-150}"
OBSERVER_DURATION_MS="${VIRGL_OBSERVER_DURATION_MS:-15000}"
STREAM_SECONDS="${STREAM_SECONDS:-10}"
SUNSHINE_PORT="${SUNSHINE_PORT:-48189}"
PIN="${MOONLIGHT_PIN:-4242}"
DISPLAY_NUMBER="${MOONLIGHT_DISPLAY:-:94}"
MESA_EGL_VENDOR="${MESA_EGL_VENDOR:-/usr/share/glvnd/egl_vendor.d/50_mesa.json}"
EXPECTED_RENDERER="${VIRGL_EXPECTED_RENDERER:-NVIDIA}"
WINDOW_MODE="${MOONLIGHT_WINDOW_MODE:-fullscreen}"

die() {
  printf 'Moonlight Sunshine VirGL E2E: %s\n' "$*" >&2
  exit 1
}

require() {
  local item=$1
  if [[ -e "$item" ]]; then
    return 0
  fi
  command -v "$item" >/dev/null || die "missing required command or path: $item"
}

show_logs() {
  [[ -n "${OUTPUT_DIR:-}" ]] || return 0
  printf '%s\n' '--- QEMU log ---' >&2
  tail -160 "$OUTPUT_DIR/qemu.log" >&2 2>/dev/null || true
  printf '%s\n' '--- guest telemetry ---' >&2
  cat "$OUTPUT_DIR/guest-telemetry.log" >&2 2>/dev/null || true
  printf '%s\n' '--- Sunshine log ---' >&2
  tail -180 "$OUTPUT_DIR/sunshine.log" >&2 2>/dev/null || true
  printf '%s\n' '--- Moonlight stream log ---' >&2
  tail -160 "$OUTPUT_DIR/moonlight-stream.log" >&2 2>/dev/null || true
  printf '%s\n' '--- Display1 observer log ---' >&2
  cat "$OUTPUT_DIR/display1-observer.log" >&2 2>/dev/null || true
}

run_as_qemu_user() {
  if [[ "$QEMU_USE_SUDO" == '1' ]]; then
    sudo -n -u "$QEMU_RUN_AS" "$@"
  else
    "$@"
  fi
}

stop_pid() {
  local pid=${1:-}
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

wait_for_qemu_bus() {
  for _ in $(seq 1 300); do
    if busctl --address="$bus_address" --no-pager list 2>/dev/null | \
        awk '{print $1}' | grep -Fxq org.qemu; then
      return 0
    fi
    kill -0 "$qemu_pid" 2>/dev/null || { show_logs; die 'QEMU exited before exposing Display1'; }
    sleep 0.05
  done
  show_logs
  die 'QEMU did not expose the org.qemu Display1 destination'
}

wait_for_guest_marker() {
  local marker=$1
  local label=$2
  local iterations=$((BOOT_TIMEOUT_SECONDS * 10))
  for _ in $(seq 1 "$iterations"); do
    grep -Fqx "$marker" "$telemetry_log" 2>/dev/null && return 0
    if grep -F 'QMDP_VIRGL_GUEST_E2E_FAILED:' "$telemetry_log" 2>/dev/null; then
      show_logs
      die "guest reported a failure while waiting for $label"
    fi
    kill -0 "$qemu_pid" 2>/dev/null || { show_logs; die "QEMU exited while waiting for $label"; }
    sleep 0.1
  done
  show_logs
  die "guest did not report $label within ${BOOT_TIMEOUT_SECONDS}s"
}

wait_for_xvfb() {
  for _ in $(seq 1 160); do
    DISPLAY="$DISPLAY_NUMBER" xdpyinfo >/dev/null 2>&1 && return 0
    kill -0 "$xvfb_pid" 2>/dev/null || { cat "$xvfb_log" >&2 || true; die 'Xvfb exited early'; }
    sleep 0.05
  done
  die 'client-side Xvfb did not become ready'
}

wait_for_sunshine_http() {
  for _ in $(seq 1 300); do
    if curl --silent --show-error --fail --max-time 1 \
        "http://127.0.0.1:${SUNSHINE_PORT}/serverinfo?uniqueid=0123456789ABCDEF" \
        >"$OUTPUT_DIR/serverinfo-before-pair.xml" 2>/dev/null; then
      return 0
    fi
    kill -0 "$sunshine_pid" 2>/dev/null || { cat "$sunshine_log" >&2 || true; die 'Sunshine exited early'; }
    sleep 0.05
  done
  die 'Sunshine HTTP server did not become ready'
}

wait_for_moonlight_video() {
  for _ in $(seq 1 300); do
    grep -Fq 'Received first video packet after' "$stream_log" 2>/dev/null && return 0
    kill -0 "$moonlight_pid" 2>/dev/null || { cat "$stream_log" >&2 || true; die 'Moonlight exited before receiving video'; }
    sleep 0.05
  done
  die 'Moonlight did not receive an RTP video packet'
}

wait_for_moonlight_window() {
  for _ in $(seq 1 200); do
    moonlight_window="$(DISPLAY="$DISPLAY_NUMBER" xdotool search --name 'Moonlight' 2>/dev/null | head -n1 || true)"
    [[ -n "$moonlight_window" ]] && return 0
    kill -0 "$moonlight_pid" 2>/dev/null || { cat "$stream_log" >&2 || true; die 'Moonlight exited before creating its SDL window'; }
    sleep 0.05
  done
  DISPLAY="$DISPLAY_NUMBER" xwininfo -root -tree >"$OUTPUT_DIR/xvfb-window-tree.txt" 2>&1 || true
  die 'Moonlight SDL window was not found on the disposable Xvfb display'
}

wait_for_sunshine_log() {
  local pattern=$1
  local label=$2
  for _ in $(seq 1 160); do
    grep -Fq "$pattern" "$sunshine_log" 2>/dev/null && return 0
    kill -0 "$sunshine_pid" 2>/dev/null || { cat "$sunshine_log" >&2 || true; die 'Sunshine exited early'; }
    sleep 0.05
  done
  die "Sunshine did not log $label"
}

wait_for_observer_segment() {
  local segments=()
  for _ in $(seq 1 140); do
    shopt -s nullglob
    segments=("$observer_encoded_dir"/*.mkv)
    shopt -u nullglob
    (( ${#segments[@]} > 0 )) && return 0
    kill -0 "$observer_pid" 2>/dev/null || { cat "$observer_log" >&2 || true; die 'Display1 observer exited before an initial frame'; }
    sleep 0.05
  done
  die 'Display1 observer did not receive the pre-Sunshine guest scanout'
}

for required in "$SUNSHINE_BINARY" "$MOONLIGHT_BINARY" "$QEMU_BINARY" "$QEMU_IMG_BINARY" \
                "$DISPLAY_PROBE" "$PROVISIONER" "$USER_DATA" "$META_DATA" \
                cloud-localds dbus-daemon busctl Xvfb xdpyinfo xdotool xwd \
                curl ffmpeg ffprobe timeout stdbuf awk sed grep find head tail cat \
                mkdir mktemp chmod sha512sum sleep seq setsid date kill readlink; do
  require "$required"
done
if [[ "$QEMU_USE_SUDO" == '1' ]]; then
  require sudo
fi

[[ -f "$(dirname -- "$SUNSHINE_BINARY")/assets/apps.json" ]] ||
  die "Sunshine assets are missing beside $SUNSHINE_BINARY"
[[ -f "$MESA_EGL_VENDOR" ]] || die "Mesa EGL vendor file not found: $MESA_EGL_VENDOR"
"$QEMU_BINARY" -display help | grep -Fxq dbus || die "QEMU lacks the D-Bus display backend: $QEMU_BINARY"
[[ "$ACCEL" == 'kvm' ]] || die 'this native qualification requires VIRGL_ACCEL=kvm'
[[ "$QEMU_USE_SUDO" == '0' || "$QEMU_USE_SUDO" == '1' ]] || die 'VIRGL_QEMU_USE_SUDO must be 0 or 1'
[[ "${QEMU_EGL_SURFACELESS_FALLBACK:-0}" == '0' ]] ||
  die 'QEMU_EGL_SURFACELESS_FALLBACK must remain 0 for the native NVIDIA/VirGL gate'
[[ "$BOOT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_BOOT_TIMEOUT_SECONDS must be a positive integer'
[[ "$OBSERVER_DURATION_MS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_OBSERVER_DURATION_MS must be a positive integer'
(( OBSERVER_DURATION_MS >= 10000 && OBSERVER_DURATION_MS <= 16000 )) ||
  die 'VIRGL_OBSERVER_DURATION_MS must be 10000..16000 so it finishes before the fixture powers off'
[[ "$STREAM_SECONDS" =~ ^[1-9][0-9]*$ ]] || die 'STREAM_SECONDS must be a positive integer'
(( STREAM_SECONDS >= 8 && STREAM_SECONDS <= 12 )) ||
  die 'STREAM_SECONDS must be 8..12 so the disposable guest remains alive through the stream'
[[ "$SUNSHINE_PORT" =~ ^[1-9][0-9]*$ ]] || die 'SUNSHINE_PORT must be a positive integer'
(( SUNSHINE_PORT >= 1029 && SUNSHINE_PORT <= 65500 )) || die "SUNSHINE_PORT is outside Sunshine's safe base-port range"
[[ "$PIN" =~ ^[0-9]{4}$ ]] || die 'MOONLIGHT_PIN must have exactly four digits'
[[ "$WINDOW_MODE" == 'windowed' || "$WINDOW_MODE" == 'fullscreen' ]] ||
  die 'MOONLIGHT_WINDOW_MODE must be windowed or fullscreen'
[[ -c /dev/kvm ]] || die 'native KVM requires /dev/kvm'
[[ -c "$RENDER_NODE" ]] || die "native VirGL requires DRM render node: $RENDER_NODE"
if ! run_as_qemu_user test -r /dev/kvm -a -w /dev/kvm; then
  die "QEMU user $QEMU_RUN_AS cannot access /dev/kvm"
fi
if ! run_as_qemu_user test -r "$RENDER_NODE" -a -w "$RENDER_NODE"; then
  die "QEMU user $QEMU_RUN_AS cannot access render node: $RENDER_NODE"
fi
[[ -r "$RENDER_NODE" && -w "$RENDER_NODE" ]] ||
  die "Sunshine/observer user cannot access render node: $RENDER_NODE"

if [[ ! -f "$BASE_IMAGE" ]]; then
  [[ "${VIRGL_AUTO_PROVISION:-1}" == '1' ]] || die "base image is absent: $BASE_IMAGE"
  VIRGL_ALPINE_VERSION="$ALPINE_VERSION" VIRGL_VM_DIR="$VM_DIR" "$PROVISIONER"
fi
[[ -f "$BASE_IMAGE" ]] || die "base image is absent after provisioning: $BASE_IMAGE"

mkdir -p "$OUTPUT_PARENT"
chmod 700 "$OUTPUT_PARENT"
OUTPUT_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
chmod 700 "$OUTPUT_DIR"
printf 'Moonlight Sunshine native VirGL E2E evidence=%s\n' "$OUTPUT_DIR"

qemu_pid=''
qemu_launcher_pid=''
bus_pid=''
sunshine_pid=''
moonlight_pid=''
xvfb_pid=''
observer_pid=''
cleanup() {
  # Stop consumers before their producer, then release the private D-Bus.
  stop_pid "$moonlight_pid"
  stop_pid "$observer_pid"
  stop_pid "$sunshine_pid"
  stop_pid "$xvfb_pid"
  stop_pid "$qemu_pid"
  stop_pid "$qemu_launcher_pid"
  stop_pid "$bus_pid"
}
trap cleanup EXIT INT TERM

seed_iso="$OUTPUT_DIR/nocloud.iso"
overlay="$OUTPUT_DIR/guest-overlay.qcow2"
serial_log="$OUTPUT_DIR/guest-serial.log"
telemetry_log="$OUTPUT_DIR/guest-telemetry.log"
qemu_log="$OUTPUT_DIR/qemu.log"
qemu_pidfile="$OUTPUT_DIR/qemu.pid"
sunshine_log="$OUTPUT_DIR/sunshine.log"
stream_log="$OUTPUT_DIR/moonlight-stream.log"
pair_log="$OUTPUT_DIR/moonlight-pair.log"
list_log="$OUTPUT_DIR/moonlight-list.log"
xvfb_log="$OUTPUT_DIR/xvfb.log"
observer_log="$OUTPUT_DIR/display1-observer.log"
observer_encoded_dir="$OUTPUT_DIR/display1-observer-encoded"
xdg_config_dir="$OUTPUT_DIR/xdg-config"
moonlight_key_dir="$OUTPUT_DIR/moonlight-keys"
mkdir -p "$observer_encoded_dir" "$xdg_config_dir" "$moonlight_key_dir"
chmod 700 "$observer_encoded_dir" "$xdg_config_dir" "$moonlight_key_dir"

cloud-localds "$seed_iso" "$USER_DATA" "$META_DATA"
"$QEMU_IMG_BINARY" create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$overlay" 2G

mapfile -t bus_info < <(dbus-daemon --session --fork --print-address=1 --print-pid=1)
[[ ${#bus_info[@]} -eq 2 ]] || die 'private D-Bus did not return address and PID'
bus_address="${bus_info[0]}"
bus_pid="${bus_info[1]}"
printf '%s\n' "$bus_address" >"$OUTPUT_DIR/private-dbus.address"
printf '%s\n' "$bus_pid" >"$OUTPUT_DIR/private-dbus.pid"

display_option="dbus,gl=on,rendernode=$RENDER_NODE"
qemu_args=(
  -name moonlight-sunshine-virgl-e2e
  -nodefaults
  -machine "q35,accel=$ACCEL"
  -smp 2
  -m 1536
  -boot order=c
  -drive "file=$overlay,if=virtio,format=qcow2"
  -drive "file=$seed_iso,media=cdrom,readonly=on,format=raw"
  -vga none
  -device virtio-vga-gl
  -display "$display_option"
  -serial "file:$serial_log"
  -chardev "file,id=virgltelemetry,path=$telemetry_log"
  -device virtio-serial-pci,id=virgl-serial0
  -device virtserialport,chardev=virgltelemetry,name=org.qsunshine.virgl.telemetry
  -monitor none
  -nic user,model=virtio-net-pci
  -no-reboot
  -pidfile "$qemu_pidfile"
)

# Explicitly remove all client-side/Mesa software variables.  This is the
# native NVIDIA render-node path, not the opt-in surfaceless llvmpipe fallback.
qemu_env=(env -u DISPLAY -u WAYLAND_DISPLAY -u __EGL_VENDOR_LIBRARY_FILENAMES \
  -u LIBGL_ALWAYS_SOFTWARE "DBUS_SESSION_BUS_ADDRESS=$bus_address" \
  QEMU_EGL_SURFACELESS_FALLBACK=0)
printf '%s\n' 'QEMU command (private D-Bus address omitted):' >"$OUTPUT_DIR/qemu-command.txt"
printf '%q ' "$QEMU_BINARY" "${qemu_args[@]}" >>"$OUTPUT_DIR/qemu-command.txt"
printf '\n' >>"$OUTPUT_DIR/qemu-command.txt"

if [[ "$QEMU_USE_SUDO" == '1' ]]; then
  setsid sudo -n -u "$QEMU_RUN_AS" "${qemu_env[@]}" "$QEMU_BINARY" "${qemu_args[@]}" >"$qemu_log" 2>&1 &
else
  setsid "${qemu_env[@]}" "$QEMU_BINARY" "${qemu_args[@]}" >"$qemu_log" 2>&1 &
fi
qemu_launcher_pid=$!
printf '%s\n' "$qemu_launcher_pid" >"$OUTPUT_DIR/qemu-launcher.pid"

for _ in $(seq 1 300); do
  if [[ -s "$qemu_pidfile" ]]; then
    qemu_pid="$(<"$qemu_pidfile")"
    break
  fi
  kill -0 "$qemu_launcher_pid" 2>/dev/null || { show_logs; die 'QEMU exited before writing its PID'; }
  sleep 0.05
done
[[ -n "$qemu_pid" ]] || { show_logs; die 'QEMU did not write its PID'; }
wait_for_qemu_bus
wait_for_guest_marker QMDP_VIRGL_GUEST_READY 'VirGL renderer readiness'

# Attach the observer before Sunshine's encoder probing.  Its first H.264
# segment is therefore the guest's pre-SetUIInfo scanout (1280x800 on the
# pinned fixture); the same observer later witnesses the 1280x720 mode from
# the actual Moonlight-triggered Sunshine request.
"$DISPLAY_PROBE" \
  --dbus-address "$bus_address" \
  --destination org.qemu \
  --duration-ms "$OBSERVER_DURATION_MS" \
  --no-audio \
  --encode-dir "$observer_encoded_dir" \
  >"$observer_log" 2>&1 &
observer_pid=$!
wait_for_observer_segment

# This Xvfb exists only to give the real SDL Moonlight client a disposable
# drawable.  The fullscreen lane uses the negotiated mode as its entire root;
# the windowed lane deliberately keeps a larger root to distinguish ordinary
# presentation from exclusive fullscreen.  The NVIDIA driver is not used for
# it; forcing Mesa here prevents an unrelated NVIDIA+Xvfb EGL crash without
# affecting QEMU.
xvfb_screen='1280x720x24'
if [[ "$WINDOW_MODE" == 'windowed' ]]; then
  xvfb_screen='1600x900x24'
fi
__EGL_VENDOR_LIBRARY_FILENAMES="$MESA_EGL_VENDOR" LIBGL_ALWAYS_SOFTWARE=1 \
  Xvfb "$DISPLAY_NUMBER" -screen 0 "$xvfb_screen" +extension GLX >"$xvfb_log" 2>&1 &
xvfb_pid=$!
wait_for_xvfb

# Do not inherit the test X display into the deployed host components.
printf '%s\n' "$PIN" | \
  env -u DISPLAY -u WAYLAND_DISPLAY \
    "XDG_CONFIG_HOME=$xdg_config_dir" \
    "SUNSHINE_QEMU_DBUS_ADDRESS=$bus_address" \
    "SUNSHINE_QEMU_DBUS_RENDER_NODE=$RENDER_NODE" \
    "$SUNSHINE_BINARY" -0 \
      capture=qemu_dbus encoder=software stream_audio=false \
      system_tray=false bind_address=127.0.0.1 port="$SUNSHINE_PORT" \
      >"$sunshine_log" 2>&1 &
sunshine_pid=$!
wait_for_sunshine_http

"$MOONLIGHT_BINARY" -debug -pin "$PIN" -keydir "$moonlight_key_dir" -port "$SUNSHINE_PORT" \
  pair 127.0.0.1 >"$pair_log" 2>&1
"$MOONLIGHT_BINARY" -debug -keydir "$moonlight_key_dir" -port "$SUNSHINE_PORT" \
  list 127.0.0.1 >"$list_log" 2>&1

set +e
moonlight_window_argument=()
if [[ "$WINDOW_MODE" == 'windowed' ]]; then
  moonlight_window_argument=(-windowed)
fi
DISPLAY="$DISPLAY_NUMBER" SDL_AUDIODRIVER=dummy \
  __EGL_VENDOR_LIBRARY_FILENAMES="$MESA_EGL_VENDOR" LIBGL_ALWAYS_SOFTWARE=1 \
  stdbuf -oL -eL timeout --signal=TERM --kill-after=5s "${STREAM_SECONDS}s" \
    "$MOONLIGHT_BINARY" -debug -keydir "$moonlight_key_dir" -port "$SUNSHINE_PORT" \
      -platform sdl "${moonlight_window_argument[@]}" -app Desktop -codec h264 -width 1280 -height 720 -fps 30 -bitrate 4000 \
      stream 127.0.0.1 >"$stream_log" 2>&1 &
moonlight_pid=$!
set -e

wait_for_moonlight_video
wait_for_moonlight_window

# This is a live SDL input event on the disposable Moonlight client.  The
# derived guest fixture watches its Q35 PS/2 evdev node for both KEY_A make and
# break events and emits QMDP_VIRGL_GUEST_INPUT_PRESS_RELEASE_OK only after
# they arrived through GameStream -> Sunshine -> Display1 Keyboard -> QEMU.
DISPLAY="$DISPLAY_NUMBER" xdotool key --window "$moonlight_window" --clearmodifiers a
# Moonlight Embedded starts SDL in relative-pointer mode.  XTest (which powers
# xdotool on the disposable Xvfb client) reliably provides button/key events,
# but not an SDL relative delta.  Its documented Ctrl+Alt+Shift+Z toggle
# switches only the client to absolute-pointer mode, so the following XTest
# positions exercise the normal Moonlight `LiSendMousePositionEvent` path and
# Sunshine's QEMU Display1 `SetAbsPosition` path without adding host input
# shims.  Send two deliberately distinct positions because a single move can
# be a no-op when the Xvfb pointer starts at that coordinate.
#
# The guest fixture accepts either QEMU's absolute or relative evdev movement,
# then requires both BTN_LEFT edges rather than trusting a host-side method
# return or an xdotool exit status.
DISPLAY="$DISPLAY_NUMBER" xdotool windowfocus "$moonlight_window"
# Do not use xdotool's compact `ctrl+alt+shift+z` spelling here: it releases
# the modifiers before Z, whereas Moonlight recognizes this action only when
# Z's *release* still carries all three modifier bits.
DISPLAY="$DISPLAY_NUMBER" xdotool keydown ctrl
DISPLAY="$DISPLAY_NUMBER" xdotool keydown alt
DISPLAY="$DISPLAY_NUMBER" xdotool keydown shift
DISPLAY="$DISPLAY_NUMBER" xdotool key z
DISPLAY="$DISPLAY_NUMBER" xdotool keyup shift
DISPLAY="$DISPLAY_NUMBER" xdotool keyup alt
DISPLAY="$DISPLAY_NUMBER" xdotool keyup ctrl
sleep 0.1
DISPLAY="$DISPLAY_NUMBER" xdotool mousemove --sync --window "$moonlight_window" 100 100
sleep 0.1
DISPLAY="$DISPLAY_NUMBER" xdotool mousemove --sync --window "$moonlight_window" 1100 600
sleep 0.1
DISPLAY="$DISPLAY_NUMBER" xdotool click --window "$moonlight_window" 1

# Capture after a complete decoded presentation, rather than accepting the
# first network packet as proof of a visible fullscreen image.  The first
# packet can precede kmscube's visible scene, so wait for a non-black decoded
# SDL frame instead of retaining a potentially blank initial drawable.
decoded_yavg=''
decoded_ymax=''
for _ in $(seq 1 120); do
  DISPLAY="$DISPLAY_NUMBER" xwd -silent -id "$moonlight_window" -out "$OUTPUT_DIR/moonlight-client.xwd"
  ffmpeg -hide_banner -loglevel error -y -f xwd_pipe -i "$OUTPUT_DIR/moonlight-client.xwd" \
    "$OUTPUT_DIR/moonlight-client.png"
  ffmpeg -hide_banner -loglevel error -i "$OUTPUT_DIR/moonlight-client.png" \
    -vf "signalstats,metadata=print:file=$OUTPUT_DIR/moonlight-client.signalstats" \
    -f null -
  decoded_yavg="$(sed -n 's/^lavfi\.signalstats\.YAVG=//p' "$OUTPUT_DIR/moonlight-client.signalstats" | head -n1)"
  decoded_ymax="$(sed -n 's/^lavfi\.signalstats\.YMAX=//p' "$OUTPUT_DIR/moonlight-client.signalstats" | head -n1)"
  if [[ "$decoded_yavg" =~ ^[0-9]+([.][0-9]+)?$ && "$decoded_ymax" =~ ^[0-9]+([.][0-9]+)?$ ]] && \
      awk -v average="$decoded_yavg" -v maximum="$decoded_ymax" 'BEGIN { exit !(average > 20 && maximum > 32) }'; then
    break
  fi
  kill -0 "$moonlight_pid" 2>/dev/null || { cat "$stream_log" >&2 || true; die 'Moonlight exited before a non-black decoded frame'; }
  sleep 0.1
done
[[ "$decoded_yavg" =~ ^[0-9]+([.][0-9]+)?$ && "$decoded_ymax" =~ ^[0-9]+([.][0-9]+)?$ ]] ||
  die 'could not read decoded Moonlight signal statistics'
awk -v average="$decoded_yavg" -v maximum="$decoded_ymax" 'BEGIN { exit !(average > 20 && maximum > 32) }' ||
  die "decoded Moonlight presentation remained black (YAVG=$decoded_yavg YMAX=$decoded_ymax)"

DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$moonlight_window" >"$OUTPUT_DIR/moonlight-window.xwininfo"
DISPLAY="$DISPLAY_NUMBER" xwininfo -root >"$OUTPUT_DIR/moonlight-root.xwininfo"
DISPLAY="$DISPLAY_NUMBER" xwd -silent -root -out "$OUTPUT_DIR/moonlight-root.xwd"
ffmpeg -hide_banner -loglevel error -y -f xwd_pipe -i "$OUTPUT_DIR/moonlight-root.xwd" \
  "$OUTPUT_DIR/moonlight-root.png"
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
  "$OUTPUT_DIR/moonlight-client.png" >"$OUTPUT_DIR/moonlight-client.ffprobe"
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
  "$OUTPUT_DIR/moonlight-root.png" >"$OUTPUT_DIR/moonlight-root.ffprobe"

set +e
wait "$moonlight_pid"
moonlight_status=$?
set -e
moonlight_pid=''
[[ "$moonlight_status" == '124' ]] || { cat "$stream_log" >&2 || true; die "Moonlight must stay active until bounded shutdown; status=$moonlight_status"; }

# 0004 emits this at destruction of each QEMU Display1 capture instance.  The
# real streaming instance must have nonzero scanouts and no readback failures;
# a success banner by itself is not enough to establish a healthy DMA-BUF run.
# Its close() summary is intentionally emitted only when the display object is
# destroyed, so stop Sunshine now while QEMU and the independent observer are
# still alive.  This keeps the final counters attributable to this exact run.
stop_pid "$sunshine_pid"
sunshine_pid=''
wait_for_sunshine_log 'DMA-BUF scanouts/updates/failures:' 'DMA-BUF counters'

set +e
wait "$observer_pid"
observer_status=$?
set -e
observer_pid=''
printf '%s\n' "$observer_status" >"$OUTPUT_DIR/display1-observer.exit-status"
[[ "$observer_status" == '0' ]] || { show_logs; die "Display1 observer failed with status $observer_status"; }

wait_for_guest_marker QMDP_VIRGL_GUEST_E2E_OK 'VirGL guest completion'

# Private pairing and authenticated GameStream control plane.  Moonlight
# itself records the HTTPS requests (including the launch URI); the pre-pair
# curl only establishes that the disposable Sunshine listener came up.
grep -Fq '<HttpsPort>' "$OUTPUT_DIR/serverinfo-before-pair.xml" || die 'Sunshine serverinfo did not expose HTTPS'
grep -Fq 'Request https://127.0.0.1:' "$pair_log" || die 'pairing did not use HTTPS'
grep -Fq 'Succesfully paired' "$pair_log" || die 'Moonlight private pairing did not complete'
grep -Fq 'Request https://127.0.0.1:' "$list_log" || die 'application listing did not use HTTPS'
grep -Fq '<AppTitle>Desktop</AppTitle>' "$list_log" || die 'Sunshine Desktop application was not listed'
grep -Fq 'Request https://127.0.0.1:' "$stream_log" || die 'stream launch did not use HTTPS'
grep -Fq '/launch?' "$stream_log" || die 'Moonlight did not launch the Desktop application'
grep -Fq '<sessionUrl0>rtspenc://' "$stream_log" || die 'Sunshine did not return an RTSP session URL'
grep -Fq 'Starting RTSP handshake...' "$stream_log" || die 'Moonlight did not start an RTSP handshake'
grep -Fq 'Starting video stream...' "$stream_log" || die 'Moonlight did not start the RTP video stream'
grep -Fq 'Received first video packet after' "$stream_log" || die 'Moonlight received no RTP video packet'
grep -Fq 'Using FFmpeg decoder: h264' "$stream_log" || die 'Moonlight did not use its FFmpeg H.264 decoder'
! grep -Fq 'No video traffic was ever received from the host!' "$stream_log" || die 'Moonlight reported no video traffic'
! grep -Fq 'Audio stream start failed' "$stream_log" || die 'the disposable SDL dummy-audio client failed to start audio'

# Client presentation is deliberately separate from the guest mode assertion
# below.  Both lanes decode a 1280x720 stream; fullscreen must own the entire
# matching root while windowed mode must remain inside a deliberately larger
# client-only root.
[[ -s "$OUTPUT_DIR/moonlight-client.png" && -s "$OUTPUT_DIR/moonlight-root.png" ]] || die 'decoded Moonlight screenshots are absent'
grep -Fq 'codec_name=png' "$OUTPUT_DIR/moonlight-client.ffprobe"
grep -Fq 'width=1280' "$OUTPUT_DIR/moonlight-client.ffprobe"
grep -Fq 'height=720' "$OUTPUT_DIR/moonlight-client.ffprobe"
grep -Fq 'codec_name=png' "$OUTPUT_DIR/moonlight-root.ffprobe"
grep -Fq 'Width: 1280' "$OUTPUT_DIR/moonlight-window.xwininfo"
grep -Fq 'Height: 720' "$OUTPUT_DIR/moonlight-window.xwininfo"
if [[ "$WINDOW_MODE" == 'fullscreen' ]]; then
  grep -Fq 'width=1280' "$OUTPUT_DIR/moonlight-root.ffprobe"
  grep -Fq 'height=720' "$OUTPUT_DIR/moonlight-root.ffprobe"
  grep -Eq 'Absolute upper-left X:[[:space:]]+0$' "$OUTPUT_DIR/moonlight-window.xwininfo"
  grep -Eq 'Absolute upper-left Y:[[:space:]]+0$' "$OUTPUT_DIR/moonlight-window.xwininfo"
  grep -Fq 'Width: 1280' "$OUTPUT_DIR/moonlight-root.xwininfo"
  grep -Fq 'Height: 720' "$OUTPUT_DIR/moonlight-root.xwininfo"
else
  grep -Fq 'width=1600' "$OUTPUT_DIR/moonlight-root.ffprobe"
  grep -Fq 'height=900' "$OUTPUT_DIR/moonlight-root.ffprobe"
  grep -Fq 'Width: 1600' "$OUTPUT_DIR/moonlight-root.xwininfo"
  grep -Fq 'Height: 900' "$OUTPUT_DIR/moonlight-root.xwininfo"
fi

# Direct Sunshine listener proof.  The importer message occurs only after the
# native listener accepts a QEMU ScanoutDMABUF and turns it into a CPU frame for
# Sunshine's existing software encoder.  Any callback error is a hard failure.
grep -Fq 'Screencasting with QEMU Display1 D-Bus' "$sunshine_log" || die 'Sunshine did not select QEMU Display1 capture'
grep -Fq 'Found H.264 encoder: libx264 [software]' "$sunshine_log" || die 'Sunshine did not qualify libx264 software encoding'
grep -Fq 'New streaming session started' "$sunshine_log" || die 'Sunshine did not start a GameStream session'
grep -Fq '[qemu-dbus] Console.SetUIInfo accepted 1280x720' "$sunshine_log" || die 'Sunshine did not get QEMU acceptance for the 1280x720 request'
grep -Fq '[qemu-dbus] imported QEMU ScanoutDMABUF with headless EGL CPU readback' "$sunshine_log" ||
  die 'Sunshine did not import a native QEMU DMA-BUF; build/apply the QEMU DMA-BUF Sunshine patch'
grep -Fq '[qemu-dbus] QEMU scanout geometry changed to 1280x720' "$sunshine_log" ||
  die 'Sunshine did not observe the requested 1280x720 guest scanout transition'
! grep -Fq '[qemu-dbus] listener callback failed:' "$sunshine_log" || die 'Sunshine reported a DMA-BUF/listener callback failure'
! grep -Fq 'QEMU DMA-BUF readback was not enabled at build time' "$sunshine_log" || die 'Sunshine was built without DMA-BUF readback'
grep -Eq 'DMA-BUF scanouts/updates/failures: [1-9][0-9]*/[0-9]+/0' "$sunshine_log" ||
  die 'Sunshine did not report nonzero DMA-BUF scanouts with zero failures'
! grep -Eq 'DMA-BUF scanouts/updates/failures: [0-9]+/[0-9]+/[1-9][0-9]*' "$sunshine_log" ||
  die 'Sunshine reported a DMA-BUF readback failure'
# The real Moonlight mouse move is deliberately switched to SDL absolute mode
# for Xvfb/XTest.  On QEMU's absolute Display1 mouse it must produce queued
# SetAbsPosition calls, never an invalid RelMotion call or a geometry drop.
grep -Eq '\[qemu-dbus\] input stats: relative_calls=[0-9]+ relative_nonzero=[0-9]+ absolute_calls=[1-9][0-9]* button_calls=[1-9][0-9]* queued_rel=0 queued_abs=[1-9][0-9]* dropped_no_geometry=0' "$sunshine_log" ||
  die 'Sunshine did not route the live Moonlight absolute mouse move/click into QEMU input'
! grep -Eq '\[qemu-dbus\] input stats: .*dropped_no_geometry=[1-9][0-9]*' "$sunshine_log" ||
  die 'Sunshine dropped a live Moonlight mouse event before QEMU geometry was ready'
sunshine_input_stats="$(grep -E '\[qemu-dbus\] input stats: relative_calls=[0-9]+ relative_nonzero=[0-9]+ absolute_calls=[1-9][0-9]* button_calls=[1-9][0-9]* queued_rel=0 queued_abs=[1-9][0-9]* dropped_no_geometry=0' "$sunshine_log" | tail -n1)"

# Guest-native renderer and authoritative parallel Display1 observer.  The
# observer's counters prove nonzero DMA-BUF traffic with zero readback errors;
# its H.264 segments prove that the pre-request 1280x800 scanout changed to
# the actual 1280x720 guest scanout accepted through Sunshine, rather than
# merely scaling a fixed guest framebuffer at the Moonlight client.
grep -Fqx 'guest_drm_driver=virtio_gpu' "$telemetry_log" || die 'guest did not prove the virtio_gpu DRM driver'
guest_renderer="$(sed -n 's/^guest_gl_renderer=//p' "$telemetry_log" | head -n1)"
[[ -n "$guest_renderer" ]] || die 'guest did not report an OpenGL renderer'
printf '%s' "$guest_renderer" | grep -qi virgl || die "guest did not use VirGL: $guest_renderer"
printf '%s' "$guest_renderer" | grep -Fqi -- "$EXPECTED_RENDERER" || die "guest renderer lacks expected native GPU marker '$EXPECTED_RENDERER': $guest_renderer"
! printf '%s' "$guest_renderer" | grep -qi llvmpipe || die "native guest unexpectedly used llvmpipe: $guest_renderer"
grep -Eq '^kmscube_status=(0|124)$' "$telemetry_log" || die 'guest did not complete the KMS VirGL scene'
grep -Fqx 'QMDP_VIRGL_GUEST_INPUT_PRESS_RELEASE_OK' "$telemetry_log" ||
  die 'guest did not observe the real Moonlight A press/release through evdev'
grep -Fqx 'QMDP_VIRGL_GUEST_MOUSE_MOVE_CLICK_OK' "$telemetry_log" ||
  die 'guest did not observe the real Moonlight mouse move/left-click through evdev'
grep -Fq 'QEMU_DISPLAY_PROBE_RESULT' "$observer_log" || die 'Display1 observer produced no result block'
grep -Eq 'frames published/encoded/dropped: [1-9][0-9]*/[1-9][0-9]*/' "$observer_log" || die 'Display1 observer did not publish and encode a video frame'
grep -Fq 'session errors: 0' "$observer_log" || die 'Display1 observer reported session errors'
grep -Eq 'DMA-BUF scanouts/updates/failures: [1-9][0-9]*/[0-9]+/0' "$observer_log" ||
  die 'native Display1 DMA-BUF counters are missing or report a readback failure'
! grep -Fq 'DMA-BUF capture is not enabled in the CPU-first MVP' "$qemu_log" || die 'host observer rejected QEMU DMA-BUF frames'

shopt -s nullglob
observer_segments=("$observer_encoded_dir"/*.mkv)
shopt -u nullglob
(( ${#observer_segments[@]} > 0 )) || die 'Display1 observer produced no encoded H.264 segment'
: >"$OUTPUT_DIR/display1-observer.ffprobe"
saw_pre_request_mode=0
saw_requested_mode=0
for segment in "${observer_segments[@]}"; do
  segment_info="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 "$segment")"
  {
    printf '[%s]\n' "$(basename "$segment")"
    printf '%s\n' "$segment_info"
  } >>"$OUTPUT_DIR/display1-observer.ffprobe"
  grep -Fqx 'codec_name=h264' <<<"$segment_info" || die "observer segment is not H.264: $segment"
  if grep -Fqx 'width=1280' <<<"$segment_info" && grep -Fqx 'height=800' <<<"$segment_info"; then
    saw_pre_request_mode=1
  fi
  if grep -Fqx 'width=1280' <<<"$segment_info" && grep -Fqx 'height=720' <<<"$segment_info"; then
    saw_requested_mode=1
  fi
done
(( saw_pre_request_mode == 1 )) || die 'observer did not encode the pre-SetUIInfo 1280x800 guest mode'
(( saw_requested_mode == 1 )) || die 'observer did not encode the requested 1280x720 guest mode'

{
  printf '%s\n' 'QMDP_MOONLIGHT_SUNSHINE_VIRGL_E2E'
  printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'qemu=%s\n' "$("$QEMU_BINARY" --version | head -n1)"
  printf 'qemu_binary=%s\n' "$QEMU_BINARY"
  printf 'sunshine_binary=%s\n' "$SUNSHINE_BINARY"
  printf 'moonlight_binary=%s\n' "$MOONLIGHT_BINARY"
  printf 'display_probe=%s\n' "$DISPLAY_PROBE"
  printf 'accel=kvm\n'
  printf 'display=%s\n' "$display_option"
  printf 'host_gui=none (Xvfb is client-only)\n'
  printf 'guest_drm_driver=virtio_gpu\n'
  printf 'guest_gl_renderer=%s\n' "$guest_renderer"
  printf 'game_stream=private pairing + HTTPS launch + RTSP + RTP + FFmpeg H.264 decode\n'
  printf 'client_window_mode=%s\n' "$WINDOW_MODE"
  if [[ "$WINDOW_MODE" == 'fullscreen' ]]; then
    printf 'client_presentation=fullscreen 1280x720@(0,0) on 1280x720 root\n'
  else
    printf 'client_presentation=windowed 1280x720 on 1600x900 root\n'
  fi
  printf 'decoded_client_luma=YAVG:%s YMAX:%s (non-black)\n' "$decoded_yavg" "$decoded_ymax"
  printf 'moonlight_input=guest evdev observed KEY_A press+release\n'
  printf 'moonlight_mouse=guest evdev observed movement + BTN_LEFT press+release\n'
  printf 'sunshine_input_stats=%s\n' "$sunshine_input_stats"
  printf 'set_ui_info=Sunshine Console.SetUIInfo accepted 1280x720\n'
  printf 'guest_encoder_geometry_transition=1280x800->1280x720 (parallel Display1 H.264 observer)\n'
  printf 'sunshine_dmabuf=headless EGL CPU readback accepted\n'
  printf 'observer_dmabuf=nonzero scanouts, zero failures\n'
  printf 'moonlight_timeout_status=%s\n' "$moonlight_status"
  printf 'base_image=%s\n' "$BASE_IMAGE"
  printf 'base_image_sha512=%s\n' "$(sha512sum "$BASE_IMAGE" | awk '{print $1}')"
  printf '\n[guest-telemetry]\n'
  cat "$telemetry_log"
  printf '\n[sunshine-dmabuf-and-resize]\n'
  grep -E '\[qemu-dbus\] (Console\.SetUIInfo accepted 1280x720|imported QEMU ScanoutDMABUF|QEMU scanout geometry changed to 1280x720|DMA-BUF scanouts/updates/failures:|input stats:)' "$sunshine_log"
  printf '\n[display1-observer]\n'
  cat "$observer_log"
  printf '\n[display1-observer-encoded-h264]\n'
  cat "$OUTPUT_DIR/display1-observer.ffprobe"
  printf '%s\n' 'QMDP_MOONLIGHT_SUNSHINE_VIRGL_E2E_OK'
} >"$OUTPUT_DIR/trace.txt"

printf 'QMDP_MOONLIGHT_SUNSHINE_VIRGL_E2E_OK output=%s\n' "$OUTPUT_DIR"
