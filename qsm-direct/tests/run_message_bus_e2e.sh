#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 FAKE_QEMU_BINARY PROBE_BINARY OUTPUT_DIR [map|inline]" >&2
  exit 2
fi

fake_qemu=$1
probe=$2
output_dir=$3
mode=${4:-map}
case "$mode" in
  map) fake_mode_args=() ;;
  inline) fake_mode_args=(--inline) ;;
  *) echo "invalid mode: $mode (expected map or inline)" >&2; exit 2 ;;
esac

rm -rf "$output_dir"
mkdir -p "$output_dir"

exec dbus-run-session -- bash -euo pipefail -c '
  fake_qemu=$1
  probe=$2
  output_dir=$3
  mode=$4
  fake_log="$output_dir/fake-qemu.log"
  probe_log="$output_dir/probe.log"

  mode_args=()
  if [[ "$mode" == inline ]]; then
    mode_args+=(--inline)
  fi

  "$fake_qemu" --width 320 --height 180 --frames 30 --fps 30 \
      "${mode_args[@]}" >"$fake_log" 2>&1 &
  fake_pid=$!
  cleanup() {
    kill "$fake_pid" 2>/dev/null || true
    wait "$fake_pid" 2>/dev/null || true
  }
  trap cleanup EXIT

  for _ in $(seq 1 100); do
    if grep -q "FAKE_QEMU_READY" "$fake_log" 2>/dev/null; then
      break
    fi
    if ! kill -0 "$fake_pid" 2>/dev/null; then
      cat "$fake_log" >&2
      exit 1
    fi
    sleep 0.02
  done
  grep -q "FAKE_QEMU_READY" "$fake_log"

  "$probe" \
      --dbus-address "$DBUS_SESSION_BUS_ADDRESS" \
      --destination org.qemu \
      --duration-ms 1200 \
      --fps 30 \
      --request-size 320x180 \
      --input-smoke \
      --no-audio \
      --snapshot "$output_dir/final.ppm" \
      >"$probe_log" 2>&1

  wait "$fake_pid"
  trap - EXIT

  grep -q "QEMU_DISPLAY_PROBE_RESULT" "$probe_log"
  grep -q "FAKE_QEMU_RESULT" "$fake_log"
  grep -q "frames published/encoded/dropped:" "$probe_log"
  grep -q "requested=320x180" "$fake_log"
  grep -Eq "keyboard=[1-9][0-9]*" "$fake_log"
  grep -Eq "mouse=[1-9][0-9]*" "$fake_log"
  if [[ "$mode" == map ]]; then
    grep -Eq "scanout inline/map: 0/[1-9][0-9]*" "$probe_log"
    grep -Eq "updates inline/map: 0/[1-9][0-9]*" "$probe_log"
  else
    grep -Eq "scanout inline/map: [1-9][0-9]*/0" "$probe_log"
    grep -Eq "updates inline/map: [1-9][0-9]*/0" "$probe_log"
  fi
  test -s "$output_dir/final.ppm"
  cat "$fake_log"
  cat "$probe_log"
' bash "$fake_qemu" "$probe" "$output_dir" "$mode"
