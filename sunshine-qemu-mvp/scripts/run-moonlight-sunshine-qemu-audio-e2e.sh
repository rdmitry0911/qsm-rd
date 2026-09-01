#!/usr/bin/env bash
# Exercise a real, host-audio-free GameStream audio path:
#
#   QEMU KVM BIOS PC-speaker tone -> Display1 AudioOutListener -> Sunshine
#   qemu_dbus capture/Opus -> Moonlight Embedded SDL -> SDL disk PCM.
#
# The last hop is deliberately SDL's disk audio driver, not a host sound
# server.  This makes the gate useful on a headless machine and lets it reject
# an otherwise-valid stream that only carries generated silence.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"

# The documented isolated-libva build location is the default.  A caller can
# still supply SUNSHINE_BINARY, but the runner never silently consumes an
# opaque /tmp build.
SUNSHINE_BUILD_DIR="${SUNSHINE_BUILD_DIR:-$ROOT/.upstream/build-sunshine-qemu}"
SUNSHINE_BINARY="${SUNSHINE_BINARY:-$SUNSHINE_BUILD_DIR/sunshine}"
MOONLIGHT_BINARY="${MOONLIGHT_BINARY:-$ROOT/.upstream/build-moonlight-embedded/moonlight}"
QEMU_BINARY="${QEMU_BINARY:-qemu-system-x86_64}"
OUTPUT_PARENT="${OUTPUT_DIR:-$ROOT/artifacts/validation/moonlight-sunshine-qemu-audio-e2e}"
ASSEMBLER="${ASSEMBLER:-as}"
LINKER="${LINKER:-ld}"
STREAM_SECONDS="${STREAM_SECONDS:-12}"
SUNSHINE_PORT="${SUNSHINE_PORT:-48191}"
PIN="${MOONLIGHT_PIN:-4243}"
DISPLAY_NUMBER="${MOONLIGHT_DISPLAY:-:94}"
QEMU_ACCEL="${QEMU_ACCEL:-kvm}"
MESA_EGL_VENDOR="${MESA_EGL_VENDOR:-/usr/share/glvnd/egl_vendor.d/50_mesa.json}"
BOOT_SOURCE="$ROOT/tests/fixtures/qmdp_audio_tone.S"
PCM_RATE=48000
PCM_CHANNELS=2
PCM_BYTES_PER_SAMPLE=2
# Require at least one second of completed, decoded stereo PCM.  SDL's disk
# backend emits raw S16LE frames, so the size must be divisible by four.
MIN_PCM_BYTES=$((PCM_RATE * PCM_CHANNELS * PCM_BYTES_PER_SAMPLE))

