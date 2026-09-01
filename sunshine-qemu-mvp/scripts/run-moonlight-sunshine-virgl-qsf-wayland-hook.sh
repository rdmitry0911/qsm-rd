#!/usr/bin/env bash
# Post-agent hook for run-virgl-qsf-wayland-clipboard-e2e.sh.
#
# The outer runner owns the native KVM/QEMU/Weston/QSF lifecycle and calls this
# executable only after its guest agent and input watcher are ready.  This
# hook owns only disposable Sunshine, Moonlight and Xvfb processes; it must
# never stop or reconfigure the caller's QEMU or private D-Bus session.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

SUNSHINE_BINARY="${SUNSHINE_BINARY:-$ROOT/.upstream/build-sunshine-qemu/sunshine}"
MOONLIGHT_BINARY="${MOONLIGHT_BINARY:-$ROOT/.upstream/build-moonlight-embedded/moonlight}"
RENDER_NODE="${VIRGL_RENDER_NODE:-/dev/dri/renderD128}"
SUNSHINE_PORT="${SUNSHINE_PORT:-48189}"
PIN="${MOONLIGHT_PIN:-4242}"
DISPLAY_NUMBER="${MOONLIGHT_DISPLAY:-:95}"
STREAM_SECONDS="${STREAM_SECONDS:-15}"
INPUT_TIMEOUT_SECONDS="${QSF_WAYLAND_MOONLIGHT_INPUT_TIMEOUT_SECONDS:-10}"
MESA_EGL_VENDOR="${MESA_EGL_VENDOR:-/usr/share/glvnd/egl_vendor.d/50_mesa.json}"

DBUS_ADDRESS="${QSF_WAYLAND_DBUS_ADDRESS:-}"
DBUS_DESTINATION="${QSF_WAYLAND_DBUS_DESTINATION:-org.qemu}"
OUTER_OUTPUT_DIR="${QSF_WAYLAND_OUTPUT_DIR:-}"
QEMU_PID="${QSF_WAYLAND_QEMU_PID:-}"
GUEST_TELEMETRY="${QSF_WAYLAND_GUEST_TELEMETRY:-}"
INPUT_WATCH_READY="${QSF_WAYLAND_MOONLIGHT_INPUT_WATCH_READY_MARKER:-QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCH_READY}"
INPUT_KEY_MARKER="${QSF_WAYLAND_MOONLIGHT_INPUT_KEY_MARKER:-QSF_VIRGL_WAYLAND_GUEST_INPUT_KEY_A=observed}"
INPUT_MOUSE_ABS_MARKER="${QSF_WAYLAND_MOONLIGHT_INPUT_MOUSE_ABS_MARKER:-QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_ABS=observed}"
INPUT_MOUSE_BUTTON_MARKER="${QSF_WAYLAND_MOONLIGHT_INPUT_MOUSE_BUTTON_MARKER:-QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_BTN=observed}"
INPUT_COMPLETE_MARKER="${QSF_WAYLAND_MOONLIGHT_INPUT_COMPLETE_MARKER:-QSF_VIRGL_WAYLAND_GUEST_INPUT_E2E_OK}"

# These are deliberately initialized before any preflight check.  A failing
# hook must be useful to its outer runner even when it fails part way through
# startup, rather than letting `set -e` hide the pairing/stream diagnostics.
HOOK_OUTPUT_DIR=''
sunshine_log=''
stream_log=''
logs_shown=0

die() {
  printf 'Moonlight Sunshine VirGL QSF-Wayland hook: %s\n' "$*" >&2
  show_logs || true
  exit 1
}

require() {
  local item=$1
  if [[ -e "$item" ]]; then
    return 0
  fi
  command -v "$item" >/dev/null 2>&1 || die "missing required command or path: $item"
}

