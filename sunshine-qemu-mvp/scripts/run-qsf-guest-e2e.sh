#!/usr/bin/env bash
# EXPERIMENTAL cloud-init prototype.  It is retained as a provisioning sketch,
# but has no passing retained trace because the multi-CD datasource layout was
# not deterministic on the Alpine cloud image.  Use
# run-qsf-initramfs-guest-e2e.sh for the supported, passing real-KVM gate.
# QSF deliberately remains a local authenticated companion to a
# Moonlight/GameStream session; it is not a replacement wire protocol for
# Moonlight itself.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
QEMU_IMG_BINARY="${QEMU_IMG_BINARY:-qemu-img}"
PYTHON_BINARY="${PYTHON_BINARY:-python3}"
CC_BINARY="${CC_BINARY:-gcc}"
OUTPUT_PARENT="${OUTPUT_DIR:-$ROOT/artifacts/validation/qsf-guest-e2e}"
BASE_IMAGE="${QSF_BASE_IMAGE:-$ROOT/vm/alpine-virgl-3.20.10/generic_alpine-3.20.10-x86_64-bios-cloudinit-r0.qcow2}"
ACCEL="${QSF_ACCEL:-kvm}"
BOOT_TIMEOUT_SECONDS="${QSF_BOOT_TIMEOUT_SECONDS:-150}"
QSF_CONTROL="$ROOT/extensions/qsf_control/qsf_control.py"
QSF_CLIENT="$ROOT/extensions/qsf_control/qsf_client.py"
QSF_GUEST_SOURCE="$ROOT/guest/qsf_guest_agent.c"
QSF_USER_DATA="$ROOT/tests/fixtures/qsf-cloud-init-user-data.yaml"
QSF_META_DATA="$ROOT/tests/fixtures/qsf-cloud-init-meta-data.yaml"
CLIENT_CLIPBOARD="$ROOT/tests/fixtures/qsf-client-clipboard.txt"
GUEST_CLIPBOARD="$ROOT/tests/fixtures/qsf-guest-clipboard.txt"
CLIENT_UPLOAD="$ROOT/tests/fixtures/qsf-client-upload.txt"
GUEST_DOWNLOAD="$ROOT/tests/fixtures/qsf-guest-download.txt"
RESIZE_EXPECTED="$ROOT/tests/fixtures/qsf-resize-expected.txt"

die() {
  echo "QSF guest e2e: $*" >&2
  exit 1
}

