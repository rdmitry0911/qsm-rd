#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Token-mode QSM console over an emulated VPN path, without root:
#   unshare -rn lab/sctp-lossy-path/run-lossy-path.sh
# The private network namespace gets a dummy interface (ICE candidates) and
# netem on loopback: DELAY ms each way, LOSS percent. A QEMU/TCG guest redraws
# its text screen continuously; headless Chromium keeps the input timer and
# moves the pointer for DURATION seconds. Prints the server's input snapshot
# count every 10 s: it must keep growing (aiortc 1.11 stalled it after
# seconds to minutes, see tests/browser_bridge/test_sctp_receive.py).
# Needs: PYTHON (with aiortc/av/aiohttp/playwright), WORKER, BROWSER.
set -euo pipefail
export PATH=/usr/sbin:/sbin:$PATH
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
: "${PYTHON:?}" "${WORKER:?}" "${BROWSER:?}"
W=$(mktemp -d /tmp/qsm-lossy-XXXXXX)
ip link set lo up
ip link add d0 type dummy && ip addr add 10.99.0.1/24 dev d0 && ip link set d0 up && ip route add default dev d0
tc qdisc add dev lo root netem delay "${DELAY:-60}ms" loss "${LOSS:-1}%"
as --32 -o "$W/anim.o" "$HERE/anim.S"
ld -m elf_i386 -Ttext 0x7c00 --oformat binary -e _start -o "$W/anim.bin" "$W/anim.o"
"$PYTHON" -I -c "import sys; b=open(sys.argv[1],'rb').read(); open(sys.argv[2],'wb').write(b+bytes(1474560-len(b)))" "$W/anim.bin" "$W/anim.img"
dbus-daemon --session --nofork --nopidfile --address="unix:path=$W/bus" >"$W/dbus.log" 2>&1 & DP=$!
sleep 0.5
qemu-system-x86_64 -name anim -machine pc -accel tcg -m 64 -vga std -display "dbus,addr=unix:path=$W/bus,gl=off" \
    -monitor none -serial none -drive "file=$W/anim.img,if=floppy,format=raw,readonly=on" >"$W/qemu.log" 2>&1 & QP=$!
trap 'kill $QP $DP 2>/dev/null || true' EXIT
sleep 2
cd "$ROOT"
"$PYTHON" "$HERE/lossy-path-probe.py" "$ROOT" "$WORKER" "$BROWSER" "unix:path=$W/bus" "${DURATION:-150}" >"$W/probe.log" 2>&1 || true
grep "media health" "$W/probe.log" | grep -o "input_snapshots=[0-9]*" | paste -sd" "
grep "^PROBE.*sctp" "$W/probe.log" | tail -1
echo "log: $W/probe.log"
