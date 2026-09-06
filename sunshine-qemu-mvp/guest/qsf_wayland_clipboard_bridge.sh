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
clipboard_generation="$state_dir/qsf-clipboard-generation"
clipboard_applied="$state_dir/qsf-clipboard-applied"
ready_file="$state_dir/wayland-clipboard-bridge.ready"
failure_file="$state_dir/wayland-clipboard-bridge.failed"
candidate="$state_dir/.wayland-clipboard-candidate"
validated="$state_dir/.wayland-clipboard-validated"
event_pipe="$state_dir/.wayland-clipboard-events"
state_event="$state_dir/.qsf-clipboard-state-event"
maximum_bytes=1048576
clipboard_watcher_pid=''
state_watcher_pid=''
wl_copy_pid=''
clipboard_backend='generic-poll'
qdbus_binary=''
state_poll_interval=0.10
# The generic path has no compositor event API.  It is strictly a recovery
# path for compositors without a clipboard broker; probing too often visibly
# wakes KWin/Plasma and makes its clipboard indicator flash.
fallback_probe_ticks=20
backend_retry_ticks=50
bridge_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || exit 1
state_watcher_binary="$bridge_directory/qsm-desktop-state-watcher"

stop_watchers() {
  if [ -n "$wl_copy_pid" ]; then
    kill "$wl_copy_pid" 2>/dev/null || true
    wait "$wl_copy_pid" 2>/dev/null || true
    wl_copy_pid=''
  fi
  if [ -n "$clipboard_watcher_pid" ]; then
    # The monitor is a child of this per-user systemd service.  Stop the
    # direct shell first; systemd's cgroup cleanup is the final safeguard for
    # the pipe reader it owns.
    kill "$clipboard_watcher_pid" 2>/dev/null || true
    wait "$clipboard_watcher_pid" 2>/dev/null || true
    clipboard_watcher_pid=''
  fi
  if [ -n "$state_watcher_pid" ]; then
    kill "$state_watcher_pid" 2>/dev/null || true
    wait "$state_watcher_pid" 2>/dev/null || true
    state_watcher_pid=''
  fi
}

cleanup() {
  stop_watchers
  # A ready marker is part of the clipboard completion protocol.  Removing it
  # before the user service exits prevents the system agent from waiting for
  # an acknowledgement from a bridge which is no longer able to send one.
  rm -f "$ready_file" "$event_pipe" "$state_event"
}

report() {
  printf '%s\n' "$*" >"$telemetry" 2>/dev/null || true
}

fail() {
  stop_watchers
  report "QSF_VIRGL_WAYLAND_GUEST_E2E_FAILED=wayland_bridge_$*"
  : >"$failure_file" 2>/dev/null || true
  chmod 600 "$failure_file" 2>/dev/null || true
  # Never leave a readiness sentinel from an earlier compositor behind: the
  # direct Console must not report GUI clipboard support while this bridge is
  # no longer attached to a Wayland selection.
  rm -f "$ready_file" "$candidate" "$validated" "$event_pipe" "$state_event"
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

clipboard_file_generation() {
  file=$1
  [ -f "$file" ] || return 1
  generation=$(cat "$file" 2>/dev/null) || return 1
  case "$generation" in
    ''|0|*[!0-9]*) return 1 ;;
  esac
  printf '%s\n' "$generation"
}

publish_clipboard_applied() {
  generation=$1
  case "$generation" in
    ''|0|*[!0-9]*) return 1 ;;
  esac
  temporary="$state_dir/.qsf-clipboard-applied.tmp.$$"
  umask 077
  printf '%s\n' "$generation" >"$temporary" || return 1
  chmod 600 "$temporary" || return 1
  mv -f "$temporary" "$clipboard_applied" || return 1
  last_applied_generation=$generation
  report "QSF_WAYLAND_BRIDGE_CLIPBOARD_APPLIED=$generation"
}

