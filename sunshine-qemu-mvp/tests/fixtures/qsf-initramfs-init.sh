#!/bin/busybox sh
# PID 1 for the deterministic Alpine-kernel QSF e2e guest.  The runner packs
# this script, BusyBox, the static agent, and fixed test payloads into a newc
# initramfs; it has no network, SSH, package-manager, or desktop dependency.
set -eu

busybox=/bin/busybox
state=/run/qsf
serial=/dev/ttyS0
port=''

report() {
  printf '%s\n' "$*" >"$serial" 2>/dev/null || true
}

fail() {
  report "QSF_INITRAMFS_GUEST_E2E_FAILED=$*"
  while true; do
    "$busybox" sleep 1
  done
}

"$busybox" mkdir -p /dev /proc /sys /run "$state/incoming" "$state/outgoing"
"$busybox" mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
"$busybox" mount -t proc proc /proc 2>/dev/null || true
"$busybox" mount -t sysfs sysfs /sys 2>/dev/null || true

report QSF_INITRAMFS_GUEST_E2E
for _ in $("$busybox" seq 1 30); do
  # A full distro's udev creates /dev/virtio-ports/<name>. This intentionally
  # minimal initramfs has devtmpfs only, which exposes the same single port as
  # /dev/vport0p1 without the convenience symlink.
  for candidate in /dev/virtio-ports/org.qsunshine.agent /dev/vport*; do
    if [ -c "$candidate" ]; then
      port=$candidate
      break 2
    fi
  done
  "$busybox" sleep 1
done
[ -n "$port" ] || fail virtio_port_missing
[ -x /qsf-guest-agent ] || fail agent_missing
report "QSF_GUEST_PORT=$port"

"$busybox" cp /qsf-guest-download.txt "$state/outgoing/guest-download.txt"
: > "$state/qsf-clipboard.txt"

/qsf-guest-agent --device "$port" --state-dir "$state" >"$serial" 2>&1 &
agent_pid=$!
"$busybox" sleep 1
"$busybox" kill -0 "$agent_pid" 2>/dev/null || fail agent_exited
report QSF_GUEST_AGENT_STARTED

# These markers are created from real guest files after the control channel
# completes each operation.  The host compares them with independently
# calculated SHA-256 values rather than trusting a bridge JSON reply alone.
(
  while ! "$busybox" cmp -s "$state/qsf-clipboard.txt" /qsf-client-clipboard.txt; do
    "$busybox" sleep 1
  done
  report "QSF_GUEST_CLIENT_CLIPBOARD_SHA256=$($busybox sha256sum "$state/qsf-clipboard.txt" | $busybox awk '{print $1}')"

  "$busybox" cp /qsf-guest-clipboard.txt "$state/qsf-clipboard.txt"
  report "QSF_GUEST_REPLY_CLIPBOARD_SHA256=$($busybox sha256sum "$state/qsf-clipboard.txt" | $busybox awk '{print $1}')"

  while [ ! -f "$state/incoming/client-upload.txt" ]; do
    "$busybox" sleep 1
  done
  report "QSF_GUEST_UPLOAD_SHA256=$($busybox sha256sum "$state/incoming/client-upload.txt" | $busybox awk '{print $1}')"

  while ! "$busybox" cmp -s "$state/resolution" /qsf-resize-expected.txt; do
    "$busybox" sleep 1
  done
  resolution=$($busybox cat "$state/resolution")
  report "QSF_GUEST_RESIZE=$resolution"
) &

report "QSF_GUEST_DOWNLOAD_SHA256=$($busybox sha256sum "$state/outgoing/guest-download.txt" | $busybox awk '{print $1}')"
while true; do
  "$busybox" sleep 3600
done
