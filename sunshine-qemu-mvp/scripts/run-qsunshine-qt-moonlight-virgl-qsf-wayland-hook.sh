#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Full client-side qualification hook for
# run-virgl-qsf-wayland-clipboard-e2e.sh.
#
# It deliberately owns only a disposable client display, Sunshine, a local
# mTLS QSF gateway, and the real Qt-shell runtime driver.  The caller remains
# the sole owner of QEMU, its private D-Bus bus, the guest, qsf-control, and
# the host-local QSF token.  In particular, this hook never reads or prints
# the token and never kills/reconfigures the caller's QEMU process.
#
# The hook is intentionally *not* safe with the outer runner's legacy raw-QSF
# block enabled: both actors would mutate the same guest clipboard, inbox and
# display mode.  The outer runner has an explicit ownership guard; invoke it
# with VIRGL_QSF_WAYLAND_QT_QSF_OWNER=qt.
set -Eeuo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# The default is the deliberately no-X11 Sunshine build.  Xvfb below belongs
# exclusively to the disposable Qt/Moonlight client lane.
SUNSHINE_BINARY="${SUNSHINE_BINARY:-$ROOT/.upstream/build-sunshine-qemu-no-x11/sunshine}"
QSUNSHINE_REAL_E2E_DRIVER="${QSUNSHINE_REAL_E2E_DRIVER:-$ROOT/.build-qt-client/clients/qsunshine-qt/qsunshine-real-e2e-driver}"
# The clean clone is intentionally separate from the older experimental tree.
# A package-installed clean Moonlight is also acceptable when supplied here.
STOCK_MOONLIGHT_QT_BINARY="${QSUNSHINE_STOCK_MOONLIGHT_QT_BINARY:-$ROOT/.upstream/moonlight-qt-clean/app/moonlight}"
QSF_TLS_GATEWAY="$ROOT/extensions/qsf_control/qsf_tls_gateway.py"

RENDER_NODE="${VIRGL_RENDER_NODE:-/dev/dri/renderD128}"
SUNSHINE_PORT="${SUNSHINE_PORT:-48189}"
MOONLIGHT_HOST="${QSUNSHINE_MOONLIGHT_HOST:-127.0.0.1:${SUNSHINE_PORT}}"
MOONLIGHT_PIN="${QSUNSHINE_MOONLIGHT_PIN:-4242}"
SUNSHINE_APP="${QSUNSHINE_SUNSHINE_APP:-Desktop}"
DISPLAY_NUMBER="${QSUNSHINE_QT_MOONLIGHT_DISPLAY:-:96}"
# The pair QML window and the SDL streaming window have different WM_CLASS
# values.  Neither can safely be found via WM_NAME in a no-WM Xvfb server:
# their visible X titles may instead be _NET_WM_NAME. Keep their discovery
# rules deliberately separate and based on the stock client class names.
MOONLIGHT_STREAM_WINDOW_CLASS="${QSUNSHINE_MOONLIGHT_STREAM_WINDOW_CLASS:-^com[.]moonlight_stream[.]Moonlight$}"
MOONLIGHT_PAIR_WINDOW_CLASS="${QSUNSHINE_MOONLIGHT_PAIR_WINDOW_CLASS:-^moonlight$}"
MESA_EGL_VENDOR="${MESA_EGL_VENDOR:-/usr/share/glvnd/egl_vendor.d/50_mesa.json}"

WINDOWED_RESOLUTION="${QSUNSHINE_QT_WINDOWED_RESOLUTION:-1280x800}"
QSF_RESOLUTION="${QSUNSHINE_QT_QSF_RESOLUTION:-1280x720}"
CLIENT_ROOT_WIDTH="${QSUNSHINE_QT_CLIENT_ROOT_WIDTH:-1600}"
CLIENT_ROOT_HEIGHT="${QSUNSHINE_QT_CLIENT_ROOT_HEIGHT:-900}"
# A real fullscreen presentation must occupy the entire disposable client
# output.  Keep its default tied to the Xvfb root instead of requesting an
# arbitrary smaller stream size: unlike a desktop compositor, bare Xvfb has
# no display-mode switcher that would make (say) 1280x720 physically fill a
# 1600x900 root.  Guest-side QSF resize is deliberately independent below.
FULLSCREEN_RESOLUTION="${QSUNSHINE_QT_FULLSCREEN_RESOLUTION:-${CLIENT_ROOT_WIDTH}x${CLIENT_ROOT_HEIGHT}}"
PHASE_TIMEOUT_SECONDS="${QSUNSHINE_QT_E2E_PHASE_TIMEOUT_SECONDS:-170}"
PAIR_DIALOG_TIMEOUT_SECONDS="${QSUNSHINE_QT_E2E_PAIR_DIALOG_TIMEOUT_SECONDS:-35}"
VISUAL_TIMEOUT_SECONDS="${QSUNSHINE_QT_E2E_VISUAL_TIMEOUT_SECONDS:-30}"
GUEST_TIMEOUT_SECONDS="${QSUNSHINE_QT_E2E_GUEST_TIMEOUT_SECONDS:-100}"
# The outer Display1 listener starts before this hook creates Xvfb, Sunshine,
# mTLS material and the Qt driver.  Reserve time for that setup and the final
# evidence/teardown work in addition to the driver's whole-run watchdog.  A
# six-and-a-half-minute listener is deliberately longer than the expected test; it makes
# a slow but valid pairing or first video presentation observable rather than
# cutting the authoritative Display1 trace at the driver's deadline.
MINIMUM_PROBE_DURATION_MS="${QSUNSHINE_QT_E2E_MINIMUM_PROBE_DURATION_MS:-390000}"
PROBE_SETUP_AND_ATTESTATION_BUDGET_MS="${QSUNSHINE_QT_E2E_PROBE_SETUP_AND_ATTESTATION_BUDGET_MS:-90000}"
# The real driver has one whole-run watchdog in addition to phase-local
# waits. Keep it below the live Display1 listener lifetime, otherwise a slow
# but valid Qt/Moonlight phase could be cut off by the driver after the video
# probe has already stopped observing the stream.
DRIVER_TIMEOUT_MS="${QSUNSHINE_QT_E2E_DRIVER_TIMEOUT_MS:-300000}"

# Explicitly supplied only by the small outer-runner branch described below.
# Requiring both values makes an accidental use as the old generic hook fail
# before creating a QSF gateway or changing guest state.
# These names intentionally match the explicit variables supplied by the
# outer VirGL/QSF runner.  They are separate from the user-facing
# QSUNSHINE_* knobs so a generic post-ready invocation cannot accidentally
# claim ownership of the guest's mutable QSF state.
OUTER_QSF_OWNER="${QSF_WAYLAND_QT_E2E_OUTER_QSF_OWNER:-}"
OUTER_RAW_QSF_SKIPPED="${QSF_WAYLAND_QT_E2E_OUTER_RAW_QSF_SKIPPED:-}"
OUTER_LIVE_PROBE_DURATION_MS="${QSF_WAYLAND_QT_E2E_LIVE_PROBE_DURATION_MS:-${VIRGL_QSF_WAYLAND_PROBE_DURATION_MS:-}}"

DBUS_ADDRESS="${QSF_WAYLAND_DBUS_ADDRESS:-}"
DBUS_DESTINATION="${QSF_WAYLAND_DBUS_DESTINATION:-org.qemu}"
OUTER_OUTPUT_DIR="${QSF_WAYLAND_OUTPUT_DIR:-}"
QEMU_PID="${QSF_WAYLAND_QEMU_PID:-}"
CONTROL_SOCKET="${QSF_WAYLAND_CONTROL_SOCKET:-}"
TOKEN_FILE="${QSF_WAYLAND_TOKEN_FILE:-}"
GUEST_TELEMETRY="${QSF_WAYLAND_GUEST_TELEMETRY:-}"
OUTER_SUMMARY=''
OUTER_RECEIVED_CLIPBOARD=''
OUTER_DOWNLOADED_FILE=''
FULLSCREEN_DOWNLOADED_FILE=''

CLIENT_CLIPBOARD="$ROOT/tests/fixtures/qsf-client-clipboard.txt"
GUEST_CLIPBOARD="$ROOT/tests/fixtures/qsf-guest-clipboard.txt"
CLIENT_UPLOAD="$ROOT/tests/fixtures/qsf-client-upload.txt"
GUEST_DOWNLOAD="$ROOT/tests/fixtures/qsf-guest-download.txt"

HOOK_OUTPUT_DIR=''
PHASE_DIR=''
TLS_DIR=''
CLIENT_CONFIG_DIR=''
CLIENT_DATA_DIR=''
CLIENT_CACHE_DIR=''
CLIENT_RUNTIME_DIR=''
MOONLIGHT_PORTABLE_DIR=''
SUNSHINE_CONFIG_DIR=''
SUNSHINE_PIN_FIFO=''
sunshine_log=''
driver_log=''
moonlight_log=''
moonlight_pair_log=''
moonlight_list_log=''
gateway_log=''
xvfb_log=''
gateway_pid=''
sunshine_pid=''
driver_pid=''
xvfb_pid=''
logs_shown=0
handling_error=0