copy_state_to_wayland() {
  valid_utf8_text "$clipboard" || fail qsf_state_is_not_valid_utf8_text
  if [ "$clipboard_backend" = 'kde-dbus' ]; then
    # qdbus prints text results with one record-terminating newline, but its
    # setter accepts a normal D-Bus string.  xargs -0 transports the file as
    # precisely one argv item, including embedded and trailing newlines.
    { cat "$clipboard"; printf '\0'; } |
      xargs -0 "$qdbus_binary" org.kde.klipper /klipper \
        org.kde.klipper.klipper.setClipboardContents \
      >/dev/null 2>"$state_dir/.wayland-klipper-set.log" || fail klipper_rejected_qsf_state
    return
  fi
  # Own exactly one selection source.  The default wl-copy daemonizes, which
  # makes consecutive clipboard changes accumulate orphaned owners on some
  # Plasma versions.  A managed foreground child gives the bridge explicit
  # replacement and shutdown semantics without blocking its event loop.
  if [ -n "$wl_copy_pid" ]; then
    kill "$wl_copy_pid" 2>/dev/null || true
    wait "$wl_copy_pid" 2>/dev/null || true
    wl_copy_pid=''
  fi
  wl-copy --foreground --type 'text/plain;charset=utf-8' <"$clipboard" \
    >"$state_dir/.wayland-wl-copy.log" 2>&1 &
  wl_copy_pid=$!
}

publish_wayland_to_state() {
  valid_utf8_text "$candidate" || fail wayland_selection_is_not_valid_utf8_text
  temporary="$state_dir/.qsf-clipboard.tmp.$$"
  umask 077
  cp "$candidate" "$temporary" || fail cannot_stage_wayland_selection
  chmod 600 "$temporary" || fail cannot_protect_staged_wayland_selection
  mv -f "$temporary" "$clipboard" || fail cannot_publish_wayland_selection
}

capture_wayland_to_candidate() {
  # This call is deliberately event-driven for Plasma.  Invoking wl-paste
  # periodically opens a fresh Wayland selection offer; KWin/Plasma reacts to
  # that traffic visibly (including its clipboard indicator) even when the
  # text is unchanged.  A single read after a genuine clipboard notification
  # preserves the exact no-newline payload and avoids that compositor churn.
  if [ "$clipboard_backend" = 'kde-dbus' ]; then
    "$qdbus_binary" org.kde.klipper /klipper org.kde.klipper.klipper.getClipboardContents \
      >"$candidate" 2>"$state_dir/.wayland-klipper-get.log" || return 1
    # qdbus appends exactly one output separator after a string reply. Remove
    # that separator only; a newline genuinely contained in the clipboard
    # remains present.
    [ -s "$candidate" ] && truncate -s -1 "$candidate"
    return 0
  fi
  # A broken/vanished selection owner must not freeze input or later clipboard
  # events behind an unbounded Wayland read.
  timeout --foreground 1s wl-paste --no-newline --type 'text/plain;charset=utf-8' >"$candidate" \
    2>"$state_dir/.wayland-wl-paste.log"
}

kde_clipboard_watcher() {
  # Klipper is Plasma's supported clipboard authority.  Its signal contains
  # no clipboard data, so it is safe to consume here; the payload itself is
  # still fetched from Klipper only after the signal. Keep the matcher
  # narrow: busctl also prints method calls and unrelated KDirNotify traffic.
  busctl --user monitor org.kde.klipper 2>>"$state_dir/.wayland-klipper-monitor.log" |
    while IFS= read -r line; do
      case "$line" in
        *'Interface=org.kde.klipper.klipper  Member=clipboardHistoryUpdated'*)
          printf '%s\n' native >"$event_pipe"
          ;;
      esac
    done
}

qsf_state_watcher() {
  "$state_watcher_binary" --directory "$state_dir" --name qsf-clipboard.txt \
    2>>"$state_dir/.qsf-state-watcher.log" |
    while IFS= read -r event; do
      if [ "$event" = changed ]; then
        if [ "$clipboard_backend" = 'kde-dbus' ]; then
          printf '%s\n' state >"$event_pipe"
        else
          : >"$state_event"
          chmod 600 "$state_event" 2>/dev/null || true
        fi
      fi
    done
}

start_qsf_state_watcher() {
  [ -x "$state_watcher_binary" ] || fail qsf_state_watcher_missing
  qsf_state_watcher &
  state_watcher_pid=$!
}

prepare_event_channel() {
  if [ "$clipboard_backend" = 'kde-dbus' ]; then
    rm -f "$event_pipe"
    mkfifo -m 600 "$event_pipe" || fail cannot_create_event_pipe
  fi
}

