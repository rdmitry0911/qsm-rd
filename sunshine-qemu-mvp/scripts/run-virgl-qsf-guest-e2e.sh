#!/usr/bin/env bash
# A combined, headless qualification of the real VirGL cloud guest and the
# authenticated QSF companion channel.  It is intentionally separate from
# Sunshine and from the Moonlight presentation test.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
QEMU_IMG_BINARY="${QEMU_IMG_BINARY:-qemu-img}"
PYTHON_BINARY="${PYTHON_BINARY:-python3}"
CC_BINARY="${CC_BINARY:-gcc}"
if [[ -x "$ROOT/.build-dmabuf/qemu-display-probe" ]]; then
  default_probe="$ROOT/.build-dmabuf/qemu-display-probe"
else
  default_probe="$ROOT/build-runtime/qemu-display-probe"
fi
DISPLAY_PROBE="${QEMU_DISPLAY_PROBE:-$default_probe}"
PROVISIONER="$ROOT/scripts/provision-alpine-virgl-guest.sh"
QSF_CONTROL="$ROOT/extensions/qsf_control/qsf_control.py"
QSF_CLIENT="$ROOT/extensions/qsf_control/qsf_client.py"
QSF_GUEST_SOURCE="$ROOT/guest/qsf_guest_agent.c"
USER_DATA_TEMPLATE="$ROOT/tests/fixtures/virgl-qsf-cloud-init-user-data.yaml.in"
META_DATA="$ROOT/tests/fixtures/virgl-qsf-cloud-init-meta-data.yaml"
CLIENT_CLIPBOARD="$ROOT/tests/fixtures/qsf-client-clipboard.txt"
GUEST_CLIPBOARD="$ROOT/tests/fixtures/qsf-guest-clipboard.txt"
CLIENT_UPLOAD="$ROOT/tests/fixtures/qsf-client-upload.txt"
GUEST_DOWNLOAD="$ROOT/tests/fixtures/qsf-guest-download.txt"
RESIZE_EXPECTED="$ROOT/tests/fixtures/qsf-resize-expected.txt"
ALPINE_VERSION="${VIRGL_ALPINE_VERSION:-3.20.10}"
VM_DIR="${VIRGL_VM_DIR:-$ROOT/vm/alpine-virgl-$ALPINE_VERSION}"
BASE_IMAGE="${VIRGL_BASE_IMAGE:-$VM_DIR/generic_alpine-${ALPINE_VERSION}-x86_64-bios-cloudinit-r0.qcow2}"
OUTPUT_PARENT="${VIRGL_QSF_OUTPUT_DIR:-$VM_DIR/qsf-e2e}"
ACCEL="${VIRGL_QSF_ACCEL:-kvm}"
QEMU_RUN_AS="${VIRGL_QEMU_RUN_AS:-$(id -un)}"
QEMU_USE_SUDO="${VIRGL_QEMU_USE_SUDO:-1}"
RENDER_NODE="${VIRGL_RENDER_NODE:-/dev/dri/renderD128}"
BOOT_TIMEOUT_SECONDS="${VIRGL_QSF_BOOT_TIMEOUT_SECONDS:-180}"
PROBE_DURATION_MS="${VIRGL_QSF_PROBE_DURATION_MS:-24000}"
DBUS_DESTINATION="${VIRGL_DBUS_DESTINATION:-org.qemu}"

