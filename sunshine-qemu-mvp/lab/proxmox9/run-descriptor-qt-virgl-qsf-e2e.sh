#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Lab-only, real acceptance lane:
# PVE browser Console menu -> one-use .qsm -> Qt driver -> patched Moonlight
# in client-only Xvfb -> q-sunshine terminal service -> Sunshine/QEMU/VirGL
# -> QSF clipboard, files and guest profile transitions.
#
# No PVE password, cookie, VNC ticket, media/QSF endpoint, CA or lease route
# is given to the Qt driver.  It accepts only the real browser-downloaded .qsm.
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_DIR="$ROOT_DIR/lab/proxmox9"
LAB_RUNNER="$LAB_DIR/run-lab.sh"
BROWSER_GATE="$LAB_DIR/qualify-pve-console-launch.cjs"

PVE_URL="${QSUNSHINE_PVE_E2E_PVE_URL:-https://127.0.0.1:18006}"
PVE_USER="${QSUNSHINE_PVE_E2E_USER:-qsmconsole@pve}"
PVE_PASSWORD_FILE="${QSUNSHINE_PVE_E2E_PASSWORD_FILE:-$LAB_DIR/runtime/pve-console-user.password}"
PVE_NODE="${QSUNSHINE_PVE_E2E_NODE:-qsm-pve9-lab}"
VMID="${QSUNSHINE_PVE_E2E_VMID:-100}"

REAL_E2E_DRIVER="${QSUNSHINE_REAL_E2E_DRIVER:-$ROOT_DIR/.build-qt-client/clients/qsunshine-qt/qsunshine-real-e2e-driver}"
# Despite its historical directory name, this candidate must pass the binary
# and CLI checks below; a stock Moonlight binary fails closed.
PATCHED_MOONLIGHT_BINARY="${QSUNSHINE_PATCHED_MOONLIGHT_BINARY:-$ROOT_DIR/.upstream/moonlight-qt-clean/app/moonlight}"

OUTPUT_PARENT="${QSUNSHINE_PVE_E2E_OUTPUT_DIR:-$LAB_DIR/e2e-runs}"
DISPLAY_NUMBER="${QSUNSHINE_PVE_E2E_DISPLAY:-:97}"
MOONLIGHT_STREAM_WINDOW_CLASS="${QSUNSHINE_MOONLIGHT_STREAM_WINDOW_CLASS:-^com[.]moonlight_stream[.]Moonlight$}"

WINDOWED_RESOLUTION="${QSUNSHINE_PVE_E2E_WINDOWED_RESOLUTION:-1280x800}"
NEGOTIATED_RESOLUTION="${QSUNSHINE_PVE_E2E_NEGOTIATED_RESOLUTION:-1280x720}"
FULLSCREEN_RESOLUTION="${QSUNSHINE_PVE_E2E_FULLSCREEN_RESOLUTION:-1600x900}"
CLIENT_ROOT_WIDTH="${QSUNSHINE_PVE_E2E_CLIENT_ROOT_WIDTH:-1600}"
CLIENT_ROOT_HEIGHT="${QSUNSHINE_PVE_E2E_CLIENT_ROOT_HEIGHT:-900}"
NEGOTIATED_FPS="${QSUNSHINE_PVE_E2E_NEGOTIATED_FPS:-60}"
NEGOTIATED_BITRATE_KBPS="${QSUNSHINE_PVE_E2E_NEGOTIATED_BITRATE_KBPS:-8000}"
NEGOTIATED_VIDEO_CODEC="${QSUNSHINE_PVE_E2E_NEGOTIATED_VIDEO_CODEC:-H.264}"
FULLSCREEN_FPS="${QSUNSHINE_PVE_E2E_FULLSCREEN_FPS:-60}"
FULLSCREEN_BITRATE_KBPS="${QSUNSHINE_PVE_E2E_FULLSCREEN_BITRATE_KBPS:-12000}"
FULLSCREEN_VIDEO_CODEC="${QSUNSHINE_PVE_E2E_FULLSCREEN_VIDEO_CODEC:-H.264}"

# Assertions about the fixed forwarded lab topology only. These values are not
# direct client arguments; descriptor redemption remains the route authority.
EXPECTED_MEDIA_HOST="${QSUNSHINE_PVE_E2E_MEDIA_HOST:-127.0.0.1}"
EXPECTED_MEDIA_PORT="${QSUNSHINE_PVE_E2E_MEDIA_PORT:-47989}"

BROWSER_TIMEOUT_MS="${QSUNSHINE_PVE_E2E_BROWSER_TIMEOUT_MS:-60000}"
DRIVER_TIMEOUT_MS="${QSUNSHINE_PVE_E2E_DRIVER_TIMEOUT_MS:-300000}"
PHASE_TIMEOUT_SECONDS="${QSUNSHINE_PVE_E2E_PHASE_TIMEOUT_SECONDS:-170}"
VISUAL_TIMEOUT_SECONDS="${QSUNSHINE_PVE_E2E_VISUAL_TIMEOUT_SECONDS:-30}"
GUEST_TIMEOUT_SECONDS="${QSUNSHINE_PVE_E2E_GUEST_TIMEOUT_SECONDS:-120}"
GUEST_BOOT_TIMEOUT_SECONDS="${QSUNSHINE_PVE_E2E_GUEST_BOOT_TIMEOUT_SECONDS:-300}"
# The guest fixture is intentionally causal/one-shot. A fresh reboot captures
# bootstrap VirGL evidence and avoids treating stale clipboard/file marks as new.
FRESH_BOOT="${QSUNSHINE_PVE_E2E_FRESH_BOOT:-1}"
BROWSER_HEADED="${QSUNSHINE_PVE_E2E_BROWSER_HEADED:-0}"
IGNORE_HTTPS_ERRORS="${QSUNSHINE_PVE_E2E_IGNORE_HTTPS_ERRORS:-1}"

CLIENT_CLIPBOARD="$ROOT_DIR/tests/fixtures/qsf-client-clipboard.txt"
GUEST_CLIPBOARD="$ROOT_DIR/tests/fixtures/qsf-guest-clipboard.txt"
CLIENT_UPLOAD="$ROOT_DIR/tests/fixtures/qsf-client-upload.txt"
GUEST_DOWNLOAD="$ROOT_DIR/tests/fixtures/qsf-guest-download.txt"

RUN_DIR=''
DOWNLOAD_DIR=''
PHASE_DIR=''
CLIENT_CONFIG_DIR=''
CLIENT_DATA_DIR=''
CLIENT_CACHE_DIR=''
CLIENT_RUNTIME_DIR=''
CLIENT_PORTABLE_DIR=''
BROWSER_LOG=''
DRIVER_LOG=''
MOONLIGHT_LOG=''
GUEST_TELEMETRY_LOG=''
GUEST_CAPTURE_ERR=''
REMOTE_PREFLIGHT_LOG=''
REMOTE_WORKER_LOG=''
REMOTE_PORT_SURFACE_LOG=''
REMOTE_FINAL_LOG=''
REMOTE_JOURNAL_SINCE=''
xvfb_pid=''
driver_pid=''
telemetry_capture_pid=''
handling_error=0

usage() {
    cat <<'EOF'
Usage:
  ./lab/proxmox9/run-descriptor-qt-virgl-qsf-e2e.sh [options]

Runs the real PVE browser -> .qsm -> Qt/Moonlight -> terminal-service E2E
qualification. VM 100 is rebooted by default so the disposable guest fixture
starts cleanly.

Options:
  --pve-url URL
  --user USER@REALM
  --password-file FILE
  --vmid ID
  --moonlight-binary FILE
  --driver FILE
  --display :N
  --output-parent DIRECTORY
  --browser-timeout-ms N
  --driver-timeout-ms N
  --headed
  --trusted-https
  --fresh-boot | --no-fresh-boot
  -h, --help

The browser alone reads the PVE password from its owner-private file. The Qt
driver receives only the downloaded launch descriptor and no PVE credentials.
EOF
}

die() {
    printf 'PVE descriptor Qt/VirGL/QSF E2E: %s\n' "$*" >&2
    [[ -z "$RUN_DIR" ]] || printf 'PVE descriptor Qt/VirGL/QSF E2E: evidence=%s\n' "$RUN_DIR" >&2
    exit 1
}

on_error() {
    local status=$1 line=$2
    if ((handling_error)); then
        exit "$status"
    fi
    handling_error=1
    printf 'PVE descriptor Qt/VirGL/QSF E2E: unexpected failure status=%s line=%s\n' \
        "$status" "$line" >&2
    [[ -z "$RUN_DIR" ]] || printf 'PVE descriptor Qt/VirGL/QSF E2E: evidence=%s\n' "$RUN_DIR" >&2
    exit "$status"
}

stop_owned_pid() {
    local pid=${1:-} attempt
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 0
    if kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        for attempt in $(seq 1 50); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
        done
    fi
    wait "$pid" 2>/dev/null || true
}

cleanup() {
    # Only direct child processes created by this runner are signalled.
    set +e
    stop_owned_pid "$driver_pid"
    stop_owned_pid "$telemetry_capture_pid"
    stop_owned_pid "$xvfb_pid"
}

