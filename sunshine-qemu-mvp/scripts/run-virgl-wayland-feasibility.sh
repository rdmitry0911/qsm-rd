#!/usr/bin/env bash
# Test a guest-only Weston DRM + wl-clipboard stack on native headless VirGL.
# This is a feasibility gate, not the QSF desktop adapter itself.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
QEMU_IMG_BINARY="${QEMU_IMG_BINARY:-qemu-img}"
PROVISIONER="$ROOT/scripts/provision-alpine-virgl-guest.sh"
USER_DATA="$ROOT/tests/fixtures/virgl-wayland-feasibility-cloud-init-user-data.yaml"
META_DATA="$ROOT/tests/fixtures/virgl-wayland-feasibility-cloud-init-meta-data.yaml"
ALPINE_VERSION="${VIRGL_ALPINE_VERSION:-3.20.10}"
VM_DIR="${VIRGL_VM_DIR:-$ROOT/vm/alpine-virgl-$ALPINE_VERSION}"
BASE_IMAGE="${VIRGL_BASE_IMAGE:-$VM_DIR/generic_alpine-${ALPINE_VERSION}-x86_64-bios-cloudinit-r0.qcow2}"
OUTPUT_PARENT="${VIRGL_WAYLAND_OUTPUT_DIR:-$VM_DIR/wayland-feasibility}"
ACCEL="${VIRGL_WAYLAND_ACCEL:-kvm}"
QEMU_RUN_AS="${VIRGL_QEMU_RUN_AS:-$(id -un)}"
QEMU_USE_SUDO="${VIRGL_QEMU_USE_SUDO:-1}"
RENDER_NODE="${VIRGL_RENDER_NODE:-/dev/dri/renderD128}"
BOOT_TIMEOUT_SECONDS="${VIRGL_WAYLAND_BOOT_TIMEOUT_SECONDS:-210}"