die() {
  printf 'VirGL + QSF guest E2E: %s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" != '--inside-private-bus' ]]; then
  for required in "$QEMU_BINARY" "$QEMU_IMG_BINARY" "$PYTHON_BINARY" "$CC_BINARY" \
                  "$DISPLAY_PROBE" "$PROVISIONER" "$QSF_CONTROL" "$QSF_CLIENT" \
                  "$QSF_GUEST_SOURCE" "$USER_DATA_TEMPLATE" "$META_DATA" \
                  "$CLIENT_CLIPBOARD" "$GUEST_CLIPBOARD" "$CLIENT_UPLOAD" \
                  "$GUEST_DOWNLOAD" "$RESIZE_EXPECTED" \
                  cloud-localds genisoimage dbus-run-session busctl ffprobe \
                  awk sed grep cmp sha256sum install mkdir mktemp chmod stat \
                  sleep seq find head tail setsid date tr cat readlink; do
    if [[ -e "$required" ]]; then
      continue
    fi
    command -v "$required" >/dev/null || die "missing required command or path: $required"
  done
  [[ "$ACCEL" == 'kvm' || "$ACCEL" == 'tcg' ]] || die 'VIRGL_QSF_ACCEL must be kvm or tcg'
  [[ "$QEMU_USE_SUDO" == '0' || "$QEMU_USE_SUDO" == '1' ]] || die 'VIRGL_QEMU_USE_SUDO must be 0 or 1'
  [[ "$BOOT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_QSF_BOOT_TIMEOUT_SECONDS must be a positive integer'
  [[ "$PROBE_DURATION_MS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_QSF_PROBE_DURATION_MS must be a positive integer'
  if [[ "$QEMU_USE_SUDO" == '1' ]]; then
    command -v sudo >/dev/null || die 'VIRGL_QEMU_USE_SUDO=1 requires sudo'
  fi

  run_as_qemu_user() {
    if [[ "$QEMU_USE_SUDO" == '1' ]]; then
      sudo -n -u "$QEMU_RUN_AS" "$@"
    else
      "$@"
    fi
  }

  if [[ ! -f "$BASE_IMAGE" ]]; then
    [[ "${VIRGL_AUTO_PROVISION:-1}" == '1' ]] || die "base image is absent: $BASE_IMAGE"
    VIRGL_ALPINE_VERSION="$ALPINE_VERSION" VIRGL_VM_DIR="$VM_DIR" "$PROVISIONER"
  fi
  [[ -f "$BASE_IMAGE" ]] || die "base image is absent after provisioning: $BASE_IMAGE"
  [[ -c /dev/dri/renderD128 ]] || die 'native VirGL requires /dev/dri/renderD128'
  if ! run_as_qemu_user test -r /dev/dri/renderD128 -a -w /dev/dri/renderD128; then
    die "QEMU user $QEMU_RUN_AS cannot access /dev/dri/renderD128"
  fi
  if [[ "$ACCEL" == 'kvm' ]]; then
    [[ -c /dev/kvm ]] || die 'VIRGL_QSF_ACCEL=kvm requires /dev/kvm'
    if ! run_as_qemu_user test -r /dev/kvm -a -w /dev/kvm; then
      die "QEMU user $QEMU_RUN_AS cannot access /dev/kvm"
    fi
  fi
  "$QEMU_BINARY" -display help | grep -Fxq dbus || die "QEMU lacks the D-Bus display backend: $QEMU_BINARY"

  mkdir -p "$OUTPUT_PARENT"
  chmod 700 "$OUTPUT_PARENT"
  OUTPUT_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
  chmod 700 "$OUTPUT_DIR"
  printf 'VirGL + QSF guest E2E evidence=%s\n' "$OUTPUT_DIR"

  payload_dir="$OUTPUT_DIR/qsf-payload"
  agent_binary="$OUTPUT_DIR/qsf-guest-agent"
  user_data="$OUTPUT_DIR/user-data"
  seed_iso="$OUTPUT_DIR/nocloud.iso"
  payload_iso="$OUTPUT_DIR/qsf-payload.iso"
  overlay="$OUTPUT_DIR/guest-overlay.qcow2"
  socket_dir="$(mktemp -d /tmp/virgl-qsf-e2e.XXXXXX)"
  chmod 700 "$socket_dir"
  mkdir -p "$payload_dir"
  chmod 700 "$payload_dir"

  "$CC_BINARY" -std=c11 -O2 -static "$QSF_GUEST_SOURCE" -o "$agent_binary"
  test -x "$agent_binary" || die 'failed to build static QSF guest agent'
  install -m 0700 "$agent_binary" "$payload_dir/qsf-guest-agent"
  install -m 0644 "$CLIENT_CLIPBOARD" "$payload_dir/qsf-client-clipboard.txt"
  install -m 0644 "$GUEST_CLIPBOARD" "$payload_dir/qsf-guest-clipboard.txt"
  install -m 0644 "$CLIENT_UPLOAD" "$payload_dir/qsf-client-upload.txt"
  install -m 0644 "$GUEST_DOWNLOAD" "$payload_dir/qsf-guest-download.txt"
  install -m 0644 "$RESIZE_EXPECTED" "$payload_dir/qsf-resize-expected.txt"
  (
    cd "$payload_dir"
    LC_ALL=C sha256sum qsf-guest-agent qsf-client-clipboard.txt qsf-guest-clipboard.txt \
      qsf-client-upload.txt qsf-guest-download.txt qsf-resize-expected.txt > manifest.sha256
  )
  agent_sha256="$(sha256sum "$payload_dir/qsf-guest-agent" | awk '{print $1}')"
  manifest_sha256="$(sha256sum "$payload_dir/manifest.sha256" | awk '{print $1}')"
  sed -e "s/@AGENT_SHA256@/$agent_sha256/g" \
      -e "s/@PAYLOAD_MANIFEST_SHA256@/$manifest_sha256/g" \
      "$USER_DATA_TEMPLATE" > "$user_data"
  grep -Fq "$agent_sha256" "$user_data" || die 'could not materialize agent checksum in NoCloud user-data'
  grep -Fq "$manifest_sha256" "$user_data" || die 'could not materialize manifest checksum in NoCloud user-data'
  cloud-localds "$seed_iso" "$user_data" "$META_DATA"
  # The payload is a virtio block device, not a second CD.  That leaves the
  # NoCloud CIDATA CD unambiguous while keeping the static agent read-only.
  genisoimage -quiet -output "$payload_iso" -volid qsfvirglpayload -joliet -rock "$payload_dir"
  "$QEMU_IMG_BINARY" create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$overlay" 2G

  dbus-run-session -- "$SCRIPT_PATH" --inside-private-bus "$OUTPUT_DIR" "$overlay" "$seed_iso" \
    "$payload_iso" "$agent_sha256" "$manifest_sha256" "$ACCEL" "$BOOT_TIMEOUT_SECONDS" \
    "$PROBE_DURATION_MS" "$socket_dir"

  normalized_serial="$OUTPUT_DIR/guest-serial.normalized.log"
  tr -d '\r' < "$OUTPUT_DIR/guest-serial.log" > "$normalized_serial"
  client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
  guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
  client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
  guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
  telemetry="$OUTPUT_DIR/guest-telemetry.log"

  grep -Fqx "QSF_VIRGL_GUEST_PAYLOAD_MANIFEST_SHA256=$manifest_sha256" "$telemetry"
  grep -Fqx "QSF_VIRGL_GUEST_AGENT_SHA256=$agent_sha256" "$telemetry"
  grep -Fqx 'QSF_VIRGL_GUEST_AGENT_MODE=700' "$telemetry"
  grep -Fqx 'QSF_VIRGL_GUEST_AGENT_STARTED' "$telemetry"
  grep -Fqx 'guest_drm_driver=virtio_gpu' "$telemetry"
  guest_renderer="$(sed -n 's/^guest_gl_renderer=//p' "$telemetry" | head -n1)"
  [[ -n "$guest_renderer" ]] || die 'guest did not report a GL renderer'
  printf '%s' "$guest_renderer" | grep -qi virgl || die "guest is not using VirGL: $guest_renderer"
  printf '%s' "$guest_renderer" | grep -qi llvmpipe && die "native guest unexpectedly used llvmpipe: $guest_renderer"
  grep -Fqx 'QSF_VIRGL_GUEST_VIRGL_OK' "$telemetry"
  grep -Fq '"agent": "ready"' "$OUTPUT_DIR/qsf-status.json"
  [[ "$(<"$OUTPUT_DIR/qsf-control.socket.mode")" == '600' ]] || die 'QSF control socket is not mode 0600'
  [[ "$(<"$OUTPUT_DIR/qsf-control.token.mode")" == '600' ]] || die 'QSF token is not mode 0600'
  grep -Fqx "QSF_VIRGL_GUEST_CLIENT_CLIPBOARD_SHA256=$client_clipboard_hash" "$telemetry"
  grep -Fqx "QSF_VIRGL_GUEST_REPLY_CLIPBOARD_SHA256=$guest_clipboard_hash" "$telemetry"
  cmp -s "$GUEST_CLIPBOARD" "$OUTPUT_DIR/client-received-clipboard.txt" || die 'guest-to-client clipboard differs'
  grep -Fqx "QSF_VIRGL_GUEST_UPLOAD_SHA256=$client_upload_hash" "$telemetry"
  grep -Fqx "QSF_VIRGL_GUEST_QSF_DOWNLOAD_SHA256=$guest_download_hash" "$telemetry"
  cmp -s "$GUEST_DOWNLOAD" "$OUTPUT_DIR/client-downloaded-guest-file.txt" || die 'guest-to-client file differs'
  grep -Fqx 'QSF_VIRGL_GUEST_RESIZE=1280x720' "$telemetry"
  grep -Fq '"qemu_set_ui_info": "applied"' "$OUTPUT_DIR/qsf-resize.json"
  grep -Eq 'frames published/encoded/dropped: [1-9][0-9]*/[1-9][0-9]*/' "$OUTPUT_DIR/probe.log" || die 'Display1 probe did not encode frames'
  grep -Eq 'DMA-BUF scanouts/updates/failures: [1-9][0-9]*/[0-9]+/0' "$OUTPUT_DIR/probe.log" || die 'DMA-BUF readback had no scanout or failed'
  grep -Fq 'session errors: 0' "$OUTPUT_DIR/probe.log" || die 'Display1 probe reported session errors'
  grep -Fqx 'codec_name=h264' "$OUTPUT_DIR/ffprobe.txt" || die 'Display1 output is not H.264'
  grep -Fqx 'width=1280' "$OUTPUT_DIR/ffprobe.txt"
  grep -Fqx 'height=800' "$OUTPUT_DIR/ffprobe.txt" || die 'pre-resize 1280x800 scanout was not retained'
  grep -Fqx 'height=720' "$OUTPUT_DIR/ffprobe.txt" || die 'QSF SetUIInfo did not produce a 1280x720 scanout'

  {
    printf '%s\n' 'QSF_VIRGL_GUEST_E2E'
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'qemu=%s\n' "$("$QEMU_BINARY" --version | head -n1)"
    printf 'qemu_binary=%s\n' "$QEMU_BINARY"
    printf 'accel=%s\n' "$ACCEL"
    printf 'display=dbus,gl=on,rendernode=/dev/dri/renderD128\n'
    printf 'host_gui=none\n'
    printf 'guest=Alpine-cloud+verified-static-QSF-agent+native-VirGL\n'
    printf 'base_image=%s\n' "$BASE_IMAGE"
    printf 'base_image_sha512=%s\n' "$(sha512sum "$BASE_IMAGE" | awk '{print $1}')"
    printf 'payload_manifest_sha256=%s\n' "$manifest_sha256"
    printf 'static_agent_sha256=%s\n' "$agent_sha256"
    printf 'static_agent_guest_mode=0700\n'
    printf 'transport=0600-token-local-QSF-socket+virtio-serial+private-Display1-DBus+GBM-or-headless-EGL-DMABUF-readback\n'
    printf 'guest_drm_driver=virtio_gpu\n'
    printf 'guest_gl_renderer=%s\n' "$guest_renderer"
    printf 'clipboard_client_to_guest_sha256=%s\n' "$client_clipboard_hash"
    printf 'clipboard_guest_to_client_sha256=%s\n' "$guest_clipboard_hash"
    printf 'file_client_to_guest_sha256=%s\n' "$client_upload_hash"
    printf 'file_guest_to_client_sha256=%s\n' "$guest_download_hash"
    printf 'resize_request=1280x720\n'
    printf 'qemu_set_ui_info=applied\n'
    printf 'guest_resize_state=1280x720\n'
    printf 'display_geometry_transition=1280x800-to-1280x720\n'
    printf '\n[guest-telemetry]\n'
    cat "$telemetry"
    printf '\n[qsf-resize]\n'
    cat "$OUTPUT_DIR/qsf-resize.json"
    printf '\n[display1-probe]\n'
    cat "$OUTPUT_DIR/probe.log"
    printf '\n[encoded-h264]\n'
    cat "$OUTPUT_DIR/ffprobe.txt"
    printf '%s\n' 'QSF_VIRGL_GUEST_E2E_OK'
  } > "$OUTPUT_DIR/trace.txt"
  printf 'QSF_VIRGL_GUEST_E2E_OK output=%s\n' "$OUTPUT_DIR"
  exit 0
fi

[[ $# -eq 11 ]] || die 'internal invocation has invalid arguments'
output_dir=$2
overlay=$3
seed_iso=$4
payload_iso=$5
agent_sha256=$6
manifest_sha256=$7
accel=$8
boot_timeout_seconds=$9
probe_duration_ms=${10}
socket_dir=${11}

agent_socket="$socket_dir/agent.sock"
control_socket="$socket_dir/control.sock"
token_file="$output_dir/qsf-control.token"
serial_log="$output_dir/guest-serial.log"
telemetry_log="$output_dir/guest-telemetry.log"
qemu_log="$output_dir/qemu.log"
qemu_pidfile="$output_dir/qemu.pid"
control_log="$output_dir/qsf-control.log"
probe_log="$output_dir/probe.log"
encoded_dir="$output_dir/encoded"
qemu_pid=''
qemu_launcher_pid=''
control_pid=''
probe_pid=''

show_logs() {
  printf '%s\n' '--- QEMU log ---' >&2
  tail -160 "$qemu_log" >&2 2>/dev/null || true
  printf '%s\n' '--- guest telemetry ---' >&2
  cat "$telemetry_log" >&2 2>/dev/null || true
  printf '%s\n' '--- guest serial tail ---' >&2
  tail -160 "$serial_log" >&2 2>/dev/null || true
  printf '%s\n' '--- QSF control log ---' >&2
  cat "$control_log" >&2 2>/dev/null || true
  printf '%s\n' '--- Display1 probe ---' >&2
  cat "$probe_log" >&2 2>/dev/null || true
}

cleanup() {
  if [[ -n "$probe_pid" ]] && kill -0 "$probe_pid" 2>/dev/null; then
    kill "$probe_pid" 2>/dev/null || true
  fi
  if [[ -n "$control_pid" ]] && kill -0 "$control_pid" 2>/dev/null; then
    kill "$control_pid" 2>/dev/null || true
  fi
  if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
    kill "$qemu_pid" 2>/dev/null || true
  fi
  if [[ -n "$qemu_launcher_pid" ]] && kill -0 "$qemu_launcher_pid" 2>/dev/null; then
    kill "$qemu_launcher_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

[[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]] || die 'inner runner requires dbus-run-session'
mkdir -p "$encoded_dir"
qemu_args=(
  -name virgl-qsf-guest-e2e
  -nodefaults
  -machine "q35,accel=$accel"
  -smp 2
  -m 1536
  -boot order=c
  -drive "file=$overlay,if=virtio,format=qcow2"
  -drive "file=$seed_iso,media=cdrom,readonly=on,format=raw"
  -drive "file=$payload_iso,if=virtio,format=raw,readonly=on"
  -vga none
  -device virtio-vga-gl
  -display 'dbus,gl=on,rendernode=/dev/dri/renderD128'
  -chardev "socket,id=qsf_agent,path=$agent_socket,server=on,wait=off"
  -chardev "file,id=virgltelemetry,path=$telemetry_log"
  -device virtio-serial-pci,id=qsf-virgl-serial0
  -device virtserialport,chardev=qsf_agent,name=org.qsunshine.agent
  -device virtserialport,chardev=virgltelemetry,name=org.qsunshine.virgl.telemetry
  -serial "file:$serial_log"
  -monitor none
  -nic user,model=virtio-net-pci
  -no-reboot
  -pidfile "$qemu_pidfile"
)

if [[ "$QEMU_USE_SUDO" == '1' ]]; then
  setsid sudo -n -u "$QEMU_RUN_AS" env -u __EGL_VENDOR_LIBRARY_FILENAMES -u LIBGL_ALWAYS_SOFTWARE \
    "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS" QEMU_EGL_SURFACELESS_FALLBACK=0 \
    "$QEMU_BINARY" "${qemu_args[@]}" >"$qemu_log" 2>&1 &
else
  setsid env -u __EGL_VENDOR_LIBRARY_FILENAMES -u LIBGL_ALWAYS_SOFTWARE \
    "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS" QEMU_EGL_SURFACELESS_FALLBACK=0 \
    "$QEMU_BINARY" "${qemu_args[@]}" >"$qemu_log" 2>&1 &
fi
qemu_launcher_pid=$!

for _ in $(seq 1 200); do
  if [[ -s "$qemu_pidfile" ]]; then
    qemu_pid="$(<"$qemu_pidfile")"
    break
  fi
  kill -0 "$qemu_launcher_pid" 2>/dev/null || { show_logs; die 'QEMU exited before writing PID'; }
  sleep 0.05
done
[[ -n "$qemu_pid" ]] || { show_logs; die 'QEMU did not write PID'; }
for _ in $(seq 1 200); do
  if busctl --user --no-pager list 2>/dev/null | awk '{print $1}' | grep -Fxq "$DBUS_DESTINATION"; then
    break
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { show_logs; die 'QEMU exited before exposing Display1'; }
  sleep 0.05
done
busctl --user --no-pager list | awk '{print $1}' | grep -Fxq "$DBUS_DESTINATION" || {
  show_logs
  die "QEMU did not expose D-Bus destination $DBUS_DESTINATION"
}
for _ in $(seq 1 200); do
  [[ -S "$agent_socket" ]] && break
  kill -0 "$qemu_pid" 2>/dev/null || { show_logs; die 'QEMU exited before QSF socket'; }
  sleep 0.05
done
[[ -S "$agent_socket" ]] || die 'QEMU did not create QSF virtio-serial socket'

wait_marker() {
  local marker=$1
  local label=$2
  local iterations=$((boot_timeout_seconds * 10))
  for _ in $(seq 1 "$iterations"); do
    grep -Fqx "$marker" "$telemetry_log" 2>/dev/null && return 0
    if grep -F 'QSF_VIRGL_GUEST_E2E_FAILED=' "$telemetry_log" 2>/dev/null; then
      show_logs
      die "guest reported failure while waiting for $label"
    fi
    kill -0 "$qemu_pid" 2>/dev/null || { show_logs; die "QEMU exited while waiting for $label"; }
    sleep 0.1
  done
  show_logs
  die "guest did not report $label within ${boot_timeout_seconds}s"
}

wait_marker 'QSF_VIRGL_GUEST_AGENT_STARTED' 'verified static QSF agent startup'
"$PYTHON_BINARY" "$QSF_CONTROL" --agent-socket "$agent_socket" --control-socket "$control_socket" \
  --token-file "$token_file" >"$control_log" 2>&1 &
control_pid=$!
for _ in $(seq 1 100); do
  [[ -S "$control_socket" && -f "$token_file" ]] && break
  kill -0 "$control_pid" 2>/dev/null || { show_logs; die 'QSF control exited early'; }
  sleep 0.05
done
[[ -S "$control_socket" && -f "$token_file" ]] || die 'QSF control did not become ready'

qsf_client() {
  "$PYTHON_BINARY" "$QSF_CLIENT" --socket "$control_socket" --token-file "$token_file" "$@"
}
status_ok=0
for _ in $(seq 1 20); do
  if qsf_client status >"$output_dir/qsf-status.json" 2>"$output_dir/qsf-status.err"; then
    status_ok=1
    break
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { show_logs; die 'QEMU exited before QSF status'; }
  kill -0 "$control_pid" 2>/dev/null || { show_logs; die 'QSF control exited before status'; }
  sleep 0.5
done
[[ "$status_ok" == 1 ]] || { show_logs; die 'static QSF agent did not answer PING'; }

wait_marker 'QSF_VIRGL_GUEST_VIRGL_READY' 'native VirGL renderer readiness'
"$DISPLAY_PROBE" --dbus-address "$DBUS_SESSION_BUS_ADDRESS" --destination "$DBUS_DESTINATION" \
  --duration-ms "$probe_duration_ms" --no-audio --encode-dir "$encoded_dir" >"$probe_log" 2>&1 &
probe_pid=$!
# Allow RegisterListener and its initial 1280x800 scanout to settle before
# QSF calls SetUIInfo.  The probe then records both sides of the transition.
sleep 2

qsf_client clipboard-set < "$CLIENT_CLIPBOARD" >"$output_dir/qsf-clipboard-set.json"
client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
wait_marker "QSF_VIRGL_GUEST_CLIENT_CLIPBOARD_SHA256=$client_clipboard_hash" 'client-to-guest clipboard hash'
wait_marker "QSF_VIRGL_GUEST_REPLY_CLIPBOARD_SHA256=$guest_clipboard_hash" 'guest-to-client clipboard hash'
qsf_client clipboard-get >"$output_dir/client-received-clipboard.txt"

qsf_client upload "$CLIENT_UPLOAD" --name client-upload.txt >"$output_dir/qsf-upload.json"
client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
wait_marker "QSF_VIRGL_GUEST_UPLOAD_SHA256=$client_upload_hash" 'client-to-guest upload hash'
qsf_client download guest-download.txt "$output_dir/client-downloaded-guest-file.txt" >"$output_dir/qsf-download.json"
guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
wait_marker "QSF_VIRGL_GUEST_QSF_DOWNLOAD_SHA256=$guest_download_hash" 'guest-to-client download hash'

qsf_client resize 1280 720 >"$output_dir/qsf-resize.json"
wait_marker 'QSF_VIRGL_GUEST_RESIZE=1280x720' 'guest QSF resize state'
wait_marker 'QSF_VIRGL_GUEST_VIRGL_OK' 'KMS VirGL workload completion'

set +e
wait "$probe_pid"
probe_status=$?
set -e
probe_pid=''
printf '%s\n' "$probe_status" > "$output_dir/probe.exit-status"
[[ "$probe_status" == 0 ]] || { show_logs; die "Display1 probe failed with status $probe_status"; }

stat -c '%a' "$control_socket" > "$output_dir/qsf-control.socket.mode"
stat -c '%a' "$token_file" > "$output_dir/qsf-control.token.mode"
shopt -s nullglob
segments=("$encoded_dir"/*.mkv)
shopt -u nullglob
(( ${#segments[@]} > 0 )) || die 'Display1 probe produced no H.264 segments'
: > "$output_dir/ffprobe.txt"
for segment in "${segments[@]}"; do
  [[ -s "$segment" ]] || continue
  printf '%s\n' "[$(basename "$segment")]" >> "$output_dir/ffprobe.txt"
  ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,pix_fmt \
    -of default=noprint_wrappers=1 "$segment" >> "$output_dir/ffprobe.txt"
done