on_signal() {
    local signal=$1 status=$2
    trap - ERR EXIT INT TERM
    cleanup
    printf 'PVE descriptor Qt/VirGL/QSF E2E: received %s; owned client processes stopped\n' "$signal" >&2
    exit "$status"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

require_file() {
    [[ -f "$1" && ! -L "$1" ]] || die "missing required regular file: $1"
}

in_range() {
    local value=$1 lower=$2 upper=$3
    [[ "$value" =~ ^[1-9][0-9]*$ ]] && ((value >= lower && value <= upper))
}

validate_resolution() {
    local resolution=$1 label=$2
    [[ "$resolution" =~ ^([0-9]{2,5})x([0-9]{2,5})$ ]] || die "invalid $label resolution: $resolution"
    ((10#${BASH_REMATCH[1]} >= 64 && 10#${BASH_REMATCH[1]} <= 16384 &&
       10#${BASH_REMATCH[2]} >= 64 && 10#${BASH_REMATCH[2]} <= 16384)) ||
        die "$label resolution is outside 64..16384: $resolution"
}

assert_remote_vm_running() {
    "$LAB_RUNNER" ssh "qm status $VMID | grep -Fx 'status: running' >/dev/null && systemctl is-active --quiet q-sunshine-terminal.service" \
        >/dev/null 2>&1 || die 'nested VM or q-sunshine-terminal.service stopped during E2E'
}

driver_is_alive() {
    [[ "$driver_pid" =~ ^[1-9][0-9]*$ ]] || die 'Qt real-E2E driver was never started'
    if ! kill -0 "$driver_pid" 2>/dev/null; then
        wait "$driver_pid" 2>/dev/null || true
        die 'Qt real-E2E driver exited before the expected phase'
    fi
}

guest_failed() {
    grep -Fq 'QSF_VIRGL_WAYLAND_GUEST_E2E_FAILED=' "$GUEST_TELEMETRY_LOG" 2>/dev/null
}

wait_for_guest_initial_marker() {
    local marker=$1 label=$2 attempt
    for attempt in $(seq 1 $((GUEST_BOOT_TIMEOUT_SECONDS * 5))); do
        grep -Fqx "$marker" "$GUEST_TELEMETRY_LOG" 2>/dev/null && return 0
        guest_failed && die "guest reported failure while waiting for $label"
        ((attempt % 25 != 0)) || assert_remote_vm_running
        sleep 0.2
    done
    die "guest did not report $label during fresh boot"
}

# Bootstrap evidence is emitted before the descriptor has been downloaded and
# before the Qt driver starts. Keep this path distinct from data-plane waits:
# checking driver liveness here would create an impossible dependency cycle.
wait_for_guest_initial_pattern() {
    local pattern=$1 label=$2 attempt
    for attempt in $(seq 1 $((GUEST_BOOT_TIMEOUT_SECONDS * 5))); do
        grep -Eq "$pattern" "$GUEST_TELEMETRY_LOG" 2>/dev/null && return 0
        guest_failed && die "guest reported failure while waiting for $label"
        ((attempt % 25 != 0)) || assert_remote_vm_running
        sleep 0.2
    done
    die "guest did not report $label during fresh boot"
}

wait_for_guest_marker() {
    local marker=$1 label=$2 attempt
    for attempt in $(seq 1 $((GUEST_TIMEOUT_SECONDS * 10))); do
        grep -Fqx "$marker" "$GUEST_TELEMETRY_LOG" 2>/dev/null && return 0
        guest_failed && die "guest reported failure while waiting for $label"
        driver_is_alive
        ((attempt % 30 != 0)) || assert_remote_vm_running
        sleep 0.1
    done
    die "guest did not report $label within ${GUEST_TIMEOUT_SECONDS}s"
}

wait_for_guest_pattern() {
    local pattern=$1 label=$2 attempt
    for attempt in $(seq 1 $((GUEST_TIMEOUT_SECONDS * 10))); do
        grep -Eq "$pattern" "$GUEST_TELEMETRY_LOG" 2>/dev/null && return 0
        guest_failed && die "guest reported failure while waiting for $label"
        driver_is_alive
        ((attempt % 30 != 0)) || assert_remote_vm_running
        sleep 0.1
    done
    die "guest did not report $label within ${GUEST_TIMEOUT_SECONDS}s"
}

wait_for_phase() {
    local phase=$1 label=${2:-$1} attempt
    for attempt in $(seq 1 $((PHASE_TIMEOUT_SECONDS * 10))); do
        [[ -f "$PHASE_DIR/$phase" ]] && return 0
        [[ -f "$PHASE_DIR/failed" ]] && die "Qt driver reported failure while waiting for $label"
        driver_is_alive
        ((attempt % 30 != 0)) || assert_remote_vm_running
        sleep 0.1
    done
    die "timed out waiting for Qt driver phase $label"
}

write_phase_request() {
    local phase=$1 temporary
    [[ "$phase" =~ ^[a-z0-9-]+$ ]] || die 'invalid internal phase request'
    [[ ! -e "$PHASE_DIR/$phase" ]] || die "duplicate harness request: $phase"
    temporary="$(mktemp "$PHASE_DIR/.${phase}.XXXXXX")"
    printf '%s\n' "$phase" >"$temporary"
    mv -- "$temporary" "$PHASE_DIR/$phase"
}

wait_for_xvfb() {
    local attempt
    for attempt in $(seq 1 160); do
        DISPLAY="$DISPLAY_NUMBER" xdpyinfo >/dev/null 2>&1 && return 0
        kill -0 "$xvfb_pid" 2>/dev/null || die 'client-only Xvfb exited early'
        sleep 0.05
    done
    die 'client-only Xvfb did not become ready'
}

with_client_environment() {
    env -u WAYLAND_DISPLAY -u WAYLAND_SOCKET -u XDG_SESSION_TYPE \
        DISPLAY="$DISPLAY_NUMBER" QT_QPA_PLATFORM=xcb \
        XDG_CONFIG_HOME="$CLIENT_CONFIG_DIR" XDG_DATA_HOME="$CLIENT_DATA_DIR" \
        XDG_CACHE_HOME="$CLIENT_CACHE_DIR" XDG_RUNTIME_DIR="$CLIENT_RUNTIME_DIR" \
        QML_DISK_CACHE_PATH="$CLIENT_CACHE_DIR/qmlcache" \
        LIBGL_ALWAYS_SOFTWARE=1 SDL_VIDEODRIVER=x11 SDL_AUDIODRIVER=dummy "$@"
}

window_geometry() {
    local window=$1 output x y width height
    output="$(DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$window" 2>/dev/null)" || return 1
    x="$(awk '/Absolute upper-left X:/{print $4; exit}' <<<"$output")"
    y="$(awk '/Absolute upper-left Y:/{print $4; exit}' <<<"$output")"
    width="$(awk '/^[[:space:]]*Width:/{print $2; exit}' <<<"$output")"
    height="$(awk '/^[[:space:]]*Height:/{print $2; exit}' <<<"$output")"
    [[ "$x" =~ ^-?[0-9]+$ && "$y" =~ ^-?[0-9]+$ &&
       "$width" =~ ^[1-9][0-9]*$ && "$height" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '%s:%s:%s:%s\n' "$x" "$y" "$width" "$height"
}

find_moonlight_window() {
    local wanted=$1 window geometry x y width height
    local -a windows=()
    mapfile -t windows < <(DISPLAY="$DISPLAY_NUMBER" xdotool search --onlyvisible --class "$MOONLIGHT_STREAM_WINDOW_CLASS" 2>/dev/null || true)
    for window in "${windows[@]}"; do
        [[ "$window" =~ ^[1-9][0-9]*$ ]] || continue
        geometry="$(window_geometry "$window" || true)"
        [[ -n "$geometry" ]] || continue
        IFS=: read -r x y width height <<<"$geometry"
        case "$wanted" in
            windowed)
                [[ "$width" == "$WINDOWED_WIDTH" && "$height" == "$WINDOWED_HEIGHT" &&
                   ( "$width" != "$CLIENT_ROOT_WIDTH" || "$height" != "$CLIENT_ROOT_HEIGHT" ) ]] || continue ;;
            negotiated)
                [[ "$width" == "$NEGOTIATED_WIDTH" && "$height" == "$NEGOTIATED_HEIGHT" &&
                   ( "$width" != "$CLIENT_ROOT_WIDTH" || "$height" != "$CLIENT_ROOT_HEIGHT" ) ]] || continue ;;
            fullscreen)
                [[ "$x" == 0 && "$y" == 0 && "$width" == "$CLIENT_ROOT_WIDTH" &&
                   "$height" == "$CLIENT_ROOT_HEIGHT" ]] || continue ;;
            *) die 'invalid presentation selector' ;;
        esac
        printf '%s\n' "$window"
        return 0
    done
    return 1
}

wait_for_moonlight_window() {
    local wanted=$1 label=$2 attempt window=''
    for attempt in $(seq 1 $((VISUAL_TIMEOUT_SECONDS * 10))); do
        window="$(find_moonlight_window "$wanted" || true)"
        [[ -z "$window" ]] || { printf '%s\n' "$window"; return 0; }
        driver_is_alive
        sleep 0.1
    done
    DISPLAY="$DISPLAY_NUMBER" xwininfo -root -tree >"$RUN_DIR/${label}-window-tree.xwininfo" 2>&1 || true
    die "patched Moonlight did not create a valid $label window"
}

