#!/usr/bin/env bash
# Real QSF guest qualification with an Alpine Linux kernel and a deterministic
# static initramfs.  It deliberately has no host X11, guest network, SSH, or
# package-manager dependency.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
PYTHON_BINARY="${PYTHON_BINARY:-python3}"
CC_BINARY="${CC_BINARY:-gcc}"
BUSYBOX_BINARY="${BUSYBOX_BINARY:-/usr/bin/busybox}"
ALPINE_ISO="${QSF_ALPINE_ISO:-$ROOT/vm/alpine-virt-3.24.1/alpine-virt-3.24.1-x86_64.iso}"
ACCEL="${QSF_ACCEL:-kvm}"
BOOT_TIMEOUT_SECONDS="${QSF_BOOT_TIMEOUT_SECONDS:-45}"
OUTPUT_PARENT="${OUTPUT_DIR:-$ROOT/artifacts/validation/qsf-guest-e2e}"
QSF_CONTROL="$ROOT/extensions/qsf_control/qsf_control.py"
QSF_CLIENT="$ROOT/extensions/qsf_control/qsf_client.py"
# Optional executable adapter for a remote, authenticated companion client.
# It receives the usual qsf_client operation as positional arguments and these
# environment variables: QSF_CONTROL_SOCKET, QSF_TOKEN_FILE,
# QSF_AGENT_SOCKET, QSF_OUTPUT_DIR, QSF_CLIENT_BINARY, QSF_PYTHON_BINARY, and
# DBUS_SESSION_BUS_ADDRESS.  It must write the same JSON result to stdout and
# use stdin/stdout for payloads just like qsf_client.py.  No shell evaluation
# is performed.
QSF_COMPANION_HOOK="${QSF_COMPANION_HOOK:-}"
QSF_GUEST_SOURCE="$ROOT/guest/qsf_guest_agent.c"
INIT_SOURCE="$ROOT/tests/fixtures/qsf-initramfs-init.sh"
CLIENT_CLIPBOARD="$ROOT/tests/fixtures/qsf-client-clipboard.txt"
GUEST_CLIPBOARD="$ROOT/tests/fixtures/qsf-guest-clipboard.txt"
CLIENT_UPLOAD="$ROOT/tests/fixtures/qsf-client-upload.txt"
GUEST_DOWNLOAD="$ROOT/tests/fixtures/qsf-guest-download.txt"
RESIZE_EXPECTED="$ROOT/tests/fixtures/qsf-resize-expected.txt"

die() {
  echo "QSF initramfs guest e2e: $*" >&2
  exit 1
}

