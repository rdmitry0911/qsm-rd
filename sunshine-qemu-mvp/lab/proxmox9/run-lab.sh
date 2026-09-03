#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Launch a disposable nested Proxmox VE 9 node for q-sunshine integration
# qualification.  The outer VM uses KVM; its own QEMU guests can use nested
# KVM when the parent host exposes AMD-V/VT-x.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_DIR="$ROOT_DIR/lab/proxmox9"
RUNTIME_DIR="${QSUNSHINE_PVE_LAB_RUNTIME_DIR:-$LAB_DIR/runtime}"
CACHE_DIR="${QSUNSHINE_PVE_LAB_CACHE_DIR:-$LAB_DIR/cache}"
BASE_IMAGE="$CACHE_DIR/debian-13-genericcloud-amd64.qcow2"
OVERLAY_IMAGE="$RUNTIME_DIR/pve9-lab.qcow2"
SEED_IMAGE="$RUNTIME_DIR/pve9-lab-seed.iso"
PID_FILE="$RUNTIME_DIR/pve9-lab.pid"
QMP_SOCKET="$RUNTIME_DIR/pve9-lab.qmp"
SERIAL_LOG="$RUNTIME_DIR/pve9-lab.serial.log"
SSH_PORT="${QSUNSHINE_PVE_LAB_SSH_PORT:-12222}"
WEB_PORT="${QSUNSHINE_PVE_LAB_WEB_PORT:-18006}"
TERMINAL_PORT="${QSUNSHINE_PVE_LAB_TERMINAL_PORT:-58123}"
VIRGL_RENDER_NODE="${QSUNSHINE_PVE_LAB_VIRGL_RENDER_NODE:-/dev/dri/renderD128}"
ENABLE_VIRGL="${QSUNSHINE_PVE_LAB_ENABLE_VIRGL:-1}"

readonly IMAGE_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
readonly LAB_NODE_NAME="qsm-pve9-lab"

