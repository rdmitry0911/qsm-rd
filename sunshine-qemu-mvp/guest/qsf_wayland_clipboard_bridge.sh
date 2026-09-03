#!/bin/sh
# qsf_wayland_clipboard_bridge.sh - guest-only bridge between the constrained
# QSF clipboard-state file and a real Wayland text/plain selection.
#
# It is deliberately separate from qsf_guest_agent: the agent remains usable
# on a headless guest, while this adapter is started only after a compositor
# and its Wayland socket are known to exist.
set -u

state_dir=''
telemetry=''
runtime_dir=''

usage() {
  printf '%s\n' "usage: $0 --state-dir DIRECTORY --telemetry DEVICE --runtime-dir DIRECTORY" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --state-dir)
      [ "$#" -ge 2 ] || usage
      state_dir=$2
      shift 2
      ;;
    --telemetry)
      [ "$#" -ge 2 ] || usage
      telemetry=$2
      shift 2
      ;;
    --runtime-dir)
      [ "$#" -ge 2 ] || usage
      runtime_dir=$2
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

[ -n "$state_dir" ] && [ -n "$telemetry" ] && [ -n "$runtime_dir" ] || usage

clipboard="$state_dir/qsf-clipboard.txt"
ready_file="$state_dir/wayland-clipboard-bridge.ready"
failure_file="$state_dir/wayland-clipboard-bridge.failed"
candidate="$state_dir/.wayland-clipboard-candidate"
validated="$state_dir/.wayland-clipboard-validated"
maximum_bytes=1048576

report() {
  printf '%s\n' "$*" >"$telemetry" 2>/dev/null || true
}

fail() {
  report "QSF_VIRGL_WAYLAND_GUEST_E2E_FAILED=wayland_bridge_$*"
  : >"$failure_file" 2>/dev/null || true
  chmod 600 "$failure_file" 2>/dev/null || true
  rm -f "$candidate" "$validated"
  exit 1
}

hash_file() {
  sha256sum "$1" | awk '{print $1}'
}

valid_utf8_text() {
  file=$1
  size=$(wc -c <"$file" | tr -d '[:space:]')
  case "$size" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$size" -le "$maximum_bytes" ] || return 1
  # QSF text intentionally excludes NUL even though it is a Unicode scalar:
  # this keeps clipboard state safe for line-oriented control peers.
  nul_bytes=$(LC_ALL=C tr -cd '\000' <"$file" | wc -c | tr -d '[:space:]')
  case "$nul_bytes" in
    0) ;;
    *) return 1 ;;
  esac
  # Alpine supplies gnu-iconv while Debian/Kubuntu expose the same utility as
  # iconv. A byte-for-byte round trip rejects malformed UTF-8 on either.
  if command -v gnu-iconv >/dev/null 2>&1; then
    gnu-iconv -f UTF-8 -t UTF-8 "$file" >"$validated" 2>/dev/null || return 1
  else
    iconv -f UTF-8 -t UTF-8 "$file" >"$validated" 2>/dev/null || return 1
  fi
  cmp -s "$file" "$validated"
}

copy_state_to_wayland() {
  valid_utf8_text "$clipboard" || fail qsf_state_is_not_valid_utf8_text
  wl-copy --type 'text/plain;charset=utf-8' <"$clipboard" \
    >"$state_dir/.wayland-wl-copy.log" 2>&1 || fail wl_copy_rejected_qsf_state
}

publish_wayland_to_state() {
  valid_utf8_text "$candidate" || fail wayland_selection_is_not_valid_utf8_text
  temporary="$state_dir/.qsf-clipboard.tmp.$$"
  umask 077
  cp "$candidate" "$temporary" || fail cannot_stage_wayland_selection
  chmod 600 "$temporary" || fail cannot_protect_staged_wayland_selection
  mv -f "$temporary" "$clipboard" || fail cannot_publish_wayland_selection
}

[ -d "$state_dir" ] || fail state_directory_missing
[ -f "$clipboard" ] || fail qsf_clipboard_state_missing
[ -S "$runtime_dir/wayland-0" ] || fail wayland_socket_missing
command -v wl-copy >/dev/null 2>&1 || fail wl_copy_missing
command -v wl-paste >/dev/null 2>&1 || fail wl_paste_missing
command -v gnu-iconv >/dev/null 2>&1 || command -v iconv >/dev/null 2>&1 || fail iconv_missing

export XDG_RUNTIME_DIR="$runtime_dir"
export WAYLAND_DISPLAY=wayland-0
chmod 700 "$state_dir" 2>/dev/null || fail cannot_protect_state_directory
rm -f "$failure_file" "$candidate" "$validated"
: >"$ready_file" || fail cannot_create_ready_file
chmod 600 "$ready_file" || fail cannot_protect_ready_file

initial_state_hash=$(hash_file "$clipboard") || fail cannot_hash_initial_qsf_state
# A compositor restart loses the Wayland selection while QSF state persists.
# Seed the newly created Wayland server before entering the change loop so the
# adapter remains a mirror across an explicit desktop reconfiguration.
copy_state_to_wayland
last_state_hash=$initial_state_hash
last_wayland_hash=$initial_state_hash
report 'QSF_WAYLAND_BRIDGE_READY'
report "QSF_WAYLAND_BRIDGE_QSF_TO_WAYLAND_SHA256=$initial_state_hash"

while :; do
  current_state_hash=$(hash_file "$clipboard") || fail cannot_hash_qsf_state
  if [ "$current_state_hash" != "$last_state_hash" ]; then
    if [ "$current_state_hash" != "$last_wayland_hash" ]; then
      copy_state_to_wayland
      last_wayland_hash=$current_state_hash
      report "QSF_WAYLAND_BRIDGE_QSF_TO_WAYLAND_SHA256=$current_state_hash"
    fi
    last_state_hash=$current_state_hash
  fi

  if wl-paste --no-newline --type 'text/plain;charset=utf-8' >"$candidate" \
      2>"$state_dir/.wayland-wl-paste.log"; then
    candidate_hash=$(hash_file "$candidate") || fail cannot_hash_wayland_selection
    if [ "$candidate_hash" != "$last_wayland_hash" ]; then
      if [ "$candidate_hash" != "$current_state_hash" ]; then
        publish_wayland_to_state
        last_state_hash=$candidate_hash
        report "QSF_WAYLAND_BRIDGE_WAYLAND_TO_QSF_SHA256=$candidate_hash"
      fi
      last_wayland_hash=$candidate_hash
    fi
  fi
  sleep 0.1
done