synchronise_qsf_state() {
  current_state_hash=$(hash_file "$clipboard") || fail cannot_hash_qsf_state
  requested_generation=$(clipboard_file_generation "$clipboard_generation" 2>/dev/null || true)
  if [ "$current_state_hash" != "$last_state_hash" ]; then
    if [ "$current_state_hash" != "$last_wayland_hash" ]; then
      copy_state_to_wayland
      last_wayland_hash=$current_state_hash
      report "QSF_WAYLAND_BRIDGE_QSF_TO_WAYLAND_SHA256=$current_state_hash"
    fi
    last_state_hash=$current_state_hash
  fi
  # The D-Bus setter has completed successfully at this point.  A generation
  # makes this an acknowledgement of one concrete host write rather than a
  # timing guess.  The identical-text case still needs an ACK: writing the
  # same text twice is valid and must not make the second paste wait forever.
  if [ -n "$requested_generation" ] &&
      [ "$requested_generation" != "$last_applied_generation" ] &&
      [ "$current_state_hash" = "$last_wayland_hash" ]; then
    publish_clipboard_applied "$requested_generation" || fail cannot_acknowledge_clipboard
  fi
}

start_native_clipboard_watcher() {
  # wl-paste --watch needs the wlroots data-control extension, which Plasma
  # intentionally does not expose.  Prefer Klipper's event API when present.
  # Generic compositors retain a low-frequency fallback below instead of
  # hammering their Wayland clipboard ten times per second.
  if [ "$clipboard_backend" = 'kde-dbus' ]; then
    kde_clipboard_watcher &
    clipboard_watcher_pid=$!
    report 'QSF_WAYLAND_BRIDGE_NATIVE_WATCHER=kde_dbus'
  else
    report 'QSF_WAYLAND_BRIDGE_NATIVE_WATCHER=poll'
  fi
}

synchronise_native_clipboard() {
  current_state_hash=$last_state_hash
  if ! capture_wayland_to_candidate; then
    return
  fi
  candidate_hash=$(hash_file "$candidate") || fail cannot_hash_wayland_selection
  if [ "$candidate_hash" != "$last_wayland_hash" ]; then
    if [ "$candidate_hash" != "$current_state_hash" ]; then
      publish_wayland_to_state
      last_state_hash=$candidate_hash
      report "QSF_WAYLAND_BRIDGE_WAYLAND_TO_QSF_SHA256=$candidate_hash"
    fi
    last_wayland_hash=$candidate_hash
  fi
}

detect_clipboard_backend() {
  # Plasma intentionally does not expose wlroots data-control to unfocused
  # clients. Klipper is its supported selection authority, and the D-Bus
  # method both works for a user service and reports selection changes.
  for candidate in qdbus6 qdbus-qt6 qdbus; do
    if command -v "$candidate" >/dev/null 2>&1; then
      qdbus_binary=$(command -v "$candidate")
      break
    fi
  done
  if [ -n "$qdbus_binary" ] &&
      busctl --user --quiet introspect org.kde.klipper /klipper 2>/dev/null |
      grep -Fq 'clipboardHistoryUpdated'; then
    clipboard_backend='kde-dbus'
    return 0
  fi
  return 1
}

command -v wl-copy >/dev/null 2>&1 || fail wl_copy_missing
command -v wl-paste >/dev/null 2>&1 || fail wl_paste_missing
command -v timeout >/dev/null 2>&1 || fail timeout_missing
command -v xargs >/dev/null 2>&1 || fail xargs_missing
command -v truncate >/dev/null 2>&1 || fail truncate_missing
command -v mkfifo >/dev/null 2>&1 || fail mkfifo_missing
command -v gnu-iconv >/dev/null 2>&1 || command -v iconv >/dev/null 2>&1 || fail iconv_missing