attest_nonblack_window() {
    local window=$1 label=$2 attempt candidate xwd png stats yavg ymax
    local retained_xwd="$RUN_DIR/${label}-client.xwd"
    local retained_png="$RUN_DIR/${label}-client.png"
    local retained_stats="$RUN_DIR/${label}-client.signalstats"
    for attempt in $(seq 1 $((VISUAL_TIMEOUT_SECONDS * 5))); do
        candidate="$(find_moonlight_window "$label" || true)"
        [[ -z "$candidate" ]] || window="$candidate"
        # Failed probes have distinct retained names, avoiding any destructive
        # cleanup or replacement of the final visual evidence.
        xwd="$RUN_DIR/.${label}-${attempt}.xwd"
        png="$RUN_DIR/.${label}-${attempt}.png"
        stats="$RUN_DIR/.${label}-${attempt}.signalstats"
        if DISPLAY="$DISPLAY_NUMBER" xwd -silent -id "$window" -out "$xwd" 2>/dev/null &&
            ffmpeg -hide_banner -loglevel error -f xwd_pipe -i "$xwd" "$png" &&
            ffmpeg -hide_banner -loglevel error -i "$png" \
                -vf "signalstats,metadata=print:file=$stats" -f null -; then
            yavg="$(sed -n 's/^lavfi[.]signalstats[.]YAVG=//p' "$stats" | head -n1)"
            ymax="$(sed -n 's/^lavfi[.]signalstats[.]YMAX=//p' "$stats" | head -n1)"
            if [[ "$yavg" =~ ^[0-9]+([.][0-9]+)?$ && "$ymax" =~ ^[0-9]+([.][0-9]+)?$ ]] &&
                awk -v average="$yavg" -v maximum="$ymax" 'BEGIN { exit !(average > 20 && maximum > 32) }'; then
                mv -- "$xwd" "$retained_xwd"
                mv -- "$png" "$retained_png"
                mv -- "$stats" "$retained_stats"
                printf '%s:%s:%s\n' "$window" "$yavg" "$ymax"
                return 0
            fi
        fi
        driver_is_alive
        sleep 0.2
    done
    die "patched Moonlight $label image was black or never drawable"
}

