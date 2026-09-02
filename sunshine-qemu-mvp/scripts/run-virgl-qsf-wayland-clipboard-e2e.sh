#!/usr/bin/env bash
# Native KVM VirGL + Weston DRM + QSF clipboard/files/resize qualification.
# The host stays headless; Weston, seatd, udevd and wl-clipboard are guest-only.
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
QSF_WAYLAND_BRIDGE="$ROOT/guest/qsf_wayland_clipboard_bridge.sh"
QSF_INPUT_WATCHER_SOURCE="$ROOT/guest/qsf_input_watcher.c"
USER_DATA_TEMPLATE="$ROOT/tests/fixtures/virgl-qsf-wayland-cloud-init-user-data.yaml.in"
META_DATA="$ROOT/tests/fixtures/virgl-qsf-wayland-cloud-init-meta-data.yaml"
CLIENT_CLIPBOARD="$ROOT/tests/fixtures/qsf-client-clipboard.txt"
GUEST_CLIPBOARD="$ROOT/tests/fixtures/qsf-guest-clipboard.txt"
CLIENT_UPLOAD="$ROOT/tests/fixtures/qsf-client-upload.txt"
GUEST_DOWNLOAD="$ROOT/tests/fixtures/qsf-guest-download.txt"
RESIZE_EXPECTED="$ROOT/tests/fixtures/qsf-resize-expected.txt"
ALPINE_VERSION="${VIRGL_ALPINE_VERSION:-3.20.10}"
VM_DIR="${VIRGL_VM_DIR:-$ROOT/vm/alpine-virgl-$ALPINE_VERSION}"
BASE_IMAGE="${VIRGL_BASE_IMAGE:-$VM_DIR/generic_alpine-${ALPINE_VERSION}-x86_64-bios-cloudinit-r0.qcow2}"
OUTPUT_PARENT="${VIRGL_QSF_WAYLAND_OUTPUT_DIR:-$VM_DIR/wayland-qsf-e2e}"
ACCEL="${VIRGL_QSF_WAYLAND_ACCEL:-kvm}"
QEMU_RUN_AS="${VIRGL_QEMU_RUN_AS:-$(id -un)}"
QEMU_USE_SUDO="${VIRGL_QEMU_USE_SUDO:-1}"
RENDER_NODE="${VIRGL_RENDER_NODE:-/dev/dri/renderD128}"
BOOT_TIMEOUT_SECONDS="${VIRGL_QSF_WAYLAND_BOOT_TIMEOUT_SECONDS:-210}"
# A short closed pre-hook capture proves the original mode. The longer live
# listener remains active through an optional Moonlight/Sunshine hook and the
# QSF actions; increase it if a hook intentionally runs longer than a minute.
PREHOOK_PROBE_DURATION_MS="${VIRGL_QSF_WAYLAND_PREHOOK_PROBE_DURATION_MS:-4000}"
PROBE_DURATION_MS="${VIRGL_QSF_WAYLAND_PROBE_DURATION_MS:-60000}"
POST_AGENT_READY_HOOK="${VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK:-}"
# In the default mode this runner owns the local QSF socket directly.  The
# Qt composite mode transfers that ownership to the post-ready hook so a real
# QsfClient, rather than this legacy Python helper, performs clipboard, files
# and resize against the same guest.  It is deliberately a two-value switch:
# any typo fails before a VM is created.
QT_QSF_OWNER="${VIRGL_QSF_WAYLAND_QT_QSF_OWNER:-outer}"
export QT_QSF_OWNER
DBUS_DESTINATION=org.qemu