die() {
    echo "q-sunshine Proxmox lab: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

require_kvm() {
    [[ -c /dev/kvm ]] || die "/dev/kvm is not available; pass it into the LXC first"
    sudo -n test -r /dev/kvm -a -w /dev/kvm ||
        die "sudo cannot access /dev/kvm"
}

require_virgl() {
    [[ "$ENABLE_VIRGL" == "0" ]] && return 0
    [[ "$ENABLE_VIRGL" == "1" ]] || die "QSUNSHINE_PVE_LAB_ENABLE_VIRGL must be 0 or 1"
    [[ -c "$VIRGL_RENDER_NODE" ]] ||
        die "VirGL is enabled but render node is unavailable: $VIRGL_RENDER_NODE"
}

lab_pid() {
    local pid
    if [[ -e "$PID_FILE" ]]; then
        pid="$(sudo -n cat "$PID_FILE" 2>/dev/null)" || return 1
    else
        # A stale cleanup must not turn a live lab into an orphan: recover its
        # deliberately unique QEMU process when the pidfile was removed.
        pid="$(sudo -n pgrep -fo -- "qemu-system-x86_64.*-name ${LAB_NODE_NAME}" 2>/dev/null)" ||
            return 1
    fi
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '%s\n' "$pid"
}

running() {
    local pid
    pid="$(lab_pid)" || return 1
    sudo -n kill -0 "$pid" 2>/dev/null
}

make_seed() {
    local public_key rendered_user_data
    [[ -r /home/dima/.ssh/id_ed25519.pub ]] ||
        die "expected SSH public key /home/dima/.ssh/id_ed25519.pub"
    public_key="$(< /home/dima/.ssh/id_ed25519.pub)"
    [[ "$public_key" == ssh-ed25519\ * ]] || die "unsupported lab SSH public-key format"
    rendered_user_data="$RUNTIME_DIR/user-data"
    sed "s|@QSUNSHINE_LAB_SSH_PUBLIC_KEY@|$public_key|" \
        "$LAB_DIR/cloud-init/user-data.in" >"$rendered_user_data"
    cloud-localds --network-config "$LAB_DIR/cloud-init/network-config" "$SEED_IMAGE" \
        "$rendered_user_data" "$LAB_DIR/cloud-init/meta-data" >/dev/null
    chmod 0600 "$rendered_user_data" "$SEED_IMAGE"
}

ensure_image() {
    mkdir -p "$CACHE_DIR" "$RUNTIME_DIR"
    if [[ ! -s "$BASE_IMAGE" ]]; then
        curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
            --output "$BASE_IMAGE" "$IMAGE_URL"
    fi
    qemu-img info --output=json "$BASE_IMAGE" | jq -e '.format == "qcow2"' >/dev/null ||
        die "unexpected Debian cloud image format"
}

start() {
    require_kvm
    require_virgl
    for command in qemu-system-x86_64 qemu-img cloud-localds curl jq sudo; do
        require_command "$command"
    done
    if running; then
        echo "q-sunshine Proxmox lab already running (pid $(lab_pid))"
        return 0
    fi
    ensure_image
    sudo -n rm -f "$QMP_SOCKET" "$PID_FILE"
    if [[ ! -e "$OVERLAY_IMAGE" ]]; then
        qemu-img create -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$OVERLAY_IMAGE" 80G >/dev/null
    fi
    make_seed

    # Keep every forwarded port separate from the normal local e2e stack.
    # Sunshine needs its fixed relative GameStream ports, so each is forwarded
    # explicitly through the outer user-mode NIC. QSF/broker ports are added
    # for the final PVE Console -> q-sunshine launch test.
    local network
    network="user,id=net0,hostname=$LAB_NODE_NAME"
    network+=",hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22"
    network+=",hostfwd=tcp:127.0.0.1:${WEB_PORT}-:8006"
    network+=",hostfwd=tcp:127.0.0.1:${TERMINAL_PORT}-:48123"
    for port in 47984 47989 47990 48010 47998 47999 48000 48002; do
        network+=",hostfwd=tcp:127.0.0.1:${port}-:${port}"
        network+=",hostfwd=udp:127.0.0.1:${port}-:${port}"
    done
    network+=",hostfwd=tcp:127.0.0.1:48122-:48122"

    local -a graphics
    if [[ "$ENABLE_VIRGL" == "1" ]]; then
        # The outer PVE node receives a render node backed by the host GPU;
        # inner QEMU VMs can then expose a real nested VirGL renderer.
        graphics=(
            -vga none
            -display "egl-headless,rendernode=$VIRGL_RENDER_NODE"
            -device virtio-vga-gl
        )
    else
        graphics=(-display none)
    fi

    sudo -n qemu-system-x86_64 \
        -name "$LAB_NODE_NAME" \
        -machine q35,accel=kvm \
        -cpu host \
        -m "${QSUNSHINE_PVE_LAB_MEMORY_MB:-8192}" \
        -smp "${QSUNSHINE_PVE_LAB_CPUS:-8}" \
        -drive "file=$OVERLAY_IMAGE,if=virtio,format=qcow2,cache=none,aio=native" \
        -drive "file=$SEED_IMAGE,if=virtio,media=cdrom,readonly=on" \
        -netdev "$network" \
        -device virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56 \
        -virtfs "local,path=$ROOT_DIR,mount_tag=qsm-src,security_model=none,readonly=on" \
        "${graphics[@]}" \
        -serial "file:$SERIAL_LOG" \
        -monitor none \
        -qmp "unix:$QMP_SOCKET,server=on,wait=off" \
        -pidfile "$PID_FILE" \
        -daemonize
    echo "q-sunshine Proxmox lab started: web=https://127.0.0.1:${WEB_PORT} ssh=127.0.0.1:${SSH_PORT}"
}

wait_ready() {
    local deadline now
    deadline=$((SECONDS + ${QSUNSHINE_PVE_LAB_TIMEOUT_SECONDS:-1800}))
    while true; do
        if ! running; then
            tail -n 100 "$SERIAL_LOG" 2>/dev/null || true
            die "lab VM exited before Proxmox became ready"
        fi
        if ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=5 -p "$SSH_PORT" root@127.0.0.1 \
            'test -e /var/lib/qsm-lab-ready && pveversion >/dev/null && systemctl is-active --quiet pveproxy' \
            >/dev/null 2>&1; then
            echo "q-sunshine Proxmox lab is ready"
            return 0
        fi
        now=$SECONDS
        if (( now >= deadline )); then
            tail -n 160 "$SERIAL_LOG" 2>/dev/null || true
            die "timed out waiting for cloud-init and Proxmox VE"
        fi
        sleep 5
    done
}

ssh_lab() {
    running || die "lab VM is not running"
    exec ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -p "$SSH_PORT" root@127.0.0.1 "$@"
}

status() {
    if running; then
        echo "running pid=$(lab_pid) web=https://127.0.0.1:${WEB_PORT} ssh=127.0.0.1:${SSH_PORT}"
    else
        echo "stopped"
        return 1
    fi
}

stop() {
    if ! running; then
        sudo -n rm -f "$PID_FILE" "$QMP_SOCKET"
        echo "q-sunshine Proxmox lab is already stopped"
        return 0
    fi
    local pid
    pid="$(lab_pid)"
    sudo -n kill -TERM "$pid"
    for _ in $(seq 1 30); do
        if ! sudo -n kill -0 "$pid" 2>/dev/null; then
            sudo -n rm -f "$PID_FILE" "$QMP_SOCKET"
            echo "q-sunshine Proxmox lab stopped"
            return 0
        fi
        sleep 1
    done
    die "lab VM did not stop gracefully (pid $pid)"
}

usage() {
    echo "usage: $0 {start|wait|ssh|status|stop}" >&2
    exit 2
}

case "${1:-}" in
    start) start ;;
    wait) wait_ready ;;
    ssh) shift; ssh_lab "$@" ;;
    status) status ;;
    stop) stop ;;
    *) usage ;;
esac