record_stream_contract() {
    local window=$1 label=$2 mode=$3 resolution=$4 fps=${5:-} bitrate=${6:-} codec=${7:-}
    local pid command_file environment_file environment
    pid="$(DISPLAY="$DISPLAY_NUMBER" xdotool getwindowpid "$window" 2>/dev/null || true)"
    [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/cmdline" && -r "/proc/$pid/environ" ]] ||
        die "cannot inspect patched Moonlight $label process"
    command_file="$RUN_DIR/${label}-moonlight-command.txt"
    tr '\0' '\n' <"/proc/$pid/cmdline" >"$command_file"
    grep -Fxq -- stream "$command_file" || die "$label child is not Moonlight stream"
    grep -Fxq -- --qsm-system-auth "$command_file" || die "$label child lacks --qsm-system-auth"
    grep -Fxq -- --display-mode "$command_file" && grep -Fxq -- "$mode" "$command_file" ||
        die "$label Moonlight display mode differs"
    grep -Fxq -- --resolution "$command_file" && grep -Fxq -- "$resolution" "$command_file" ||
        die "$label Moonlight resolution differs"
    grep -Fxq -- --no-quit-after "$command_file" || die "$label Moonlight lacks no-quit-after"
    grep -Fxq -- --absolute-mouse "$command_file" || die "$label Moonlight lacks absolute mouse"
    grep -Fxq -- --video-decoder "$command_file" && grep -Fxq -- software "$command_file" ||
        die "$label Moonlight does not use the Xvfb-safe software decoder"
    if [[ -n "$fps" ]]; then
        grep -Fxq -- --fps "$command_file" && grep -Fxq -- "$fps" "$command_file" ||
            die "$label Moonlight FPS differs from profile"
        grep -Fxq -- --bitrate "$command_file" && grep -Fxq -- "$bitrate" "$command_file" ||
            die "$label Moonlight bitrate differs from profile"
        grep -Fxq -- --video-codec "$command_file" && grep -Fxq -- "$codec" "$command_file" ||
            die "$label Moonlight codec differs from profile"
    fi

    # Do not persist a raw environment: inspect it in memory, reject a bearer
    # leak, and retain only non-secret route metadata.
    environment="$(tr '\0' '\n' <"/proc/$pid/environ")"
    [[ ! "$environment" =~ qsa1[.][A-Za-z0-9_-]+ ]] ||
        die "$label Moonlight environment exposed a system-auth bearer"
    environment_file="$RUN_DIR/${label}-moonlight-native-environment.txt"
    printf '%s\n' "$environment" |
        grep -E '^(QSM_GAMESTREAM_(AUTH_HOST|AUTH_PORT|AUTH_SNI|AUDIENCE|TICKET_FD|HOST|HTTPS_PORT))=' \
        >"$environment_file" || die "$label Moonlight lacks managed lease metadata"
    grep -Fqx "QSM_GAMESTREAM_AUDIENCE=vm-$VMID" "$environment_file" ||
        die "$label Moonlight audience is not VM-bound"
    grep -Fqx 'QSM_GAMESTREAM_TICKET_FD=0' "$environment_file" ||
        die "$label Moonlight does not use its managed ticket FD"
    grep -Fqx "QSM_GAMESTREAM_HOST=$EXPECTED_MEDIA_HOST" "$environment_file" ||
        die "$label Moonlight media host was not broker-provided"
    grep -Fqx "QSM_GAMESTREAM_HTTPS_PORT=$EXPECTED_MEDIA_HTTPS_PORT" "$environment_file" ||
        die "$label Moonlight media HTTPS port was not broker-provided"
    grep -Eq '^QSM_GAMESTREAM_AUTH_PORT=[1-9][0-9]{0,4}$' "$environment_file" ||
        die "$label Moonlight lacks a broker-provided lease endpoint"
}

inject_input() {
    local window=$1 label=$2 key=$3 geometry x y width height first_x first_y second_x second_y
    geometry="$(window_geometry "$window" || true)"
    [[ -n "$geometry" ]] || die "cannot determine $label window geometry"
    IFS=: read -r x y width height <<<"$geometry"
    first_x=$((width / 8)); first_y=$((height / 8))
    second_x=$((width - (width / 8) - 1)); second_y=$((height - (height / 8) - 1))
    ((first_x >= 0 && first_y >= 0 && second_x >= first_x && second_y >= first_y)) ||
        die "invalid $label input geometry"
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
        "$key" "$first_x" "$first_y" "$second_x" "$second_y" >"$RUN_DIR/${label}-input-injected.txt"
}

remote_preflight() {
    "$LAB_RUNNER" ssh "bash -s -- '$VMID' '$PVE_NODE'" >"$REMOTE_PREFLIGHT_LOG" <<'REMOTE'
set -Eeuo pipefail
vmid=$1
node=$2
die() { printf 'remote preflight: %s\n' "$*" >&2; exit 1; }
[[ "$vmid" =~ ^[1-9][0-9]{1,8}$ ]] || die 'invalid VM ID'
[[ "$node" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$ ]] || die 'invalid node'
[[ "$(hostname -s)" == "$node" ]] || die 'unexpected PVE node'
command -v socat >/dev/null || die 'missing socat'
command -v ss >/dev/null || die 'missing ss'
systemctl is-active --quiet q-sunshine-terminal.service || die 'terminal service is inactive'
qm status "$vmid" | grep -Fx 'status: running' >/dev/null || die 'VM is not running'
test -c /dev/dri/renderD128 || die 'nested PVE has no VirGL render node'
config="$(qm config "$vmid")"
[[ "$config" == *'vga: none'* ]] || die 'VM does not use vga: none'
[[ "$config" == *'-display dbus,addr=unix:path=/run/q-sunshine/'"$vmid"'/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128'* ]] ||
    die 'VM lacks Display1 VirGL arguments'
[[ "$config" == *'-device virtio-vga-gl'* ]] || die 'VM lacks virtio-vga-gl'
[[ "$config" == *'-chardev socket,id=qsf_agent,path=/run/q-sunshine/'"$vmid"'/qsf-agent.sock,server=on,wait=off'* ]] ||
    die 'VM lacks QSF agent socket'
[[ "$config" == *'-chardev socket,id=qsf_telemetry,path=/run/q-sunshine/'"$vmid"'/qsf-telemetry.sock,server=on,wait=off'* ]] ||
    die 'VM lacks guest telemetry socket'
[[ "$config" == *'-device virtserialport,chardev=qsf_agent,'* ]] || die 'VM lacks QSF virtserial'
[[ "$config" == *'-device virtserialport,chardev=qsf_telemetry,name=org.qsunshine.virgl.wayland.telemetry'* ]] ||
    die 'VM lacks telemetry virtserial'
for socket in "/run/q-sunshine/$vmid/qemu-display1.bus" "/run/q-sunshine/$vmid/qsf-agent.sock" "/run/q-sunshine/$vmid/qsf-telemetry.sock"; do
    [[ -S "$socket" && ! -L "$socket" ]] || die "missing live socket: $socket"
    [[ "$(stat -Lc '%u' -- "$socket")" == 0 ]] || die "socket is not root-owned: $socket"
done
pid="$(cat "/run/qemu-server/$vmid.pid")"
[[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null || die 'QEMU PID unavailable'
command_line="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
[[ "$command_line" == *"-id $vmid "* ]] || die 'live QEMU PID does not belong to the requested VM'
[[ "$command_line" == *'-display dbus,addr=unix:path=/run/q-sunshine/'"$vmid"'/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128'* ]] ||
    die 'live QEMU lacks Display1 VirGL argument'
[[ "$command_line" == *'-device virtio-vga-gl'* ]] || die 'live QEMU lacks virtio-vga-gl'
[[ "$command_line" == *'-chardev socket,id=qsf_agent,path=/run/q-sunshine/'"$vmid"'/qsf-agent.sock,server=on,wait=off'* ]] ||
    die 'live QEMU lacks QSF agent socket argument'
[[ "$command_line" == *'-device virtio-serial-pci,id=qsf_virtio_serial'* ]] ||
    die 'live QEMU lacks reviewed virtio-serial controller'
[[ "$command_line" == *'-device virtserialport,chardev=qsf_agent,name=org.qsunshine.agent'* ]] ||
    die 'live QEMU lacks QSF agent virtserial argument'
[[ "$command_line" == *'-chardev socket,id=qsf_telemetry,path=/run/q-sunshine/'"$vmid"'/qsf-telemetry.sock,server=on,wait=off'* ]] ||
    die 'live QEMU lacks telemetry socket argument'
[[ "$command_line" == *'-device virtserialport,chardev=qsf_telemetry,name=org.qsunshine.virgl.wayland.telemetry'* ]] ||
    die 'live QEMU lacks telemetry virtserial argument'
printf '%s\n' 'QSM_PVE_DESCRIPTOR_E2E_REMOTE_PREFLIGHT_OK'
printf 'node=%s vmid=%s\n' "$node" "$vmid"
printf '%s\n' '[qm-config]'
printf '%s\n' "$config"
printf '%s\n' '[qemu-display-options]'
printf '%s\n' '{"execute":"qmp_capabilities"}' '{"execute":"query-display-options"}' |
    socat - UNIX-CONNECT:"/run/qemu-server/$vmid.qmp"
REMOTE
}

start_guest_telemetry_capture() {
    local capture_seconds=$((DRIVER_TIMEOUT_MS / 1000 + GUEST_BOOT_TIMEOUT_SECONDS + 120)) attempt
    "$LAB_RUNNER" ssh "exec timeout --signal=TERM ${capture_seconds}s socat -u UNIX-CONNECT:/run/q-sunshine/$VMID/qsf-telemetry.sock STDOUT" \
        >"$GUEST_TELEMETRY_LOG" 2>"$GUEST_CAPTURE_ERR" &
    telemetry_capture_pid=$!
    for attempt in $(seq 1 20); do
        kill -0 "$telemetry_capture_pid" 2>/dev/null && return 0
        sleep 0.1
    done
    die 'could not attach guest telemetry capture'
}

request_fresh_guest_boot() {
    # Cloud-init's runcmd is intentionally one-shot for a given instance-id.
    # A simple `qm reboot` therefore cannot prove that a newly added
    # virtserial telemetry device is observed by the disposable guest. Rebuild
    # only VM 100's known NoCloud seed with the exact existing user-data and a
    # new instance-id, while the VM is stopped. This is deliberately a lab
    # fixture operation: it never touches the guest disk, a caller path, or a
    # production VM.
    "$LAB_RUNNER" ssh "bash -s -- '$VMID'" >"$RUN_DIR/fresh-guest-boot-request.txt" <<'REMOTE'
set -Eeuo pipefail
vmid=$1
seed=/var/lib/vz/template/iso/qsm-pve-lab-100-nocloud.iso

die() { printf 'fresh guest boot: %s\n' "$*" >&2; exit 1; }
[[ "$vmid" == 100 ]] || die 'refusing a VM other than the fixed lab VM 100'
for command in cloud-localds date grep isoinfo mktemp mv qm sha256sum stat; do
    command -v "$command" >/dev/null 2>&1 || die "missing required command: $command"
done
[[ -f "$seed" && ! -L "$seed" ]] || die 'NoCloud seed is not a regular non-symlink file'
[[ "$(stat -Lc '%u' -- "$seed")" == 0 ]] || die 'NoCloud seed is not root-owned'
seed_mode="$(stat -Lc '%a' -- "$seed")"
[[ "$seed_mode" =~ ^[0-7]{3,4}$ ]] || die 'NoCloud seed mode is invalid'
(( (8#$seed_mode & 0022) == 0 )) || die 'NoCloud seed is group/world writable'

case "$(qm status "$vmid")" in
    'status: running')
        qm stop "$vmid" --timeout 60
        ;;
    'status: stopped')
        ;;
    *)
        die 'VM is not safely running or stopped'
        ;;
esac
for _ in $(seq 1 75); do
    [[ "$(qm status "$vmid")" == 'status: stopped' ]] && break
    sleep 1
done
[[ "$(qm status "$vmid")" == 'status: stopped' ]] || die 'VM did not stop for seed replacement'

stage="$(mktemp -d /var/tmp/qsm-pve-e2e-seed.XXXXXX)"
cleanup() { rm -rf -- "$stage"; }
trap cleanup EXIT
user_data="$stage/user-data"
metadata="$stage/meta-data"
candidate="$stage/nocloud.iso"
isoinfo -i "$seed" -R -x /user-data >"$user_data"
[[ -s "$user_data" ]] || die 'NoCloud seed has no user-data'
# Treat the original fixture as data, not arbitrary executable input. These
# fixed markers bind this narrow re-seed path to the reviewed VirGL/QSF lab.
grep -Fqx '#cloud-config' "$user_data" >/dev/null || die 'unexpected seed user-data header'
grep -Fq '/usr/local/sbin/qsf-virgl-wayland-bootstrap' "$user_data" ||
    die 'seed is not the reviewed VirGL/QSF fixture'
grep -Fq 'org.qsunshine.virgl.wayland.telemetry' "$user_data" ||
    die 'seed does not contain the reviewed telemetry fixture'
epoch="$(date -u +%Y%m%dT%H%M%SZ)"
printf 'instance-id: qsm-pve9-lab-virgl-100-e2e-%s\nlocal-hostname: qsm-virgl-100\n' "$epoch" >"$metadata"
cloud-localds "$candidate" "$user_data" "$metadata" >/dev/null
[[ -s "$candidate" && ! -L "$candidate" ]] || die 'could not create replacement NoCloud seed'
chmod 0600 -- "$candidate"
[[ "$(stat -Lc '%u:%a' -- "$candidate")" == '0:600' ]] ||
    die 'replacement NoCloud seed has unsafe ownership or mode'
# `mv` is an atomic same-filesystem replacement after all validation. The
# target is the exact disposable lab ISO checked above, never a glob.
mv -f -- "$candidate" "$seed"
qm start "$vmid"
for _ in $(seq 1 45); do
    if [[ "$(qm status "$vmid")" == 'status: running' &&
          -S "/run/q-sunshine/$vmid/qsf-telemetry.sock" &&
          ! -L "/run/q-sunshine/$vmid/qsf-telemetry.sock" &&
          "$(stat -Lc '%u' -- "/run/q-sunshine/$vmid/qsf-telemetry.sock")" == 0 ]]; then
        printf 'QSM_PVE_DESCRIPTOR_E2E_FRESH_GUEST_BOOT_READY instance=%s\n' "$epoch"
        exit 0
    fi
    sleep 1
done
die 'VM did not expose the root-owned telemetry socket after fresh boot'
REMOTE
}

wait_for_guest_bootstrap() {
    wait_for_guest_initial_marker 'guest_drm_driver=virtio_gpu' 'guest virtio_gpu DRM binding'
    wait_for_guest_initial_pattern '^guest_gl_renderer=.*[Vv][Ii][Rr][Gg][Ll].*$' 'guest VirGL renderer'
    wait_for_guest_initial_marker 'guest_wayland_compositor=weston-drm' 'guest Weston DRM compositor'
    wait_for_guest_initial_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCH_READY' 'guest raw evdev watcher'
    wait_for_guest_initial_marker 'QSF_VIRGL_WAYLAND_GUEST_VIRGL_READY' 'guest VirGL workload'
    wait_for_guest_initial_marker 'QSF_VIRGL_WAYLAND_GUEST_VIRGL_OK' 'guest VirGL bootstrap'
}

wait_for_pve_qsunshine_ready() {
    local attempt

    # A freshly booted VM can publish guest telemetry before the protected PVE
    # Console route has observed the VM as runnable. Exercise that exact local
    # route through the package-owned diagnostic launcher. Its descriptor is
    # discarded immediately; this is a readiness probe, never a credential or
    # a browser-side fallback route.
    for attempt in $(seq 1 30); do
        if "$LAB_RUNNER" ssh "timeout --signal=TERM 12s /usr/lib/q-sunshine/pve9-api/q-sunshine-pvesh create /nodes/$PVE_NODE/qemu/$VMID/q-sunshine --output-format json >/dev/null 2>&1"; then
            return 0
        fi
        assert_remote_vm_running
        sleep 1
    done

    die 'PVE did not make the protected q-sunshine Console endpoint ready after the fresh guest boot'
}

wait_for_remote_workers() {
    local attempt
    for attempt in $(seq 1 $((PHASE_TIMEOUT_SECONDS * 5))); do
        if "$LAB_RUNNER" ssh "bash -s -- '$VMID'" >"$REMOTE_WORKER_LOG" <<'REMOTE'
set -Eeuo pipefail
vmid=$1
[[ "$vmid" =~ ^[1-9][0-9]{1,8}$ ]] || exit 2
# /usr/bin/q-sunshine is an installed symlink, so Linux reports its canonical
# target in ps. Match the reviewed package payload and the VM-bound arguments,
# not a generic process name or an unrelated QSF fixture.
ps -eo pid=,args= | awk -v vmid="$vmid" '
  /\/usr\/lib\/q-sunshine\/bin\/sunshine capture=qemu_dbus/ &&
      $0 ~ ("qsm_system_auth_audience=vm-" vmid) &&
      $0 ~ /qsm_system_auth_mode=enabled/ { sunshine=1; print }
  /\/usr\/lib\/q-sunshine\/qsf\/qsf_control[.]py/ &&
      $0 ~ ("--agent-socket=/run/q-sunshine/" vmid "/qsf-agent.sock") &&
      $0 ~ ("--control-socket=/run/q-sunshine-terminal/workers/vm-" vmid "/control.sock") &&
      $0 ~ ("--token-file=/run/q-sunshine-terminal/workers/vm-" vmid "/control.token") { qsf=1; print }
  /\/usr\/lib\/q-sunshine\/qsf\/qsf_tls_gateway[.]py/ &&
      $0 ~ ("--control-socket=/run/q-sunshine-terminal/workers/vm-" vmid "/control.sock") &&
      $0 ~ ("--token-file=/run/q-sunshine-terminal/workers/vm-" vmid "/control.token") &&
      $0 ~ ("--system-auth-audience vm-" vmid) { gateway=1; print }
  END { exit !(sunshine && qsf && gateway) }
'
REMOTE
        then
            return 0
        fi
        driver_is_alive
        sleep 0.2
    done
    die 'terminal did not create Sunshine, QSF control and QSF TLS workers'
}

assert_active_worker_port_surface() {
    # This proof runs only after a non-black Moonlight window has been
    # observed, so the RTP sockets belong to a real native media session. Do
    # not infer inner-PVE listeners from the outer lab's static host forwards.
    # The remote side emits only a fixed port summary and PID, never command
    # lines, descriptors, tickets, certificates, or socket payloads.
    "$LAB_RUNNER" ssh "bash -s -- '$VMID'" >"$REMOTE_PORT_SURFACE_LOG" <<'REMOTE'
set -Eeuo pipefail
vmid=$1
die() { printf 'transport port surface: %s\n' "$*" >&2; exit 1; }
[[ "$vmid" =~ ^[1-9][0-9]{1,8}$ ]] || die 'invalid VM ID'
command -v ss >/dev/null || die 'missing ss'

mapfile -t sunshine_pids < <(ps -eo pid=,args= | awk -v vmid="$vmid" '
  /\/usr\/lib\/q-sunshine\/bin\/sunshine capture=qemu_dbus/ &&
      $0 ~ ("qsm_system_auth_audience=vm-" vmid) &&
      $0 ~ /qsm_system_auth_mode=enabled/ { print $1 }
')
[[ "${#sunshine_pids[@]}" == 1 ]] || die 'expected exactly one native Sunshine worker'
sunshine_pid=${sunshine_pids[0]}
[[ "$sunshine_pid" =~ ^[1-9][0-9]*$ ]] || die 'invalid Sunshine PID'

worker_ports() {
    local protocol=$1
    case "$protocol" in
        tcp) ss -H -ltnp ;;
        udp) ss -H -lunp ;;
        *) die 'invalid socket protocol' ;;
    esac | awk -v wanted="pid=$sunshine_pid," '
        index($0, wanted) {
            address = $4
            sub(/^.*:/, "", address)
            print address
        }
    ' | sort -n -u | awk 'NR > 1 { printf "," } { printf "%s", $0 }'
}

port_has_listener() {
    local protocol=$1 expected_port=$2
    case "$protocol" in
        tcp) ss -H -ltn ;;
        udp) ss -H -lun ;;
        *) die 'invalid listener protocol' ;;
    esac | awk -v expected="$expected_port" '
        {
            address = $4
            sub(/^.*:/, "", address)
            if (address == expected) {
                found = 1
            }
        }
        END { exit found ? 0 : 1 }
    '
}