print_outer_integration_patch() {
  # Kept as a discoverable, copyable invocation for operators who previously
  # used the generic post-ready hook. The matching branch now lives in the
  # outer runner, so this is not a patch to apply by hand.
  printf '%s\n' \
    '# Qt/Moonlight composite invocation:' \
    '' \
    '# A clean stock Moonlight Qt binary and Qt driver must already be built.' \
    '# The long live Display1 probe covers pairing, both video presentations,' \
    '# QSF clipboard/files/resize, and the controlled reconnect.' \
    'VIRGL_QSF_WAYLAND_QT_QSF_OWNER=qt \' \
    'VIRGL_QSF_WAYLAND_PROBE_DURATION_MS=390000 \' \
    'VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK="$PWD/scripts/run-qsunshine-qt-moonlight-virgl-qsf-wayland-hook.sh" \' \
    './scripts/run-virgl-qsf-wayland-clipboard-e2e.sh'
}

if [[ "${1:-}" == '--print-outer-integration-patch' ]]; then
  print_outer_integration_patch
  exit 0
fi
if [[ $# -ne 0 ]]; then
  printf 'usage: %s [--print-outer-integration-patch]\n' "${0##*/}" >&2
  exit 2
fi

redacted_tail() {
  local path=$1 lines=${2:-120}
  [[ -n "$path" && -f "$path" ]] || return 0
  # The short-lived test PIN may be rendered by upstream's pair QML; do not
  # reflect it into the outer runner's captured hook log on failure.
  tail -n "$lines" "$path" 2>/dev/null | sed "s/${MOONLIGHT_PIN}/[redacted]/g" >&2 || true
}

show_logs() {
  [[ -n "$HOOK_OUTPUT_DIR" ]] || return 0
  if (( logs_shown )); then
    return 0
  fi
  logs_shown=1
  printf '%s\n' '--- Qt real-E2E driver ---' >&2
  redacted_tail "$driver_log" 180
  printf '%s\n' '--- Sunshine ---' >&2
  redacted_tail "$sunshine_log" 180
  printf '%s\n' '--- QSF TLS gateway ---' >&2
  redacted_tail "$gateway_log" 120
  printf '%s\n' '--- guest telemetry ---' >&2
  redacted_tail "$GUEST_TELEMETRY" 180
}

die() {
  printf 'Qt Moonlight Sunshine VirGL QSF hook: %s\n' "$*" >&2
  show_logs || true
  exit 1
}

on_error() {
  local status=$1 line=$2
  if (( handling_error )); then
    exit "$status"
  fi
  handling_error=1
  printf 'Qt Moonlight Sunshine VirGL QSF hook: unexpected failure status=%s line=%s\n' \
    "$status" "$line" >&2
  show_logs || true
  exit "$status"
}

on_signal() {
  local signal=$1 status=$2
  # INT/TERM handlers must end this hook.  A bare `trap cleanup INT TERM`
  # returns to the interrupted command sequence after killing its children,
  # which can otherwise make a signal look like a later unrelated E2E error.
  trap - ERR EXIT INT TERM
  cleanup
  printf 'Qt Moonlight Sunshine VirGL QSF hook: received %s; cleaned up owned processes\n' \
    "$signal" >&2
  exit "$status"
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
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 0
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid" 2>/dev/null || true
    return 0
  fi
  kill "$pid" 2>/dev/null || true
  # Sunshine flushes its final DMA-BUF counters as capture objects retire.
  # Give that orderly shutdown a real opportunity before the emergency kill;
  # two seconds proved too close to the logging/teardown boundary on a loaded
  # host.
  for _ in $(seq 1 200); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.05
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

stop_process_tree() {
  local pid=${1:-} children_file child
  local -a children=()
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 0
  children_file="/proc/$pid/task/$pid/children"
  if [[ -r "$children_file" ]]; then
    read -r -a children <"$children_file" || true
    for child in "${children[@]}"; do
      [[ "$child" =~ ^[1-9][0-9]*$ ]] || continue
      stop_process_tree "$child"
    done
  fi
  stop_pid "$pid"
}

cleanup() {
  # These are all processes/files created below.  QEMU, qsf-control and its
  # local capability token belong to the outer runner and are never touched.
  # The Qt driver owns a stock Moonlight QProcess.  If the hook itself is
  # signalled, Qt destructors do not necessarily get a chance to terminate
  # that child, so stop its known owned descendant tree before the driver.
  stop_process_tree "$driver_pid"
  stop_pid "$gateway_pid"
  stop_pid "$sunshine_pid"
  stop_pid "$xvfb_pid"
  if [[ -n "$TLS_DIR" ]]; then
    rm -f -- "$TLS_DIR/ca.key" "$TLS_DIR/server.key" "$TLS_DIR/client.key" \
      "$TLS_DIR/server.csr" "$TLS_DIR/client.csr" "$TLS_DIR/ca.srl" 2>/dev/null || true
  fi
  # Moonlight portable state and Sunshine's temporary pairing database live
  # below deliberately fresh roots. Retain sanitized evidence in
  # HOOK_OUTPUT_DIR, not credentials.
  [[ -n "$CLIENT_CONFIG_DIR" ]] && rm -rf -- "$CLIENT_CONFIG_DIR" 2>/dev/null || true
  [[ -n "$CLIENT_DATA_DIR" ]] && rm -rf -- "$CLIENT_DATA_DIR" 2>/dev/null || true
  [[ -n "$CLIENT_CACHE_DIR" ]] && rm -rf -- "$CLIENT_CACHE_DIR" 2>/dev/null || true
  [[ -n "$CLIENT_RUNTIME_DIR" ]] && rm -rf -- "$CLIENT_RUNTIME_DIR" 2>/dev/null || true
  [[ -n "$MOONLIGHT_PORTABLE_DIR" ]] && rm -rf -- "$MOONLIGHT_PORTABLE_DIR" 2>/dev/null || true
  [[ -n "$SUNSHINE_CONFIG_DIR" ]] && rm -rf -- "$SUNSHINE_CONFIG_DIR" 2>/dev/null || true
  [[ -n "$SUNSHINE_PIN_FIFO" ]] && rm -f -- "$SUNSHINE_PIN_FIFO" 2>/dev/null || true
}

qemu_is_alive() {
  kill -0 "$QEMU_PID" 2>/dev/null || die 'the outer runner QEMU exited while the Qt hook was active'
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

driver_is_alive() {
  [[ "$driver_pid" =~ ^[1-9][0-9]*$ ]] || die 'Qt real-E2E driver was never started'
  if ! kill -0 "$driver_pid" 2>/dev/null; then
    wait "$driver_pid" 2>/dev/null || true
    die 'Qt real-E2E driver exited before the expected phase'
  fi
}

wait_for_phase() {
  local phase=$1 label=${2:-$1} iterations=$((PHASE_TIMEOUT_SECONDS * 10))
  for _ in $(seq 1 "$iterations"); do
    [[ -f "$PHASE_DIR/$phase" ]] && return 0
    if [[ -f "$PHASE_DIR/failed" ]]; then
      die "Qt real-E2E driver reported failure while waiting for $label"
    fi
    driver_is_alive
    qemu_is_alive
    sleep 0.1
  done
  die "timed out waiting for Qt driver phase $label"
}

write_phase_request() {
  local phase temporary
  phase=$1
  temporary="$PHASE_DIR/.${phase}.$$"
  [[ ! -e "$PHASE_DIR/$phase" ]] || die "stale or duplicate harness phase request: $phase"
  printf '%s\n' "$phase" >"$temporary"
  mv -f -- "$temporary" "$PHASE_DIR/$phase"
}

wait_for_guest_marker() {
  local marker=$1 label=$2 iterations=$((GUEST_TIMEOUT_SECONDS * 10))
  for _ in $(seq 1 "$iterations"); do
    grep -Fqx "$marker" "$GUEST_TELEMETRY" 2>/dev/null && return 0
    if grep -Fq 'QSF_VIRGL_WAYLAND_GUEST_E2E_FAILED=' "$GUEST_TELEMETRY" 2>/dev/null; then
      die "guest reported failure while waiting for $label"
    fi
    qemu_is_alive
    driver_is_alive
    sleep 0.1
  done
  die "guest did not report $label within ${GUEST_TIMEOUT_SECONDS}s"
}

wait_for_xvfb() {
  for _ in $(seq 1 160); do
    DISPLAY="$DISPLAY_NUMBER" xdpyinfo >/dev/null 2>&1 && return 0
    kill -0 "$xvfb_pid" 2>/dev/null || die 'client-only Xvfb exited early'
    sleep 0.05
  done
  die 'client-only Xvfb did not become ready'
}

wait_for_sunshine_http() {
  for _ in $(seq 1 300); do
    if curl --silent --show-error --fail --max-time 1 \
        "http://127.0.0.1:${SUNSHINE_PORT}/serverinfo?uniqueid=0123456789ABCDEF" \
        >"$HOOK_OUTPUT_DIR/sunshine-serverinfo.xml" 2>/dev/null; then
      return 0
    fi
    kill -0 "$sunshine_pid" 2>/dev/null || die 'Sunshine exited before its HTTP endpoint became ready'
    qemu_is_alive
    sleep 0.05
  done
  die 'Sunshine HTTP endpoint did not become ready'
}

wait_for_sunshine_pair_prompt() {
  # Sunshine's -0 option calls getline() only after Moonlight reaches its
  # getservercert pairing request. Do not pre-fill a short-lived pipe at
  # daemon startup: a closed early writer can leave this later read at EOF.
  for _ in $(seq 1 $((PAIR_DIALOG_TIMEOUT_SECONDS * 10))); do
    grep -Fq 'Please insert pin:' "$sunshine_log" && return 0
    kill -0 "$sunshine_pid" 2>/dev/null || die 'Sunshine exited before requesting its private pairing PIN'
    driver_is_alive
    qemu_is_alive
    sleep 0.1
  done
  die 'Sunshine did not request its private pairing PIN after Moonlight pairing started'
}

wait_for_sunshine_stream() {
  for _ in $(seq 1 $((VISUAL_TIMEOUT_SECONDS * 10))); do
    grep -Fq 'New streaming session started' "$sunshine_log" && return 0
    kill -0 "$sunshine_pid" 2>/dev/null || die 'Sunshine exited before the GameStream session was established'
    driver_is_alive
    qemu_is_alive
    sleep 0.1
  done
  die 'Sunshine did not record a GameStream session after Moonlight video became visible'
}

wait_for_gateway() {
  local ready_line=''
  for _ in $(seq 1 160); do
    ready_line="$(grep -E '^QSF_TLS_GATEWAY_READY host=127[.]0[.]0[.]1 port=[0-9]+$' "$gateway_log" 2>/dev/null | tail -n1 || true)"
    if [[ -n "$ready_line" ]]; then
      QSF_GATEWAY_PORT="${ready_line##*port=}"
      [[ "$QSF_GATEWAY_PORT" =~ ^[1-9][0-9]*$ ]] && (( QSF_GATEWAY_PORT <= 65535 )) && return 0
      die 'QSF TLS gateway announced an invalid ephemeral port'
    fi
    kill -0 "$gateway_pid" 2>/dev/null || die 'QSF TLS gateway exited before readiness'
    qemu_is_alive
    sleep 0.05
  done
  die 'QSF TLS gateway did not announce readiness'
}

with_client_environment() {
  # Force every graphical component of the disposable client lane onto the
  # private Xvfb display.  In particular SDL may otherwise prefer inherited
  # Wayland variables even when Moonlight's Qt front-end was told to use xcb.
  env -u WAYLAND_DISPLAY -u WAYLAND_SOCKET -u XDG_SESSION_TYPE \
    DISPLAY="$DISPLAY_NUMBER" \
    QT_QPA_PLATFORM=xcb \
    XDG_CONFIG_HOME="$CLIENT_CONFIG_DIR" \
    XDG_DATA_HOME="$CLIENT_DATA_DIR" \
    XDG_CACHE_HOME="$CLIENT_CACHE_DIR" \
    XDG_RUNTIME_DIR="$CLIENT_RUNTIME_DIR" \
    QML_DISK_CACHE_PATH="$CLIENT_CACHE_DIR/qmlcache" \
    __EGL_VENDOR_LIBRARY_FILENAMES="$MESA_EGL_VENDOR" \
    LIBGL_ALWAYS_SOFTWARE=1 \
    SDL_VIDEODRIVER=x11 \
    SDL_AUDIODRIVER=dummy \
    "$@"
}

# The real driver is supervised by the hook, so replace its short-lived
# launcher subshell instead of leaving the Qt process as an orphaned child if
# a timeout or a signal reaches cleanup.
exec_with_client_environment() {
  unset WAYLAND_DISPLAY WAYLAND_SOCKET XDG_SESSION_TYPE
  export DISPLAY="$DISPLAY_NUMBER"
  export QT_QPA_PLATFORM=xcb
  export XDG_CONFIG_HOME="$CLIENT_CONFIG_DIR"
  export XDG_DATA_HOME="$CLIENT_DATA_DIR"
  export XDG_CACHE_HOME="$CLIENT_CACHE_DIR"
  export XDG_RUNTIME_DIR="$CLIENT_RUNTIME_DIR"
  export QML_DISK_CACHE_PATH="$CLIENT_CACHE_DIR/qmlcache"
  export __EGL_VENDOR_LIBRARY_FILENAMES="$MESA_EGL_VENDOR"
  export LIBGL_ALWAYS_SOFTWARE=1
  export SDL_VIDEODRIVER=x11
  export SDL_AUDIODRIVER=dummy
  exec "$@"
}

window_geometry() {
  local window dump
  window=$1
  dump="$HOOK_OUTPUT_DIR/window-probe-${window}.xwininfo"
  local x y width height
  DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$window" >"$dump" 2>/dev/null || return 1
  x="$(awk '/Absolute upper-left X:/{print $4; exit}' "$dump")"
  y="$(awk '/Absolute upper-left Y:/{print $4; exit}' "$dump")"
  width="$(awk '/^[[:space:]]*Width:/{print $2; exit}' "$dump")"
  height="$(awk '/^[[:space:]]*Height:/{print $2; exit}' "$dump")"
  [[ "$x" =~ ^-?[0-9]+$ && "$y" =~ ^-?[0-9]+$ && "$width" =~ ^[1-9][0-9]*$ && "$height" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s:%s:%s:%s\n' "$x" "$y" "$width" "$height"
}

find_visible_moonlight_window() {
  local wanted=$1 window geometry x y width height
  local -a windows=()
  mapfile -t windows < <(DISPLAY="$DISPLAY_NUMBER" xdotool search --onlyvisible --class "$MOONLIGHT_STREAM_WINDOW_CLASS" 2>/dev/null || true)
  for window in "${windows[@]}"; do
    [[ "$window" =~ ^[1-9][0-9]*$ ]] || continue
    geometry="$(window_geometry "$window" || true)"
    [[ -n "$geometry" ]] || continue
    IFS=: read -r x y width height <<<"$geometry"
    case "$wanted" in
      any)
        printf '%s\n' "$window"
        return 0
        ;;
      windowed)
        if [[ "$width" == "$WINDOWED_WIDTH" && "$height" == "$WINDOWED_HEIGHT" &&
              ( "$width" != "$CLIENT_ROOT_WIDTH" || "$height" != "$CLIENT_ROOT_HEIGHT" ) ]]; then
          printf '%s\n' "$window"
          return 0
        fi
        ;;
      fullscreen)
        if [[ "$x" == 0 && "$y" == 0 && "$width" == "$CLIENT_ROOT_WIDTH" &&
              "$height" == "$CLIENT_ROOT_HEIGHT" ]]; then
          printf '%s\n' "$window"
          return 0
        fi
        ;;
    esac
  done
  return 1
}

wait_for_moonlight_window() {
  local wanted=$1 label=$2 window=''
  for _ in $(seq 1 $((VISUAL_TIMEOUT_SECONDS * 10))); do
    window="$(find_visible_moonlight_window "$wanted" || true)"
    [[ -n "$window" ]] && { printf '%s\n' "$window"; return 0; }
    driver_is_alive
    qemu_is_alive
    sleep 0.1
  done
  DISPLAY="$DISPLAY_NUMBER" xwininfo -root -tree >"$HOOK_OUTPUT_DIR/${label}-window-tree.xwininfo" 2>&1 || true
  die "stock Moonlight did not create the expected $label X11 window"
}

attest_nonblack_window() {
  local window=$1 label=$2 yavg='' ymax='' candidate='' xwd_candidate=''
  local xwd_file="$HOOK_OUTPUT_DIR/${label}-client.xwd"
  local png_file="$HOOK_OUTPUT_DIR/${label}-client.png"
  local stats_file="$HOOK_OUTPUT_DIR/${label}-client.signalstats"
  for _ in $(seq 1 $((VISUAL_TIMEOUT_SECONDS * 5))); do
    # SDL can destroy and recreate its X11 surface while changing to
    # fullscreen.  Do not retain a stale XID from the initial geometry
    # observation: rediscover the expected visible surface for every capture
    # attempt, and return the XID that actually produced the attestation.
    candidate="$(find_visible_moonlight_window "$label" || true)"
    [[ -n "$candidate" ]] && window="$candidate"
    # xwd truncates its output path before reporting BadWindow.  Capture into
    # an evidence-directory temporary first, so a transient stale XID cannot
    # replace the eventual retained visual artifact with an empty file.
    xwd_candidate="$(mktemp "$HOOK_OUTPUT_DIR/.${label}-client.xwd.XXXXXX")"
    if DISPLAY="$DISPLAY_NUMBER" xwd -silent -id "$window" -out "$xwd_candidate" 2>/dev/null && \
        ffmpeg -hide_banner -loglevel error -y -f xwd_pipe -i "$xwd_candidate" "$png_file" && \
        ffmpeg -hide_banner -loglevel error -i "$png_file" \
          -vf "signalstats,metadata=print:file=$stats_file" -f null -; then
      yavg="$(sed -n 's/^lavfi[.]signalstats[.]YAVG=//p' "$stats_file" | head -n1)"
      ymax="$(sed -n 's/^lavfi[.]signalstats[.]YMAX=//p' "$stats_file" | head -n1)"
      if [[ "$yavg" =~ ^[0-9]+([.][0-9]+)?$ && "$ymax" =~ ^[0-9]+([.][0-9]+)?$ ]] && \
          awk -v average="$yavg" -v maximum="$ymax" 'BEGIN { exit !(average > 20 && maximum > 32) }'; then
        mv -f -- "$xwd_candidate" "$xwd_file"
        printf '%s:%s:%s\n' "$window" "$yavg" "$ymax"
        return 0
      fi
    fi
    rm -f -- "$xwd_candidate"
    xwd_candidate=''
    driver_is_alive
    qemu_is_alive
    sleep 0.2
  done
  die "stock Moonlight $label presentation remained black or never became drawable"
}

record_stream_command() {
  local window=$1 label=$2 expected_mode=$3 expected_resolution=$4
  local process='' command_file="$HOOK_OUTPUT_DIR/${label}-moonlight-command.txt"
  process="$(DISPLAY="$DISPLAY_NUMBER" xdotool getwindowpid "$window" 2>/dev/null || true)"
  [[ "$process" =~ ^[1-9][0-9]*$ && -r "/proc/$process/cmdline" ]] || \
    die "could not resolve the stock Moonlight process for $label presentation"
  tr '\0' '\n' <"/proc/$process/cmdline" >"$command_file"
  grep -Fxq -- 'stream' "$command_file" || die "$label Moonlight child is not a stream command"
  grep -Fxq -- '--display-mode' "$command_file" || die "$label Moonlight command lacks display-mode"
  grep -Fxq -- "$expected_mode" "$command_file" || die "$label Moonlight display mode differs from requested mode"
  grep -Fxq -- '--resolution' "$command_file" || die "$label Moonlight command lacks resolution"
  grep -Fxq -- "$expected_resolution" "$command_file" || die "$label Moonlight resolution differs from requested resolution"
  grep -Fxq -- '--no-quit-after' "$command_file" || die "$label Moonlight command lacks desktop-safe no-quit-after"
  grep -Fxq -- '--absolute-mouse' "$command_file" || die "$label Moonlight command lacks absolute-mouse"
  grep -Fxq -- '--video-decoder' "$command_file" || die "$label Moonlight command lacks the visual-attestation decoder choice"
  grep -Fxq -- 'software' "$command_file" || die "$label Moonlight command does not force software decoding for Xvfb capture"
}

inject_stream_input() {
  local window=$1 label=$2 key=$3 geometry='' x='' y='' width='' height=''
  local first_x='' first_y='' second_x='' second_y=''
  geometry="$(window_geometry "$window" || true)"
  [[ -n "$geometry" ]] || die "could not determine $label Moonlight window geometry for input injection"
  IFS=: read -r x y width height <<<"$geometry"
  # Keep the two absolute samples inside the actual client drawable area.
  # This deliberately avoids a fixed 1100x600 coordinate: overridden small
  # but otherwise valid resolutions must retain the same input proof.
  first_x=$(( width / 8 ))
  first_y=$(( height / 8 ))
  second_x=$(( width - (width / 8) - 1 ))
  second_y=$(( height - (height / 8) - 1 ))
  (( first_x >= 0 && first_y >= 0 && second_x >= first_x && second_y >= first_y )) || \
    die "invalid $label Moonlight geometry for input injection: $geometry"
  DISPLAY="$DISPLAY_NUMBER" xdotool windowfocus "$window"
  sleep 0.1
  DISPLAY="$DISPLAY_NUMBER" xdotool key --window "$window" --clearmodifiers "$key"
  sleep 0.1
  DISPLAY="$DISPLAY_NUMBER" xdotool mousemove --sync --window "$window" "$first_x" "$first_y"
  sleep 0.1
  DISPLAY="$DISPLAY_NUMBER" xdotool mousemove --sync --window "$window" "$second_x" "$second_y"
  sleep 0.1
  DISPLAY="$DISPLAY_NUMBER" xdotool click --window "$window" 1
  printf 'key=%s absolute-pointer=yes button=yes points=%s,%s;%s,%s\n' \
    "$key" "$first_x" "$first_y" "$second_x" "$second_y" >"$HOOK_OUTPUT_DIR/${label}-input-injected.txt"
}

find_pairing_moonlight_window() {
  local window geometry x y width height
  local -a windows=()
  # There is intentionally no window manager on the disposable Xvfb server.
  # Upstream Moonlight's mapped QML surface has an empty WM_NAME in this
  # configuration, while its WM_CLASS is ("moonlight", "Moonlight").  A
  # title lookup finds only Qt's hidden 1x1 helper window and cannot dismiss
  # the real acknowledgement dialog.  Class plus visibility locates the
  # actual mapped application surface without relying on a window manager.
  mapfile -t windows < <(DISPLAY="$DISPLAY_NUMBER" xdotool search --onlyvisible --class "$MOONLIGHT_PAIR_WINDOW_CLASS" 2>/dev/null || true)
  for window in "${windows[@]}"; do
    [[ "$window" =~ ^[1-9][0-9]*$ ]] || continue
    geometry="$(window_geometry "$window" || true)"
    [[ -n "$geometry" ]] || continue
    IFS=: read -r x y width height <<<"$geometry"
    # Prefer the actual Qt pairing view/dialog, never the tiny selection-owner
    # surface created by Qt for clipboard ownership.
    if (( width >= 100 && height >= 100 )); then
      printf '%s\n' "$window"
      return 0
    fi
  done
  return 1
}

dismiss_pair_dialog_until_finished() {
  local window='' sent_return=0
  for _ in $(seq 1 $((PAIR_DIALOG_TIMEOUT_SECONDS * 4))); do
    [[ -f "$PHASE_DIR/pair-process-finished" ]] && return 0
    if [[ -f "$PHASE_DIR/failed" ]]; then
      die 'Qt driver reported pairing failure before its pairing dialog completed'
    fi
    window="$(find_pairing_moonlight_window || true)"
    if [[ -n "$window" ]]; then
      DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$window" >"$HOOK_OUTPUT_DIR/pair-dialog.xwininfo" 2>&1 || true
      # `key --window` does not require a window manager focus operation.
      # Repeated bounded attempts are safe while CliPair.qml transitions from
      # progress to its acknowledgement dialog.
      DISPLAY="$DISPLAY_NUMBER" xdotool key --window "$window" Return 2>/dev/null || true
      sent_return=1
    fi
    driver_is_alive
    qemu_is_alive
    sleep 0.25
  done
  (( sent_return )) || die 'stock Moonlight pairing window never became addressable on Xvfb'
  die 'stock Moonlight pairing dialog did not complete after bounded Return attempts'
}

make_tls_material() {
  TLS_DIR="$HOOK_OUTPUT_DIR/qsf-tls"
  mkdir -p "$TLS_DIR"
  chmod 700 "$TLS_DIR"
  printf '%s\n' \
    'basicConstraints=critical,CA:FALSE' \
    'keyUsage=critical,digitalSignature,keyEncipherment' \
    'extendedKeyUsage=serverAuth' \
    'subjectAltName=DNS:localhost' >"$TLS_DIR/server-extensions.cnf"
  printf '%s\n' \
    'basicConstraints=critical,CA:FALSE' \
    'keyUsage=critical,digitalSignature,keyEncipherment' \
    'extendedKeyUsage=clientAuth' >"$TLS_DIR/client-extensions.cnf"
  openssl req -x509 -newkey rsa:2048 -nodes -days 1 -sha256 \
    -subj '/CN=qsunshine-qt-e2e-ca' \
    -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,keyCertSign' >/dev/null 2>&1
  for identity in server client; do
    local subject="/CN=localhost"
    [[ "$identity" == client ]] && subject='/CN=qsunshine-qt-e2e-client'
    openssl req -newkey rsa:2048 -nodes -subj "$subject" \
      -keyout "$TLS_DIR/${identity}.key" -out "$TLS_DIR/${identity}.csr" >/dev/null 2>&1
    openssl x509 -req -days 1 -sha256 \
      -in "$TLS_DIR/${identity}.csr" \
      -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
      -out "$TLS_DIR/${identity}.crt" \
      -extfile "$TLS_DIR/${identity}-extensions.cnf" >/dev/null 2>&1
  done
  chmod 600 "$TLS_DIR"/*.key "$TLS_DIR"/*.crt "$TLS_DIR"/*.cnf
}

validate_resolution() {
  local resolution=$1 width_name=$2 height_name=$3
  if [[ ! "$resolution" =~ ^([0-9]{2,5})x([0-9]{2,5})$ ]]; then
    die "invalid resolution: $resolution"
  fi
  local width=${BASH_REMATCH[1]} height=${BASH_REMATCH[2]}
  (( width >= 64 && width <= 16384 && height >= 64 && height <= 16384 )) || \
    die "resolution is out of supported bounds: $resolution"
  printf -v "$width_name" '%s' "$width"
  printf -v "$height_name" '%s' "$height"
}

trap 'on_error "$?" "$LINENO"' ERR
trap cleanup EXIT
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

[[ "$OUTER_QSF_OWNER" == qt && "$OUTER_RAW_QSF_SKIPPED" == 1 ]] || die \
  'unsafe outer ownership: invoke this hook through the Qt ownership branch of the outer runner'
[[ -n "$DBUS_ADDRESS" ]] || die 'QSF_WAYLAND_DBUS_ADDRESS is required from the outer runner'
[[ -n "$OUTER_OUTPUT_DIR" && -d "$OUTER_OUTPUT_DIR" ]] || \
  die 'QSF_WAYLAND_OUTPUT_DIR must name the live outer-run evidence directory'
[[ "$QEMU_PID" =~ ^[1-9][0-9]*$ ]] || die 'QSF_WAYLAND_QEMU_PID must be a positive PID'
[[ -S "$CONTROL_SOCKET" ]] || die 'QSF_WAYLAND_CONTROL_SOCKET must be the live local QSF socket'
[[ -f "$TOKEN_FILE" ]] || die 'QSF_WAYLAND_TOKEN_FILE must be the live host-only token file'
[[ -z "$GUEST_TELEMETRY" ]] && GUEST_TELEMETRY="$OUTER_OUTPUT_DIR/guest-telemetry.log"
[[ -f "$GUEST_TELEMETRY" ]] || die 'guest telemetry is absent'

for required in "$SUNSHINE_BINARY" "$QSUNSHINE_REAL_E2E_DRIVER" "$STOCK_MOONLIGHT_QT_BINARY" \
                "$QSF_TLS_GATEWAY" "$CLIENT_CLIPBOARD" "$GUEST_CLIPBOARD" "$CLIENT_UPLOAD" \
                "$GUEST_DOWNLOAD" busctl curl ffmpeg ffprobe openssl python3 Xvfb xdpyinfo \
                xdotool xwd xwininfo timeout awk sed grep head tail tr sha256sum cmp mkdir mktemp \
                chmod mv rm kill sleep seq readlink stat date touch mkfifo; do
  require "$required"
done
[[ -x "$SUNSHINE_BINARY" ]] || die "Sunshine binary is not executable: $SUNSHINE_BINARY"
[[ -x "$QSUNSHINE_REAL_E2E_DRIVER" ]] || die "Qt real-E2E driver is not executable: $QSUNSHINE_REAL_E2E_DRIVER"
[[ -x "$STOCK_MOONLIGHT_QT_BINARY" ]] || die "stock Moonlight Qt binary is not executable: $STOCK_MOONLIGHT_QT_BINARY"
[[ -f "$(dirname -- "$SUNSHINE_BINARY")/assets/apps.json" ]] || die 'Sunshine assets are missing beside the executable'
[[ -f "$MESA_EGL_VENDOR" ]] || die "Mesa EGL vendor file is absent: $MESA_EGL_VENDOR"
[[ -c "$RENDER_NODE" && -r "$RENDER_NODE" && -w "$RENDER_NODE" ]] || \
  die "Sunshine native DMA-BUF import requires readable/writable $RENDER_NODE"
[[ "$SUNSHINE_PORT" =~ ^[1-9][0-9]*$ ]] && (( SUNSHINE_PORT >= 1029 && SUNSHINE_PORT <= 65500 )) || \
  die 'SUNSHINE_PORT is outside Sunshine safe base-port range'
[[ "$MOONLIGHT_PIN" =~ ^[0-9]{4}$ ]] || die 'QSUNSHINE_MOONLIGHT_PIN must have exactly four digits'
[[ "$DISPLAY_NUMBER" =~ ^:[0-9]+$ ]] || die 'QSUNSHINE_QT_MOONLIGHT_DISPLAY must be a private X display such as :96'
[[ "$CLIENT_ROOT_WIDTH" =~ ^[1-9][0-9]*$ && "$CLIENT_ROOT_HEIGHT" =~ ^[1-9][0-9]*$ ]] || \
  die 'client root dimensions must be positive integers'
[[ "$PHASE_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ && "$PAIR_DIALOG_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ &&
   "$VISUAL_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ && "$GUEST_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || \
  die 'Qt hook timeouts must be positive integers'
[[ "$MINIMUM_PROBE_DURATION_MS" =~ ^[1-9][0-9]*$ ]] || die 'minimum probe duration must be positive'
[[ "$PROBE_SETUP_AND_ATTESTATION_BUDGET_MS" =~ ^[1-9][0-9]*$ ]] || die \
  'probe setup and attestation budget must be positive'
[[ "$DRIVER_TIMEOUT_MS" =~ ^[0-9]{5,6}$ ]] && (( DRIVER_TIMEOUT_MS >= 60000 && DRIVER_TIMEOUT_MS <= 900000 )) || \
  die 'Qt real-E2E driver timeout must be 60000..900000 milliseconds'
[[ "$OUTER_LIVE_PROBE_DURATION_MS" =~ ^[1-9][0-9]*$ ]] || die \
  "qt mode requires a live Display1 probe duration >= ${MINIMUM_PROBE_DURATION_MS}"
(( OUTER_LIVE_PROBE_DURATION_MS >= MINIMUM_PROBE_DURATION_MS )) || die \
  "qt mode requires a live Display1 probe duration >= ${MINIMUM_PROBE_DURATION_MS}"
(( OUTER_LIVE_PROBE_DURATION_MS >= DRIVER_TIMEOUT_MS + PROBE_SETUP_AND_ATTESTATION_BUDGET_MS )) || die \
  'live Display1 probe duration must cover the Qt real-E2E driver plus setup and attestation budget'

validate_resolution "$WINDOWED_RESOLUTION" WINDOWED_WIDTH WINDOWED_HEIGHT
validate_resolution "$FULLSCREEN_RESOLUTION" FULLSCREEN_WIDTH FULLSCREEN_HEIGHT
validate_resolution "$QSF_RESOLUTION" QSF_WIDTH QSF_HEIGHT
(( WINDOWED_WIDTH <= CLIENT_ROOT_WIDTH && WINDOWED_HEIGHT <= CLIENT_ROOT_HEIGHT )) || \
  die 'windowed resolution must fit the disposable client Xvfb root'
(( FULLSCREEN_WIDTH == CLIENT_ROOT_WIDTH && FULLSCREEN_HEIGHT == CLIENT_ROOT_HEIGHT )) || \
  die 'fullscreen requested resolution must equal the disposable client Xvfb root'
(( WINDOWED_WIDTH != CLIENT_ROOT_WIDTH || WINDOWED_HEIGHT != CLIENT_ROOT_HEIGHT )) || \
  die 'windowed resolution must differ from Xvfb root so windowed/fullscreen presentation is distinguishable'

embedded_binary="$(readlink -f -- "$ROOT/.upstream/build-moonlight-embedded/moonlight" 2>/dev/null || true)"
stock_binary="$(readlink -f -- "$STOCK_MOONLIGHT_QT_BINARY")"
[[ "$stock_binary" != "$embedded_binary" ]] || die \
  'QSUNSHINE_STOCK_MOONLIGHT_QT_BINARY must name clean upstream Moonlight Qt, not Moonlight Embedded'
STOCK_MOONLIGHT_QT_BINARY="$stock_binary"
stock_source_revision='external-or-unverified'
clean_stock_binary="$(readlink -f -- "$ROOT/.upstream/moonlight-qt-clean/app/moonlight" 2>/dev/null || true)"
if [[ -n "$clean_stock_binary" && "$STOCK_MOONLIGHT_QT_BINARY" == "$clean_stock_binary" ]]; then
  # The default E2E binary comes from the separate clean upstream tree.  A
  # development build can create ignored qmake artifacts there, but no tracked
  # Moonlight source or index change is acceptable for a "stock" assertion.
  require git
  git -C "$ROOT/.upstream/moonlight-qt-clean" rev-parse --is-inside-work-tree >/dev/null \
    || die 'the clean Moonlight Qt source directory is not a Git worktree'
  git -C "$ROOT/.upstream/moonlight-qt-clean" diff --quiet \
    || die 'the clean Moonlight Qt worktree has tracked source modifications'
  git -C "$ROOT/.upstream/moonlight-qt-clean" diff --cached --quiet \
    || die 'the clean Moonlight Qt worktree has staged source modifications'
  stock_source_revision="$(git -C "$ROOT/.upstream/moonlight-qt-clean" rev-parse HEAD)"
fi

qemu_is_alive
wait_for_qemu_destination
if DISPLAY="$DISPLAY_NUMBER" xdpyinfo >/dev/null 2>&1; then
  die "client X display $DISPLAY_NUMBER is already in use; choose a disposable display"
fi
if curl --silent --show-error --fail --max-time 1 \
    "http://127.0.0.1:${SUNSHINE_PORT}/serverinfo?uniqueid=0123456789ABCDEF" \
    >/dev/null 2>&1; then
  die "Sunshine base port $SUNSHINE_PORT already serves an endpoint; choose a private SUNSHINE_PORT"
fi

HOOK_OUTPUT_DIR="$OUTER_OUTPUT_DIR/qsunshine-qt-moonlight-hook"
[[ ! -e "$HOOK_OUTPUT_DIR" ]] || die "Qt hook evidence directory already exists: $HOOK_OUTPUT_DIR"
OUTER_SUMMARY="$OUTER_OUTPUT_DIR/qsunshine-qt-e2e-summary.txt"
OUTER_RECEIVED_CLIPBOARD="$OUTER_OUTPUT_DIR/client-received-clipboard.txt"
OUTER_DOWNLOADED_FILE="$OUTER_OUTPUT_DIR/client-downloaded-guest-file.txt"
[[ ! -e "$OUTER_SUMMARY" && ! -e "$OUTER_RECEIVED_CLIPBOARD" && ! -e "$OUTER_DOWNLOADED_FILE" ]] || die \
  'Qt hook requires fresh outer QSF result paths and summary path'
mkdir -p "$HOOK_OUTPUT_DIR"
chmod 700 "$HOOK_OUTPUT_DIR"
PHASE_DIR="$HOOK_OUTPUT_DIR/phases"
CLIENT_CONFIG_DIR="$HOOK_OUTPUT_DIR/moonlight-xdg-config"
CLIENT_DATA_DIR="$HOOK_OUTPUT_DIR/moonlight-xdg-data"
CLIENT_CACHE_DIR="$HOOK_OUTPUT_DIR/moonlight-xdg-cache"
CLIENT_RUNTIME_DIR="$HOOK_OUTPUT_DIR/moonlight-xdg-runtime"
MOONLIGHT_PORTABLE_DIR="$HOOK_OUTPUT_DIR/moonlight-portable"
SUNSHINE_CONFIG_DIR="$HOOK_OUTPUT_DIR/sunshine-xdg-config"
mkdir -p "$PHASE_DIR" "$CLIENT_CONFIG_DIR" "$CLIENT_DATA_DIR" "$CLIENT_CACHE_DIR" \
  "$CLIENT_RUNTIME_DIR" "$MOONLIGHT_PORTABLE_DIR" "$SUNSHINE_CONFIG_DIR"
chmod 700 "$PHASE_DIR" "$CLIENT_CONFIG_DIR" "$CLIENT_DATA_DIR" "$CLIENT_CACHE_DIR" \
  "$CLIENT_RUNTIME_DIR" "$MOONLIGHT_PORTABLE_DIR" "$SUNSHINE_CONFIG_DIR"
# Sunshine consumes the pairing PIN only after Moonlight reaches its
# getservercert request. Keep a private FIFO open across daemon startup and
# write the value precisely at that request, rather than relying on a closed
# early pipe to remain readable later.
SUNSHINE_PIN_FIFO="$HOOK_OUTPUT_DIR/sunshine-pair-pin.fifo"
mkfifo -m 600 "$SUNSHINE_PIN_FIFO"
exec {sunshine_pin_fd}<>"$SUNSHINE_PIN_FIFO"
# Moonlight Qt uses its current directory as the portable QSettings root when
# this sentinel exists.  Pair, independent list verification, and both stream
# children must therefore share this private cwd; XDG isolation alone is not
# sufficient for the upstream portable path.
touch "$MOONLIGHT_PORTABLE_DIR/portable.dat"
chmod 600 "$MOONLIGHT_PORTABLE_DIR/portable.dat"
sunshine_log="$HOOK_OUTPUT_DIR/sunshine.log"
driver_log="$HOOK_OUTPUT_DIR/qt-real-e2e.log"
moonlight_log="$HOOK_OUTPUT_DIR/moonlight-stream-redacted.log"
moonlight_pair_log="$HOOK_OUTPUT_DIR/moonlight-pair-redacted.log"
moonlight_list_log="$HOOK_OUTPUT_DIR/moonlight-list.log"
gateway_log="$HOOK_OUTPUT_DIR/qsf-tls-gateway.log"
xvfb_log="$HOOK_OUTPUT_DIR/xvfb.log"
FULLSCREEN_DOWNLOADED_FILE="$HOOK_OUTPUT_DIR/client-downloaded-guest-file-fullscreen.txt"

printf 'Qt Moonlight Sunshine VirGL QSF hook evidence=%s\n' "$HOOK_OUTPUT_DIR"

# Xvfb is a disposable client presentation target only.  Sunshine remains
# explicitly display-less and captures QEMU Display1 over the private D-Bus.
__EGL_VENDOR_LIBRARY_FILENAMES="$MESA_EGL_VENDOR" LIBGL_ALWAYS_SOFTWARE=1 \
  Xvfb "$DISPLAY_NUMBER" -screen 0 "${CLIENT_ROOT_WIDTH}x${CLIENT_ROOT_HEIGHT}x24" \
    +extension GLX -nolisten tcp >"$xvfb_log" 2>&1 &
xvfb_pid=$!
wait_for_xvfb

env -u DISPLAY -u WAYLAND_DISPLAY -u WAYLAND_SOCKET -u XDG_SESSION_TYPE \
  -u QT_QPA_PLATFORM -u SDL_VIDEODRIVER -u SDL_AUDIODRIVER \
  "XDG_CONFIG_HOME=$SUNSHINE_CONFIG_DIR" \
  "SUNSHINE_QEMU_DBUS_ADDRESS=$DBUS_ADDRESS" \
  "SUNSHINE_QEMU_DBUS_DESTINATION=$DBUS_DESTINATION" \
  "SUNSHINE_QEMU_DBUS_RENDER_NODE=$RENDER_NODE" \
  "$SUNSHINE_BINARY" -0 \
    capture=qemu_dbus encoder=software stream_audio=false system_tray=false \
    bind_address=127.0.0.1 port="$SUNSHINE_PORT" \
    <"$SUNSHINE_PIN_FIFO" >"$sunshine_log" 2>&1 &
sunshine_pid=$!
wait_for_sunshine_http

make_tls_material
python3 "$QSF_TLS_GATEWAY" \
  --control-socket "$CONTROL_SOCKET" --token-file "$TOKEN_FILE" \
  --server-cert "$TLS_DIR/server.crt" --server-key "$TLS_DIR/server.key" \
  --client-ca "$TLS_DIR/ca.crt" --listen-host 127.0.0.1 --listen-port 0 \
  >"$gateway_log" 2>&1 &
gateway_pid=$!
QSF_GATEWAY_PORT=0
wait_for_gateway

(
  cd -- "$MOONLIGHT_PORTABLE_DIR"
  exec_with_client_environment "$QSUNSHINE_REAL_E2E_DRIVER" \
    --moonlight-binary "$STOCK_MOONLIGHT_QT_BINARY" \
    --host "$MOONLIGHT_HOST" --app "$SUNSHINE_APP" --pair-pin "$MOONLIGHT_PIN" \
    --initial-resolution "$WINDOWED_RESOLUTION" --fullscreen-resolution "$FULLSCREEN_RESOLUTION" \
    --qsf-host 127.0.0.1 --qsf-port "$QSF_GATEWAY_PORT" --qsf-server-name localhost \
    --ca-file "$TLS_DIR/ca.crt" --cert-file "$TLS_DIR/client.crt" --key-file "$TLS_DIR/client.key" \
    --expected-guest-clipboard-file "$GUEST_CLIPBOARD" \
    --client-clipboard-file "$CLIENT_CLIPBOARD" \
    --received-clipboard-destination "$OUTER_RECEIVED_CLIPBOARD" \
    --upload-source "$CLIENT_UPLOAD" --upload-name client-upload.txt \
    --download-name guest-download.txt --download-destination "$OUTER_DOWNLOADED_FILE" \
    --fullscreen-download-destination "$FULLSCREEN_DOWNLOADED_FILE" \
    --expected-download-source "$GUEST_DOWNLOAD" --resize "$QSF_RESOLUTION" \
    --timeout-ms "$DRIVER_TIMEOUT_MS" \
    --phase-dir "$PHASE_DIR" --moonlight-log "$moonlight_log" \
    --moonlight-pair-log "$moonlight_pair_log"
) >"$driver_log" 2>&1 &
driver_pid=$!

wait_for_phase driver-ready 'driver-ready'
wait_for_phase pair-process-started 'stock Moonlight pair process launch'
wait_for_sunshine_pair_prompt
printf '%s\n' "$MOONLIGHT_PIN" >&"$sunshine_pin_fd"
exec {sunshine_pin_fd}>&-
dismiss_pair_dialog_until_finished
wait_for_phase pair-process-finished 'stock Moonlight pair process completion'
# Retain only redacted pairing diagnostics.  The driver does its own redaction,
# but upstream's QML may render a literal test PIN in a log line it forwards.
if [[ -f "$moonlight_pair_log" ]]; then
  sed "s/${MOONLIGHT_PIN}/[redacted]/g" "$moonlight_pair_log" >"$moonlight_pair_log.tmp"
  mv -f -- "$moonlight_pair_log.tmp" "$moonlight_pair_log"
fi
if ! (
  cd -- "$MOONLIGHT_PORTABLE_DIR"
  with_client_environment timeout --signal=TERM --kill-after=5s 35s \
    "$STOCK_MOONLIGHT_QT_BINARY" list -- "$MOONLIGHT_HOST"
) >"$moonlight_list_log" 2>&1; then
  die 'stock Moonlight list failed after the Qt-controlled pairing flow'
fi
grep -Fxq "$SUNSHINE_APP" "$moonlight_list_log" || die \
  'stock Moonlight list did not independently return the Sunshine Desktop application'
write_phase_request pair-verified
wait_for_phase pair-verification-accepted 'pair verification accepted by Qt driver'

wait_for_phase windowed-process-started 'windowed stock Moonlight stream process'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCH_READY' 'guest input watcher readiness'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_VIRGL_READY' 'guest Weston/VirGL readiness'
windowed_window="$(wait_for_moonlight_window windowed windowed)"
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_VIRGL_OK' 'guest VirGL workload start'
wait_for_sunshine_stream
windowed_attestation="$(attest_nonblack_window "$windowed_window" windowed)"
IFS=: read -r windowed_window windowed_yavg windowed_ymax <<<"$windowed_attestation"
[[ "$windowed_window" =~ ^[1-9][0-9]*$ && "$windowed_yavg" =~ ^[0-9]+([.][0-9]+)?$ && \
   "$windowed_ymax" =~ ^[0-9]+([.][0-9]+)?$ ]] || die 'windowed visual attestation returned invalid evidence'
windowed_luma="$windowed_yavg:$windowed_ymax"
DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$windowed_window" >"$HOOK_OUTPUT_DIR/windowed-window.xwininfo"
record_stream_command "$windowed_window" windowed windowed "$WINDOWED_RESOLUTION"
inject_stream_input "$windowed_window" windowed a
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_KEY_A=observed' 'guest KEY_A through stock Moonlight'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_ABS=observed' 'guest absolute pointer through stock Moonlight'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_BTN=observed' 'guest mouse button through stock Moonlight'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_E2E_OK' 'guest complete input evidence'

# This marker is the explicit product boundary: it is written only after a
# real stock-Moonlight video window is drawable and non-black, not merely after
# its QProcess started or an untrusted log string appeared.
write_phase_request activate-windowed
wait_for_phase windowed-activation-accepted 'windowed post-video QSF activation accepted'
wait_for_phase qsf-windowed-ready 'windowed mTLS QSF readiness'
wait_for_phase client-clipboard-sent 'Qt client-first clipboard acknowledgement'
client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_STATE_SHA256=$client_clipboard_hash" \
  'client-first QSF clipboard state'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_WL_PASTE_SHA256=$client_clipboard_hash" \
  'client clipboard visible through guest Wayland'
wait_for_phase guest-clipboard-received 'guest native clipboard delivered to Qt'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_NATIVE_WL_COPY_SHA256=$guest_clipboard_hash" \
  'native guest Wayland clipboard mutation'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_WAYLAND_TO_QSF_STATE_SHA256=$guest_clipboard_hash" \
  'guest Wayland clipboard state published to QSF'
cmp -s "$GUEST_CLIPBOARD" "$OUTER_RECEIVED_CLIPBOARD" || die \
  'retained Qt clipboard result differs from the guest fixture'
wait_for_phase windowed-qsf-operations-complete 'Qt QSF clipboard/file/resize completion'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_UPLOAD_SHA256=$client_upload_hash" \
  'Qt upload received by guest'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_QSF_DOWNLOAD_SHA256=$guest_download_hash" \
  'guest download fixture retained'
cmp -s "$GUEST_DOWNLOAD" "$OUTER_DOWNLOADED_FILE" || die \
  'Qt QSF download differs from guest fixture'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_RESIZE_RECONFIGURE=weston-drm-restart' \
  'guest resize reconfiguration start'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_RESIZE_WESTON_RESTARTED' \
  'guest Weston restart after QSF resize'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_RESIZE=${QSF_RESOLUTION}" \
  'guest QSF resize state'
# Do not reconnect a graphics client into the interval where the guest fixture
# has accepted SetUIInfo but is still retiring/restarting Weston DRM.  The
# driver accepts this request only after the independently observed restart.
write_phase_request begin-fullscreen-reconnect
wait_for_phase fullscreen-reconnect-accepted 'guest-stable fullscreen reconnect accepted by Qt driver'
wait_for_phase qsf-deactivated-for-reconnect 'QSF teardown before controlled Moonlight reconnect'

wait_for_phase fullscreen-process-started 'fullscreen stock Moonlight reconnect process'
fullscreen_window="$(wait_for_moonlight_window fullscreen fullscreen)"
fullscreen_attestation="$(attest_nonblack_window "$fullscreen_window" fullscreen)"
IFS=: read -r fullscreen_window fullscreen_yavg fullscreen_ymax <<<"$fullscreen_attestation"
[[ "$fullscreen_window" =~ ^[1-9][0-9]*$ && "$fullscreen_yavg" =~ ^[0-9]+([.][0-9]+)?$ && \
   "$fullscreen_ymax" =~ ^[0-9]+([.][0-9]+)?$ ]] || die 'fullscreen visual attestation returned invalid evidence'
fullscreen_luma="$fullscreen_yavg:$fullscreen_ymax"
DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$fullscreen_window" >"$HOOK_OUTPUT_DIR/fullscreen-window.xwininfo"
record_stream_command "$fullscreen_window" fullscreen fullscreen "$FULLSCREEN_RESOLUTION"
# KEY_B is a phase barrier in the guest watcher.  The watcher counts its
# pointer and button only after B, so a delayed windowed event cannot satisfy
# the fullscreen-reconnect proof.
inject_stream_input "$fullscreen_window" fullscreen b
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_KEY_B=observed' \
  'guest KEY_B through fullscreen stock Moonlight'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_MOUSE_ABS=observed' \
  'guest fullscreen absolute pointer through stock Moonlight'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_MOUSE_BTN=observed' \
  'guest fullscreen mouse button through stock Moonlight'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_E2E_OK' \
  'guest fullscreen complete input evidence'
write_phase_request activate-fullscreen
wait_for_phase fullscreen-activation-accepted 'fullscreen post-video QSF activation accepted'
wait_for_phase qsf-fullscreen-ready 'fullscreen mTLS QSF readiness'
wait_for_phase fullscreen-download-received 'post-fullscreen Qt QSF download'
cmp -s "$GUEST_DOWNLOAD" "$FULLSCREEN_DOWNLOADED_FILE" || die \
  'post-fullscreen Qt QSF download differs from guest fixture'
wait_for_phase qsf-deactivated-for-stop 'QSF teardown after fullscreen stream stop'
wait_for_phase complete 'Qt real-E2E completion'

if wait "$driver_pid"; then
  driver_status=0
else
  driver_status=$?
fi
driver_pid=''
[[ "$driver_status" == 0 ]] || die "Qt real-E2E driver exited with status $driver_status"
grep -Fqx 'QSUNSHINE_QT_REAL_E2E_OK' "$driver_log" || die 'Qt real-E2E driver did not print success marker'

# Session/DMA-BUF counters are finalized by Sunshine as it tears its capture
# object down. Stop only this hook-owned server now, before reading the trace;
# the outer runner still owns QEMU, Display1 and QSF control exclusively.
stop_pid "$sunshine_pid"
sunshine_pid=''

grep -Fq 'Screencasting with QEMU Display1 D-Bus' "$sunshine_log" || die \
  'Sunshine did not select the QEMU Display1 capture backend'
grep -Fq 'New streaming session started' "$sunshine_log" || die \
  'Sunshine did not record a GameStream session'
grep -Fq '[qemu-dbus] QEMU input ready: keyboard=yes mouse=absolute' "$sunshine_log" || die \
  'Sunshine did not discover QEMU absolute input'
grep -Fq '[qemu-dbus] imported QEMU ScanoutDMABUF' "$sunshine_log" || die \
  'Sunshine did not import a native QEMU DMA-BUF scanout'
! grep -Fq '[qemu-dbus] listener callback failed:' "$sunshine_log" || die \
  'Sunshine reported a QEMU listener callback failure'
grep -Eq 'DMA-BUF scanouts/updates/failures: [1-9][0-9]*/[0-9]+/0' "$sunshine_log" || die \
  'Sunshine did not report nonzero zero-failure DMA-BUF scanouts'
[[ -s "$HOOK_OUTPUT_DIR/windowed-client.png" && -s "$HOOK_OUTPUT_DIR/fullscreen-client.png" ]] || die \
  'retained stock Moonlight client screenshots are absent'
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
  "$HOOK_OUTPUT_DIR/windowed-client.png" >"$HOOK_OUTPUT_DIR/windowed-client.ffprobe"
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
  "$HOOK_OUTPUT_DIR/fullscreen-client.png" >"$HOOK_OUTPUT_DIR/fullscreen-client.ffprobe"
grep -Fqx 'codec_name=png' "$HOOK_OUTPUT_DIR/windowed-client.ffprobe"
grep -Fqx "width=$WINDOWED_WIDTH" "$HOOK_OUTPUT_DIR/windowed-client.ffprobe"
grep -Fqx "height=$WINDOWED_HEIGHT" "$HOOK_OUTPUT_DIR/windowed-client.ffprobe"
grep -Fqx 'codec_name=png' "$HOOK_OUTPUT_DIR/fullscreen-client.ffprobe"
grep -Fqx "width=$CLIENT_ROOT_WIDTH" "$HOOK_OUTPUT_DIR/fullscreen-client.ffprobe"
grep -Fqx "height=$CLIENT_ROOT_HEIGHT" "$HOOK_OUTPUT_DIR/fullscreen-client.ffprobe"

stock_version="$({
  cd -- "$MOONLIGHT_PORTABLE_DIR"
  with_client_environment timeout --signal=TERM --kill-after=2s 5s \
    "$STOCK_MOONLIGHT_QT_BINARY" --version 2>&1 || true
} | head -n1)"
stock_binary_sha256="$(sha256sum "$STOCK_MOONLIGHT_QT_BINARY" | awk '{print $1}')"
{
  printf '%s\n' 'QSUNSHINE_QT_MOONLIGHT_VIRGL_QSF_HOOK'
  printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'outer_output=%s\n' "$OUTER_OUTPUT_DIR"
  printf 'qemu_pid=%s\n' "$QEMU_PID"
  printf 'qemu_dbus_destination=%s\n' "$DBUS_DESTINATION"
  printf 'sunshine_binary=%s\n' "$SUNSHINE_BINARY"
  printf 'stock_moonlight_binary=%s\n' "$STOCK_MOONLIGHT_QT_BINARY"
  printf 'stock_moonlight_version=%s\n' "$stock_version"
  printf 'stock_moonlight_sha256=%s\n' "$stock_binary_sha256"
  printf 'stock_moonlight_source_revision=%s\n' "$stock_source_revision"
  printf 'qt_driver=%s\n' "$QSUNSHINE_REAL_E2E_DRIVER"
  printf 'host_gui=none (Xvfb is Qt/Moonlight-client-only)\n'
  printf 'qsf_transport=TLS1.3 mTLS loopback gateway; local broker token not exported\n'
  printf 'pairing=Qt MoonlightController pair + stock Moonlight list Desktop verification\n'
  printf 'windowed_presentation=%sx%s luma=YAVG:%s YMAX:%s\n' \
    "$WINDOWED_WIDTH" "$WINDOWED_HEIGHT" "${windowed_luma%%:*}" "${windowed_luma##*:}"
  printf 'fullscreen_presentation=%sx%s@(0,0) luma=YAVG:%s YMAX:%s\n' \
    "$CLIENT_ROOT_WIDTH" "$CLIENT_ROOT_HEIGHT" "${fullscreen_luma%%:*}" "${fullscreen_luma##*:}"
  printf 'controlled_reconnect=windowed->fullscreen only after guest Weston DRM restart; QSF deactivated before reconnect\n'
  printf 'input=KEY_A + absolute pointer + button observed by guest evdev in windowed phase; KEY_B + fresh absolute pointer + button observed after fullscreen reconnect\n'
  printf 'clipboard=Qt client-first then independent guest native Wayland fixture\n'
  printf 'file_transfer=Qt upload+download fixtures verified byte-for-byte; download repeated after fullscreen reconnect\n'
  printf 'resize=Qt QSF %s + guest Weston DRM restart + QEMU SetUIInfo acknowledgement\n' "$QSF_RESOLUTION"
  printf 'client_clipboard_sha256=%s\n' "$client_clipboard_hash"
  printf 'guest_clipboard_sha256=%s\n' "$guest_clipboard_hash"
  printf 'upload_sha256=%s\n' "$client_upload_hash"
  printf 'download_sha256=%s\n' "$guest_download_hash"
  printf '\n[Qt phases]\n'
  for phase in driver-ready pair-process-started pair-process-finished pair-verified \
      pair-verification-accepted windowed-process-started windowed-activation-accepted \
      qsf-windowed-ready client-clipboard-sent guest-clipboard-received \
      windowed-qsf-operations-complete fullscreen-reconnect-accepted \
      qsf-deactivated-for-reconnect fullscreen-process-started \
      fullscreen-activation-accepted qsf-fullscreen-ready \
      fullscreen-download-received qsf-deactivated-for-stop complete; do
    printf '%s\n' "$phase"
  done
  printf '\n[guest evidence]\n'
  grep -F -e "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_STATE_SHA256=$client_clipboard_hash" \
    -e "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_WL_PASTE_SHA256=$client_clipboard_hash" \
    -e "QSF_VIRGL_WAYLAND_GUEST_NATIVE_WL_COPY_SHA256=$guest_clipboard_hash" \
    -e "QSF_VIRGL_WAYLAND_GUEST_WAYLAND_TO_QSF_STATE_SHA256=$guest_clipboard_hash" \
    -e "QSF_VIRGL_WAYLAND_GUEST_UPLOAD_SHA256=$client_upload_hash" \
    -e "QSF_VIRGL_WAYLAND_GUEST_QSF_DOWNLOAD_SHA256=$guest_download_hash" \
    -e "QSF_VIRGL_WAYLAND_GUEST_RESIZE=$QSF_RESOLUTION" \
    -e 'QSF_VIRGL_WAYLAND_GUEST_RESIZE_WESTON_RESTARTED' \
    -e 'QSF_VIRGL_WAYLAND_GUEST_INPUT_E2E_OK' \
    -e 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_E2E_OK' "$GUEST_TELEMETRY"
  printf '\n[Sunshine]\n'
  grep -E '\[qemu-dbus\] (QEMU input ready|imported QEMU ScanoutDMABUF|DMA-BUF scanouts/updates/failures:)' "$sunshine_log"
  printf '%s\n' 'QSUNSHINE_QT_MOONLIGHT_VIRGL_QSF_HOOK_OK'
} >"$HOOK_OUTPUT_DIR/trace.txt"

# Emit the fixed outer handoff only after every local Qt, guest, visual and
# Sunshine assertion above has passed.  The temporary is in the same fresh
# outer evidence directory, so mv gives the patched outer runner an atomic
# success boundary without exposing a token, PIN, or private key.
summary_temporary="$(mktemp "$OUTER_OUTPUT_DIR/.qsunshine-qt-e2e-summary.XXXXXX")"
{
  printf '%s\n' 'QSUNSHINE_QT_E2E_SUMMARY_VERSION=1'
  printf '%s\n' 'QSUNSHINE_QT_QSF_OPERATIONS_OK=1'
  printf 'QSUNSHINE_QT_QSF_CLIENT_CLIPBOARD_SHA256=%s\n' "$client_clipboard_hash"
  printf 'QSUNSHINE_QT_QSF_GUEST_CLIPBOARD_SHA256=%s\n' "$guest_clipboard_hash"
  printf 'QSUNSHINE_QT_QSF_UPLOAD_SHA256=%s\n' "$client_upload_hash"
  printf 'QSUNSHINE_QT_QSF_DOWNLOAD_SHA256=%s\n' "$guest_download_hash"
  printf 'QSUNSHINE_QT_QSF_RESIZE=%s\n' "$QSF_RESOLUTION"
  printf '%s\n' 'QSUNSHINE_QT_QSF_QEMU_SET_UI_INFO=applied'
  printf '%s\n' 'QSUNSHINE_QT_MOONLIGHT_WINDOWED_AND_FULLSCREEN_OK=1'
  printf '%s\n' 'QSUNSHINE_QT_MOONLIGHT_INPUT_E2E_OK=1'
  printf '%s\n' 'QSUNSHINE_QT_MOONLIGHT_FULLSCREEN_INPUT_E2E_OK=1'
  printf '%s\n' 'QSUNSHINE_QT_QSF_FULLSCREEN_DOWNLOAD_OK=1'
} >"$summary_temporary"
mv -f -- "$summary_temporary" "$OUTER_SUMMARY"

printf 'QSUNSHINE_QT_MOONLIGHT_VIRGL_QSF_HOOK_OK output=%s\n' "$HOOK_OUTPUT_DIR"
