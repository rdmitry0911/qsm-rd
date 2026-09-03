#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Upgrade the already-packaged q-sunshine terminal topology in the disposable
# nested PVE lab.  This is deliberately separate from
# deploy-terminal-service.sh: that script proves the one-time transition from
# the old manual harness, while this one proves a later package upgrade without
# pretending that an active terminal service is a legacy deployment.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_DIR="$ROOT_DIR/lab/proxmox9"
LAB_RUNNER="$LAB_DIR/run-lab.sh"

readonly LAB_NODE='qsm-pve9-lab'
readonly VMID='100'
readonly RENDER_NODE='/dev/dri/renderD128'
readonly SSH_PORT="${QSUNSHINE_PVE_LAB_SSH_PORT:-12222}"

die() {
    printf 'q-sunshine lab upgrade: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
usage: ./lab/proxmox9/upgrade-terminal-package.sh --deb /path/q-sunshine-pve_<new-version>_amd64.deb

Safely upgrades the already-running qsm-pve9-lab topology only. It verifies
the exact artifact identity and SHA-256 locally and remotely, then stops only
VM 100 and q-sunshine-terminal.service, installs the newer package, reconciles
the guarded PVE Console UI, starts the one service and VM again, and checks
the Display1/QSF/telemetry sockets.

This is a lab qualification tool, not a production updater. On a failed
upgrade it deliberately leaves the VM/service stopped rather than attempting
an unvalidated rollback; the root-only staged artifact is retained for review.
EOF
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required local command: $1"
}

# run-lab.sh owns the fixed nested-lab SSH target and options. Quote every
# remote argument as a separate bash argv item rather than interpolating an
# artifact path, digest, or version into a remote shell program.
run_remote_script() {
    local argument quoted command='bash -s --'
    for argument in "$@"; do
        printf -v quoted '%q' "$argument"
        command+=" $quoted"
    done
    "$LAB_RUNNER" ssh "$command"
}

