#!/usr/bin/env bash
# Qualify a real Alpine guest's virtio-gpu/VirGL path and QEMU Display1
# transport without starting X11, Wayland, GTK, or a desktop compositor.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
QEMU_IMG_BINARY="${QEMU_IMG_BINARY:-qemu-img}"
DISPLAY_PROBE="${QEMU_DISPLAY_PROBE:-$ROOT/build-runtime/qemu-display-probe}"
PROVISIONER="$ROOT/scripts/provision-alpine-virgl-guest.sh"
USER_DATA="$ROOT/tests/fixtures/virgl-cloud-init-user-data.yaml"
META_DATA="$ROOT/tests/fixtures/virgl-cloud-init-meta-data.yaml"
ALPINE_VERSION="${VIRGL_ALPINE_VERSION:-3.20.10}"
VM_DIR="${VIRGL_VM_DIR:-$ROOT/vm/alpine-virgl-$ALPINE_VERSION}"
BASE_IMAGE="${VIRGL_BASE_IMAGE:-$VM_DIR/generic_alpine-${ALPINE_VERSION}-x86_64-bios-cloudinit-r0.qcow2}"
OUTPUT_PARENT="${VIRGL_OUTPUT_DIR:-$VM_DIR/e2e}"
ACCEL="${VIRGL_ACCEL:-kvm}"
QEMU_RUN_AS="${VIRGL_QEMU_RUN_AS:-$(id -un)}"
QEMU_USE_SUDO="${VIRGL_QEMU_USE_SUDO:-1}"
RENDER_NODE="${VIRGL_RENDER_NODE:-/dev/dri/renderD128}"
SURFACELESS_FALLBACK="${QEMU_EGL_SURFACELESS_FALLBACK:-0}"
BOOT_TIMEOUT_SECONDS="${VIRGL_BOOT_TIMEOUT_SECONDS:-150}"
PROBE_DURATION_MS="${VIRGL_PROBE_DURATION_MS:-9000}"
REQUEST_SIZE="${VIRGL_REQUEST_SIZE:-}"
DBUS_DESTINATION="${VIRGL_DBUS_DESTINATION:-org.qemu}"

die() {
  printf 'VirGL guest E2E: %s\n' "$*" >&2
  exit 1
}

show_logs() {
  [[ -n "${OUTPUT_DIR:-}" ]] || return 0
  printf '%s\n' '--- QEMU log ---' >&2
  tail -160 "$OUTPUT_DIR/qemu.log" >&2 2>/dev/null || true
  printf '%s\n' '--- guest telemetry ---' >&2
  cat "$OUTPUT_DIR/guest-telemetry.log" >&2 2>/dev/null || true
  printf '%s\n' '--- guest serial tail ---' >&2
  tail -120 "$OUTPUT_DIR/guest-serial.log" >&2 2>/dev/null || true
  printf '%s\n' '--- Display1 probe ---' >&2
  cat "$OUTPUT_DIR/probe.log" >&2 2>/dev/null || true
}

for required in "$QEMU_BINARY" "$QEMU_IMG_BINARY" "$DISPLAY_PROBE" "$PROVISIONER" \
                "$USER_DATA" "$META_DATA" \
                cloud-localds dbus-daemon busctl ffprobe awk grep find head sed tail cat basename \
                mkdir mktemp chmod sha512sum sleep seq setsid date kill; do
  if [[ -e "$required" ]]; then
    continue
  fi
  command -v "$required" >/dev/null || die "missing required command or path: $required"
done
if [[ "$QEMU_USE_SUDO" == '1' ]]; then
  command -v sudo >/dev/null || die 'VIRGL_QEMU_USE_SUDO=1 requires sudo'
fi