die() {
  printf 'VirGL QSF Wayland E2E: %s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" != '--inside-private-bus' ]]; then
  for required in "$QEMU_BINARY" "$QEMU_IMG_BINARY" "$PYTHON_BINARY" "$CC_BINARY" \
                  "$DISPLAY_PROBE" "$PROVISIONER" "$QSF_CONTROL" "$QSF_CLIENT" \
                  "$QSF_GUEST_SOURCE" "$QSF_WAYLAND_BRIDGE" "$QSF_INPUT_WATCHER_SOURCE" \
                  "$USER_DATA_TEMPLATE" "$META_DATA" \
                  "$CLIENT_CLIPBOARD" "$GUEST_CLIPBOARD" "$CLIENT_UPLOAD" "$GUEST_DOWNLOAD" \
                  "$RESIZE_EXPECTED" cloud-localds genisoimage dbus-run-session busctl ffprobe \
                  awk sed grep cmp sha256sum install mkdir mktemp chmod stat sleep seq find head tail \
                  setsid date tr cat readlink; do
    if [[ -e "$required" ]]; then
      continue
    fi
    command -v "$required" >/dev/null || die "missing required command or path: $required"
  done
  [[ "$ACCEL" == kvm || "$ACCEL" == tcg ]] || die 'VIRGL_QSF_WAYLAND_ACCEL must be kvm or tcg'
  [[ "$QEMU_USE_SUDO" == 0 || "$QEMU_USE_SUDO" == 1 ]] || die 'VIRGL_QEMU_USE_SUDO must be 0 or 1'
  [[ "$BOOT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_QSF_WAYLAND_BOOT_TIMEOUT_SECONDS must be positive'
  [[ "$PREHOOK_PROBE_DURATION_MS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_QSF_WAYLAND_PREHOOK_PROBE_DURATION_MS must be positive'
  [[ "$PROBE_DURATION_MS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_QSF_WAYLAND_PROBE_DURATION_MS must be positive'
  [[ "$QT_QSF_OWNER" == outer || "$QT_QSF_OWNER" == qt ]] || \
    die 'VIRGL_QSF_WAYLAND_QT_QSF_OWNER must be outer or qt'
  if [[ -n "$POST_AGENT_READY_HOOK" && ! -x "$POST_AGENT_READY_HOOK" ]]; then
    die "VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK is not an executable: $POST_AGENT_READY_HOOK"
  fi
  if [[ "$QT_QSF_OWNER" == qt && -z "$POST_AGENT_READY_HOOK" ]]; then
    die 'Qt QSF ownership requires VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK'
  fi
  if [[ "$QEMU_USE_SUDO" == 1 ]]; then
    command -v sudo >/dev/null || die 'VIRGL_QEMU_USE_SUDO=1 requires sudo'
  fi

  run_as_qemu_user() {
    if [[ "$QEMU_USE_SUDO" == 1 ]]; then
      sudo -n -u "$QEMU_RUN_AS" "$@"
    else
      "$@"
    fi
  }
  if [[ ! -f "$BASE_IMAGE" ]]; then
    [[ "${VIRGL_AUTO_PROVISION:-1}" == 1 ]] || die "base image is absent: $BASE_IMAGE"
    VIRGL_ALPINE_VERSION="$ALPINE_VERSION" VIRGL_VM_DIR="$VM_DIR" "$PROVISIONER"
  fi
  [[ -f "$BASE_IMAGE" ]] || die "base image is absent after provisioning: $BASE_IMAGE"
  [[ -c "$RENDER_NODE" ]] || die "native VirGL requires render node: $RENDER_NODE"
  run_as_qemu_user test -r "$RENDER_NODE" -a -w "$RENDER_NODE" \
    || die "QEMU user $QEMU_RUN_AS cannot access $RENDER_NODE"
  if [[ "$ACCEL" == kvm ]]; then
    [[ -c /dev/kvm ]] || die 'KVM requested but /dev/kvm is absent'
    run_as_qemu_user test -r /dev/kvm -a -w /dev/kvm \
      || die "QEMU user $QEMU_RUN_AS cannot access /dev/kvm"
  fi
  "$QEMU_BINARY" -display help | grep -Fxq dbus || die 'QEMU lacks Display1 D-Bus backend'

  mkdir -p "$OUTPUT_PARENT"
  chmod 700 "$OUTPUT_PARENT"
  OUTPUT_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
  chmod 700 "$OUTPUT_DIR"
  payload_dir="$OUTPUT_DIR/qsf-payload"
  agent_binary="$OUTPUT_DIR/qsf-guest-agent"
  input_watcher_binary="$OUTPUT_DIR/qsf-input-watcher"
  user_data="$OUTPUT_DIR/user-data"
  seed_iso="$OUTPUT_DIR/nocloud.iso"
  payload_iso="$OUTPUT_DIR/qsf-payload.iso"
  overlay="$OUTPUT_DIR/guest-overlay.qcow2"
  socket_dir="$(mktemp -d /tmp/virgl-qsf-wayland.XXXXXX)"
  chmod 700 "$socket_dir"
  # The inner runner owns the live sockets and must finish its process cleanup
  # before this directory disappears.  On a normal success/error return this
  # EXIT trap removes the private directory; on a signal while the child is
  # still active it deliberately leaves it alone rather than unlinking a live
  # QEMU chardev socket underneath that child.
  inner_runner_active=0
  cleanup_socket_dir() {
    [[ "${inner_runner_active:-0}" == 0 ]] || return 0
    case "${socket_dir:-}" in
      /tmp/virgl-qsf-wayland.*) ;;
      *) return 0 ;;
    esac
    [[ -d "$socket_dir" ]] || return 0
    rm -rf -- "$socket_dir"
  }
  trap cleanup_socket_dir EXIT
  mkdir -p "$payload_dir"
  chmod 700 "$payload_dir"

  "$CC_BINARY" -std=c11 -O2 -static "$QSF_GUEST_SOURCE" -o "$agent_binary"
  test -x "$agent_binary" || die 'failed to build static QSF guest agent'
  "$CC_BINARY" -std=c11 -O2 -static "$QSF_INPUT_WATCHER_SOURCE" -o "$input_watcher_binary"
  test -x "$input_watcher_binary" || die 'failed to build static guest input watcher'
  install -m 0700 "$agent_binary" "$payload_dir/qsf-guest-agent"
  install -m 0700 "$input_watcher_binary" "$payload_dir/qsf-input-watcher"
  install -m 0700 "$QSF_WAYLAND_BRIDGE" "$payload_dir/qsf-wayland-clipboard-bridge"
  install -m 0644 "$CLIENT_CLIPBOARD" "$payload_dir/qsf-client-clipboard.txt"
  install -m 0644 "$GUEST_CLIPBOARD" "$payload_dir/qsf-guest-clipboard.txt"
  install -m 0644 "$CLIENT_UPLOAD" "$payload_dir/qsf-client-upload.txt"
  install -m 0644 "$GUEST_DOWNLOAD" "$payload_dir/qsf-guest-download.txt"
  install -m 0644 "$RESIZE_EXPECTED" "$payload_dir/qsf-resize-expected.txt"
  (
    cd "$payload_dir"
    LC_ALL=C sha256sum qsf-guest-agent qsf-input-watcher qsf-wayland-clipboard-bridge \
      qsf-client-clipboard.txt qsf-guest-clipboard.txt qsf-client-upload.txt \
      qsf-guest-download.txt qsf-resize-expected.txt > manifest.sha256
  )
  agent_sha256="$(sha256sum "$payload_dir/qsf-guest-agent" | awk '{print $1}')"
  input_watcher_sha256="$(sha256sum "$payload_dir/qsf-input-watcher" | awk '{print $1}')"
  bridge_sha256="$(sha256sum "$payload_dir/qsf-wayland-clipboard-bridge" | awk '{print $1}')"
  manifest_sha256="$(sha256sum "$payload_dir/manifest.sha256" | awk '{print $1}')"
  sed -e "s/@AGENT_SHA256@/$agent_sha256/g" \
      -e "s/@INPUT_WATCHER_SHA256@/$input_watcher_sha256/g" \
      -e "s/@BRIDGE_SHA256@/$bridge_sha256/g" \
      -e "s/@PAYLOAD_MANIFEST_SHA256@/$manifest_sha256/g" \
      "$USER_DATA_TEMPLATE" > "$user_data"
  grep -Fq "$agent_sha256" "$user_data" || die 'could not materialize agent checksum'
  grep -Fq "$input_watcher_sha256" "$user_data" || die 'could not materialize input watcher checksum'
  grep -Fq "$bridge_sha256" "$user_data" || die 'could not materialize bridge checksum'
  grep -Fq "$manifest_sha256" "$user_data" || die 'could not materialize manifest checksum'
  cloud-localds "$seed_iso" "$user_data" "$META_DATA"
  genisoimage -quiet -output "$payload_iso" -volid qsfvirglwayland -joliet -rock "$payload_dir"
  "$QEMU_IMG_BINARY" create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$overlay" 2G
  printf 'VirGL QSF Wayland E2E evidence=%s\n' "$OUTPUT_DIR"

  inner_runner_active=1
  set +e
  dbus-run-session -- "$SCRIPT_PATH" --inside-private-bus "$OUTPUT_DIR" "$overlay" "$seed_iso" \
    "$payload_iso" "$agent_sha256" "$bridge_sha256" "$manifest_sha256" "$ACCEL" \
    "$BOOT_TIMEOUT_SECONDS" "$PREHOOK_PROBE_DURATION_MS" "$PROBE_DURATION_MS" "$socket_dir"
  inner_runner_status=$?
  set -e
  inner_runner_active=0
  [[ "$inner_runner_status" == 0 ]] || exit "$inner_runner_status"

  telemetry="$OUTPUT_DIR/guest-telemetry.log"
  client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
  guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
  client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
  guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_PAYLOAD_MANIFEST_SHA256=$manifest_sha256" "$telemetry"
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_AGENT_SHA256=$agent_sha256" "$telemetry"
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCHER_SHA256=$input_watcher_sha256" "$telemetry"
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_BRIDGE_SHA256=$bridge_sha256" "$telemetry"
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_AGENT_MODE=700' "$telemetry"
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCHER_MODE=700' "$telemetry"
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_BRIDGE_MODE=700' "$telemetry"
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_AGENT_STARTED' "$telemetry"
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCHER_STARTED' "$telemetry"
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_BRIDGE_STARTED' "$telemetry"
  grep -Fqx 'guest_drm_driver=virtio_gpu' "$telemetry"
  grep -Eq '^guest_wayland_input=/dev/input/event[0-9]+$' "$telemetry"
  grep -Fqx 'guest_wayland_seat=seatd' "$telemetry"
  grep -Fqx 'guest_wayland_compositor=weston-drm' "$telemetry"
  wayland_packages=(
    mesa-dri-gallium mesa-egl mesa-utils
    weston weston-backend-drm weston-shell-desktop weston-clients
    wl-clipboard wayland-utils seatd eudev gnu-libiconv
  )
  wayland_package_markers=()
  for package in "${wayland_packages[@]}"; do
    package_marker="$(grep -E "^guest_wayland_package_${package}=${package}-[^[:space:]]+$" "$telemetry" | head -n1 || true)"
    [[ -n "$package_marker" ]] || die "guest did not report installed Wayland package version: $package"
    wayland_package_markers+=("$package_marker")
  done
  guest_renderer="$(sed -n 's/^guest_gl_renderer=//p' "$telemetry" | head -n1)"
  [[ -n "$guest_renderer" ]] || die 'guest did not report a GL renderer'
  printf '%s' "$guest_renderer" | grep -qi virgl || die "guest is not using VirGL: $guest_renderer"
  printf '%s' "$guest_renderer" | grep -qi llvmpipe && die "native guest unexpectedly used llvmpipe: $guest_renderer"
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_VIRGL_OK' "$telemetry"
  grep -Fq '"agent": "ready"' "$OUTPUT_DIR/qsf-status.json"
  [[ "$(<"$OUTPUT_DIR/qsf-control.socket.mode")" == 600 ]] || die 'QSF control socket is not mode 0600'
  [[ "$(<"$OUTPUT_DIR/qsf-control.token.mode")" == 600 ]] || die 'QSF token is not mode 0600'
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_STATE_SHA256=$client_clipboard_hash" "$telemetry"
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_WL_PASTE_SHA256=$client_clipboard_hash" "$telemetry"
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_NATIVE_WL_COPY_SHA256=$guest_clipboard_hash" "$telemetry"
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_WAYLAND_TO_QSF_STATE_SHA256=$guest_clipboard_hash" "$telemetry"
  cmp -s "$GUEST_CLIPBOARD" "$OUTPUT_DIR/client-received-clipboard.txt" || die 'native Wayland clipboard did not return through QSF'
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_UPLOAD_SHA256=$client_upload_hash" "$telemetry"
  grep -Fqx "QSF_VIRGL_WAYLAND_GUEST_QSF_DOWNLOAD_SHA256=$guest_download_hash" "$telemetry"
  cmp -s "$GUEST_DOWNLOAD" "$OUTPUT_DIR/client-downloaded-guest-file.txt" || die 'guest-to-client file differs'
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_RESIZE_RECONFIGURE=weston-drm-restart' "$telemetry"
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_RESIZE_WESTON_RESTARTED' "$telemetry"
  grep -Fqx 'QSF_VIRGL_WAYLAND_GUEST_RESIZE=1280x720' "$telemetry"
  grep -Eq '^QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_APPLIED=version=2,generation=[1-9][0-9]*,resolution=1280x720,fps=[1-9][0-9]*,bitrate_kbps=[1-9][0-9]*,video_codec=(H264|HEVC|AV1)$' \
    "$telemetry" || die 'guest did not acknowledge a canonical negotiated profile after its Weston scanout'
  grep -Eq '^QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_ACK_OBSERVED=generation=[1-9][0-9]*,resolution=1280x720$' \
    "$telemetry" || die 'guest coordinator did not independently observe the exact scanout acknowledgement'
  if [[ "$QT_QSF_OWNER" == qt ]]; then
    grep -Eq '^QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_APPLIED=version=2,generation=[1-9][0-9]*,resolution=1600x900,fps=60,bitrate_kbps=12000,video_codec=H264$' \
      "$telemetry" || die 'guest did not acknowledge the requested fullscreen scanout after Weston restart'
    grep -Eq '^QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_ACK_OBSERVED=generation=[1-9][0-9]*,resolution=1600x900$' \
      "$telemetry" || die 'guest coordinator did not observe the exact fullscreen scanout acknowledgement'
    qt_summary="$OUTPUT_DIR/qsunshine-qt-e2e-summary.txt"
    [[ -f "$qt_summary" ]] || die 'Qt QSF owner did not produce its atomic success summary'
    grep -Fqx 'QSUNSHINE_QT_E2E_SUMMARY_VERSION=1' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_QSF_OPERATIONS_OK=1' "$qt_summary"
    grep -Fqx "QSUNSHINE_QT_QSF_CLIENT_CLIPBOARD_SHA256=$client_clipboard_hash" "$qt_summary"
    grep -Fqx "QSUNSHINE_QT_QSF_GUEST_CLIPBOARD_SHA256=$guest_clipboard_hash" "$qt_summary"
    grep -Fqx "QSUNSHINE_QT_QSF_UPLOAD_SHA256=$client_upload_hash" "$qt_summary"
    grep -Fqx "QSUNSHINE_QT_QSF_DOWNLOAD_SHA256=$guest_download_hash" "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_QSF_RESIZE=1280x720' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_QSF_QEMU_SET_UI_INFO=applied' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_QSF_GUEST_SCANOUT_ACK=observed' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_QSF_FULLSCREEN_GUEST_SCANOUT_ACK=observed' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_MOONLIGHT_WINDOWED_AND_FULLSCREEN_OK=1' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_MOONLIGHT_INPUT_E2E_OK=1' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_MOONLIGHT_FULLSCREEN_INPUT_E2E_OK=1' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_QSF_FULLSCREEN_DOWNLOAD_OK=1' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_SYSTEM_AUTH_GAMESTREAM_LEASE_OK=1' "$qt_summary"
    grep -Fqx 'QSUNSHINE_QT_SYSTEM_AUTH_NO_PIN_FALLBACK_OK=1' "$qt_summary"
  else
    [[ -f "$OUTPUT_DIR/qsf-connection-optimize.json" ]] || die \
      'native QSF profile negotiation did not retain its result'
    grep -Fq '"qemu_set_ui_info": "applied"' "$OUTPUT_DIR/qsf-connection-optimize.json"
  fi
  grep -Fqx 'codec_name=h264' "$OUTPUT_DIR/prehook-ffprobe.txt" || die 'pre-hook Display1 output is not H.264'
  grep -Fqx 'width=1280' "$OUTPUT_DIR/prehook-ffprobe.txt"
  grep -Fqx 'height=800' "$OUTPUT_DIR/prehook-ffprobe.txt" || die 'pre-hook capture did not establish 1280x800'
  grep -Eq 'frames published/encoded/dropped: [1-9][0-9]*/[1-9][0-9]*/' "$OUTPUT_DIR/probe.log" || die 'Display1 probe did not encode frames'
  grep -Eq 'DMA-BUF scanouts/updates/failures: [1-9][0-9]*/[0-9]+/0' "$OUTPUT_DIR/probe.log" || die 'DMA-BUF readback failed'
  grep -Fq 'session errors: 0' "$OUTPUT_DIR/probe.log" || die 'Display1 probe reported session errors'
  grep -Fqx 'codec_name=h264' "$OUTPUT_DIR/ffprobe.txt" || die 'Display1 output is not H.264'
  grep -Fqx 'width=1280' "$OUTPUT_DIR/ffprobe.txt"
  grep -Fqx 'height=720' "$OUTPUT_DIR/ffprobe.txt" || die 'QSF SetUIInfo did not produce a 1280x720 scanout'

  {
    printf '%s\n' 'QSF_VIRGL_WAYLAND_GUEST_E2E'
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'qemu=%s\n' "$("$QEMU_BINARY" --version | head -n1)"
    printf 'accel=%s\n' "$ACCEL"
    printf 'display=dbus,gl=on,rendernode=%s\n' "$RENDER_NODE"
    printf 'host_gui=none\n'
    printf 'guest=Alpine-cloud+verified-static-QSF-agent+Weston-DRM+native-VirGL\n'
    printf 'static_input_watcher_sha256=%s\n' "$input_watcher_sha256"
    printf 'guest_drm_driver=virtio_gpu\n'
    printf 'guest_gl_renderer=%s\n' "$guest_renderer"
    printf 'guest_wayland_compositor=weston-drm+seatd+eudev\n'
    for package_marker in "${wayland_package_markers[@]}"; do
      printf '%s\n' "$package_marker"
    done
    printf 'clipboard_client_to_wayland_sha256=%s\n' "$client_clipboard_hash"
    printf 'clipboard_wayland_to_client_sha256=%s\n' "$guest_clipboard_hash"
    printf 'file_client_to_guest_sha256=%s\n' "$client_upload_hash"
    printf 'file_guest_to_client_sha256=%s\n' "$guest_download_hash"
    printf 'requested_guest_scanout=1280x720\n'
    printf 'qemu_set_ui_info=applied before guest profile commit\n'
    printf 'guest_scanout_ack=canonical connection-profile-applied after Weston current-mode check\n'
    printf 'qsf_operation_owner=%s\n' "$QT_QSF_OWNER"
    printf 'post_agent_ready_hook=%s\n' "${POST_AGENT_READY_HOOK:+executed}"
    if [[ -n "$POST_AGENT_READY_HOOK" ]]; then
      printf 'post_hook_input=KEY_A+ABS+BTN observed by guest evdev\n'
    fi
    if [[ "$QT_QSF_OWNER" == qt ]]; then
      printf 'post_fullscreen_input=KEY_B+ABS+BTN observed by guest evdev after reconnect\n'
      printf 'post_fullscreen_qsf=authenticated guest-download.txt byte-for-byte verification\n'
    fi
    printf '\n[guest-telemetry]\n'
    cat "$telemetry"
    if [[ "$QT_QSF_OWNER" == qt ]]; then
      printf '\n[qt-qsf-summary]\n'
      cat "$OUTPUT_DIR/qsunshine-qt-e2e-summary.txt"
      printf '\n[qt-moonlight-hook]\n'
      cat "$OUTPUT_DIR/qsunshine-qt-moonlight-hook/trace.txt"
    else
      printf '\n[qsf-connection-optimize]\n'
      cat "$OUTPUT_DIR/qsf-connection-optimize.json"
    fi
    printf '\n[display1-probe]\n'
    cat "$OUTPUT_DIR/probe.log"
    printf '\n[prehook-display1-probe]\n'
    cat "$OUTPUT_DIR/prehook-probe.log"
    printf '\n[prehook-encoded-h264]\n'
    cat "$OUTPUT_DIR/prehook-ffprobe.txt"
    printf '\n[encoded-h264]\n'
    cat "$OUTPUT_DIR/ffprobe.txt"
    printf '%s\n' 'QSF_VIRGL_WAYLAND_GUEST_E2E_OK'
  } > "$OUTPUT_DIR/trace.txt"
  printf 'QSF_VIRGL_WAYLAND_GUEST_E2E_OK output=%s\n' "$OUTPUT_DIR"
  exit 0
fi

[[ $# -eq 13 ]] || die 'internal invocation has invalid arguments'
output_dir=$2
overlay=$3
seed_iso=$4
payload_iso=$5
agent_sha256=$6
bridge_sha256=$7
manifest_sha256=$8
accel=$9
boot_timeout_seconds=${10}
prehook_probe_duration_ms=${11}
probe_duration_ms=${12}
socket_dir=${13}
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
prehook_probe_log="$output_dir/prehook-probe.log"
prehook_encoded_dir="$output_dir/prehook-encoded"
prehook_ffprobe="$output_dir/prehook-ffprobe.txt"
hook_log="$output_dir/post-agent-ready-hook.log"
qemu_pid=''
qemu_launcher_pid=''
control_pid=''
probe_pid=''

read_qemu_pidfile() {
  # QEMU intentionally writes its pidfile 0600. When this private test runs
  # QEMU under a different permitted account (for example root solely to
  # reach /dev/kvm), the orchestration user cannot read it directly. Ask the
  # same account for this one already-created private path; never relax the
  # pidfile mode or make the control endpoint public.
  if [[ -r "$qemu_pidfile" ]]; then
    cat -- "$qemu_pidfile"
  elif [[ "$QEMU_USE_SUDO" == 1 ]]; then
    sudo -n -u "$QEMU_RUN_AS" cat -- "$qemu_pidfile" 2>/dev/null
  else
    return 1
  fi
}

# QEMU can intentionally run under a permitted account other than the
# orchestration account (most notably root when only root can reach /dev/kvm).
# In that case an ordinary kill(0) from this shell returns EPERM even while
# QEMU is healthy.  Check and signal the already-owned PID through precisely
# the same account that launched it; do not mistake a permissions boundary for
# a crashed VM.
qemu_process_alive() {
  local pid=${1:-}
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  if [[ "$QEMU_USE_SUDO" == 1 ]]; then
    sudo -n -u "$QEMU_RUN_AS" kill -0 "$pid" 2>/dev/null
  else
    kill -0 "$pid" 2>/dev/null
  fi
}

signal_qemu_process() {
  local signal=${1:-} pid=${2:-}
  [[ "$signal" =~ ^(TERM|KILL)$ ]] || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  if [[ "$QEMU_USE_SUDO" == 1 ]]; then
    sudo -n -u "$QEMU_RUN_AS" kill "-$signal" "$pid" 2>/dev/null
  else
    kill "-$signal" "$pid" 2>/dev/null
  fi
}

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
  printf '%s\n' '--- pre-hook Display1 probe ---' >&2
  cat "$prehook_probe_log" >&2 2>/dev/null || true
  printf '%s\n' '--- post-agent-ready hook ---' >&2
  cat "$hook_log" >&2 2>/dev/null || true
}
cleanup() {
  stop_owned_pid "$probe_pid"
  stop_owned_pid "$control_pid"
  stop_qemu_pid "$qemu_pid"
  stop_owned_pid "$qemu_launcher_pid"
}
stop_owned_pid() {
  local pid=${1:-}
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 0
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid" 2>/dev/null || true
    return 0
  fi
  kill "$pid" 2>/dev/null || true
  # Let QEMU close its Display1/chardev sockets before the outer private
  # directory is removed.  The forced kill is only a bounded fallback.
  for _ in $(seq 1 200); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.05
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

stop_qemu_pid() {
  local pid=${1:-}
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 0
  if ! qemu_process_alive "$pid"; then
    wait "$pid" 2>/dev/null || true
    return 0
  fi
  signal_qemu_process TERM "$pid" || true
  # Let QEMU close its Display1/chardev sockets before the outer private
  # directory is removed. The forced kill is only a bounded fallback.
  for _ in $(seq 1 200); do
    qemu_process_alive "$pid" || break
    sleep 0.05
  done
  if qemu_process_alive "$pid"; then
    signal_qemu_process KILL "$pid" || true
  fi
  wait "$pid" 2>/dev/null || true
}
on_signal() {
  local signal=$1 status=$2
  # Do not resume the E2E sequence after its owned QEMU/probe/control
  # processes were cleaned up by a signal.  An EXIT handler alone remains for
  # ordinary success and error exits.
  trap - EXIT INT TERM
  cleanup
  printf 'VirGL QSF Wayland E2E: received %s; cleaned up owned processes\n' "$signal" >&2
  exit "$status"
}
trap cleanup EXIT
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

[[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]] || die 'inner runner requires dbus-run-session'
mkdir -p "$encoded_dir" "$prehook_encoded_dir"
qemu_args=(
  -name virgl-qsf-wayland-clipboard-e2e
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
  -display "dbus,gl=on,rendernode=$RENDER_NODE"
  -device virtio-keyboard-pci
  -device virtio-mouse-pci
  -device virtio-tablet-pci
  -chardev "socket,id=qsf_agent,path=$agent_socket,server=on,wait=off"
  -chardev "file,id=waylandtelemetry,path=$telemetry_log"
  -device virtio-serial-pci,id=qsf-virgl-wayland-serial0
  -device virtserialport,chardev=qsf_agent,name=org.qsunshine.agent
  -device virtserialport,chardev=waylandtelemetry,name=org.qsunshine.virgl.wayland.telemetry
  -serial "file:$serial_log"
  -monitor none
  -nic user,model=virtio-net-pci
  -no-reboot
  -pidfile "$qemu_pidfile"
)
if [[ "$QEMU_USE_SUDO" == 1 ]]; then
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
    qemu_pid="$(read_qemu_pidfile || true)"
    [[ "$qemu_pid" =~ ^[1-9][0-9]*$ ]] && break
  fi
  kill -0 "$qemu_launcher_pid" 2>/dev/null || { show_logs; die 'QEMU exited before writing PID'; }
  sleep 0.05
done
[[ -n "$qemu_pid" ]] || { show_logs; die 'QEMU did not write PID'; }
for _ in $(seq 1 200); do
  busctl --user --no-pager list 2>/dev/null | awk '{print $1}' | grep -Fxq "$DBUS_DESTINATION" && break
  qemu_process_alive "$qemu_pid" || { show_logs; die 'QEMU exited before exposing Display1'; }
  sleep 0.05
done
busctl --user --no-pager list | awk '{print $1}' | grep -Fxq "$DBUS_DESTINATION" || { show_logs; die 'QEMU did not expose Display1'; }
for _ in $(seq 1 200); do
  [[ -S "$agent_socket" ]] && break
  qemu_process_alive "$qemu_pid" || { show_logs; die 'QEMU exited before QSF socket'; }
  sleep 0.05
done
[[ -S "$agent_socket" ]] || die 'QEMU did not create QSF virtio-serial socket'

wait_marker() {
  local marker=$1 label=$2 iterations=$((boot_timeout_seconds * 10))
  for _ in $(seq 1 "$iterations"); do
    grep -Fqx "$marker" "$telemetry_log" 2>/dev/null && return 0
    if grep -F 'QSF_VIRGL_WAYLAND_GUEST_E2E_FAILED=' "$telemetry_log" 2>/dev/null; then
      show_logs; die "guest reported failure while waiting for $label"
    fi
    qemu_process_alive "$qemu_pid" || { show_logs; die "QEMU exited while waiting for $label"; }
    sleep 0.1
  done
  show_logs
  die "guest did not report $label within ${boot_timeout_seconds}s"
}

wait_marker 'QSF_VIRGL_WAYLAND_GUEST_AGENT_STARTED' 'verified static QSF agent startup'
# This disposable suite launches Sunshine with its constrained software H.264
# encoder below.  Declare the separately tested encoder envelope explicitly
# to qsf-control instead of treating the KVM or VirGL device nodes as a host
# capability probe. Clear an inherited /serverinfo URL because Sunshine is
# intentionally started later by the optional client hook.
env -u QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL \
  -u QSUNSHINE_QSF_SUNSHINE_SERVERINFO_CA_FILE \
  QSUNSHINE_QSF_HOST_MAX_WIDTH=1920 \
  QSUNSHINE_QSF_HOST_MAX_HEIGHT=1080 \
  QSUNSHINE_QSF_HOST_MAX_FPS=60 \
  QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS=18000 \
  QSUNSHINE_QSF_HOST_ENCODER_CODECS=H264 \
  "$PYTHON_BINARY" "$QSF_CONTROL" --agent-socket "$agent_socket" \
  --control-socket "$control_socket" --token-file "$token_file" >"$control_log" 2>&1 &
control_pid=$!
for _ in $(seq 1 100); do
  [[ -S "$control_socket" && -f "$token_file" ]] && break
  kill -0 "$control_pid" 2>/dev/null || { show_logs; die 'QSF control exited early'; }
  sleep 0.05
done
[[ -S "$control_socket" && -f "$token_file" ]] || die 'QSF control did not become ready'
qsf_client() { "$PYTHON_BINARY" "$QSF_CLIENT" --socket "$control_socket" --token-file "$token_file" "$@"; }
for _ in $(seq 1 20); do
  if qsf_client status >"$output_dir/qsf-status.json" 2>"$output_dir/qsf-status.err"; then break; fi
  sleep 0.5
done
grep -Fq '"agent": "ready"' "$output_dir/qsf-status.json" || { show_logs; die 'static QSF agent did not answer PING'; }
wait_marker 'QSF_VIRGL_WAYLAND_GUEST_VIRGL_READY' 'Weston DRM + native VirGL readiness'

# Close a short listener before an optional composite hook. This makes the
# original 1280x800 screen an independently encoded artifact, so a hook's own
# SetUIInfo request cannot erase the baseline from the later long-lived trace.
set +e
"$DISPLAY_PROBE" --dbus-address "$DBUS_SESSION_BUS_ADDRESS" --destination "$DBUS_DESTINATION" \
  --duration-ms "$prehook_probe_duration_ms" --no-audio --encode-dir "$prehook_encoded_dir" \
  >"$prehook_probe_log" 2>&1
prehook_probe_status=$?
set -e
[[ "$prehook_probe_status" == 0 ]] || { show_logs; die "pre-hook Display1 probe failed with status $prehook_probe_status"; }
grep -Eq 'frames published/encoded/dropped: [1-9][0-9]*/[1-9][0-9]*/' "$prehook_probe_log" \
  || { show_logs; die 'pre-hook Display1 probe did not encode frames'; }
grep -Eq 'DMA-BUF scanouts/updates/failures: [1-9][0-9]*/[0-9]+/0' "$prehook_probe_log" \
  || { show_logs; die 'pre-hook DMA-BUF readback failed'; }
grep -Fq 'session errors: 0' "$prehook_probe_log" \
  || { show_logs; die 'pre-hook Display1 probe reported session errors'; }
shopt -s nullglob
prehook_segments=("$prehook_encoded_dir"/*.mkv)
shopt -u nullglob
(( ${#prehook_segments[@]} > 0 )) || die 'pre-hook Display1 probe produced no H.264 segments'
: > "$prehook_ffprobe"
for segment in "${prehook_segments[@]}"; do
  [[ -s "$segment" ]] || continue
  printf '[%s]\n' "$(basename "$segment")" >> "$prehook_ffprobe"
  ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,pix_fmt \
    -of default=noprint_wrappers=1 "$segment" >> "$prehook_ffprobe"
done
grep -Fqx 'codec_name=h264' "$prehook_ffprobe" || die 'pre-hook output is not H.264'
grep -Fqx 'width=1280' "$prehook_ffprobe"
grep -Fqx 'height=800' "$prehook_ffprobe" || die 'pre-hook output was not 1280x800'

# Keep this listener alive through both the optional Moonlight/Sunshine hook
# and all QSF operations. The duration is intentionally configurable for a
# composite client test with a longer connection/setup phase.
"$DISPLAY_PROBE" --dbus-address "$DBUS_SESSION_BUS_ADDRESS" --destination "$DBUS_DESTINATION" \
  --duration-ms "$probe_duration_ms" --no-audio --encode-dir "$encoded_dir" >"$probe_log" 2>&1 &
probe_pid=$!
sleep 1
if [[ -n "$POST_AGENT_READY_HOOK" ]]; then
  wait_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCH_READY' 'guest input watcher readiness'
  raw_qsf_skipped=0
  if [[ "$QT_QSF_OWNER" == qt ]]; then
    raw_qsf_skipped=1
  fi
  QSF_WAYLAND_OUTPUT_DIR="$output_dir" \
  QSF_WAYLAND_QEMU_PID="$qemu_pid" \
  QSF_WAYLAND_QEMU_PIDFILE="$qemu_pidfile" \
  QSF_WAYLAND_AGENT_SOCKET="$agent_socket" \
  QSF_WAYLAND_CONTROL_SOCKET="$control_socket" \
  QSF_WAYLAND_TOKEN_FILE="$token_file" \
  QSF_WAYLAND_DBUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
  QSF_WAYLAND_DBUS_DESTINATION="$DBUS_DESTINATION" \
  QSF_WAYLAND_QT_E2E_OUTER_QSF_OWNER="$QT_QSF_OWNER" \
  QSF_WAYLAND_QT_E2E_OUTER_RAW_QSF_SKIPPED="$raw_qsf_skipped" \
  QSF_WAYLAND_QT_E2E_LIVE_PROBE_DURATION_MS="$probe_duration_ms" \
  "$POST_AGENT_READY_HOOK" >"$hook_log" 2>&1 || { show_logs; die 'post-agent-ready hook failed'; }
  wait_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_KEY_A=observed' 'post-hook keyboard input evidence'
  wait_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_ABS=observed' 'post-hook absolute mouse evidence'
  wait_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_BTN=observed' 'post-hook mouse button evidence'
  wait_marker 'QSF_VIRGL_WAYLAND_GUEST_INPUT_E2E_OK' 'post-hook guest input completion'
  if [[ "$QT_QSF_OWNER" == qt ]]; then
    wait_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_KEY_B=observed' \
      'post-fullscreen keyboard input evidence'
    wait_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_MOUSE_ABS=observed' \
      'post-fullscreen absolute mouse evidence'
    wait_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_MOUSE_BTN=observed' \
      'post-fullscreen mouse button evidence'
    wait_marker 'QSF_VIRGL_WAYLAND_GUEST_FULLSCREEN_INPUT_E2E_OK' \
      'post-fullscreen guest input completion'
  fi
fi

client_clipboard_hash="$(sha256sum "$CLIENT_CLIPBOARD" | awk '{print $1}')"
guest_clipboard_hash="$(sha256sum "$GUEST_CLIPBOARD" | awk '{print $1}')"
client_upload_hash="$(sha256sum "$CLIENT_UPLOAD" | awk '{print $1}')"
guest_download_hash="$(sha256sum "$GUEST_DOWNLOAD" | awk '{print $1}')"
if [[ "$QT_QSF_OWNER" == qt ]]; then
  qt_summary="$output_dir/qsunshine-qt-e2e-summary.txt"
  [[ -f "$qt_summary" ]] || { show_logs; die 'Qt QSF hook did not produce its success summary'; }
  grep -Fqx 'QSUNSHINE_QT_E2E_SUMMARY_VERSION=1' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary has an unsupported version'; }
  grep -Fqx 'QSUNSHINE_QT_QSF_OPERATIONS_OK=1' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary does not attest its operations'; }
  grep -Fqx "QSUNSHINE_QT_QSF_CLIENT_CLIPBOARD_SHA256=$client_clipboard_hash" "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary client clipboard hash differs'; }
  grep -Fqx "QSUNSHINE_QT_QSF_GUEST_CLIPBOARD_SHA256=$guest_clipboard_hash" "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary guest clipboard hash differs'; }
  grep -Fqx "QSUNSHINE_QT_QSF_UPLOAD_SHA256=$client_upload_hash" "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary upload hash differs'; }
  grep -Fqx "QSUNSHINE_QT_QSF_DOWNLOAD_SHA256=$guest_download_hash" "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary download hash differs'; }
  grep -Fqx 'QSUNSHINE_QT_QSF_RESIZE=1280x720' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary resize differs'; }
  grep -Fqx 'QSUNSHINE_QT_QSF_QEMU_SET_UI_INFO=applied' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary lacks QEMU SetUIInfo acknowledgement'; }
  grep -Fqx 'QSUNSHINE_QT_QSF_GUEST_SCANOUT_ACK=observed' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary lacks guest compositor scanout acknowledgement'; }
  grep -Fqx 'QSUNSHINE_QT_QSF_FULLSCREEN_GUEST_SCANOUT_ACK=observed' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary lacks fullscreen guest compositor scanout acknowledgement'; }
  grep -Fqx 'QSUNSHINE_QT_MOONLIGHT_WINDOWED_AND_FULLSCREEN_OK=1' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary lacks windowed/fullscreen attestation'; }
  grep -Fqx 'QSUNSHINE_QT_MOONLIGHT_INPUT_E2E_OK=1' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary lacks input attestation'; }
  grep -Fqx 'QSUNSHINE_QT_MOONLIGHT_FULLSCREEN_INPUT_E2E_OK=1' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary lacks fullscreen input attestation'; }
  grep -Fqx 'QSUNSHINE_QT_QSF_FULLSCREEN_DOWNLOAD_OK=1' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary lacks post-fullscreen file-transfer attestation'; }
  grep -Fqx 'QSUNSHINE_QT_SYSTEM_AUTH_GAMESTREAM_LEASE_OK=1' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary lacks the system-auth GameStream lease attestation'; }
  grep -Fqx 'QSUNSHINE_QT_SYSTEM_AUTH_NO_PIN_FALLBACK_OK=1' "$qt_summary" \
    || { show_logs; die 'Qt QSF hook summary permits an obsolete PIN fallback'; }
else
  qsf_client clipboard-set < "$CLIENT_CLIPBOARD" >"$output_dir/qsf-clipboard-set.json"
  wait_marker "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_STATE_SHA256=$client_clipboard_hash" 'client-to-guest QSF state'
  wait_marker "QSF_VIRGL_WAYLAND_GUEST_CLIENT_TO_WAYLAND_WL_PASTE_SHA256=$client_clipboard_hash" 'client clipboard in Wayland'
  wait_marker "QSF_VIRGL_WAYLAND_GUEST_WAYLAND_TO_QSF_STATE_SHA256=$guest_clipboard_hash" 'guest native Wayland clipboard in QSF state'
  qsf_client clipboard-get >"$output_dir/client-received-clipboard.txt"
  qsf_client upload "$CLIENT_UPLOAD" --name client-upload.txt >"$output_dir/qsf-upload.json"
  wait_marker "QSF_VIRGL_WAYLAND_GUEST_UPLOAD_SHA256=$client_upload_hash" 'client-to-guest upload hash'
  qsf_client download guest-download.txt "$output_dir/client-downloaded-guest-file.txt" >"$output_dir/qsf-download.json"
  wait_marker "QSF_VIRGL_WAYLAND_GUEST_QSF_DOWNLOAD_SHA256=$guest_download_hash" 'guest-to-client download hash'
  # The client-selected geometry is a request for the VirGL scanout.  This
  # transaction is deliberately not the legacy RESIZE shortcut: qsf-control
  # first resolves client decoder / host encoder / guest display limits, asks
  # QEMU for the virtual mode, then waits for the guest's canonical scanout
  # acknowledgement before returning to a reconnecting client.
  qsf_client optimize-connection --resolution 1280x720 --max-fps 60 --decoder-codecs H264 \
    >"$output_dir/qsf-connection-optimize.json"
  "$PYTHON_BINARY" - "$output_dir/qsf-connection-optimize.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    profile = json.load(source)
expected = {
    "version": 2,
    "requested_width": 1280,
    "requested_height": 720,
    "width": 1280,
    "height": 720,
    "fps": 60,
    "bitrate_kbps": 8000,
    "video_codec": "H.264",
    "qemu_set_ui_info": "applied",
}
for name, value in expected.items():
    if profile.get(name) != value:
        raise SystemExit(f"unexpected negotiated {name}: {profile.get(name)!r}")
generation = profile.get("guest_profile_generation")
if not isinstance(generation, str) or not generation.isdecimal() or int(generation) < 1:
    raise SystemExit(f"invalid guest profile generation: {generation!r}")
PY
  wait_marker 'QSF_VIRGL_WAYLAND_GUEST_RESIZE=1280x720' 'guest negotiated scanout state'
fi
wait_marker 'QSF_VIRGL_WAYLAND_GUEST_VIRGL_OK' 'Weston VirGL workload start'
set +e
wait "$probe_pid"
probe_status=$?
set -e
probe_pid=''
[[ "$probe_status" == 0 ]] || { show_logs; die "Display1 probe failed with status $probe_status"; }
stat -c '%a' "$control_socket" > "$output_dir/qsf-control.socket.mode"
stat -c '%a' "$token_file" > "$output_dir/qsf-control.token.mode"
shopt -s nullglob
segments=("$encoded_dir"/*.mkv)
shopt -u nullglob
(( ${#segments[@]} > 0 )) || die 'Display1 probe produced no H.264 segments'
: > "$output_dir/ffprobe.txt"
saw_live_1280x800=0
saw_ordered_1280x720=0
saw_ordered_1600x900=0
for segment in "${segments[@]}"; do
  [[ -s "$segment" ]] || continue
  printf '[%s]\n' "$(basename "$segment")" >> "$output_dir/ffprobe.txt"
  segment_probe="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,width,height,pix_fmt -of default=noprint_wrappers=1 "$segment")"
  printf '%s\n' "$segment_probe" >> "$output_dir/ffprobe.txt"
  if grep -Fqx 'codec_name=h264' <<<"$segment_probe" && grep -Fqx 'width=1280' <<<"$segment_probe"; then
    if grep -Fqx 'height=800' <<<"$segment_probe"; then
      saw_live_1280x800=1
    fi
    if [[ "$saw_live_1280x800" == 1 ]] && grep -Fqx 'height=720' <<<"$segment_probe"; then
      saw_ordered_1280x720=1
    fi
  fi
  if [[ "$QT_QSF_OWNER" == qt && "$saw_ordered_1280x720" == 1 ]] && \
      grep -Fqx 'codec_name=h264' <<<"$segment_probe" && \
      grep -Fqx 'width=1600' <<<"$segment_probe" && \
      grep -Fqx 'height=900' <<<"$segment_probe"; then
    saw_ordered_1600x900=1
  fi
done
[[ "$saw_live_1280x800" == 1 ]] || die 'live Display1 trace did not contain H.264 1280x800 before resize'
[[ "$saw_ordered_1280x720" == 1 ]] || die 'live Display1 trace did not contain ordered H.264 1280x800 then 1280x720 segments'
if [[ "$QT_QSF_OWNER" == qt ]]; then
  [[ "$saw_ordered_1600x900" == 1 ]] || die \
    'live Display1 trace did not contain ordered H.264 1280x800 then 1280x720 then 1600x900 segments'
  printf '%s\n' '1280x800->1280x720->1600x900' > "$output_dir/live-h264-geometry-order.txt"
else
  printf '%s\n' '1280x800->1280x720' > "$output_dir/live-h264-geometry-order.txt"
fi