die() {
  printf 'VirGL Wayland feasibility: %s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" != '--inside-private-bus' ]]; then
  for required in "$QEMU_BINARY" "$QEMU_IMG_BINARY" "$PROVISIONER" "$USER_DATA" "$META_DATA" \
                  cloud-localds dbus-run-session busctl awk grep sed head tail cat tr \
                  mkdir mktemp chmod sleep seq setsid readlink sha256sum; do
    if [[ -e "$required" ]]; then
      continue
    fi
    command -v "$required" >/dev/null || die "missing required command or path: $required"
  done
  [[ "$ACCEL" == 'kvm' || "$ACCEL" == 'tcg' ]] || die 'VIRGL_WAYLAND_ACCEL must be kvm or tcg'
  [[ "$QEMU_USE_SUDO" == '0' || "$QEMU_USE_SUDO" == '1' ]] || die 'VIRGL_QEMU_USE_SUDO must be 0 or 1'
  [[ "$BOOT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_WAYLAND_BOOT_TIMEOUT_SECONDS must be positive'
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
  [[ -c "$RENDER_NODE" ]] || die "native VirGL requires render node: $RENDER_NODE"
  run_as_qemu_user test -r "$RENDER_NODE" -a -w "$RENDER_NODE" \
    || die "QEMU user $QEMU_RUN_AS cannot access $RENDER_NODE"
  if [[ "$ACCEL" == 'kvm' ]]; then
    [[ -c /dev/kvm ]] || die 'KVM requested but /dev/kvm is absent'
    run_as_qemu_user test -r /dev/kvm -a -w /dev/kvm \
      || die "QEMU user $QEMU_RUN_AS cannot access /dev/kvm"
  fi
  "$QEMU_BINARY" -display help | grep -Fxq dbus || die 'QEMU lacks Display1 D-Bus backend'

  mkdir -p "$OUTPUT_PARENT"
  chmod 700 "$OUTPUT_PARENT"
  output_dir="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
  chmod 700 "$output_dir"
  cloud-localds "$output_dir/nocloud.iso" "$USER_DATA" "$META_DATA"
  "$QEMU_IMG_BINARY" create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$output_dir/guest-overlay.qcow2" 2G
  printf 'VirGL Wayland feasibility evidence=%s\n' "$output_dir"
  dbus-run-session -- "$SCRIPT_PATH" --inside-private-bus "$output_dir" "$ACCEL" "$BOOT_TIMEOUT_SECONDS"

  telemetry="$output_dir/guest-telemetry.log"
  renderer="$(sed -n 's/^guest_gl_renderer=//p' "$telemetry" | head -n1)"
  [[ -n "$renderer" ]] || die 'guest did not report a GL renderer'
  printf '%s' "$renderer" | grep -qi virgl || die "guest did not use VirGL: $renderer"
  grep -Fqx 'guest_drm_driver=virtio_gpu' "$telemetry"
  grep -Fqx 'guest_wayland_compositor=weston-drm' "$telemetry"
  wayland_packages=(
    mesa-dri-gallium mesa-egl mesa-utils
    weston weston-backend-drm weston-shell-desktop weston-clients
    wl-clipboard seatd eudev
  )
  wayland_package_markers=()
  for package in "${wayland_packages[@]}"; do
    package_marker="$(grep -E "^guest_wayland_package_${package}=${package}-[^[:space:]]+$" "$telemetry" | head -n1 || true)"
    [[ -n "$package_marker" ]] || die "guest did not report installed Wayland package version: $package"
    wayland_package_markers+=("$package_marker")
  done
  clipboard_hash="$(sed -n 's/^guest_wayland_clipboard_sha256=//p' "$telemetry" | head -n1)"
  [[ "$clipboard_hash" =~ ^[0-9a-f]{64}$ ]] || die 'guest did not report a Wayland clipboard hash'
  grep -Fqx 'VIRGL_WAYLAND_FEASIBILITY_OK' "$telemetry"
  {
    printf '%s\n' 'VIRGL_WAYLAND_FEASIBILITY'
    printf 'qemu=%s\n' "$("$QEMU_BINARY" --version | head -n1)"
    printf 'accel=%s\n' "$ACCEL"
    printf 'display=dbus,gl=on,rendernode=%s\n' "$RENDER_NODE"
    printf 'host_gui=none\n'
    printf 'guest_compositor=weston-drm\n'
    printf 'guest_clipboard=wl-copy/wl-paste\n'
    printf 'guest_drm_driver=virtio_gpu\n'
    printf 'guest_gl_renderer=%s\n' "$renderer"
    for package_marker in "${wayland_package_markers[@]}"; do
      printf '%s\n' "$package_marker"
    done
    printf 'guest_wayland_clipboard_sha256=%s\n' "$clipboard_hash"
    printf '\n[guest-telemetry]\n'
    cat "$telemetry"
    printf '\n[guest-serial]\n'
    tr -d '\r' < "$output_dir/guest-serial.log" 2>/dev/null || true
    printf '%s\n' 'VIRGL_WAYLAND_FEASIBILITY_OK'
  } > "$output_dir/trace.txt"
  printf 'VIRGL_WAYLAND_FEASIBILITY_OK output=%s\n' "$output_dir"
  exit 0
fi

[[ $# -eq 4 ]] || die 'internal invocation has invalid arguments'
output_dir=$2
accel=$3
boot_timeout_seconds=$4
telemetry="$output_dir/guest-telemetry.log"
serial="$output_dir/guest-serial.log"
qemu_log="$output_dir/qemu.log"
qemu_pidfile="$output_dir/qemu.pid"
qemu_pid=''
qemu_launcher_pid=''

show_logs() {
  printf '%s\n' '--- QEMU log ---' >&2
  tail -160 "$qemu_log" >&2 2>/dev/null || true
  printf '%s\n' '--- guest telemetry ---' >&2
  cat "$telemetry" >&2 2>/dev/null || true
  printf '%s\n' '--- guest serial tail ---' >&2
  tail -160 "$serial" >&2 2>/dev/null || true
}
cleanup() {
  if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
    kill "$qemu_pid" 2>/dev/null || true
  fi
  if [[ -n "$qemu_launcher_pid" ]] && kill -0 "$qemu_launcher_pid" 2>/dev/null; then
    kill "$qemu_launcher_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

[[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]] || die 'inner runner requires dbus-run-session'
qemu_args=(
  -name virgl-wayland-feasibility
  -nodefaults
  -machine "q35,accel=$accel"
  -smp 2
  -m 1536
  -boot order=c
  -drive "file=$output_dir/guest-overlay.qcow2,if=virtio,format=qcow2"
  -drive "file=$output_dir/nocloud.iso,media=cdrom,readonly=on,format=raw"
  -vga none
  -device virtio-vga-gl
  -display "dbus,gl=on,rendernode=$RENDER_NODE"
  # Weston DRM deliberately refuses a graphical seat with no input device.
  # These are guest devices; no host X11/Wayland input stack is involved.
  -device virtio-keyboard-pci
  -device virtio-mouse-pci
  -serial "file:$serial"
  -chardev "file,id=waylandtelemetry,path=$telemetry"
  -device virtio-serial-pci,id=wayland-serial0
  -device virtserialport,chardev=waylandtelemetry,name=org.qsunshine.wayland.telemetry
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
  if busctl --user --no-pager list 2>/dev/null | awk '{print $1}' | grep -Fxq org.qemu; then
    break
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { show_logs; die 'QEMU exited before exposing Display1'; }
  sleep 0.05
done
busctl --user --no-pager list | awk '{print $1}' | grep -Fxq org.qemu || { show_logs; die 'QEMU did not expose Display1'; }

for _ in $(seq 1 "$((boot_timeout_seconds * 10))"); do
  grep -Fqx 'VIRGL_WAYLAND_FEASIBILITY_OK' "$telemetry" 2>/dev/null && exit 0
  if grep -F 'VIRGL_WAYLAND_FEASIBILITY_FAILED=' "$telemetry" 2>/dev/null; then
    show_logs
    die 'guest reported Wayland feasibility failure'
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { show_logs; die 'QEMU exited during guest feasibility test'; }
  sleep 0.1
done
show_logs
die "guest did not complete Wayland feasibility within ${boot_timeout_seconds}s"