die() {
  echo "moonlight Sunshine QEMU audio e2e: $*" >&2
  exit 1
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

if [[ "${1:-}" != "--inside-private-bus" ]]; then
  for required in "$SUNSHINE_BINARY" "$MOONLIGHT_BINARY" "$QEMU_BINARY" "$ASSEMBLER" \
                  "$LINKER" "$BOOT_SOURCE" dbus-run-session busctl timeout truncate dd \
                  Xvfb xdpyinfo curl ffmpeg ffprobe stdbuf sg awk sed stat; do
    if [[ -e "$required" ]]; then
      continue
    fi
    command -v "$required" >/dev/null || die "missing required command or path: $required"
  done
  [[ -x "$SUNSHINE_BINARY" ]] ||
    die "Sunshine binary is not executable: $SUNSHINE_BINARY (run $ROOT/scripts/build-upstream-sunshine-qemu.sh)"
  [[ -x "$MOONLIGHT_BINARY" ]] || die "Moonlight binary is not executable: $MOONLIGHT_BINARY"
  [[ -f "$(dirname -- "$SUNSHINE_BINARY")/assets/apps.json" ]] ||
    die "Sunshine assets are missing beside $SUNSHINE_BINARY"
  [[ -f "$MESA_EGL_VENDOR" ]] || die "Mesa EGL vendor file not found: $MESA_EGL_VENDOR"
  "$QEMU_BINARY" -display help | grep -Fxq dbus ||
    die "QEMU lacks the D-Bus display backend: $QEMU_BINARY"
  "$QEMU_BINARY" -audiodev help | grep -Fxq dbus ||
    die "QEMU lacks the D-Bus audio backend: $QEMU_BINARY"
  is_positive_integer "$STREAM_SECONDS" || die "STREAM_SECONDS must be a positive integer"
  is_positive_integer "$SUNSHINE_PORT" || die "SUNSHINE_PORT must be a positive integer"
  (( SUNSHINE_PORT >= 1029 && SUNSHINE_PORT <= 65500 )) ||
    die "SUNSHINE_PORT is outside Sunshine's safe base-port range"
  [[ "$PIN" =~ ^[0-9]{4}$ ]] || die "MOONLIGHT_PIN must have exactly four digits"
  [[ "$QEMU_ACCEL" == "kvm" || "$QEMU_ACCEL" == "tcg" ]] ||
    die "QEMU_ACCEL must be kvm or tcg"
  if [[ "$QEMU_ACCEL" == "kvm" ]]; then
    [[ -e /dev/kvm ]] || die "QEMU_ACCEL=kvm requires /dev/kvm"
    sg kvm -c 'test -r /dev/kvm && test -w /dev/kvm' ||
      die "current user cannot access /dev/kvm through group kvm"
  fi

  mkdir -p "$OUTPUT_PARENT"
  chmod 700 "$OUTPUT_PARENT"
  OUTPUT_DIR="$(mktemp -d "$OUTPUT_PARENT/run.XXXXXX")"
  chmod 700 "$OUTPUT_DIR"
  printf 'moonlight Sunshine QEMU audio e2e evidence=%s\n' "$OUTPUT_DIR"

  boot_object="$OUTPUT_DIR/qmdp_audio_tone.o"
  boot_sector="$OUTPUT_DIR/qmdp_audio_tone.bin"
  floppy_image="$OUTPUT_DIR/qmdp-audio-tone.img"
  "$ASSEMBLER" --32 -o "$boot_object" "$BOOT_SOURCE"
  "$LINKER" -m elf_i386 -Ttext 0x7c00 --oformat binary -e _start \
    -o "$boot_sector" "$boot_object"
  [[ "$(wc -c < "$boot_sector")" == "512" ]] || die "fixture must be one BIOS sector"
  [[ "$(od -An -tx1 -j 510 -N 2 "$boot_sector" | tr -d '[:space:]')" == "55aa" ]] ||
    die "fixture has no BIOS signature"
  truncate -s 1474560 "$floppy_image"
  dd if="$boot_sector" of="$floppy_image" conv=notrunc status=none

  dbus-run-session -- "$SCRIPT_PATH" --inside-private-bus \
    "$SUNSHINE_BINARY" "$MOONLIGHT_BINARY" "$OUTPUT_DIR" "$floppy_image" \
    "$STREAM_SECONDS" "$SUNSHINE_PORT" "$PIN" "$DISPLAY_NUMBER" \
    "$MESA_EGL_VENDOR" "$QEMU_ACCEL"

  grep -Fq 'AUDIO_TONE_ON' "$OUTPUT_DIR/guest-debugcon.log"
  grep -Fq 'Succesfully paired' "$OUTPUT_DIR/moonlight-pair.log"
  grep -Fq 'Desktop' "$OUTPUT_DIR/moonlight-list.log"
  grep -Fq 'Starting RTSP handshake...' "$OUTPUT_DIR/moonlight-stream.log"
  grep -Fq 'Starting video stream...' "$OUTPUT_DIR/moonlight-stream.log"
  grep -Fq 'Received first video packet after' "$OUTPUT_DIR/moonlight-stream.log"
  ! grep -Fq 'Audio stream start failed' "$OUTPUT_DIR/moonlight-stream.log"
  ! grep -Fq 'No video traffic was ever received from the host!' "$OUTPUT_DIR/moonlight-stream.log"
  grep -Fq 'Screencasting with QEMU Display1 D-Bus' "$OUTPUT_DIR/sunshine.log"
  grep -Fq 'Found H.264 encoder: libx264 [software]' "$OUTPUT_DIR/sunshine.log"
  grep -Fq 'New streaming session started' "$OUTPUT_DIR/sunshine.log"
  grep -Fq '[qemu-dbus] registered QEMU AudioOutListener for guest PCM' "$OUTPUT_DIR/sunshine.log"
  grep -Fq '[qemu-dbus] guest audio stream initialized:' "$OUTPUT_DIR/sunshine.log"
  grep -Fq '[qemu-dbus] accepted guest PCM for Sunshine audio capture' "$OUTPUT_DIR/sunshine.log"
  grep -Fxq 'decoded_pcm_non_silent=yes' "$OUTPUT_DIR/audio-verdict.txt"
  grep -Fxq 'moonlight_timeout_status=124' "$OUTPUT_DIR/trace.txt"
  printf 'MOONLIGHT_SUNSHINE_QEMU_AUDIO_E2E_OK output=%s\n' "$OUTPUT_DIR"
  exit 0
fi

[[ $# -eq 11 ]] || die "internal invocation has invalid arguments"
sunshine_binary=$2
moonlight_binary=$3
output_dir=$4
floppy_image=$5
stream_seconds=$6
sunshine_port=$7
pin=$8
display_number=$9
mesa_egl_vendor=${10}
qemu_accel=${11}

qemu_log="$output_dir/qemu.log"
guest_debugcon_log="$output_dir/guest-debugcon.log"
sunshine_log="$output_dir/sunshine.log"
pair_log="$output_dir/moonlight-pair.log"
list_log="$output_dir/moonlight-list.log"
stream_log="$output_dir/moonlight-stream.log"
xvfb_log="$output_dir/xvfb.log"
decoded_pcm="$output_dir/moonlight-decoded.s16le"
volume_log="$output_dir/moonlight-decoded-volumedetect.log"
audio_verdict="$output_dir/audio-verdict.txt"
xdg_config_dir="$output_dir/xdg-config"
moonlight_key_dir="$output_dir/moonlight-keys"
mkdir -p "$xdg_config_dir" "$moonlight_key_dir"
chmod 700 "$xdg_config_dir" "$moonlight_key_dir"

# Keep QEMU's actual audio driver entirely on the private Display1 D-Bus
# connection.  `sg kvm` refreshes supplementary group membership in this
# already-running agent session; the TCG escape hatch remains useful for
# diagnosis, but a passing default gate is a KVM run.
qemu_args=(
  "$QEMU_BINARY"
  -name qmdp-moonlight-sunshine-qemu-audio-e2e
  -machine "pc,accel=${qemu_accel},pcspk-audiodev=dbus0"
  -m 128
  -vga std
  -display dbus,gl=off,audiodev=dbus0
  -audiodev dbus,id=dbus0,out.frequency=48000,out.channels=2
  -monitor none
  -serial none
  -chardev "file,id=qmdp_debugcon,path=$guest_debugcon_log"
  -device isa-debugcon,iobase=0xe9,chardev=qmdp_debugcon
  -no-reboot
  -no-shutdown
  -drive "file=$floppy_image,if=floppy,format=raw,readonly=on"
)
if [[ "$qemu_accel" == "kvm" ]]; then
  # `sg` accepts a single shell string.  Bash's %q makes the generated
  # command literal-safe for the known paths/arguments above.
  printf -v qemu_shell_command '%q ' "${qemu_args[@]}"
  sg kvm -c "exec $qemu_shell_command" >"$qemu_log" 2>&1 &
else
  "${qemu_args[@]}" >"$qemu_log" 2>&1 &
fi
qemu_pid=$!

__EGL_VENDOR_LIBRARY_FILENAMES="$mesa_egl_vendor" LIBGL_ALWAYS_SOFTWARE=1 \
  Xvfb "$display_number" -screen 0 1280x720x24 +extension GLX >"$xvfb_log" 2>&1 &
xvfb_pid=$!
sunshine_pid=''
moonlight_pid=''

cleanup() {
  if [[ -n "$moonlight_pid" ]]; then
    kill "$moonlight_pid" 2>/dev/null || true
    wait "$moonlight_pid" 2>/dev/null || true
  fi
  if [[ -n "$sunshine_pid" ]]; then
    kill "$sunshine_pid" 2>/dev/null || true
    wait "$sunshine_pid" 2>/dev/null || true
  fi
  kill "$xvfb_pid" "$qemu_pid" 2>/dev/null || true
  wait "$xvfb_pid" 2>/dev/null || true
  wait "$qemu_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 200); do
  if busctl --user --no-pager list 2>/dev/null | awk '{print $1}' | grep -Fxq org.qemu; then
    break
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { cat "$qemu_log" >&2 || true; die "QEMU exited early"; }
  sleep 0.05
done
busctl --user --no-pager list | awk '{print $1}' | grep -Fxq org.qemu ||
  die "QEMU did not expose org.qemu"

for _ in $(seq 1 200); do
  DISPLAY="$display_number" xdpyinfo >/dev/null 2>&1 && break
  kill -0 "$xvfb_pid" 2>/dev/null || { cat "$xvfb_log" >&2 || true; die "Xvfb exited early"; }
  sleep 0.05
done
DISPLAY="$display_number" xdpyinfo >/dev/null || die "Xvfb did not become ready"

for _ in $(seq 1 200); do
  grep -Fq 'AUDIO_TONE_ON' "$guest_debugcon_log" 2>/dev/null && break
  kill -0 "$qemu_pid" 2>/dev/null || { cat "$qemu_log" >&2 || true; die "QEMU exited before tone boot"; }
  sleep 0.05
done
grep -Fq 'AUDIO_TONE_ON' "$guest_debugcon_log" || die "guest did not arm PC-speaker tone"

# -0 reads the pairing PIN from this private pipe.  The explicit qemu-dbus
# sink protects against accidentally selecting host PulseAudio/PipeWire.
printf '%s\n' "$pin" | \
  XDG_CONFIG_HOME="$xdg_config_dir" \
  SUNSHINE_QEMU_DBUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
  "$sunshine_binary" -0 \
    capture=qemu_dbus encoder=software stream_audio=true audio_sink=qemu-dbus \
    system_tray=false bind_address=127.0.0.1 port="$sunshine_port" \
    >"$sunshine_log" 2>&1 &
sunshine_pid=$!

for _ in $(seq 1 240); do
  if curl --silent --show-error --fail --max-time 1 \
      "http://127.0.0.1:${sunshine_port}/serverinfo?uniqueid=0123456789ABCDEF" \
      >"$output_dir/serverinfo-before-pair.xml" 2>/dev/null; then
    break
  fi
  kill -0 "$sunshine_pid" 2>/dev/null || { cat "$sunshine_log" >&2 || true; die "Sunshine exited early"; }
  sleep 0.05
done
[[ -s "$output_dir/serverinfo-before-pair.xml" ]] || die "Sunshine HTTP server did not become ready"

"$moonlight_binary" -debug -pin "$pin" -keydir "$moonlight_key_dir" -port "$sunshine_port" \
  pair 127.0.0.1 >"$pair_log" 2>&1
"$moonlight_binary" -debug -keydir "$moonlight_key_dir" -port "$sunshine_port" \
  list 127.0.0.1 >"$list_log" 2>&1

set +e
# SDL disk writes the exact S16LE samples Moonlight has already decoded from
# GameStream's Opus packets.  No physical sound device, PipeWire, PulseAudio,
# or X11 audio bridge is involved.
DISPLAY="$display_number" SDL_AUDIODRIVER=disk SDL_DISKAUDIOFILE="$decoded_pcm" \
  __EGL_VENDOR_LIBRARY_FILENAMES="$mesa_egl_vendor" LIBGL_ALWAYS_SOFTWARE=1 \
  stdbuf -oL -eL timeout --signal=TERM --kill-after=5s "${stream_seconds}s" \
  "$moonlight_binary" -debug -keydir "$moonlight_key_dir" -port "$sunshine_port" \
    -platform sdl -windowed -app Desktop -codec h264 -width 1280 -height 720 -fps 30 -bitrate 4000 \
    stream 127.0.0.1 >"$stream_log" 2>&1 &
moonlight_pid=$!
set -e

for _ in $(seq 1 240); do
  if grep -Fq 'Received first video packet after' "$stream_log"; then
    break
  fi
  kill -0 "$moonlight_pid" 2>/dev/null || { cat "$stream_log" >&2 || true; die "Moonlight exited before receiving video"; }
  sleep 0.05
done
grep -Fq 'Received first video packet after' "$stream_log" || die "Moonlight did not receive video"

set +e
wait "$moonlight_pid"
moonlight_status=$?
set -e
moonlight_pid=''
if [[ "$moonlight_status" != "124" ]]; then
  cat "$stream_log" >&2 || true
  die "Moonlight must stay active until bounded shutdown; status=$moonlight_status"
fi

[[ -f "$decoded_pcm" ]] || die "SDL disk audio driver did not create decoded PCM"
pcm_bytes="$(stat -c '%s' "$decoded_pcm")"
[[ "$pcm_bytes" =~ ^[0-9]+$ ]] || die "decoded PCM size is invalid"
(( pcm_bytes >= MIN_PCM_BYTES )) ||
  die "decoded PCM is too short: ${pcm_bytes} bytes (need >= ${MIN_PCM_BYTES})"
(( pcm_bytes % (PCM_CHANNELS * PCM_BYTES_PER_SAMPLE) == 0 )) ||
  die "decoded PCM is not whole S16LE stereo frames: ${pcm_bytes} bytes"

# volumedetect reads the decoder's raw output, rather than a Sunshine/QEMU
# counter.  Require a meaningful peak and RMS level so continuous-silence
# fallback packets cannot satisfy the E2E gate.
ffmpeg -hide_banner -nostats -f s16le -ar "$PCM_RATE" -ac "$PCM_CHANNELS" -i "$decoded_pcm" \
  -af volumedetect -f null - >"$volume_log" 2>&1
max_volume="$(sed -nE 's/.*max_volume: ([+-]?[0-9]+(\.[0-9]+)?) dB.*/\1/p' "$volume_log" | tail -n1)"
mean_volume="$(sed -nE 's/.*mean_volume: ([+-]?[0-9]+(\.[0-9]+)?) dB.*/\1/p' "$volume_log" | tail -n1)"
[[ "$max_volume" =~ ^[+-]?[0-9]+(\.[0-9]+)?$ ]] ||
  die "decoded PCM has no finite maximum volume (likely silence)"
[[ "$mean_volume" =~ ^[+-]?[0-9]+(\.[0-9]+)?$ ]] ||
  die "decoded PCM has no finite mean volume (likely silence)"
awk -v maximum="$max_volume" -v mean="$mean_volume" 'BEGIN { exit !(maximum > -45.0 && mean > -55.0) }' ||
  die "decoded PCM is insufficiently non-silent: max=${max_volume} dB mean=${mean_volume} dB"

{
  echo "decoded_pcm_format=s16le/${PCM_RATE}Hz/${PCM_CHANNELS}ch"
  echo "decoded_pcm_bytes=$pcm_bytes"
  echo "decoded_pcm_seconds=$(awk -v bytes="$pcm_bytes" 'BEGIN { printf "%.3f", bytes / (48000 * 2 * 2) }')"
  echo "decoded_pcm_max_volume_db=$max_volume"
  echo "decoded_pcm_mean_volume_db=$mean_volume"
  echo "decoded_pcm_non_silent=yes"
} > "$audio_verdict"

{
  echo "QMDP_MOONLIGHT_SUNSHINE_QEMU_AUDIO_E2E"
  echo "qemu=$($QEMU_BINARY --version | head -n1)"
  echo "sunshine_binary=$sunshine_binary"
  echo "moonlight_binary=$moonlight_binary"
  echo "moonlight_version=$($moonlight_binary help | head -n1)"
  echo "accel=$qemu_accel"
  echo "guest_audio=BIOS PC-speaker 1kHz PIT -> QEMU dbus audiodev"
  echo "capture=qemu_dbus AudioOutListener -> Sunshine Opus"
  echo "client=Moonlight Embedded SDL/FFmpeg/Opus decode -> SDL disk raw PCM"
  echo "host_audio_dependency=none"
  echo "transport=Moonlight GameStream pairing + HTTPS launch + RTSP + RTP"
  echo "sunshine_config=capture=qemu_dbus encoder=software stream_audio=true audio_sink=qemu-dbus"
  echo "pairing=private deterministic PIN and disposable client keys"
  echo "moonlight_timeout_status=$moonlight_status"
  cat "$audio_verdict"
  echo
  cat "$stream_log"
} > "$output_dir/trace.txt"