tcp_ports="$(worker_ports tcp)"
udp_ports="$(worker_ports udp)"
[[ "$tcp_ports" == '47984,48010' ]] || die "unexpected Sunshine TCP listener set"
[[ "$udp_ports" == '47998,47999,48000' ]] || die "unexpected Sunshine UDP listener set"

# The raw GameStream bootstrap port, removed configuration port, and unused
# legacy RTP forward must be completely absent in the PVE network namespace.
for blocked_port in 47989 47990 48002; do
    if port_has_listener tcp "$blocked_port" || port_has_listener udp "$blocked_port"; then
        die "unexpected listener on disabled legacy port"
    fi
done

# QSF and the PAM/TLS issuer remain separate process-owned listeners. Their
# individual authenticated behavior is exercised by the existing QSF and
# patched-Moonlight portions of this same E2E run.
for protected_port in 48122 48123; do
    port_line="$(ss -H -ltnp "sport = :$protected_port" || true)"
    [[ -n "$port_line" && "$port_line" != *"pid=$sunshine_pid,"* ]] ||
        die 'QSF/auth protected listener is absent or owned by Sunshine'
done

printf '%s\n' 'QSM_PVE_DESCRIPTOR_E2E_TRANSPORT_PORT_SURFACE_OK'
printf 'sunshine_pid=%s\n' "$sunshine_pid"
printf 'sunshine_tcp_listener_ports=%s\n' "$tcp_ports"
printf 'sunshine_udp_listener_ports=%s\n' "$udp_ports"
printf '%s\n' 'plaintext_47989=absent config_47990=absent legacy_48002=absent qsf_48122=separate auth_48123=separate'
REMOTE
}

collect_remote_final_evidence() {
    "$LAB_RUNNER" ssh "bash -s -- '$VMID' '$REMOTE_JOURNAL_SINCE'" >"$REMOTE_FINAL_LOG" <<'REMOTE'
set -Eeuo pipefail
vmid=$1
since=$2
[[ "$vmid" =~ ^[1-9][0-9]{1,8}$ ]] || exit 2
# Keep the regex in a variable: a literal space in an unquoted =~ expression
# is parsed by Bash as an argument separator on the remote shell.  The local
# caller emits ISO-8601 UTC without spaces, so the narrower grammar is both
# sufficient and executable under Bash.
since_pattern='^[0-9T:+-]{19,40}$'
[[ "$since" =~ $since_pattern ]] || exit 2
printf '%s\n' '[terminal-service-journal]'
journalctl -u q-sunshine-terminal.service --since "$since" --no-pager
printf '%s\n' '[live-qemu-display-options]'
printf '%s\n' '{"execute":"qmp_capabilities"}' '{"execute":"query-display-options"}' |
    socat - UNIX-CONNECT:"/run/qemu-server/$vmid.qmp"
printf '%s\n' '[live-vm-status]'
qm status "$vmid"
REMOTE
}

assert_no_secret_diagnostics() {
    local path
    for path in "$BROWSER_LOG" "$DRIVER_LOG" "$MOONLIGHT_LOG"; do
        [[ -f "$path" ]] || continue
        if grep -Eaq 'PVEVNC:|PVEAuthCookie|qsa1[.][A-Za-z0-9_-]+|qsd1[.][A-Za-z0-9_-]+' "$path"; then
            die 'retained diagnostic contains a bearer credential'
        fi
    done
}

write_trace() {
    local client_clipboard_hash guest_clipboard_hash client_upload_hash guest_download_hash
    client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
    guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
    client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
    guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
    {
        printf '%s\n' 'QSM_PVE_DESCRIPTOR_QT_VIRGL_QSF_E2E'
        printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'pve_browser=actual Chromium visible-login/resource-tree/Console-split-menu; no route/API mock\n'
        printf 'node=%s vmid=%s\n' "$PVE_NODE" "$VMID"
        printf 'authorization=same-origin PVE session+VM.Console -> protected pvedaemon -> root-only local handoff -> one-use descriptor; no PVE secret reaches Qt\n'
        printf 'presentation=windowed:%s negotiated:%s fullscreen:%s@(0,0)\n' \
            "$WINDOWED_RESOLUTION" "$NEGOTIATED_RESOLUTION" "$FULLSCREEN_RESOLUTION"
        printf 'virgl=virtio_gpu + Weston DRM + guest VirGL telemetry; profile ACK follows guest current-mode check\n'
        printf 'fresh_guest_boot=%s\n' "$FRESH_BOOT"
        printf 'input=KEY_A+absolute pointer+button and fresh KEY_B+pointer+button observed by guest evdev\n'
        printf 'clipboard_client_to_guest_sha256=%s\n' "$client_clipboard_hash"
        printf 'clipboard_guest_to_client_sha256=%s\n' "$guest_clipboard_hash"
        printf 'file_client_to_guest_sha256=%s\n' "$client_upload_hash"
        printf 'file_guest_to_client_sha256=%s\n' "$guest_download_hash"
        printf 'patched_moonlight_sha256=%s\n' "$(sha256sum "$PATCHED_MOONLIGHT_BINARY" | awk '{print $1}')"
        printf 'qt_driver_sha256=%s\n' "$(sha256sum "$REAL_E2E_DRIVER" | awk '{print $1}')"
        printf '\n[phase protocol]\n'
        for phase in \
            driver-ready descriptor-redeem-started descriptor-redeemed \
            windowed-process-started windowed-activation-accepted qsf-windowed-ready \
            client-clipboard-sent guest-clipboard-received windowed-qsf-operations-complete \
            qsf-deactivated-for-negotiated-profile windowed-stream-quiesced qsf-negotiated-profile-ready \
            negotiated-profile-confirmed negotiated-process-started negotiated-video-verification-accepted \
            activate-negotiated-qsf negotiated-qsf-activation-accepted qsf-negotiated-ready \
            activate-fullscreen-profile fullscreen-profile-activation-accepted \
            qsf-deactivated-for-fullscreen-profile negotiated-stream-quiesced qsf-fullscreen-profile-ready \
            fullscreen-profile-confirmed fullscreen-process-started fullscreen-activation-accepted \
            qsf-fullscreen-ready fullscreen-download-received qsf-deactivated-for-stop complete; do
            printf '%s\n' "$phase"
        done
        printf '\n[guest telemetry]\n'
        if [[ "$FRESH_BOOT" == 1 ]]; then
            grep -E \
                -e '^guest_drm_driver=virtio_gpu$' \
                -e '^guest_gl_renderer=.*[Vv][Ii][Rr][Gg][Ll].*$' \
                -e '^guest_wayland_compositor=weston-drm$' \
                -e '^QSF_VIRGL_WAYLAND_GUEST_VIRGL_OK$' \
                "$GUEST_TELEMETRY_LOG"
        fi
        grep -E \
            -e '^QSF_VIRGL_WAYLAND_GUEST_(INPUT_E2E_OK|FULLSCREEN_INPUT_E2E_OK)$' \
            -e '^QSF_VIRGL_WAYLAND_GUEST_(CLIENT_TO_WAYLAND|NATIVE_WL_COPY|WAYLAND_TO_QSF_STATE|UPLOAD|QSF_DOWNLOAD)_SHA256=' \
            -e '^QSF_VIRGL_WAYLAND_GUEST_(RESIZE|RESIZE_WESTON_RESTARTED|CONNECTION_PROFILE_APPLIED|CONNECTION_PROFILE_ACK_OBSERVED)' \
            "$GUEST_TELEMETRY_LOG"
        printf '\n[workers while descriptor session was active]\n'
        cat "$REMOTE_WORKER_LOG"
        printf '\n[active native transport port surface]\n'
        cat "$REMOTE_PORT_SURFACE_LOG"
        printf '\n[terminal and final QEMU]\n'
        cat "$REMOTE_FINAL_LOG"
        printf '%s\n' 'QSM_PVE_DESCRIPTOR_QT_VIRGL_QSF_E2E_OK'
    } >"$RUN_DIR/trace.txt"
}