if [[ "${1:-}" != "--inside-private-bus" ]]; then
  for required in "$QEMU_BINARY" "$QEMU_IMG_BINARY" "$PYTHON_BINARY" "$CC_BINARY" \
                  "$QSF_CONTROL" "$QSF_CLIENT" "$QSF_GUEST_SOURCE" \
                  "$QSF_USER_DATA" "$QSF_META_DATA" "$CLIENT_CLIPBOARD" \
                  "$GUEST_CLIPBOARD" "$CLIENT_UPLOAD" "$GUEST_DOWNLOAD" \
                  "$RESIZE_EXPECTED" dbus-run-session busctl cloud-localds genisoimage \
                  install mkdir mktemp grep cmp sha256sum awk stat sleep seq timeout; do
    if [[ -e "$required" ]]; then
      continue
    fi
    command -v "$required" >/dev/null || die "missing required command or path: $required"
  done
  [[ -f "$BASE_IMAGE" ]] || die "QSF base image is absent: $BASE_IMAGE"
  [[ "$BOOT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] ||
    die "QSF_BOOT_TIMEOUT_SECONDS must be a positive integer"
  [[ "$ACCEL" == 'kvm' || "$ACCEL" == 'tcg' ]] ||
    die "QSF_ACCEL must be kvm or tcg"
  if [[ "$ACCEL" == 'kvm' ]]; then
    [[ -r /dev/kvm && -w /dev/kvm ]] ||
      die "QSF_ACCEL=kvm requires readable and writable /dev/kvm"
  fi
  "$QEMU_BINARY" -display help | grep -Fxq dbus ||
    die "QEMU lacks the D-Bus display backend: $QEMU_BINARY"

  # A fresh run prevents old token material, guest markers, or cloud-init
  # state from turning a later invocation into a false positive.
  mkdir -p "$OUTPUT_PARENT"
  chmod 700 "$OUTPUT_PARENT"
  OUTPUT_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
  chmod 700 "$OUTPUT_DIR"
  printf 'QSF guest e2e evidence=%s\n' "$OUTPUT_DIR"

  seed_dir="$OUTPUT_DIR/nocloud-seed"
  payload_dir="$OUTPUT_DIR/payload"
  agent_binary="$OUTPUT_DIR/qsf-guest-agent"
  seed_iso="$OUTPUT_DIR/qsf-nocloud.iso"
  payload_iso="$OUTPUT_DIR/qsf-payload.iso"
  overlay="$OUTPUT_DIR/guest-overlay.qcow2"
  # Unix-domain socket paths are limited to 107 bytes on this host. Evidence
  # lives under a deliberately descriptive project directory, so keep only the
  # transient QEMU/control sockets in a short private /tmp directory.
  socket_dir="$(mktemp -d /tmp/qsf-guest-e2e.XXXXXX)"
  chmod 700 "$socket_dir"
  mkdir -p "$seed_dir" "$payload_dir"
  chmod 700 "$seed_dir" "$payload_dir"

  # Alpine's cloud image uses musl, so a static host-built x86-64 agent avoids
  # introducing a guest package/network dependency into the qualification.
  "$CC_BINARY" -std=c11 -O2 -static "$QSF_GUEST_SOURCE" -o "$agent_binary"
  test -x "$agent_binary" || die "failed to build static QSF guest agent"

  install -m 0644 "$QSF_USER_DATA" "$seed_dir/user-data"
  install -m 0644 "$QSF_META_DATA" "$seed_dir/meta-data"
  # Keep NoCloud conventional (only user-data and meta-data).  The payload is
  # a second labelled CD because adding binaries to the datasource volume made
  # Alpine cloud-init discovery dependent on implementation details.
  cloud-localds "$seed_iso" "$seed_dir/user-data" "$seed_dir/meta-data"
  install -m 0755 "$agent_binary" "$payload_dir/qsf-guest-agent"
  install -m 0644 "$CLIENT_CLIPBOARD" "$payload_dir/qsf-client-clipboard.txt"
  install -m 0644 "$GUEST_CLIPBOARD" "$payload_dir/qsf-guest-clipboard.txt"
  install -m 0644 "$GUEST_DOWNLOAD" "$payload_dir/qsf-guest-download.txt"
  install -m 0644 "$RESIZE_EXPECTED" "$payload_dir/qsf-resize-expected.txt"
  genisoimage -quiet -output "$payload_iso" -volid qsfpayload -joliet -rock "$payload_dir"
  "$QEMU_IMG_BINARY" create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$overlay" 2G

  dbus-run-session -- "$SCRIPT_PATH" --inside-private-bus "$OUTPUT_DIR" \
    "$BASE_IMAGE" "$overlay" "$seed_iso" "$payload_iso" "$ACCEL" "$BOOT_TIMEOUT_SECONDS" "$socket_dir"

  grep -Fqx 'QSF_GUEST_AGENT_STARTED' "$OUTPUT_DIR/guest-serial.log"
  grep -Fq '"agent": "ready"' "$OUTPUT_DIR/qsf-status.json"
  [[ "$(<"$OUTPUT_DIR/qsf-control.socket.mode")" == '600' ]] ||
    die "QSF control socket must be mode 0600"
  [[ "$(<"$OUTPUT_DIR/qsf-control.token.mode")" == '600' ]] ||
    die "QSF token must be mode 0600"

  client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
  guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
  client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
  guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
  grep -Fqx "QSF_GUEST_CLIENT_CLIPBOARD_SHA256=$client_clipboard_hash" "$OUTPUT_DIR/guest-serial.log"
  grep -Fqx "QSF_GUEST_REPLY_CLIPBOARD_SHA256=$guest_clipboard_hash" "$OUTPUT_DIR/guest-serial.log"
  cmp -s "$GUEST_CLIPBOARD" "$OUTPUT_DIR/client-received-clipboard.txt" ||
    die "guest-to-client clipboard bytes differ"
  grep -Fqx "QSF_GUEST_UPLOAD_SHA256=$client_upload_hash" "$OUTPUT_DIR/guest-serial.log"
  grep -Fqx "QSF_GUEST_DOWNLOAD_SHA256=$guest_download_hash" "$OUTPUT_DIR/guest-serial.log"
  cmp -s "$GUEST_DOWNLOAD" "$OUTPUT_DIR/client-downloaded-guest-file.txt" ||
    die "guest-to-client file bytes differ"
  grep -Fqx 'QSF_GUEST_RESIZE=1280x720' "$OUTPUT_DIR/guest-serial.log"
  grep -Fq '"qemu_set_ui_info": "applied"' "$OUTPUT_DIR/qsf-resize.json"

  {
    echo "QSF_GUEST_E2E"
    echo "qemu=$($QEMU_BINARY --version | head -n1)"
    echo "accel=$ACCEL"
    echo "base_image=$BASE_IMAGE"
    echo "base_image_sha256=$(sha256sum "$BASE_IMAGE" | awk '{print $1}')"
    echo "transport=0600 token-authenticated local QSF socket + QEMU virtio-serial + static Alpine guest agent"
    echo "clipboard_client_to_guest_sha256=$client_clipboard_hash"
    echo "clipboard_guest_to_client_sha256=$guest_clipboard_hash"
    echo "file_client_to_guest_sha256=$client_upload_hash"
    echo "file_guest_to_client_sha256=$guest_download_hash"
    echo "resize_request=1280x720"
    echo "qemu_set_ui_info=applied"
    echo "guest_resize_state=1280x720"
    echo "game_stream_transport=not-used-by-QSF"
    echo
    cat "$OUTPUT_DIR/guest-serial.log"
  } > "$OUTPUT_DIR/trace.txt"
  printf 'QSF_GUEST_E2E_OK output=%s\n' "$OUTPUT_DIR"
  exit 0
fi

[[ $# -eq 9 ]] || die "internal invocation has invalid arguments"
output_dir=$2
base_image=$3
overlay=$4
seed_iso=$5
payload_iso=$6
accel=$7
boot_timeout_seconds=$8
socket_dir=$9
agent_socket="$socket_dir/agent.sock"
control_socket="$socket_dir/control.sock"
token_file="$output_dir/qsf-control.token"
serial_log="$output_dir/guest-serial.log"
qemu_log="$output_dir/qemu.log"
control_log="$output_dir/qsf-control.log"

qemu_pid=''
control_pid=''
cleanup() {
  if [[ -n "$control_pid" ]]; then
    kill "$control_pid" 2>/dev/null || true
    wait "$control_pid" 2>/dev/null || true
  fi
  if [[ -n "$qemu_pid" ]]; then
    kill "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

"$QEMU_BINARY" \
  -name qsf-guest-e2e \
  -machine "q35,accel=$accel" \
  -smp 2 \
  -m 768 \
  -boot order=c \
  -drive "file=$overlay,if=virtio,format=qcow2" \
  -drive "file=$seed_iso,media=cdrom,readonly=on,format=raw" \
  -drive "file=$payload_iso,media=cdrom,readonly=on,format=raw" \
  -vga none \
  -device virtio-vga \
  -display dbus,gl=off \
  -chardev "socket,id=qsf_agent,path=$agent_socket,server=on,wait=off" \
  -device virtio-serial-pci,id=qsf_serial \
  -device virtserialport,chardev=qsf_agent,name=org.qsunshine.agent \
  -serial "file:$serial_log" \
  -monitor none \
  -nic user,model=virtio-net-pci \
  -no-reboot \
  -no-shutdown >"$qemu_log" 2>&1 &
qemu_pid=$!

for _ in $(seq 1 200); do
  if busctl --user --no-pager list 2>/dev/null | awk '{print $1}' | grep -Fxq org.qemu; then
    break
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { cat "$qemu_log" >&2 || true; die "QEMU exited early"; }
  sleep 0.05
done
busctl --user --no-pager list | awk '{print $1}' | grep -Fxq org.qemu ||
  die "QEMU did not expose org.qemu"

for _ in $(seq 1 100); do
  [[ -S "$agent_socket" ]] && break
  kill -0 "$qemu_pid" 2>/dev/null || { cat "$qemu_log" >&2 || true; die "QEMU exited before QSF socket"; }
  sleep 0.05
done
[[ -S "$agent_socket" ]] || die "QEMU did not create QSF virtio-serial socket"

"$PYTHON_BINARY" "$QSF_CONTROL" \
  --agent-socket "$agent_socket" \
  --control-socket "$control_socket" \
  --token-file "$token_file" >"$control_log" 2>&1 &
control_pid=$!

for _ in $(seq 1 100); do
  [[ -S "$control_socket" && -f "$token_file" ]] && break
  kill -0 "$control_pid" 2>/dev/null || { cat "$control_log" >&2 || true; die "QSF control exited early"; }
  sleep 0.05
done
[[ -S "$control_socket" && -f "$token_file" ]] || die "QSF control socket did not become ready"

wait_guest_marker() {
  local marker=$1
  local label=$2
  for _ in $(seq 1 "$boot_timeout_seconds"); do
    grep -Fqx "$marker" "$serial_log" 2>/dev/null && return 0
    kill -0 "$qemu_pid" 2>/dev/null || { cat "$qemu_log" >&2 || true; die "QEMU exited while waiting for $label"; }
    sleep 1
  done
  cat "$serial_log" >&2 || true
  die "guest did not report $label"
}

# Alpine cloud-init can need around a minute under TCG.  Wait for the guest's
# own serial marker instead of repeatedly opening client requests before its
# virtio port exists; then allow a short, bounded readiness race for READY.
wait_guest_marker 'QSF_GUEST_AGENT_STARTED' 'QSF guest agent startup'
status_ok=0
for _ in $(seq 1 10); do
  if timeout 3s "$PYTHON_BINARY" "$QSF_CLIENT" --socket "$control_socket" --token-file "$token_file" \
      status >"$output_dir/qsf-status.json" 2>"$output_dir/qsf-status.err"; then
    status_ok=1
    break
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { cat "$qemu_log" >&2 || true; die "QEMU exited while guest agent initialized"; }
  kill -0 "$control_pid" 2>/dev/null || { cat "$control_log" >&2 || true; die "QSF control exited while guest agent initialized"; }
  sleep 1
done
if [[ "$status_ok" != 1 ]]; then
  cat "$serial_log" >&2 || true
  cat "$control_log" >&2 || true
  die "guest QSF agent did not answer PING"
fi

"$PYTHON_BINARY" "$QSF_CLIENT" --socket "$control_socket" --token-file "$token_file" \
  clipboard-set < "$CLIENT_CLIPBOARD" >"$output_dir/qsf-clipboard-set.json"
client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
wait_guest_marker "QSF_GUEST_CLIENT_CLIPBOARD_SHA256=$client_clipboard_hash" 'client-to-guest clipboard hash'
wait_guest_marker "QSF_GUEST_REPLY_CLIPBOARD_SHA256=$guest_clipboard_hash" 'guest-to-client clipboard hash'
"$PYTHON_BINARY" "$QSF_CLIENT" --socket "$control_socket" --token-file "$token_file" \
  clipboard-get >"$output_dir/client-received-clipboard.txt"

"$PYTHON_BINARY" "$QSF_CLIENT" --socket "$control_socket" --token-file "$token_file" \
  upload "$CLIENT_UPLOAD" --name client-upload.txt >"$output_dir/qsf-upload.json"
client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
wait_guest_marker "QSF_GUEST_UPLOAD_SHA256=$client_upload_hash" 'client-to-guest file hash'

"$PYTHON_BINARY" "$QSF_CLIENT" --socket "$control_socket" --token-file "$token_file" \
  download guest-download.txt "$output_dir/client-downloaded-guest-file.txt" >"$output_dir/qsf-download.json"
guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
wait_guest_marker "QSF_GUEST_DOWNLOAD_SHA256=$guest_download_hash" 'guest-to-client file hash'

"$PYTHON_BINARY" "$QSF_CLIENT" --socket "$control_socket" --token-file "$token_file" \
  resize 1280 720 >"$output_dir/qsf-resize.json"
wait_guest_marker 'QSF_GUEST_RESIZE=1280x720' 'guest resize request state'

# qsf_control removes its UNIX socket during orderly shutdown. Preserve its
# permissions while it is live so the outer verifier can prove the local
# capability boundary without retaining an active endpoint after the test.
stat -c '%a' "$control_socket" > "$output_dir/qsf-control.socket.mode"
stat -c '%a' "$token_file" > "$output_dir/qsf-control.token.mode"