artifact=''
while (($#)); do
    case "$1" in
        --deb)
            (($# >= 2)) || die '--deb requires a path'
            [[ -z "$artifact" ]] || die '--deb may be supplied only once'
            artifact=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

[[ -n "$artifact" ]] || {
    usage >&2
    exit 2
}
[[ -x "$LAB_RUNNER" ]] || die "lab runner is unavailable: $LAB_RUNNER"
[[ "$SSH_PORT" =~ ^[1-9][0-9]{0,4}$ ]] && ((SSH_PORT <= 65535)) ||
    die 'QSUNSHINE_PVE_LAB_SSH_PORT must be a valid TCP port'

for command in dpkg-deb readlink scp sha256sum; do
    require_command "$command"
done

[[ -f "$artifact" && ! -L "$artifact" ]] ||
    die "artifact must be a non-symlink regular file: $artifact"
artifact="$(readlink -f -- "$artifact")"

artifact_package="$(LC_ALL=C dpkg-deb -f "$artifact" Package)" ||
    die 'cannot read Debian package identity from artifact'
artifact_version="$(LC_ALL=C dpkg-deb -f "$artifact" Version)" ||
    die 'cannot read Debian package version from artifact'
artifact_architecture="$(LC_ALL=C dpkg-deb -f "$artifact" Architecture)" ||
    die 'cannot read Debian package architecture from artifact'
[[ "$artifact_package" == 'q-sunshine-pve' ]] ||
    die "artifact package must be q-sunshine-pve (got $artifact_package)"
[[ "$artifact_architecture" == 'amd64' ]] ||
    die "artifact architecture must be amd64 (got $artifact_architecture)"
[[ "$artifact_version" =~ ^[A-Za-z0-9.+:~_-]{1,128}$ ]] ||
    die 'artifact has an unsafe Debian version string'
artifact_sha256="$(sha256sum -- "$artifact" | awk '{print $1}')"
[[ "$artifact_sha256" =~ ^[0-9a-f]{64}$ ]] || die 'cannot calculate artifact SHA-256'

# The directory is retained after both success and failure, matching the
# initial-deploy tool. It contains exactly the supplied public package bytes,
# never credentials, tickets, or transient descriptors.
upgrade_id="upgrade-$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM}"
[[ "$upgrade_id" =~ ^upgrade-[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*-[0-9]+$ ]] ||
    die 'cannot create a safe upgrade identifier'
remote_stage="/var/tmp/q-sunshine-lab-upgrade/$upgrade_id"
remote_artifact="$remote_stage/q-sunshine-pve.deb"

"$LAB_RUNNER" wait

# This phase can create only a new private staging directory. It does not
# touch the package, PVE UI, service, VM, or a runtime socket.
run_remote_script "$remote_stage" <<'REMOTE_PREPARE'
set -Eeuo pipefail

stage=$1
die() {
    printf 'q-sunshine lab upgrade: %s\n' "$*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || die 'remote upgrade must run as root'
[[ "$(hostname -s)" == 'qsm-pve9-lab' ]] || die 'refusing a node other than qsm-pve9-lab'
[[ -e /var/lib/qsm-lab-ready ]] || die 'nested PVE lab has not reached its ready marker'
[[ "$stage" =~ ^/var/tmp/q-sunshine-lab-upgrade/upgrade-[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*-[0-9]+$ ]] ||
    die 'unsafe remote staging path'

parent=/var/tmp/q-sunshine-lab-upgrade
if [[ -e "$parent" || -L "$parent" ]]; then
    [[ -d "$parent" && ! -L "$parent" ]] || die 'remote staging parent is unsafe'
else
    install -d -o root -g root -m 0700 -- "$parent"
fi
metadata="$(stat -Lc '%u:%g:%a' -- "$parent")" || die 'cannot inspect remote staging parent'
[[ "$metadata" == '0:0:700' ]] || die 'remote staging parent must be root:root mode 0700'
[[ ! -e "$stage" && ! -L "$stage" ]] || die 'remote staging path already exists'
install -d -o root -g root -m 0700 -- "$stage"
metadata="$(stat -Lc '%u:%g:%a' -- "$stage")" || die 'cannot inspect remote staging path'
[[ "$metadata" == '0:0:700' ]] || die 'remote staging path must be root:root mode 0700'
REMOTE_PREPARE

scp -O -P "$SSH_PORT" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$artifact" "root@127.0.0.1:$remote_artifact"

run_remote_script "$remote_artifact" "$artifact_sha256" "$artifact_version" <<'REMOTE_UPGRADE'
set -Eeuo pipefail

readonly LAB_NODE='qsm-pve9-lab'
readonly VMID='100'
readonly RENDER_NODE='/dev/dri/renderD128'
readonly NODE_MAP_JSON='{"version":1,"nodes":{"qsm-pve9-lab":{"host":"127.0.0.1","port":58123,"server_name":"qsm-pve9-lab"}}}'
readonly TERMINAL_MARKER='# q-sunshine-lab-managed-v1'

artifact=$1
expected_sha256=$2
expected_version=$3

die() {
    printf 'q-sunshine lab upgrade: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required remote command: $1"
}

assert_root_regular() {
    local path=$1 label=$2 metadata owner mode
    [[ -e "$path" && ! -L "$path" && -f "$path" ]] ||
        die "$label must be a non-symlink regular file"
    metadata="$(stat -Lc '%u:%g:%a' -- "$path")" || die "cannot inspect $label"
    owner=${metadata%%:*}
    mode=${metadata##*:}
    [[ "$owner" == '0' ]] || die "$label must be owned by root"
    (( (8#$mode & 0022) == 0 )) || die "$label is group/world writable"
}

await_vm_state() {
    local expected=$1 attempts current
    for attempts in $(seq 1 90); do
        current="$(qm status "$VMID" 2>/dev/null || true)"
        [[ "$current" == "status: $expected" ]] && return 0
        sleep 1
    done
    die "VM $VMID did not reach state $expected"
}

await_root_socket() {
    local path=$1 label=$2 attempts metadata owner mode
    for attempts in $(seq 1 45); do
        if [[ -S "$path" && ! -L "$path" ]]; then
            metadata="$(stat -Lc '%u:%a' -- "$path" 2>/dev/null || true)"
            owner=${metadata%%:*}
            mode=${metadata##*:}
            # QEMU recreates its chardev listener with 0750 after VM start:
            # the owner remains root and no untrusted user gets access, while
            # the qemu group can retain metadata traversal.  Require that
            # there are no world permissions, rather than incorrectly
            # insisting on a 0700 socket that QEMU does not promise.  Bash
            # uses 8#077 rather than Python's 0o077 spelling.
            if [[ "$owner" == '0' ]] && (( (8#$mode & 8#007) == 0 )); then
                return 0
            fi
        fi
        sleep 1
    done
    die "$label did not become a root-owned private socket"
}

await_socket_absent() {
    local path=$1 label=$2 attempts
    for attempts in $(seq 1 30); do
        [[ ! -e "$path" && ! -L "$path" ]] && return 0
        sleep 1
    done
    die "$label was not removed after its service stopped"
}

assert_lab_topology() {
    local config unit
    [[ "$(id -u)" -eq 0 ]] || die 'remote upgrade must run as root'
    [[ "$(hostname -s)" == "$LAB_NODE" ]] || die 'refusing a node other than qsm-pve9-lab'
    [[ -e /var/lib/qsm-lab-ready ]] || die 'nested PVE lab has not reached its ready marker'
    pveversion | grep -Eq '^pve-manager/9\.' || die 'this is not a PVE 9 lab node'
    [[ -c /dev/kvm && -c "$RENDER_NODE" ]] || die 'nested KVM/VirGL prerequisites are unavailable'
    [[ "$(qm status "$VMID")" == 'status: running' ]] || die 'VM 100 must be running before a controlled upgrade'
    systemctl is-enabled --quiet q-sunshine-terminal.service ||
        die 'q-sunshine-terminal.service must be enabled before a controlled upgrade'
    systemctl is-active --quiet q-sunshine-terminal.service ||
        die 'q-sunshine-terminal.service must be active before a controlled upgrade'
    for unit in q-sunshine@100.service q-sunshine-auth@100.service \
                q-sunshine-qsf-control@100.service q-sunshine-qsf-gateway@100.service \
                q-sunshine-qsf-system-auth-gateway@100.service; do
        systemctl is-active --quiet "$unit" && die "legacy unit is active: $unit"
    done
    config="$(qm config "$VMID")" || die 'VM 100 does not exist'
    [[ "$config" == *'vga: none'* ]] || die 'VM 100 must use vga: none'
    [[ "$config" == *'-display dbus,addr=unix:path=/run/q-sunshine/100/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128'* ]] ||
        die 'VM 100 does not use the reviewed Display1 VirGL path'
    [[ "$config" == *'-device virtio-vga-gl'* ]] || die 'VM 100 lacks virtio-vga-gl'
    [[ "$config" == *'-chardev socket,id=qsf_agent,path=/run/q-sunshine/100/qsf-agent.sock,server=on,wait=off'* ]] ||
        die 'VM 100 does not use the reviewed QSF agent socket'
    [[ "$config" == *'-chardev socket,id=qsf_telemetry,path=/run/q-sunshine/100/qsf-telemetry.sock,server=on,wait=off'* ]] ||
        die 'VM 100 does not use the reviewed guest telemetry socket'
    [[ "$config" == *'-device virtserialport,chardev=qsf_telemetry,name=org.qsunshine.virgl.wayland.telemetry'* ]] ||
        die 'VM 100 lacks the reviewed telemetry port'
    assert_root_regular /etc/q-sunshine/terminal.conf 'terminal configuration'
    grep -Fqx -- "$TERMINAL_MARKER" /etc/q-sunshine/terminal.conf ||
        die 'terminal configuration is not owned by this lab'
    assert_root_regular /etc/pve/q-sunshine-node-endpoints.json 'shared node endpoint map'
    cmp -s <(printf '%s\n' "$NODE_MAP_JSON") /etc/pve/q-sunshine-node-endpoints.json ||
        die 'shared node endpoint map is not this lab map'
    [[ "$(q-sunshine-pve-ui status)" == 'q-sunshine PVE UI: active' ]] ||
        die 'PVE Console overlay is not active before controlled upgrade'
    await_root_socket /run/q-sunshine/100/qemu-display1.bus 'terminal Display1 bus'
    await_root_socket /run/q-sunshine/100/qsf-agent.sock 'VM QSF agent socket'
    await_root_socket /run/q-sunshine/100/qsf-telemetry.sock 'VM telemetry socket'
}

for command in apt-get cmp date dpkg dpkg-deb dpkg-query grep journalctl qm seq sha256sum sleep stat systemctl; do
    require_command "$command"
done
[[ "$artifact" =~ ^/var/tmp/q-sunshine-lab-upgrade/upgrade-[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*-[0-9]+/q-sunshine-pve\.deb$ ]] ||
    die 'unsafe staged artifact path'
assert_root_regular "$artifact" 'staged artifact'
[[ "$(sha256sum -- "$artifact" | awk '{print $1}')" == "$expected_sha256" ]] ||
    die 'staged artifact SHA-256 differs from the explicitly supplied artifact'
[[ "$(dpkg-deb -f "$artifact" Package)" == 'q-sunshine-pve' ]] || die 'staged artifact is not q-sunshine-pve'
[[ "$(dpkg-deb -f "$artifact" Architecture)" == 'amd64' ]] || die 'staged artifact is not amd64'
[[ "$(dpkg-deb -f "$artifact" Version)" == "$expected_version" ]] ||
    die 'staged artifact version differs from the explicitly supplied artifact'

installed_version="$(dpkg-query -W -f='${Version}' q-sunshine-pve)" ||
    die 'q-sunshine-pve is not installed; use deploy-terminal-service.sh first'
dpkg --compare-versions "$expected_version" gt "$installed_version" ||
    die 'artifact must be strictly newer than the installed package'

# Every check above is read-only. From here onwards this is the deliberately
# bounded outage: only VM 100 and its one node-level service are stopped.
assert_lab_topology
printf 'QSM_LAB_UPGRADE_STOPPING vmid=%s from=%s to=%s\n' "$VMID" "$installed_version" "$expected_version"
qm stop "$VMID" --timeout 90
await_vm_state stopped
systemctl stop q-sunshine-terminal.service
systemctl is-active --quiet q-sunshine-terminal.service &&
    die 'q-sunshine-terminal.service remained active after stop'
await_socket_absent /run/q-sunshine/100/qemu-display1.bus 'terminal Display1 bus'

# apt resolves only the supplied package's real Trixie/PVE dependencies. The
# artifact identity is rechecked immediately before this state-changing call.
[[ "$(sha256sum -- "$artifact" | awk '{print $1}')" == "$expected_sha256" ]] ||
    die 'staged artifact changed before package installation'
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends --fix-broken "$artifact"
[[ "$(dpkg-query -W -f='${db:Status-Status}' q-sunshine-pve)" == 'installed' ]] ||
    die 'q-sunshine-pve is not configured after package installation'
[[ "$(dpkg-query -W -f='${Version}' q-sunshine-pve)" == "$expected_version" ]] ||
    die 'installed q-sunshine-pve version differs from the supplied artifact'

q-sunshine --version >/dev/null
SUNSHINE_QEMU_DBUS_RENDER_NODE="$RENDER_NODE" q-sunshine-preflight --virgl >/dev/null
q-sunshine-pve-ui reconcile >/dev/null
[[ "$(q-sunshine-pve-ui status)" == 'q-sunshine PVE UI: active' ]] ||
    die 'PVE Console overlay is not active after package upgrade'

journal_since="$(date --iso-8601=seconds)"
systemctl daemon-reload
systemctl start q-sunshine-terminal.service
systemctl is-active --quiet q-sunshine-terminal.service ||
    die 'q-sunshine-terminal.service did not start after package upgrade'
await_root_socket /run/q-sunshine/100/qemu-display1.bus 'terminal Display1 bus after upgrade'
journalctl -u q-sunshine-terminal.service --since "$journal_since" --no-pager |
    grep -Fq 'Q_SUNSHINE_TERMINAL_READY' ||
    die 'terminal service did not emit its readiness marker after package upgrade'

qm start "$VMID"
await_vm_state running
await_root_socket /run/q-sunshine/100/qsf-agent.sock 'VM QSF agent socket after upgrade'
await_root_socket /run/q-sunshine/100/qsf-telemetry.sock 'VM telemetry socket after upgrade'

printf 'QSM_LAB_PACKAGE_UPGRADE_OK node=%s vmid=%s package=%s pve_ui=active terminal=active qemu=running qsf_agent=ready guest_telemetry=ready sha256=%s\n' \
    "$LAB_NODE" "$VMID" "$expected_version" "$expected_sha256"
printf 'QSM_LAB_PACKAGE_UPGRADE_STAGING_RETAINED path=%s\n' "${artifact%/q-sunshine-pve.deb}"
REMOTE_UPGRADE