stop_pid() {
  local pid=${1:-}
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

qemu_is_alive() {
  kill -0 "$QEMU_PID" 2>/dev/null || die 'the outer runner QEMU exited while the hook was active'
}

show_logs() {
  [[ -n "${HOOK_OUTPUT_DIR:-}" ]] || return 0
  if (( logs_shown )); then
    return 0
  fi
  logs_shown=1
  printf '%s\n' '--- guest telemetry ---' >&2
  tail -160 "$GUEST_TELEMETRY" >&2 2>/dev/null || true
  printf '%s\n' '--- Sunshine ---' >&2
  tail -180 "$sunshine_log" >&2 2>/dev/null || true
  printf '%s\n' '--- Moonlight ---' >&2
  tail -180 "$stream_log" >&2 2>/dev/null || true
}

on_error() {
  local status=$1
  local line=$2
  printf 'Moonlight Sunshine VirGL QSF-Wayland hook: unexpected failure status=%s line=%s\n' \
    "$status" "$line" >&2
  show_logs || true
  exit "$status"
}

wait_for_qemu_destination() {
  for _ in $(seq 1 120); do
    if busctl --address="$DBUS_ADDRESS" --no-pager list 2>/dev/null | \
        awk '{print $1}' | grep -Fxq "$DBUS_DESTINATION"; then
      return 0
    fi
    qemu_is_alive
    sleep 0.05
  done
  die "the supplied private D-Bus has no QEMU destination $DBUS_DESTINATION"
}

wait_for_guest_marker() {
  local marker=$1
  local label=$2
  local iterations=$((INPUT_TIMEOUT_SECONDS * 10))
  for _ in $(seq 1 "$iterations"); do
    grep -Fqx "$marker" "$GUEST_TELEMETRY" 2>/dev/null && return 0
    if grep -Fq 'QSF_VIRGL_WAYLAND_GUEST_E2E_FAILED=' "$GUEST_TELEMETRY" 2>/dev/null; then
      show_logs
      die "guest reported a failure while waiting for $label"
    fi
    qemu_is_alive
    sleep 0.1
  done
  show_logs
  die "guest did not report $label within ${INPUT_TIMEOUT_SECONDS}s"
}

wait_for_xvfb() {
  for _ in $(seq 1 160); do
    DISPLAY="$DISPLAY_NUMBER" xdpyinfo >/dev/null 2>&1 && return 0
    kill -0 "$xvfb_pid" 2>/dev/null || { cat "$xvfb_log" >&2 || true; die 'client-only Xvfb exited early'; }
    sleep 0.05
  done
  die 'client-only Xvfb did not become ready'
}

wait_for_sunshine_http() {
  for _ in $(seq 1 300); do
    if curl --silent --show-error --fail --max-time 1 \
        "http://127.0.0.1:${SUNSHINE_PORT}/serverinfo?uniqueid=0123456789ABCDEF" \
        >"$HOOK_OUTPUT_DIR/serverinfo-before-pair.xml" 2>/dev/null; then
      return 0
    fi
    kill -0 "$sunshine_pid" 2>/dev/null || { cat "$sunshine_log" >&2 || true; die 'Sunshine exited early'; }
    qemu_is_alive
    sleep 0.05
  done
  die 'Sunshine HTTP server did not become ready'
}

wait_for_moonlight_video() {
  for _ in $(seq 1 300); do
    grep -Fq 'Received first video packet after' "$stream_log" 2>/dev/null && return 0
    kill -0 "$moonlight_pid" 2>/dev/null || { cat "$stream_log" >&2 || true; die 'Moonlight exited before receiving video'; }
    qemu_is_alive
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
  DISPLAY="$DISPLAY_NUMBER" xwininfo -root -tree >"$HOOK_OUTPUT_DIR/xvfb-window-tree.txt" 2>&1 || true
  die 'Moonlight SDL window was not found on client-only Xvfb'
}

[[ -n "$DBUS_ADDRESS" ]] || die 'QSF_WAYLAND_DBUS_ADDRESS is required from the outer runner'
[[ -n "$OUTER_OUTPUT_DIR" && -d "$OUTER_OUTPUT_DIR" ]] ||
  die 'QSF_WAYLAND_OUTPUT_DIR must name the live outer-run evidence directory'
[[ "$QEMU_PID" =~ ^[1-9][0-9]*$ ]] || die 'QSF_WAYLAND_QEMU_PID must be a positive PID'
[[ -z "$GUEST_TELEMETRY" ]] && GUEST_TELEMETRY="$OUTER_OUTPUT_DIR/guest-telemetry.log"

for required in "$SUNSHINE_BINARY" "$MOONLIGHT_BINARY" busctl Xvfb xdpyinfo xdotool xwd xwininfo \
                curl ffmpeg ffprobe timeout stdbuf awk sed grep head tail cat mkdir mktemp chmod sleep seq date kill; do
  require "$required"
done
[[ -x "$SUNSHINE_BINARY" ]] || die "Sunshine binary is not executable: $SUNSHINE_BINARY"
[[ -x "$MOONLIGHT_BINARY" ]] || die "Moonlight binary is not executable: $MOONLIGHT_BINARY"
[[ -f "$(dirname -- "$SUNSHINE_BINARY")/assets/apps.json" ]] ||
  die "Sunshine assets are missing beside $SUNSHINE_BINARY"
[[ -f "$MESA_EGL_VENDOR" ]] || die "Mesa EGL vendor file not found: $MESA_EGL_VENDOR"
[[ -c "$RENDER_NODE" && -r "$RENDER_NODE" && -w "$RENDER_NODE" ]] ||
  die "Sunshine native DMA-BUF import requires readable/writable $RENDER_NODE"
[[ "$SUNSHINE_PORT" =~ ^[1-9][0-9]*$ ]] && (( SUNSHINE_PORT >= 1029 && SUNSHINE_PORT <= 65500 )) ||
  die 'SUNSHINE_PORT is outside Sunshine’s safe base-port range'
[[ "$PIN" =~ ^[0-9]{4}$ ]] || die 'MOONLIGHT_PIN must have exactly four digits'
[[ "$STREAM_SECONDS" =~ ^[1-9][0-9]*$ ]] && (( STREAM_SECONDS >= 12 && STREAM_SECONDS <= 30 )) ||
  die 'STREAM_SECONDS must be 12..30 for the post-agent hook'
[[ "$INPUT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] && (( INPUT_TIMEOUT_SECONDS < STREAM_SECONDS )) ||
  die 'QSF_WAYLAND_MOONLIGHT_INPUT_TIMEOUT_SECONDS must be positive and shorter than STREAM_SECONDS'
[[ -f "$GUEST_TELEMETRY" ]] || die "guest telemetry is absent: $GUEST_TELEMETRY"
qemu_is_alive
wait_for_qemu_destination

if DISPLAY="$DISPLAY_NUMBER" xdpyinfo >/dev/null 2>&1; then
  die "MOONLIGHT_DISPLAY $DISPLAY_NUMBER is already in use; choose a disposable Xvfb display"
fi

HOOK_OUTPUT_DIR="$(mktemp -d "$OUTER_OUTPUT_DIR/moonlight-sunshine-hook.XXXXXX")"
chmod 700 "$HOOK_OUTPUT_DIR"
printf 'Moonlight Sunshine QSF-Wayland hook evidence=%s\n' "$HOOK_OUTPUT_DIR"

sunshine_pid=''
moonlight_pid=''
xvfb_pid=''
cleanup() {
  # The outer runner exclusively owns QEMU, its D-Bus daemon, QSF and guest.
  stop_pid "$moonlight_pid"
  stop_pid "$sunshine_pid"
  stop_pid "$xvfb_pid"
}
trap cleanup EXIT INT TERM
# `die` covers intentional assertion failures; ERR covers unanticipated
# commands under `set -e` (including the pair/list setup path).
trap 'status=$?; on_error "$status" "$LINENO"' ERR

sunshine_log="$HOOK_OUTPUT_DIR/sunshine.log"
stream_log="$HOOK_OUTPUT_DIR/moonlight-stream.log"
pair_log="$HOOK_OUTPUT_DIR/moonlight-pair.log"
list_log="$HOOK_OUTPUT_DIR/moonlight-list.log"
xvfb_log="$HOOK_OUTPUT_DIR/xvfb.log"
moonlight_key_dir="$HOOK_OUTPUT_DIR/moonlight-keys"
xdg_config_dir="$HOOK_OUTPUT_DIR/xdg-config"
mkdir -p "$moonlight_key_dir" "$xdg_config_dir"
chmod 700 "$moonlight_key_dir" "$xdg_config_dir"

# Xvfb is a disposable drawable for Moonlight SDL only.  Sunshine gets an
# explicitly empty DISPLAY/WAYLAND_DISPLAY and captures QEMU natively.
__EGL_VENDOR_LIBRARY_FILENAMES="$MESA_EGL_VENDOR" LIBGL_ALWAYS_SOFTWARE=1 \
  Xvfb "$DISPLAY_NUMBER" -screen 0 1280x720x24 +extension GLX >"$xvfb_log" 2>&1 &
xvfb_pid=$!
wait_for_xvfb

# Sunshine consumes the pairing PIN from stdin.  Keep this identical to the
# direct native runner: a detached daemon with no supplied stdin will expose
# HTTP but leave Moonlight pairing at PairStatus=0.
printf '%s\n' "$PIN" | \
  env -u DISPLAY -u WAYLAND_DISPLAY \
    "XDG_CONFIG_HOME=$xdg_config_dir" \
    "SUNSHINE_QEMU_DBUS_ADDRESS=$DBUS_ADDRESS" \
    "SUNSHINE_QEMU_DBUS_DESTINATION=$DBUS_DESTINATION" \
    "SUNSHINE_QEMU_DBUS_RENDER_NODE=$RENDER_NODE" \
    "$SUNSHINE_BINARY" -0 \
      capture=qemu_dbus encoder=software stream_audio=false \
      system_tray=false bind_address=127.0.0.1 port="$SUNSHINE_PORT" \
      >"$sunshine_log" 2>&1 &
sunshine_pid=$!
wait_for_sunshine_http

if ! "$MOONLIGHT_BINARY" -debug -pin "$PIN" -keydir "$moonlight_key_dir" -port "$SUNSHINE_PORT" \
    pair 127.0.0.1 >"$pair_log" 2>&1; then
  cat "$pair_log" >&2 || true
  die 'Moonlight private pairing failed'
fi
if ! "$MOONLIGHT_BINARY" -debug -keydir "$moonlight_key_dir" -port "$SUNSHINE_PORT" \
    list 127.0.0.1 >"$list_log" 2>&1; then
  cat "$list_log" >&2 || true
  die 'Moonlight application listing failed after pairing'
fi

set +e
DISPLAY="$DISPLAY_NUMBER" SDL_AUDIODRIVER=dummy \
  __EGL_VENDOR_LIBRARY_FILENAMES="$MESA_EGL_VENDOR" LIBGL_ALWAYS_SOFTWARE=1 \
  stdbuf -oL -eL timeout --signal=TERM --kill-after=5s "${STREAM_SECONDS}s" \
    "$MOONLIGHT_BINARY" -debug -keydir "$moonlight_key_dir" -port "$SUNSHINE_PORT" \
      -platform sdl -app Desktop -codec h264 -width 1280 -height 720 -fps 30 -bitrate 4000 \
      stream 127.0.0.1 >"$stream_log" 2>&1 &
moonlight_pid=$!
set -e

wait_for_moonlight_video
wait_for_moonlight_window
wait_for_guest_marker "$INPUT_WATCH_READY" 'guest Moonlight input watcher readiness'

# This runs through the live Moonlight SDL client, GameStream input transport,
# Sunshine's QEMU Display1 implementation, and the outer runner's virtio HID
# devices.  The guest's independent evdev watcher is the authoritative proof.
DISPLAY="$DISPLAY_NUMBER" xdotool key --window "$moonlight_window" --clearmodifiers a
DISPLAY="$DISPLAY_NUMBER" xdotool windowfocus "$moonlight_window"
# Moonlight Embedded consumes this action only if the Z release still carries
# Ctrl+Alt+Shift; xdotool's compact chord spelling releases modifiers too soon.
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

# A valid packet alone can still decode to an initial black drawable.  Keep a
# client-side screenshot only after it has nontrivial luma.
decoded_yavg=''
decoded_ymax=''
for _ in $(seq 1 120); do
  DISPLAY="$DISPLAY_NUMBER" xwd -silent -id "$moonlight_window" -out "$HOOK_OUTPUT_DIR/moonlight-client.xwd"
  ffmpeg -hide_banner -loglevel error -y -f xwd_pipe -i "$HOOK_OUTPUT_DIR/moonlight-client.xwd" \
    "$HOOK_OUTPUT_DIR/moonlight-client.png"
  ffmpeg -hide_banner -loglevel error -i "$HOOK_OUTPUT_DIR/moonlight-client.png" \
    -vf "signalstats,metadata=print:file=$HOOK_OUTPUT_DIR/moonlight-client.signalstats" -f null -
  decoded_yavg="$(sed -n 's/^lavfi\.signalstats\.YAVG=//p' "$HOOK_OUTPUT_DIR/moonlight-client.signalstats" | head -n1)"
  decoded_ymax="$(sed -n 's/^lavfi\.signalstats\.YMAX=//p' "$HOOK_OUTPUT_DIR/moonlight-client.signalstats" | head -n1)"
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

DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$moonlight_window" >"$HOOK_OUTPUT_DIR/moonlight-window.xwininfo"
DISPLAY="$DISPLAY_NUMBER" xwininfo -root >"$HOOK_OUTPUT_DIR/moonlight-root.xwininfo"
DISPLAY="$DISPLAY_NUMBER" xwd -silent -root -out "$HOOK_OUTPUT_DIR/moonlight-root.xwd"
ffmpeg -hide_banner -loglevel error -y -f xwd_pipe -i "$HOOK_OUTPUT_DIR/moonlight-root.xwd" \
  "$HOOK_OUTPUT_DIR/moonlight-root.png"
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
  "$HOOK_OUTPUT_DIR/moonlight-client.png" >"$HOOK_OUTPUT_DIR/moonlight-client.ffprobe"
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
  "$HOOK_OUTPUT_DIR/moonlight-root.png" >"$HOOK_OUTPUT_DIR/moonlight-root.ffprobe"

wait_for_guest_marker "$INPUT_KEY_MARKER" 'guest Moonlight KEY_A evidence'
wait_for_guest_marker "$INPUT_MOUSE_ABS_MARKER" 'guest Moonlight absolute-pointer evidence'
wait_for_guest_marker "$INPUT_MOUSE_BUTTON_MARKER" 'guest Moonlight left-button evidence'
wait_for_guest_marker "$INPUT_COMPLETE_MARKER" 'guest Moonlight input completion'

if wait "$moonlight_pid"; then
  moonlight_status=0
else
  # `timeout` deliberately returns 124 after the bounded live-stream window.
  # Keep it in an `if` condition so the global ERR diagnostics remain reserved
  # for actual hook failures.
  moonlight_status=$?
fi
moonlight_pid=''
[[ "$moonlight_status" == '124' ]] || { cat "$stream_log" >&2 || true; die "Moonlight must remain active through bounded shutdown; status=$moonlight_status"; }

# The patched Sunshine reports its final DMA-BUF counters only as the capture
# object is destroyed.  Stop it, but deliberately leave the outer QEMU and its
# probe untouched for the QSF runner's remaining clipboard/file/resize work.
stop_pid "$sunshine_pid"
sunshine_pid=''

grep -Fq '<HttpsPort>' "$HOOK_OUTPUT_DIR/serverinfo-before-pair.xml" || die 'Sunshine serverinfo did not expose HTTPS'
grep -Fq 'Request https://127.0.0.1:' "$pair_log" || die 'Moonlight pairing did not use HTTPS'
grep -Fq 'Succesfully paired' "$pair_log" || die 'Moonlight private pairing did not complete'
grep -Fq 'Request https://127.0.0.1:' "$list_log" || die 'Moonlight application listing did not use HTTPS'
grep -Fq '<AppTitle>Desktop</AppTitle>' "$list_log" || die 'Sunshine Desktop application was not listed'
grep -Fq 'Request https://127.0.0.1:' "$stream_log" || die 'Moonlight stream launch did not use HTTPS'
grep -Fq '/launch?' "$stream_log" || die 'Moonlight did not launch the Desktop application'
grep -Fq '<sessionUrl0>rtspenc://' "$stream_log" || die 'Sunshine did not return an RTSP session URL'
grep -Fq 'Starting RTSP handshake...' "$stream_log" || die 'Moonlight did not start the RTSP handshake'
grep -Fq 'Starting video stream...' "$stream_log" || die 'Moonlight did not start the RTP video stream'
grep -Fq 'Received first video packet after' "$stream_log" || die 'Moonlight received no RTP video packet'
grep -Fq 'Using FFmpeg decoder: h264' "$stream_log" || die 'Moonlight did not use its FFmpeg H.264 decoder'
! grep -Fq 'No video traffic was ever received from the host!' "$stream_log" || die 'Moonlight reported no video traffic'

[[ -s "$HOOK_OUTPUT_DIR/moonlight-client.png" && -s "$HOOK_OUTPUT_DIR/moonlight-root.png" ]] ||
  die 'decoded Moonlight screenshots are absent'
grep -Fq 'codec_name=png' "$HOOK_OUTPUT_DIR/moonlight-client.ffprobe"
grep -Fq 'width=1280' "$HOOK_OUTPUT_DIR/moonlight-client.ffprobe"
grep -Fq 'height=720' "$HOOK_OUTPUT_DIR/moonlight-client.ffprobe"
grep -Fq 'width=1280' "$HOOK_OUTPUT_DIR/moonlight-root.ffprobe"
grep -Fq 'height=720' "$HOOK_OUTPUT_DIR/moonlight-root.ffprobe"
grep -Fq 'Width: 1280' "$HOOK_OUTPUT_DIR/moonlight-window.xwininfo"
grep -Fq 'Height: 720' "$HOOK_OUTPUT_DIR/moonlight-window.xwininfo"
grep -Fq 'Width: 1280' "$HOOK_OUTPUT_DIR/moonlight-root.xwininfo"
grep -Fq 'Height: 720' "$HOOK_OUTPUT_DIR/moonlight-root.xwininfo"
grep -Eq 'Absolute upper-left X:[[:space:]]+0$' "$HOOK_OUTPUT_DIR/moonlight-window.xwininfo"
grep -Eq 'Absolute upper-left Y:[[:space:]]+0$' "$HOOK_OUTPUT_DIR/moonlight-window.xwininfo"

grep -Fq 'Screencasting with QEMU Display1 D-Bus' "$sunshine_log" || die 'Sunshine did not select QEMU Display1 capture'
grep -Fq 'Found H.264 encoder: libx264 [software]' "$sunshine_log" || die 'Sunshine did not select libx264 software encoding'
grep -Fq 'New streaming session started' "$sunshine_log" || die 'Sunshine did not start a GameStream session'
grep -Fq '[qemu-dbus] QEMU input ready: keyboard=yes mouse=absolute' "$sunshine_log" || die 'Sunshine did not find QEMU absolute input'
grep -Fq '[qemu-dbus] Console.SetUIInfo accepted 1280x720' "$sunshine_log" || die 'Sunshine did not receive the fullscreen size request'
grep -Fq '[qemu-dbus] imported QEMU ScanoutDMABUF with headless EGL CPU readback' "$sunshine_log" ||
  die 'Sunshine did not import native QEMU DMA-BUF frames'
! grep -Fq '[qemu-dbus] listener callback failed:' "$sunshine_log" || die 'Sunshine reported a QEMU listener callback failure'
grep -Eq 'DMA-BUF scanouts/updates/failures: [1-9][0-9]*/[0-9]+/0' "$sunshine_log" ||
  die 'Sunshine did not report nonzero DMA-BUF scanouts with zero failures'
! grep -Eq 'DMA-BUF scanouts/updates/failures: [0-9]+/[0-9]+/[1-9][0-9]*' "$sunshine_log" ||
  die 'Sunshine reported a DMA-BUF readback failure'
grep -Eq '\[qemu-dbus\] input stats: relative_calls=[0-9]+ relative_nonzero=[0-9]+ absolute_calls=[1-9][0-9]* button_calls=[1-9][0-9]* queued_rel=0 queued_abs=[1-9][0-9]* dropped_no_geometry=0' "$sunshine_log" ||
  die 'Sunshine did not route the live Moonlight absolute mouse move/click into QEMU input'
! grep -Eq '\[qemu-dbus\] input stats: .*dropped_no_geometry=[1-9][0-9]*' "$sunshine_log" ||
  die 'Sunshine dropped a live Moonlight mouse event before QEMU geometry was ready'
sunshine_input_stats="$(grep -E '\[qemu-dbus\] input stats: relative_calls=[0-9]+ relative_nonzero=[0-9]+ absolute_calls=[1-9][0-9]* button_calls=[1-9][0-9]* queued_rel=0 queued_abs=[1-9][0-9]* dropped_no_geometry=0' "$sunshine_log" | tail -n1)"

for marker in "$INPUT_WATCH_READY" "$INPUT_KEY_MARKER" "$INPUT_MOUSE_ABS_MARKER" \
              "$INPUT_MOUSE_BUTTON_MARKER" "$INPUT_COMPLETE_MARKER"; do
  grep -Fqx "$marker" "$GUEST_TELEMETRY" || die "guest input marker disappeared: $marker"
done

{
  printf '%s\n' 'QMDP_MOONLIGHT_SUNSHINE_VIRGL_QSF_WAYLAND_HOOK'
  printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'outer_output=%s\n' "$OUTER_OUTPUT_DIR"
  printf 'qemu_pid=%s\n' "$QEMU_PID"
  printf 'qemu_dbus_destination=%s\n' "$DBUS_DESTINATION"
  printf 'sunshine_binary=%s\n' "$SUNSHINE_BINARY"
  printf 'moonlight_binary=%s\n' "$MOONLIGHT_BINARY"
  printf 'host_gui=none (Xvfb is Moonlight-client-only)\n'
  printf 'game_stream=private pairing + HTTPS launch + RTSP + RTP + FFmpeg H.264 decode\n'
  printf 'client_presentation=fullscreen 1280x720@(0,0) on 1280x720 root\n'
  printf 'decoded_client_luma=YAVG:%s YMAX:%s (non-black)\n' "$decoded_yavg" "$decoded_ymax"
  printf 'guest_input=KEY_A + ABS pointer + BTN_LEFT observed through Moonlight/Sunshine/QEMU\n'
  printf 'sunshine_input_stats=%s\n' "$sunshine_input_stats"
  printf 'sunshine_dmabuf=headless EGL CPU readback with nonzero/zero-failure counters\n'
  printf 'moonlight_timeout_status=%s\n' "$moonlight_status"
  printf '\n[guest-input-markers]\n'
  grep -F -e "$INPUT_WATCH_READY" -e "$INPUT_KEY_MARKER" -e "$INPUT_MOUSE_ABS_MARKER" \
    -e "$INPUT_MOUSE_BUTTON_MARKER" -e "$INPUT_COMPLETE_MARKER" "$GUEST_TELEMETRY"
  printf '\n[sunshine-dmabuf-and-input]\n'
  grep -E '\[qemu-dbus\] (QEMU input ready|Console\.SetUIInfo accepted 1280x720|imported QEMU ScanoutDMABUF|DMA-BUF scanouts/updates/failures:|input stats:)' "$sunshine_log"
  printf '%s\n' 'QMDP_MOONLIGHT_SUNSHINE_VIRGL_QSF_WAYLAND_HOOK_OK'
} >"$HOOK_OUTPUT_DIR/trace.txt"

printf 'QMDP_MOONLIGHT_SUNSHINE_VIRGL_QSF_WAYLAND_HOOK_OK output=%s\n' "$HOOK_OUTPUT_DIR"