wait_for_graphical_session() {
  # A per-user systemd manager can come up through SSH or linger before the
  # desktop compositor.  Treat that as normal startup ordering, not a
  # permanent failure: systemd otherwise burns through Restart= retries and
  # clipboard remains broken when the user finally reaches Plasma.
  while [ ! -d "$state_dir" ] || [ ! -f "$clipboard" ]; do
    sleep 1
  done
  while :; do
    for socket in "$runtime_dir"/wayland-[0-9]*; do
      if [ -S "$socket" ]; then
        wayland_socket=${socket##*/}
        export WAYLAND_DISPLAY="$wayland_socket"
        return
      fi
    done
    sleep 1
  done
}

export XDG_RUNTIME_DIR="$runtime_dir"
# A user service can be started by systemd before the graphical environment
# has exported its variables.  busctl --user normally derives this path from
# XDG_RUNTIME_DIR, but giving qdbus and busctl the same explicit address
# avoids a transient system-bus lookup during Plasma startup.
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$runtime_dir/bus}"
wait_for_graphical_session
trap 'cleanup; exit 0' HUP INT TERM
chmod 700 "$state_dir" 2>/dev/null || fail cannot_protect_state_directory
rm -f "$failure_file" "$candidate" "$validated" "$event_pipe" "$state_event" "$ready_file"

initial_state_hash=$(hash_file "$clipboard") || fail cannot_hash_initial_qsf_state
# A compositor restart loses the Wayland selection while QSF state persists.
# Seed the newly created Wayland server before entering the change loop so the
# adapter remains a mirror across an explicit desktop reconfiguration.
last_state_hash=$initial_state_hash
last_wayland_hash=$initial_state_hash
last_applied_generation=$(clipboard_file_generation "$clipboard_applied" 2>/dev/null || true)
report 'QSF_WAYLAND_BRIDGE_READY'
report "QSF_WAYLAND_BRIDGE_QSF_TO_WAYLAND_SHA256=$initial_state_hash"
detect_clipboard_backend
report "QSF_WAYLAND_BRIDGE_CLIPBOARD_BACKEND=$clipboard_backend"
copy_state_to_wayland
prepare_event_channel
start_qsf_state_watcher
synchronise_qsf_state
start_native_clipboard_watcher
# Mark ready only once the setter, state watcher, and native event watcher all
# exist.  The system agent treats this as permission to await an event-driven
# acknowledgement instead of returning a state-file-only result.
: >"$ready_file" || fail cannot_create_ready_file
chmod 600 "$ready_file" || fail cannot_protect_ready_file

if [ "$clipboard_backend" = 'kde-dbus' ]; then
  # Plasma needs no idle poll at all: inotify reports host/client writes to
  # QSF state and Klipper reports native selection changes. A FIFO preserves
  # ordering between those two event sources without spawning a process every
  # 100 ms or generating compositor-visible clipboard traffic.
  while IFS= read -r bridge_event <"$event_pipe"; do
    case "$bridge_event" in
      state) synchronise_qsf_state ;;
      native) synchronise_native_clipboard ;;
    esac
  done
else
  fallback_ticks=0
  backend_retry_counter=0
  while :; do
    if [ -e "$state_event" ]; then
      rm -f "$state_event"
      synchronise_qsf_state
    fi
    # No standard unfocused Wayland clipboard change notification exists.
    # One probe every two seconds is intentionally conservative: it preserves
    # the generic fallback without continuously waking/compositing the
    # desktop.  More importantly, retry Klipper discovery: on Plasma the
    # Wayland socket normally appears before the Klipper D-Bus object.  The
    # old one-shot detection permanently selected this noisy fallback in that
    # normal startup window.
    fallback_ticks=$((fallback_ticks + 1))
    if [ "$fallback_ticks" -ge "$fallback_probe_ticks" ]; then
      fallback_ticks=0
      synchronise_native_clipboard
    fi
    backend_retry_counter=$((backend_retry_counter + 1))
    if [ "$backend_retry_counter" -ge "$backend_retry_ticks" ]; then
      backend_retry_counter=0
      if detect_clipboard_backend; then
        report 'QSF_WAYLAND_BRIDGE_BACKEND_UPGRADE=kde_dbus'
        # Re-exec gives the D-Bus mode a clean FIFO and watcher.  Explicitly
        # stop the generic foreground wl-copy owner first so no stale Wayland
        # owner or polling shell survives the mode transition.
        stop_watchers
        rm -f "$event_pipe" "$state_event"
        exec "$0" --state-dir "$state_dir" --telemetry "$telemetry" --runtime-dir "$runtime_dir"
      fi
    fi
    sleep "$state_poll_interval"
  done
fi
