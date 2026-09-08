#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Record a QSM Direct or noVNC console cursor-switch interaction on an Xvfb
# display with the hardware cursor drawn in, so the guest CSS cursor is
# captured. Usage: capture-cursor.sh <qsm|novnc> <pve-url> <user> <passfile> <vmid> <out.webm>
set -eu
transport="$1"; pve_url="$2"; user="$3"; passfile="$4"; vmid="$5"; out="$6"
display=":99"; w=1600; h=1000
ready="/tmp/qsm-cap-ready.$$"; go="/tmp/qsm-cap-go.$$"
rm -f "$ready" "$go"

Xvfb "$display" -screen 0 "${w}x${h}x24" -nolisten tcp >/tmp/qsm-xvfb.log 2>&1 &
xvfb_pid=$!
trap 'kill "$xvfb_pid" 2>/dev/null || true; rm -f "$ready" "$go"' EXIT INT TERM
sleep 2

DISPLAY="$display" node "$(dirname "$0")/capture-cursor.cjs" \
    "$transport" "$pve_url" "$user" "$passfile" "$vmid" "$ready" "$go" >/tmp/qsm-cap-node.log 2>&1 &
node_pid=$!

# Wait until the console is up and the driver signals ready.
i=0
while [ ! -f "$ready" ] && kill -0 "$node_pid" 2>/dev/null; do
    i=$((i + 1)); [ "$i" -gt 600 ] && break; sleep 0.1
done
if [ ! -f "$ready" ]; then echo "driver never became ready" >&2; cat /tmp/qsm-cap-node.log >&2; exit 1; fi

# Record ~12s of the interaction with the mouse drawn in.
ffmpeg -y -loglevel error -f x11grab -draw_mouse 1 -framerate 30 -video_size "${w}x${h}" \
    -i "$display" -t 13 -c:v libvpx-vp9 -b:v 4M -pix_fmt yuv420p "$out" >/tmp/qsm-ffmpeg.log 2>&1 &
ff_pid=$!
sleep 0.5
: > "$go"          # tell the driver to start the interaction
wait "$ff_pid" || { echo "ffmpeg failed" >&2; cat /tmp/qsm-ffmpeg.log >&2; }
wait "$node_pid" 2>/dev/null || true
echo "QSM_CAPTURE_DONE $out $(du -h "$out" 2>/dev/null | cut -f1)"