while (($#)); do
    case "$1" in
        --pve-url) (($# >= 2)) || die '--pve-url needs a value'; PVE_URL=$2; shift 2 ;;
        --user) (($# >= 2)) || die '--user needs a value'; PVE_USER=$2; shift 2 ;;
        --password-file) (($# >= 2)) || die '--password-file needs a value'; PVE_PASSWORD_FILE=$2; shift 2 ;;
        --vmid) (($# >= 2)) || die '--vmid needs a value'; VMID=$2; shift 2 ;;
        --moonlight-binary) (($# >= 2)) || die '--moonlight-binary needs a value'; PATCHED_MOONLIGHT_BINARY=$2; shift 2 ;;
        --driver) (($# >= 2)) || die '--driver needs a value'; REAL_E2E_DRIVER=$2; shift 2 ;;
        --display) (($# >= 2)) || die '--display needs a value'; DISPLAY_NUMBER=$2; shift 2 ;;
        --output-parent) (($# >= 2)) || die '--output-parent needs a value'; OUTPUT_PARENT=$2; shift 2 ;;
        --browser-timeout-ms) (($# >= 2)) || die '--browser-timeout-ms needs a value'; BROWSER_TIMEOUT_MS=$2; shift 2 ;;
        --driver-timeout-ms) (($# >= 2)) || die '--driver-timeout-ms needs a value'; DRIVER_TIMEOUT_MS=$2; shift 2 ;;
        --headed) BROWSER_HEADED=1; shift ;;
        --trusted-https) IGNORE_HTTPS_ERRORS=0; shift ;;
        --fresh-boot) FRESH_BOOT=1; shift ;;
        --no-fresh-boot) FRESH_BOOT=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

trap 'on_error "$?" "$LINENO"' ERR
trap cleanup EXIT
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

[[ "$VMID" =~ ^[1-9][0-9]{1,8}$ ]] || die 'VM ID must be 100..999999999'
[[ "$PVE_NODE" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$ ]] || die 'invalid PVE node'
[[ "$DISPLAY_NUMBER" =~ ^:[0-9]+$ ]] || die 'display must be a private X display such as :97'
[[ "$FRESH_BOOT" == 0 || "$FRESH_BOOT" == 1 ]] || die 'fresh boot flag must be 0 or 1'
[[ "$FRESH_BOOT" == 1 ]] ||
    die 'this acceptance lane requires --fresh-boot to attest a real guest bootstrap'
[[ "$BROWSER_HEADED" == 0 || "$BROWSER_HEADED" == 1 ]] || die 'headed browser flag must be 0 or 1'
[[ "$IGNORE_HTTPS_ERRORS" == 0 || "$IGNORE_HTTPS_ERRORS" == 1 ]] || die 'TLS flag must be 0 or 1'
in_range "$BROWSER_TIMEOUT_MS" 5000 120000 || die 'browser timeout must be 5000..120000 ms'
in_range "$DRIVER_TIMEOUT_MS" 60000 900000 || die 'driver timeout must be 60000..900000 ms'
in_range "$PHASE_TIMEOUT_SECONDS" 10 900 || die 'invalid phase timeout'
in_range "$VISUAL_TIMEOUT_SECONDS" 5 300 || die 'invalid visual timeout'
in_range "$GUEST_TIMEOUT_SECONDS" 10 600 || die 'invalid guest timeout'
in_range "$GUEST_BOOT_TIMEOUT_SECONDS" 30 900 || die 'invalid guest boot timeout'
in_range "$CLIENT_ROOT_WIDTH" 64 16384 && in_range "$CLIENT_ROOT_HEIGHT" 64 16384 ||
    die 'invalid Xvfb root dimensions'
in_range "$EXPECTED_MEDIA_PORT" 6 65535 || die 'invalid expected media port'
[[ "$EXPECTED_MEDIA_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$ ]] || die 'invalid expected media host'
EXPECTED_MEDIA_HTTPS_PORT=$((EXPECTED_MEDIA_PORT - 5))
for value in "$NEGOTIATED_FPS" "$FULLSCREEN_FPS"; do in_range "$value" 10 240 || die 'invalid expected FPS'; done
for value in "$NEGOTIATED_BITRATE_KBPS" "$FULLSCREEN_BITRATE_KBPS"; do in_range "$value" 500 500000 || die 'invalid expected bitrate'; done
for value in "$NEGOTIATED_VIDEO_CODEC" "$FULLSCREEN_VIDEO_CODEC"; do
    [[ "$value" == H.264 || "$value" == HEVC || "$value" == AV1 ]] || die 'invalid expected codec'
done
guest_codec_name() {
    case "$1" in
        H.264) printf '%s' H264 ;;
        HEVC|AV1) printf '%s' "$1" ;;
        *) return 1 ;;
    esac
}
NEGOTIATED_GUEST_CODEC="$(guest_codec_name "$NEGOTIATED_VIDEO_CODEC")" ||
    die 'could not map negotiated codec to guest profile spelling'
FULLSCREEN_GUEST_CODEC="$(guest_codec_name "$FULLSCREEN_VIDEO_CODEC")" ||
    die 'could not map fullscreen codec to guest profile spelling'
validate_resolution "$WINDOWED_RESOLUTION" windowed
validate_resolution "$NEGOTIATED_RESOLUTION" negotiated
validate_resolution "$FULLSCREEN_RESOLUTION" fullscreen
WINDOWED_WIDTH=${WINDOWED_RESOLUTION%x*}; WINDOWED_HEIGHT=${WINDOWED_RESOLUTION#*x}
NEGOTIATED_WIDTH=${NEGOTIATED_RESOLUTION%x*}; NEGOTIATED_HEIGHT=${NEGOTIATED_RESOLUTION#*x}
FULLSCREEN_WIDTH=${FULLSCREEN_RESOLUTION%x*}; FULLSCREEN_HEIGHT=${FULLSCREEN_RESOLUTION#*x}
# VM 100's immutable cloud-init payload contains the first transaction's
# expected scanout as qsf-resize-expected.txt. Do not pretend a caller can
# change it without rematerialising the guest: that would make a fixture
# failure look like an E2E transport regression.
[[ "$NEGOTIATED_RESOLUTION" == '1280x720' ]] ||
    die 'this fixed VM-100 fixture accepts negotiated resolution 1280x720 only'
# The deployed VM-100 policy is the software H.264 lane. HEVC/AV1 are valid
# product capabilities but not provisioned in this fixed acceptance topology.
[[ "$NEGOTIATED_VIDEO_CODEC" == H.264 && "$FULLSCREEN_VIDEO_CODEC" == H.264 ]] ||
    die 'this fixed VM-100 acceptance lane supports H.264 only'
for value in "$NEGOTIATED_FPS" "$FULLSCREEN_FPS"; do
    ((value <= 60)) || die 'VM-100 acceptance policy supports at most 60 FPS'
done
for value in "$NEGOTIATED_BITRATE_KBPS" "$FULLSCREEN_BITRATE_KBPS"; do
    ((value <= 16000)) || die 'VM-100 acceptance policy supports at most 16000 Kbps'
done
((10#$FULLSCREEN_WIDTH <= 1920 && 10#$FULLSCREEN_HEIGHT <= 1080)) ||
    die 'fullscreen exceeds the fixed VM-100 1920x1080 QSF host envelope'
((10#$WINDOWED_WIDTH <= 10#$CLIENT_ROOT_WIDTH && 10#$WINDOWED_HEIGHT <= 10#$CLIENT_ROOT_HEIGHT)) ||
    die 'windowed size does not fit Xvfb root'
((10#$NEGOTIATED_WIDTH <= 10#$CLIENT_ROOT_WIDTH && 10#$NEGOTIATED_HEIGHT <= 10#$CLIENT_ROOT_HEIGHT)) ||
    die 'negotiated size does not fit Xvfb root'
((10#$FULLSCREEN_WIDTH == 10#$CLIENT_ROOT_WIDTH && 10#$FULLSCREEN_HEIGHT == 10#$CLIENT_ROOT_HEIGHT)) ||
    die 'fullscreen size must equal Xvfb root'
[[ "$WINDOWED_RESOLUTION" != "$FULLSCREEN_RESOLUTION" && "$NEGOTIATED_RESOLUTION" != "$FULLSCREEN_RESOLUTION" ]] ||
    die 'windowed/negotiated profiles must be distinct from fullscreen'

# `socat` is intentionally checked by remote_preflight on the nested PVE
# node, where the telemetry UNIX socket exists. The local client never opens
# that socket directly and must not acquire an unnecessary host dependency.
for command in awk cmp date ffmpeg ffprobe find grep head id mktemp mv node readlink sed sha256sum sleep \
               stat timeout tr xdotool xwd xwininfo Xvfb xdpyinfo; do
    require_command "$command"
done
require_file "$LAB_RUNNER"
require_file "$BROWSER_GATE"
for path in "$CLIENT_CLIPBOARD" "$GUEST_CLIPBOARD" "$CLIENT_UPLOAD" "$GUEST_DOWNLOAD"; do require_file "$path"; done
[[ -x "$LAB_RUNNER" ]] || die 'lab runner is not executable'
[[ -x "$REAL_E2E_DRIVER" ]] || die 'Qt real-E2E driver is not executable'
[[ -x "$PATCHED_MOONLIGHT_BINARY" ]] || die 'patched Moonlight binary is not executable'
[[ -f "$PVE_PASSWORD_FILE" && ! -L "$PVE_PASSWORD_FILE" ]] || die 'PVE password file is unavailable'
REAL_E2E_DRIVER="$(readlink -f -- "$REAL_E2E_DRIVER")"
PATCHED_MOONLIGHT_BINARY="$(readlink -f -- "$PATCHED_MOONLIGHT_BINARY")"
embedded_binary="$(readlink -f -- "$ROOT_DIR/.upstream/build-moonlight-embedded/moonlight" 2>/dev/null || true)"
[[ "$PATCHED_MOONLIGHT_BINARY" != "$embedded_binary" ]] || die 'Moonlight Embedded is not a valid patched Qt child'
grep -aF -- 'q-sunshine system-auth lease' "$PATCHED_MOONLIGHT_BINARY" >/dev/null ||
    die 'Moonlight lacks compiled q-sunshine system-auth lease support'

if [[ -e "$OUTPUT_PARENT" && (! -d "$OUTPUT_PARENT" || -L "$OUTPUT_PARENT") ]]; then
    die 'output parent must be a non-symlink directory'
fi
mkdir -p -- "$OUTPUT_PARENT"
chmod 700 -- "$OUTPUT_PARENT"
RUN_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
chmod 700 -- "$RUN_DIR"
DOWNLOAD_DIR="$RUN_DIR/download"
PHASE_DIR="$RUN_DIR/phases"
CLIENT_CONFIG_DIR="$RUN_DIR/xdg-config"
CLIENT_DATA_DIR="$RUN_DIR/xdg-data"
CLIENT_CACHE_DIR="$RUN_DIR/xdg-cache"
CLIENT_RUNTIME_DIR="$RUN_DIR/xdg-runtime"
CLIENT_PORTABLE_DIR="$RUN_DIR/moonlight-portable"
mkdir -p -- "$DOWNLOAD_DIR" "$PHASE_DIR" "$CLIENT_CONFIG_DIR" "$CLIENT_DATA_DIR" "$CLIENT_CACHE_DIR" \
    "$CLIENT_RUNTIME_DIR" "$CLIENT_PORTABLE_DIR"
chmod 700 -- "$DOWNLOAD_DIR" "$PHASE_DIR" "$CLIENT_CONFIG_DIR" "$CLIENT_DATA_DIR" "$CLIENT_CACHE_DIR" \
    "$CLIENT_RUNTIME_DIR" "$CLIENT_PORTABLE_DIR"
BROWSER_LOG="$RUN_DIR/pve-browser-qualification.log"
DRIVER_LOG="$RUN_DIR/qt-real-e2e.log"
MOONLIGHT_LOG="$RUN_DIR/moonlight-stream-redacted.log"
GUEST_TELEMETRY_LOG="$RUN_DIR/guest-telemetry.log"
GUEST_CAPTURE_ERR="$RUN_DIR/guest-telemetry-capture.stderr"
REMOTE_PREFLIGHT_LOG="$RUN_DIR/remote-preflight.txt"
REMOTE_WORKER_LOG="$RUN_DIR/remote-workers-live.txt"
REMOTE_PORT_SURFACE_LOG="$RUN_DIR/remote-transport-port-surface.txt"
REMOTE_FINAL_LOG="$RUN_DIR/remote-final.txt"
touch "$GUEST_TELEMETRY_LOG"
REMOTE_JOURNAL_SINCE="$(date -u +%Y-%m-%dT%H:%M:%S)"

printf 'PVE descriptor Qt/VirGL/QSF E2E evidence=%s\n' "$RUN_DIR"
"$LAB_RUNNER" wait
remote_preflight

if DISPLAY="$DISPLAY_NUMBER" xdpyinfo >/dev/null 2>&1; then
    die "client X display $DISPLAY_NUMBER is already in use"
fi
LIBGL_ALWAYS_SOFTWARE=1 Xvfb "$DISPLAY_NUMBER" -screen 0 "${CLIENT_ROOT_WIDTH}x${CLIENT_ROOT_HEIGHT}x24" \
    +extension GLX -nolisten tcp >"$RUN_DIR/xvfb.log" 2>&1 &
xvfb_pid=$!
wait_for_xvfb

# A binary string alone is insufficient; assert the actual CLI option too.
with_client_environment timeout --signal=TERM --kill-after=2s 10s \
    "$PATCHED_MOONLIGHT_BINARY" stream --help >"$RUN_DIR/moonlight-stream-help.txt" 2>&1 ||
    die 'patched Moonlight cannot run stream --help under Xvfb'
grep -F -- '--qsm-system-auth' "$RUN_DIR/moonlight-stream-help.txt" >/dev/null ||
    die 'Moonlight stream CLI lacks --qsm-system-auth'

if [[ "$FRESH_BOOT" == 1 ]]; then
    request_fresh_guest_boot
fi
start_guest_telemetry_capture
if [[ "$FRESH_BOOT" == 1 ]]; then
    wait_for_guest_bootstrap
    wait_for_pve_qsunshine_ready
fi

browser_arguments=(
    "$BROWSER_GATE" --pve-url "$PVE_URL" --user "$PVE_USER" --password-file "$PVE_PASSWORD_FILE"
    --vmid "$VMID" --download-dir "$DOWNLOAD_DIR" --timeout-ms "$BROWSER_TIMEOUT_MS"
)
[[ "$IGNORE_HTTPS_ERRORS" == 1 ]] && browser_arguments+=(--ignore-https-errors)
[[ "$BROWSER_HEADED" == 1 ]] && browser_arguments+=(--headed)
if [[ "$BROWSER_HEADED" == 1 ]]; then
    # The visible browser shares the isolated Xvfb only while it downloads the
    # descriptor; it exits before the Qt/Moonlight video test begins.
    with_client_environment node "${browser_arguments[@]}" >"$BROWSER_LOG" 2>&1
else
    node "${browser_arguments[@]}" >"$BROWSER_LOG" 2>&1
fi

mapfile -t launch_files < <(find "$DOWNLOAD_DIR" -maxdepth 1 -type f -name '*.qsm' -printf '%p\n')
[[ "${#launch_files[@]}" == 1 ]] || die 'browser did not produce exactly one .qsm'
LAUNCH_FILE="${launch_files[0]}"
launch_metadata="$(stat -Lc '%u:%a:%s' -- "$LAUNCH_FILE")"
launch_owner=${launch_metadata%%:*}; launch_tail=${launch_metadata#*:}
launch_mode=${launch_tail%%:*}; launch_size=${launch_tail##*:}
[[ "$launch_owner" == "$(id -u)" && "$launch_mode" == 600 && "$launch_size" =~ ^[1-9][0-9]*$ ]] ||
    die 'downloaded .qsm is not non-empty mode 0600 owned by this user'

(
    cd -- "$CLIENT_PORTABLE_DIR"
    with_client_environment "$REAL_E2E_DRIVER" \
        --launch-file "$LAUNCH_FILE" --moonlight-binary "$PATCHED_MOONLIGHT_BINARY" \
        --initial-resolution "$WINDOWED_RESOLUTION" --fullscreen-resolution "$FULLSCREEN_RESOLUTION" \
        --expected-guest-clipboard-file "$GUEST_CLIPBOARD" --client-clipboard-file "$CLIENT_CLIPBOARD" \
        --received-clipboard-destination "$RUN_DIR/guest-clipboard.txt" \
        --upload-source "$CLIENT_UPLOAD" --upload-name client-upload.txt \
        --download-name guest-download.txt --download-destination "$RUN_DIR/guest-download.txt" \
        --fullscreen-download-destination "$RUN_DIR/guest-download-fullscreen.txt" \
        --expected-download-source "$GUEST_DOWNLOAD" --resize "$NEGOTIATED_RESOLUTION" \
        --expected-negotiated-fps "$NEGOTIATED_FPS" \
        --expected-negotiated-bitrate-kbps "$NEGOTIATED_BITRATE_KBPS" \
        --expected-negotiated-video-codec "$NEGOTIATED_VIDEO_CODEC" \
        --expected-fullscreen-fps "$FULLSCREEN_FPS" \
        --expected-fullscreen-bitrate-kbps "$FULLSCREEN_BITRATE_KBPS" \
        --expected-fullscreen-video-codec "$FULLSCREEN_VIDEO_CODEC" \
        --timeout-ms "$DRIVER_TIMEOUT_MS" --phase-dir "$PHASE_DIR" --moonlight-log "$MOONLIGHT_LOG"
) >"$DRIVER_LOG" 2>&1 &
driver_pid=$!

wait_for_phase driver-ready
wait_for_phase descriptor-redeem-started
wait_for_phase descriptor-redeemed
wait_for_remote_workers
wait_for_phase windowed-process-started

windowed_window="$(wait_for_moonlight_window windowed windowed)"
IFS=: read -r windowed_window windowed_yavg windowed_ymax <<<"$(attest_nonblack_window "$windowed_window" windowed)"
assert_active_worker_port_surface
DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$windowed_window" >"$RUN_DIR/windowed-window.xwininfo"
record_stream_contract "$windowed_window" windowed windowed "$WINDOWED_RESOLUTION"
inject_input "$windowed_window" windowed a
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_KEY_A=observed' 'windowed KEY_A'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_ABS=observed' 'windowed absolute pointer'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_BTN=observed' 'windowed pointer button'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_E2E_OK' 'windowed raw evdev evidence'

write_phase_request activate-windowed
wait_for_phase windowed-activation-accepted
wait_for_phase qsf-windowed-ready
wait_for_phase client-clipboard-sent
client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_STATE_SHA256=$client_clipboard_hash" 'client clipboard state'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_WL_PASTE_SHA256=$client_clipboard_hash" 'guest Wayland paste'
wait_for_phase guest-clipboard-received
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_NATIVE_WL_COPY_SHA256=$guest_clipboard_hash" 'native guest clipboard copy'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_WAYLAND_TO_QSF_STATE_SHA256=$guest_clipboard_hash" 'guest clipboard published to QSF'
cmp -s "$GUEST_CLIPBOARD" "$RUN_DIR/guest-clipboard.txt" || die 'guest clipboard bytes differ'
wait_for_phase windowed-qsf-operations-complete
wait_for_phase qsf-deactivated-for-negotiated-profile
wait_for_phase windowed-stream-quiesced
wait_for_phase qsf-negotiated-profile-ready
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_RESIZE_RECONFIGURE=weston-drm-restart' 'guest Weston restart'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_RESIZE_WESTON_RESTARTED' 'guest Weston restart completion'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_RESIZE=$NEGOTIATED_RESOLUTION" 'guest negotiated scanout'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_UPLOAD_SHA256=$client_upload_hash" 'guest upload receipt'
wait_for_guest_marker "QSF_VIRGL_WAYLAND_GUEST_QSF_DOWNLOAD_SHA256=$guest_download_hash" 'guest download fixture'
cmp -s "$GUEST_DOWNLOAD" "$RUN_DIR/guest-download.txt" || die 'windowed guest download bytes differ'
wait_for_guest_pattern "^QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_APPLIED=version=2,generation=[1-9][0-9]*,resolution=${NEGOTIATED_RESOLUTION},fps=${NEGOTIATED_FPS},bitrate_kbps=${NEGOTIATED_BITRATE_KBPS},video_codec=${NEGOTIATED_GUEST_CODEC}$" \
    'guest applied negotiated VirGL profile'
wait_for_guest_pattern "^QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_ACK_OBSERVED=generation=[1-9][0-9]*,resolution=${NEGOTIATED_RESOLUTION}$" \
    'guest observed negotiated scanout acknowledgement'
wait_for_phase negotiated-profile-confirmed
wait_for_phase negotiated-process-started

negotiated_window="$(wait_for_moonlight_window negotiated negotiated)"
IFS=: read -r negotiated_window negotiated_yavg negotiated_ymax <<<"$(attest_nonblack_window "$negotiated_window" negotiated)"
DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$negotiated_window" >"$RUN_DIR/negotiated-window.xwininfo"
record_stream_contract "$negotiated_window" negotiated windowed "$NEGOTIATED_RESOLUTION" \
    "$NEGOTIATED_FPS" "$NEGOTIATED_BITRATE_KBPS" "$NEGOTIATED_VIDEO_CODEC"
write_phase_request negotiated-video-verified
wait_for_phase negotiated-video-verification-accepted
write_phase_request activate-negotiated-qsf
wait_for_phase negotiated-qsf-activation-accepted
wait_for_phase qsf-negotiated-ready
write_phase_request activate-fullscreen-profile
wait_for_phase fullscreen-profile-activation-accepted
wait_for_phase qsf-deactivated-for-fullscreen-profile
wait_for_phase negotiated-stream-quiesced
wait_for_phase qsf-fullscreen-profile-ready
wait_for_guest_pattern "^QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_APPLIED=version=2,generation=[1-9][0-9]*,resolution=${FULLSCREEN_RESOLUTION},fps=${FULLSCREEN_FPS},bitrate_kbps=${FULLSCREEN_BITRATE_KBPS},video_codec=${FULLSCREEN_GUEST_CODEC}$" \
    'guest applied fullscreen VirGL profile'
wait_for_guest_pattern "^QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_ACK_OBSERVED=generation=[1-9][0-9]*,resolution=${FULLSCREEN_RESOLUTION}$" \
    'guest observed fullscreen scanout acknowledgement'
wait_for_phase fullscreen-profile-confirmed
wait_for_phase fullscreen-process-started

fullscreen_window="$(wait_for_moonlight_window fullscreen fullscreen)"
IFS=: read -r fullscreen_window fullscreen_yavg fullscreen_ymax <<<"$(attest_nonblack_window "$fullscreen_window" fullscreen)"
DISPLAY="$DISPLAY_NUMBER" xwininfo -id "$fullscreen_window" >"$RUN_DIR/fullscreen-window.xwininfo"
record_stream_contract "$fullscreen_window" fullscreen fullscreen "$FULLSCREEN_RESOLUTION" \
    "$FULLSCREEN_FPS" "$FULLSCREEN_BITRATE_KBPS" "$FULLSCREEN_VIDEO_CODEC"
inject_input "$fullscreen_window" fullscreen b
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_KEY_B=observed' 'fullscreen KEY_B'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_MOUSE_ABS=observed' 'fullscreen absolute pointer'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_MOUSE_BTN=observed' 'fullscreen pointer button'
wait_for_guest_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_E2E_OK' 'fullscreen raw evdev evidence'

write_phase_request activate-fullscreen
wait_for_phase fullscreen-activation-accepted
wait_for_phase qsf-fullscreen-ready
wait_for_phase fullscreen-download-received
cmp -s "$GUEST_DOWNLOAD" "$RUN_DIR/guest-download-fullscreen.txt" || die 'fullscreen guest download bytes differ'
wait_for_phase qsf-deactivated-for-stop
wait_for_phase complete
if wait "$driver_pid"; then driver_status=0; else driver_status=$?; fi
driver_pid=''
[[ "$driver_status" == 0 ]] || die "Qt descriptor driver exited with status $driver_status"
grep -Fqx 'QSUNSHINE_QT_DESCRIPTOR_E2E_OK' "$DRIVER_LOG" || die 'Qt descriptor driver did not report success'

stop_owned_pid "$telemetry_capture_pid"
telemetry_capture_pid=''
guest_failed && die 'guest reported failure during E2E'
assert_no_secret_diagnostics
collect_remote_final_evidence

for item in windowed negotiated fullscreen; do
    [[ -s "$RUN_DIR/${item}-client.png" ]] || die "missing $item client image"
done
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
    "$RUN_DIR/windowed-client.png" >"$RUN_DIR/windowed-client.ffprobe"
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
    "$RUN_DIR/negotiated-client.png" >"$RUN_DIR/negotiated-client.ffprobe"
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 \
    "$RUN_DIR/fullscreen-client.png" >"$RUN_DIR/fullscreen-client.ffprobe"
grep -Fqx 'codec_name=png' "$RUN_DIR/windowed-client.ffprobe"
grep -Fqx "width=$WINDOWED_WIDTH" "$RUN_DIR/windowed-client.ffprobe"
grep -Fqx "height=$WINDOWED_HEIGHT" "$RUN_DIR/windowed-client.ffprobe"
grep -Fqx 'codec_name=png' "$RUN_DIR/negotiated-client.ffprobe"
grep -Fqx "width=$NEGOTIATED_WIDTH" "$RUN_DIR/negotiated-client.ffprobe"
grep -Fqx "height=$NEGOTIATED_HEIGHT" "$RUN_DIR/negotiated-client.ffprobe"
grep -Fqx 'codec_name=png' "$RUN_DIR/fullscreen-client.ffprobe"
grep -Fqx "width=$FULLSCREEN_WIDTH" "$RUN_DIR/fullscreen-client.ffprobe"
grep -Fqx "height=$FULLSCREEN_HEIGHT" "$RUN_DIR/fullscreen-client.ffprobe"

write_trace
printf 'QSM_PVE_DESCRIPTOR_QT_VIRGL_QSF_E2E_OK output=%s\n' "$RUN_DIR"