if [[ "${1:-}" != '--inside-private-bus' ]]; then
  for required in "$QEMU_BINARY" "$PYTHON_BINARY" "$CC_BINARY" "$BUSYBOX_BINARY" \
                  "$ALPINE_ISO" "$QSF_CONTROL" "$QSF_CLIENT" "$QSF_GUEST_SOURCE" \
                  "$INIT_SOURCE" "$CLIENT_CLIPBOARD" "$GUEST_CLIPBOARD" \
                  "$CLIENT_UPLOAD" "$GUEST_DOWNLOAD" "$RESIZE_EXPECTED" \
                  dbus-run-session busctl isoinfo cpio gzip install mkdir mktemp \
                  grep cmp sha256sum awk stat sleep seq timeout find; do
    if [[ -e "$required" ]]; then
      continue
    fi
    command -v "$required" >/dev/null || die "missing required command or path: $required"
  done
  [[ "$ACCEL" == 'kvm' || "$ACCEL" == 'tcg' ]] ||
    die "QSF_ACCEL must be kvm or tcg"
  [[ "$BOOT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] ||
    die "QSF_BOOT_TIMEOUT_SECONDS must be a positive integer"
  if [[ -n "$QSF_COMPANION_HOOK" && ! -x "$QSF_COMPANION_HOOK" ]]; then
    die "QSF_COMPANION_HOOK must name an executable adapter"
  fi
  if [[ "$ACCEL" == 'kvm' ]]; then
    [[ -r /dev/kvm && -w /dev/kvm ]] ||
      die "QSF_ACCEL=kvm requires readable and writable /dev/kvm; use sg kvm -c"
  fi
  "$QEMU_BINARY" -display help | grep -Fxq dbus ||
    die "QEMU lacks the D-Bus display backend: $QEMU_BINARY"

  mkdir -p "$OUTPUT_PARENT"
  chmod 700 "$OUTPUT_PARENT"
  OUTPUT_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
  chmod 700 "$OUTPUT_DIR"
  printf 'QSF initramfs guest e2e evidence=%s\n' "$OUTPUT_DIR"

  initroot="$OUTPUT_DIR/initroot"
  kernel="$OUTPUT_DIR/vmlinuz-virt"
  initrd="$OUTPUT_DIR/qsf-initramfs.cpio.gz"
  agent_binary="$OUTPUT_DIR/qsf-guest-agent"
  # QEMU's UNIX sockets have a 107-byte path ceiling; keep just live sockets
  # in a short private directory and retain all evidence under OUTPUT_DIR.
  socket_dir="$(mktemp -d /tmp/qsf-initramfs-e2e.XXXXXX)"
  chmod 700 "$socket_dir"
  mkdir -p "$initroot/bin" "$initroot/dev" "$initroot/proc" "$initroot/sys" "$initroot/run"

  isoinfo -R -i "$ALPINE_ISO" -x /boot/vmlinuz-virt > "$kernel"
  [[ -s "$kernel" ]] || die "could not extract Alpine virt kernel"
  "$CC_BINARY" -std=c11 -O2 -static "$QSF_GUEST_SOURCE" -o "$agent_binary"
  test -x "$agent_binary" || die "failed to build static QSF guest agent"

  install -m 0755 "$BUSYBOX_BINARY" "$initroot/bin/busybox"
  install -m 0755 "$INIT_SOURCE" "$initroot/init"
  install -m 0755 "$agent_binary" "$initroot/qsf-guest-agent"
  install -m 0644 "$CLIENT_CLIPBOARD" "$initroot/qsf-client-clipboard.txt"
  install -m 0644 "$GUEST_CLIPBOARD" "$initroot/qsf-guest-clipboard.txt"
  install -m 0644 "$GUEST_DOWNLOAD" "$initroot/qsf-guest-download.txt"
  install -m 0644 "$RESIZE_EXPECTED" "$initroot/qsf-resize-expected.txt"
  (
    cd "$initroot"
    find . -print | cpio --quiet -o -H newc | gzip -n > "$initrd"
  )
  [[ -s "$initrd" ]] || die "could not create QSF initramfs"

  dbus-run-session -- "$SCRIPT_PATH" --inside-private-bus "$OUTPUT_DIR" \
    "$kernel" "$initrd" "$ACCEL" "$BOOT_TIMEOUT_SECONDS" "$socket_dir"

  # A PC serial console uses CRLF.  Preserve its raw bytes as evidence and
  # make a normalized companion copy solely for exact protocol-marker checks.
  # (The guest agent's virtio protocol itself remains LF-delimited.)
  tr -d '\r' < "$OUTPUT_DIR/guest-serial.log" > "$OUTPUT_DIR/guest-serial.normalized.log"
  normalized_serial="$OUTPUT_DIR/guest-serial.normalized.log"

  client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
  guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
  client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
  guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
  grep -Fqx 'QSF_INITRAMFS_GUEST_E2E' "$normalized_serial"
  grep -Fqx 'QSF_GUEST_AGENT_STARTED' "$normalized_serial"
  grep -Fq '"agent": "ready"' "$OUTPUT_DIR/qsf-status.json"
  [[ "$(<"$OUTPUT_DIR/qsf-control.socket.mode")" == '600' ]] ||
    die "QSF control socket must be mode 0600"
  [[ "$(<"$OUTPUT_DIR/qsf-control.token.mode")" == '600' ]] ||
    die "QSF token must be mode 0600"
  grep -Fqx "QSF_GUEST_CLIENT_CLIPBOARD_SHA256=$client_clipboard_hash" "$normalized_serial"
  grep -Fqx "QSF_GUEST_REPLY_CLIPBOARD_SHA256=$guest_clipboard_hash" "$normalized_serial"
  cmp -s "$GUEST_CLIPBOARD" "$OUTPUT_DIR/client-received-clipboard.txt" ||
    die "guest-to-client QSF clipboard bytes differ"
  grep -Fqx "QSF_GUEST_UPLOAD_SHA256=$client_upload_hash" "$normalized_serial"
  grep -Fqx "QSF_GUEST_DOWNLOAD_SHA256=$guest_download_hash" "$normalized_serial"
  cmp -s "$GUEST_DOWNLOAD" "$OUTPUT_DIR/client-downloaded-guest-file.txt" ||
    die "guest-to-client QSF file bytes differ"
  grep -Fqx 'QSF_GUEST_RESIZE=1280x720' "$normalized_serial"
  grep -Fq '"qemu_set_ui_info": "applied"' "$OUTPUT_DIR/qsf-resize.json"

  {
    echo 'QSF_INITRAMFS_GUEST_E2E'
    echo "qemu=$($QEMU_BINARY --version | head -n1)"
    echo "accel=$ACCEL"
    echo "alpine_iso=$ALPINE_ISO"
    echo "alpine_iso_sha256=$(sha256sum "$ALPINE_ISO" | awk '{print $1}')"
    echo 'guest=official Alpine virt kernel + static initramfs QSF agent'
    echo 'transport=0600 token-authenticated local QSF socket + QEMU virtio-serial'
    if [[ -n "$QSF_COMPANION_HOOK" ]]; then
      echo 'companion=external adapter hook (operation-compatible with qsf_client)'
    else
      echo 'companion=extensions/qsf_control/qsf_client.py'
    fi
    echo "clipboard_client_to_guest_sha256=$client_clipboard_hash"
    echo "clipboard_guest_to_client_sha256=$guest_clipboard_hash"
    echo "file_client_to_guest_sha256=$client_upload_hash"
    echo "file_guest_to_client_sha256=$guest_download_hash"
    echo 'resize_request=1280x720'
    echo 'qemu_set_ui_info=applied'
    echo 'guest_resize_state=1280x720'
    echo 'clipboard_scope=QSF guest-agent state transport; not a desktop GUI clipboard bridge'
    echo 'host_x11_dependency=none'
    echo
    cat "$normalized_serial"
  } > "$OUTPUT_DIR/trace.txt"
  printf 'QSF_INITRAMFS_GUEST_E2E_OK output=%s\n' "$OUTPUT_DIR"
  exit 0
fi

[[ $# -eq 7 ]] || die "internal invocation has invalid arguments"
output_dir=$2
kernel=$3
initrd=$4
accel=$5
boot_timeout_seconds=$6
socket_dir=$7
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

qsf_companion() {
  if [[ -n "$QSF_COMPANION_HOOK" ]]; then
    QSF_CONTROL_SOCKET="$control_socket" \
    QSF_TOKEN_FILE="$token_file" \
    QSF_AGENT_SOCKET="$agent_socket" \
    QSF_OUTPUT_DIR="$output_dir" \
    QSF_CLIENT_BINARY="$QSF_CLIENT" \
    QSF_PYTHON_BINARY="$PYTHON_BINARY" \
      "$QSF_COMPANION_HOOK" "$@"
    return
  fi
  "$PYTHON_BINARY" "$QSF_CLIENT" --socket "$control_socket" --token-file "$token_file" "$@"
}

# `timeout` cannot invoke a shell function directly.  Keep the initial status
# probe bounded while allowing the optional adapter to remain an ordinary
# executable with the same stdin/stdout contract as qsf_client.py.
qsf_status_with_timeout() {
  local child=''
  qsf_companion status >"$output_dir/qsf-status.json" 2>"$output_dir/qsf-status.err" &
  child=$!
  for _ in $(seq 1 30); do
    if ! kill -0 "$child" 2>/dev/null; then
      wait "$child"
      return $?
    fi
    sleep 0.1
  done
  kill "$child" 2>/dev/null || true
  wait "$child" 2>/dev/null || true
  return 124
}

"$QEMU_BINARY" \
  -name qsf-initramfs-guest-e2e \
  -machine "q35,accel=$accel" \
  -smp 2 \
  -m 384 \
  -kernel "$kernel" \
  -initrd "$initrd" \
  -append 'console=ttyS0 rdinit=/init panic=-1' \
  -vga none \
  -device virtio-vga \
  -display dbus,gl=off \
  -chardev "socket,id=qsf_agent,path=$agent_socket,server=on,wait=off" \
  -device virtio-serial-pci,id=qsf_serial \
  -device virtserialport,chardev=qsf_agent,name=org.qsunshine.agent \
  -serial "file:$serial_log" \
  -monitor none \
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

wait_guest_marker() {
  local marker=$1
  local label=$2
  for _ in $(seq 1 "$boot_timeout_seconds"); do
    if [[ -f "$serial_log" ]] && tr -d '\r' < "$serial_log" | grep -Fqx "$marker"; then
      return 0
    fi
    kill -0 "$qemu_pid" 2>/dev/null || { cat "$qemu_log" >&2 || true; die "QEMU exited while waiting for $label"; }
    sleep 1
  done
  cat "$serial_log" >&2 || true
  die "guest did not report $label"
}

wait_guest_marker 'QSF_GUEST_AGENT_STARTED' 'QSF guest agent startup'

# Bring up the authenticated host bridge only after the guest has opened its
# virtio port.  This also makes a failed guest bootstrap distinct from a
# connection failure in qsf-control and avoids relying on a backend's handling
# of data written before its first host peer arrives.
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

status_ok=0
for _ in $(seq 1 10); do
  if qsf_status_with_timeout; then
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
  die 'guest QSF agent did not answer PING'
fi

qsf_companion clipboard-set < "$CLIENT_CLIPBOARD" >"$output_dir/qsf-clipboard-set.json"
client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
wait_guest_marker "QSF_GUEST_CLIENT_CLIPBOARD_SHA256=$client_clipboard_hash" 'client-to-guest clipboard hash'
wait_guest_marker "QSF_GUEST_REPLY_CLIPBOARD_SHA256=$guest_clipboard_hash" 'guest-to-client clipboard hash'
qsf_companion clipboard-get >"$output_dir/client-received-clipboard.txt"

qsf_companion upload "$CLIENT_UPLOAD" --name client-upload.txt >"$output_dir/qsf-upload.json"
client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
wait_guest_marker "QSF_GUEST_UPLOAD_SHA256=$client_upload_hash" 'client-to-guest file hash'

qsf_companion download guest-download.txt "$output_dir/client-downloaded-guest-file.txt" >"$output_dir/qsf-download.json"
guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
wait_guest_marker "QSF_GUEST_DOWNLOAD_SHA256=$guest_download_hash" 'guest-to-client file hash'

qsf_companion resize 1280 720 >"$output_dir/qsf-resize.json"
wait_guest_marker 'QSF_GUEST_RESIZE=1280x720' 'guest resize request state'

# qsf_control removes its UNIX socket during orderly shutdown. Preserve mode
# evidence while it is live without leaving a live capability after the test.
stat -c '%a' "$control_socket" > "$output_dir/qsf-control.socket.mode"
stat -c '%a' "$token_file" > "$output_dir/qsf-control.token.mode"