[[ "$ACCEL" == 'kvm' || "$ACCEL" == 'tcg' ]] || die 'VIRGL_ACCEL must be kvm or tcg'
[[ "$QEMU_USE_SUDO" == '0' || "$QEMU_USE_SUDO" == '1' ]] || die 'VIRGL_QEMU_USE_SUDO must be 0 or 1'
[[ "$SURFACELESS_FALLBACK" == '0' || "$SURFACELESS_FALLBACK" == '1' ]] || die 'QEMU_EGL_SURFACELESS_FALLBACK must be 0 or 1'
[[ "$BOOT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_BOOT_TIMEOUT_SECONDS must be a positive integer'
[[ "$PROBE_DURATION_MS" =~ ^[1-9][0-9]*$ ]] || die 'VIRGL_PROBE_DURATION_MS must be a positive integer'
if [[ -n "$REQUEST_SIZE" && ! "$REQUEST_SIZE" =~ ^[1-9][0-9]*x[1-9][0-9]*$ ]]; then
  die 'VIRGL_REQUEST_SIZE must have WIDTHxHEIGHT form'
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

if [[ "$ACCEL" == 'kvm' ]]; then
  [[ -c /dev/kvm ]] || die 'VIRGL_ACCEL=kvm requires /dev/kvm'
  if ! run_as_qemu_user test -r /dev/kvm -a -w /dev/kvm; then
    die "QEMU user $QEMU_RUN_AS cannot access /dev/kvm; use VIRGL_QEMU_RUN_AS or VIRGL_QEMU_USE_SUDO=0 in a refreshed kvm-group session"
  fi
fi
if [[ "$SURFACELESS_FALLBACK" == '0' ]]; then
  [[ -c "$RENDER_NODE" ]] || die "native VirGL requires a DRM render node: $RENDER_NODE"
  if ! run_as_qemu_user test -r "$RENDER_NODE" -a -w "$RENDER_NODE"; then
    die "QEMU user $QEMU_RUN_AS cannot access render node: $RENDER_NODE"
  fi
fi
"$QEMU_BINARY" -display help | grep -Fxq dbus || die "QEMU lacks the D-Bus display backend: $QEMU_BINARY"

mkdir -p "$OUTPUT_PARENT"
chmod 700 "$OUTPUT_PARENT"
OUTPUT_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
chmod 700 "$OUTPUT_DIR"

qemu_pid=''
qemu_launcher_pid=''
bus_pid=''
cleanup() {
  if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
    kill "$qemu_pid" 2>/dev/null || true
  fi
  if [[ -n "$qemu_launcher_pid" ]] && kill -0 "$qemu_launcher_pid" 2>/dev/null; then
    kill "$qemu_launcher_pid" 2>/dev/null || true
  fi
  if [[ -n "$bus_pid" ]] && kill -0 "$bus_pid" 2>/dev/null; then
    kill "$bus_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

seed_iso="$OUTPUT_DIR/nocloud.iso"
overlay="$OUTPUT_DIR/guest-overlay.qcow2"
serial_log="$OUTPUT_DIR/guest-serial.log"
telemetry_log="$OUTPUT_DIR/guest-telemetry.log"
qemu_log="$OUTPUT_DIR/qemu.log"
qemu_pidfile="$OUTPUT_DIR/qemu.pid"
probe_log="$OUTPUT_DIR/probe.log"
encoded_dir="$OUTPUT_DIR/encoded"

cloud-localds "$seed_iso" "$USER_DATA" "$META_DATA"
"$QEMU_IMG_BINARY" create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$overlay" 2G

mapfile -t bus_info < <(dbus-daemon --session --fork --print-address=1 --print-pid=1)
[[ ${#bus_info[@]} -eq 2 ]] || die 'private D-Bus did not return address and PID'
bus_address="${bus_info[0]}"
bus_pid="${bus_info[1]}"
printf '%s\n' "$bus_address" > "$OUTPUT_DIR/private-dbus.address"
printf '%s\n' "$bus_pid" > "$OUTPUT_DIR/private-dbus.pid"

display_option='dbus,gl=on'
if [[ "$SURFACELESS_FALLBACK" == '0' ]]; then
  display_option+=",rendernode=$RENDER_NODE"
fi

qemu_args=(
  -name virgl-guest-e2e
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

# Keep the production route native: explicit Mesa/llvmpipe variables are
# removed.  The opt-in patched-QEMU fallback is intentionally isolated here.
if [[ "$SURFACELESS_FALLBACK" == '1' ]]; then
  qemu_env=(env
    "DBUS_SESSION_BUS_ADDRESS=$bus_address"
    QEMU_EGL_SURFACELESS_FALLBACK=1
    "__EGL_VENDOR_LIBRARY_FILENAMES=${VIRGL_EGL_VENDOR_LIBRARY_FILENAMES:-/usr/share/glvnd/egl_vendor.d/50_mesa.json}"
    "LIBGL_ALWAYS_SOFTWARE=${VIRGL_LIBGL_ALWAYS_SOFTWARE:-1}")
else
  qemu_env=(env -u __EGL_VENDOR_LIBRARY_FILENAMES -u LIBGL_ALWAYS_SOFTWARE
    "DBUS_SESSION_BUS_ADDRESS=$bus_address"
    QEMU_EGL_SURFACELESS_FALLBACK=0)
fi

mkdir -p "$encoded_dir"
printf '%s\n' 'QEMU command (private D-Bus address omitted):' > "$OUTPUT_DIR/qemu-command.txt"
printf '%q ' "$QEMU_BINARY" "${qemu_args[@]}" >> "$OUTPUT_DIR/qemu-command.txt"
printf '\n' >> "$OUTPUT_DIR/qemu-command.txt"

if [[ "$QEMU_USE_SUDO" == '1' ]]; then
  setsid sudo -n -u "$QEMU_RUN_AS" "${qemu_env[@]}" "$QEMU_BINARY" "${qemu_args[@]}" >"$qemu_log" 2>&1 &
else
  setsid "${qemu_env[@]}" "$QEMU_BINARY" "${qemu_args[@]}" >"$qemu_log" 2>&1 &
fi
qemu_launcher_pid=$!
printf '%s\n' "$qemu_launcher_pid" > "$OUTPUT_DIR/qemu-launcher.pid"

for _ in $(seq 1 200); do
  if [[ -s "$qemu_pidfile" ]]; then
    qemu_pid="$(<"$qemu_pidfile")"
    break
  fi
  kill -0 "$qemu_launcher_pid" 2>/dev/null || { show_logs; die 'QEMU exited before writing its PID'; }
  sleep 0.05
done
[[ -n "$qemu_pid" ]] || { show_logs; die 'QEMU did not write its PID'; }

for _ in $(seq 1 200); do
  if busctl --address="$bus_address" --no-pager list 2>/dev/null | awk '{print $1}' | grep -Fxq "$DBUS_DESTINATION"; then
    break
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { show_logs; die 'QEMU exited before exposing Display1'; }
  sleep 0.05
done
busctl --address="$bus_address" --no-pager list | awk '{print $1}' | grep -Fxq "$DBUS_DESTINATION" || {
  show_logs
  die "QEMU did not expose D-Bus destination $DBUS_DESTINATION"
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

wait_for_guest_marker 'QMDP_VIRGL_GUEST_READY' 'VirGL renderer readiness'

probe_args=(
  --dbus-address "$bus_address"
  --destination "$DBUS_DESTINATION"
  --duration-ms "$PROBE_DURATION_MS"
  --no-audio
  --encode-dir "$encoded_dir"
)
if [[ -n "$REQUEST_SIZE" ]]; then
  probe_args+=(--request-size "$REQUEST_SIZE")
fi
set +e
"$DISPLAY_PROBE" "${probe_args[@]}" >"$probe_log" 2>&1
probe_status=$?
set -e
printf '%s\n' "$probe_status" > "$OUTPUT_DIR/probe.exit-status"
if [[ "$probe_status" != 0 ]]; then
  show_logs
  die "Display1 probe failed with status $probe_status"
fi

wait_for_guest_marker 'QMDP_VIRGL_GUEST_E2E_OK' 'VirGL guest completion'
grep -Fqx 'guest_drm_driver=virtio_gpu' "$telemetry_log" || die 'guest did not prove the virtio_gpu DRM driver'
guest_renderer="$(sed -n 's/^guest_gl_renderer=//p' "$telemetry_log" | head -n1)"
[[ -n "$guest_renderer" ]] || die 'guest did not report an OpenGL renderer'
printf '%s' "$guest_renderer" | grep -qi virgl || die "guest did not use VirGL: $guest_renderer"
if [[ "$SURFACELESS_FALLBACK" == '0' ]] && printf '%s' "$guest_renderer" | grep -qi llvmpipe; then
  die "native render-node lane unexpectedly used llvmpipe: $guest_renderer"
fi

grep -Fq 'QEMU_DISPLAY_PROBE_RESULT' "$probe_log" || die 'Display1 probe did not produce its result block'
grep -Eq 'frames published/encoded/dropped: [1-9][0-9]*/[1-9][0-9]*/' "$probe_log" || die 'Display1 probe did not publish and encode a video frame'
grep -Fq 'session errors: 0' "$probe_log" || die 'Display1 probe reported session errors'
grep -Eq 'DMA-BUF scanouts/updates/failures: [1-9][0-9]*/[0-9]+/0' "$probe_log" || \
  die 'native VirGL did not complete a DMA-BUF GBM or headless-EGL CPU readback without errors'
if grep -Fq 'DMA-BUF capture is not enabled in the CPU-first MVP' "$qemu_log"; then
  die 'host Display1 adapter rejected QEMU DMABUF frames; build the DMABUF-capable adapter before qualifying the native lane'
fi

shopt -s nullglob
segments=("$encoded_dir"/*.mkv)
shopt -u nullglob
(( ${#segments[@]} > 0 )) || die 'Display1 probe produced no encoded H.264 segment'
: > "$OUTPUT_DIR/ffprobe.txt"
for segment in "${segments[@]}"; do
  printf '%s\n' "[$(basename "$segment")]" >> "$OUTPUT_DIR/ffprobe.txt"
  ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,pix_fmt -of default=noprint_wrappers=1 "$segment" >> "$OUTPUT_DIR/ffprobe.txt"
done
grep -Fqx 'codec_name=h264' "$OUTPUT_DIR/ffprobe.txt" || die 'encoded Display1 segment is not H.264'

{
  printf '%s\n' 'QMDP_VIRGL_GUEST_E2E'
  printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'qemu=%s\n' "$("$QEMU_BINARY" --version | head -n1)"
  printf 'qemu_binary=%s\n' "$QEMU_BINARY"
  printf 'accel=%s\n' "$ACCEL"
  printf 'display=%s\n' "$display_option"
  printf 'host_gui=none\n'
  printf 'surfaceless_fallback=%s\n' "$SURFACELESS_FALLBACK"
  printf 'render_node=%s\n' "$RENDER_NODE"
  printf 'base_image=%s\n' "$BASE_IMAGE"
  printf 'base_image_sha512=%s\n' "$(sha512sum "$BASE_IMAGE" | awk '{print $1}')"
  printf 'transport=private-session-dbus+Display1+virtio-vga-gl+GBM-or-headless-EGL-DMABUF-CPU-readback+virtio-serial-telemetry\n'
  printf 'guest_drm_driver=virtio_gpu\n'
  printf 'guest_gl_renderer=%s\n' "$guest_renderer"
  if [[ -n "$REQUEST_SIZE" ]]; then
    printf 'set_ui_info_request=%s\n' "$REQUEST_SIZE"
  fi
  printf '\n[guest-telemetry]\n'
  cat "$telemetry_log"
  printf '\n[display1-probe]\n'
  cat "$probe_log"
  printf '\n[encoded-h264]\n'
  cat "$OUTPUT_DIR/ffprobe.txt"
  printf '%s\n' 'QMDP_VIRGL_GUEST_E2E_OK'
} > "$OUTPUT_DIR/trace.txt"

printf 'QMDP_VIRGL_GUEST_E2E_OK output=%s\n' "$OUTPUT_DIR"
