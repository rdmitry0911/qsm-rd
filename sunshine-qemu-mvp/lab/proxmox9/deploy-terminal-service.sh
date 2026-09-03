#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Install one explicitly supplied q-sunshine PVE package into the already
# running nested-PVE lab and replace its deliberately manual VM 100 harness
# with the one node-level q-sunshine-terminal service.
#
# This is intentionally *not* a production installer.  Its fixed node, VM,
# forwarded ports, and managed configuration are the bounded qualification
# topology described in README.md.  It fails before stopping the existing
# manual stack when it sees a different node, VM layout, live legacy service,
# or an unmanaged configuration file.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_DIR="$ROOT_DIR/lab/proxmox9"
LAB_RUNNER="$LAB_DIR/run-lab.sh"

readonly LAB_NODE='qsm-pve9-lab'
readonly VMID='100'
readonly RENDER_NODE='/dev/dri/renderD128'
readonly SSH_PORT="${QSUNSHINE_PVE_LAB_SSH_PORT:-12222}"

die() {
    printf 'q-sunshine lab deploy: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
usage: ./lab/proxmox9/deploy-terminal-service.sh --deb /absolute/or/relative/path/q-sunshine-pve_*.deb

Installs the exact supplied amd64 q-sunshine-pve artifact in the currently
running qsm-pve9-lab, configures the fixed VM 100 qualification topology, and
switches only its validated manual QEMU/D-Bus/QSF harness to
q-sunshine-terminal.service.

The artifact is copied through the same ephemeral lab SSH endpoint used by
run-lab.sh.  A SHA-256 and Debian package identity are checked on both sides;
the root-only staging directory is retained on a failure for inspection.
EOF
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required local command: $1"
}

# run-lab.sh deliberately owns the SSH target and options.  Pass one
# shell-escaped command string so paths/digests remain separate remote argv
# values without relying on word splitting at the remote login shell.
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

# This name is constrained both here and remotely.  It is not used as a glob
# or a directory prefix for deletion; a failed deployment deliberately retains
# the exact root-only staging directory for forensic inspection.
deployment_id="deploy-$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM}"
[[ "$deployment_id" =~ ^deploy-[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*-[0-9]+$ ]] ||
    die 'cannot create a safe deployment identifier'
remote_stage="/var/tmp/q-sunshine-lab-deploy/$deployment_id"
remote_artifact="$remote_stage/q-sunshine-pve.deb"

"$LAB_RUNNER" wait

# Prepare a private root-owned upload destination before copying any bytes.
# This changes no package, service, VM, or manual harness state.
run_remote_script "$remote_stage" <<'REMOTE_PREPARE'
set -Eeuo pipefail

stage=$1
die() {
    printf 'q-sunshine lab deploy: %s\n' "$*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || die 'remote deployment must run as root'
[[ "$(hostname -s)" == 'qsm-pve9-lab' ]] || die 'refusing a node other than qsm-pve9-lab'
[[ -e /var/lib/qsm-lab-ready ]] || die 'nested PVE lab has not reached its ready marker'
[[ "$stage" =~ ^/var/tmp/q-sunshine-lab-deploy/deploy-[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*-[0-9]+$ ]] ||
    die 'unsafe remote staging path'

parent=/var/tmp/q-sunshine-lab-deploy
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

# scp uses the same fixed loopback endpoint/key policy as run-lab.sh.  The
# control and mutation phases below still go exclusively through run-lab.sh
# SSH; scp transports only the explicitly selected package bytes.
scp -O -P "$SSH_PORT" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$artifact" "root@127.0.0.1:$remote_artifact"

run_remote_script "$remote_artifact" "$artifact_sha256" "$artifact_version" <<'REMOTE_DEPLOY'
set -Eeuo pipefail

readonly LAB_NODE='qsm-pve9-lab'
readonly VMID='100'
readonly RENDER_NODE='/dev/dri/renderD128'
readonly MANAGED_MARKER='# q-sunshine-lab-managed-v1'
readonly NODE_MAP_JSON='{"version":1,"nodes":{"qsm-pve9-lab":{"host":"127.0.0.1","port":58123,"server_name":"qsm-pve9-lab"}}}'

artifact=$1
expected_sha256=$2
expected_version=$3

die() {
    printf 'q-sunshine lab deploy: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required remote command: $1"
}

mode_is_not_group_or_world_writable() {
    local mode=$1 numeric_mode
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
    numeric_mode=$((8#$mode))
    (( (numeric_mode & 0022) == 0 ))
}

assert_root_regular() {
    local path=$1 label=$2 metadata owner mode
    [[ -e "$path" && ! -L "$path" && -f "$path" ]] ||
        die "$label must be a non-symlink regular file"
    metadata="$(stat -Lc '%u:%g:%a' -- "$path")" || die "cannot inspect $label"
    owner=${metadata%%:*}
    mode=${metadata##*:}
    # pmxcfs commonly gives /etc/pve files the www-data group.  Root ownership
    # and no group/other write bit are the actual trust boundary for the
    # public cluster map; requiring group root would reject a normal PVE node.
    [[ "$owner" == '0' ]] || die "$label must be owned by root"
    mode_is_not_group_or_world_writable "$mode" || die "$label is group/world writable"
}

assert_root_directory() {
    local path=$1 label=$2 metadata owner mode
    [[ -d "$path" && ! -L "$path" ]] || die "$label must be a non-symlink directory"
    metadata="$(stat -Lc '%u:%g:%a' -- "$path")" || die "cannot inspect $label"
    owner=${metadata%%:*}
    mode=${metadata##*:}
    [[ "$owner" == '0' ]] || die "$label must be owned by root"
    mode_is_not_group_or_world_writable "$mode" || die "$label is group/world writable"
}

read_pid_file() {
    local path=$1 label=$2 value
    assert_root_regular "$path" "$label"
    value="$(<"$path")"
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$label is not a valid PID"
    printf '%s\n' "$value"
}

process_command() {
    local pid=$1
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    [[ "$(stat -Lc '%u' -- "/proc/$pid")" == '0' ]] || return 1
    tr '\0' ' ' <"/proc/$pid/cmdline"
}

assert_qemu_process() {
    local pid command_line
    pid="$(read_pid_file "/run/qemu-server/$VMID.pid" 'VM 100 QEMU pid file')"
    command_line="$(process_command "$pid")" || die 'VM 100 QEMU process disappeared'
    [[ "$command_line" == *"-id $VMID "* ]] || die 'QEMU pid does not belong to VM 100'
    [[ "$command_line" == *"-display dbus,addr=unix:path=/run/q-sunshine/100/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128"* ]] ||
        die 'VM 100 QEMU process does not use the reviewed Display1 VirGL path'
    [[ "$command_line" == *'-device virtio-vga-gl'* ]] ||
        die 'VM 100 QEMU process lacks virtio-vga-gl'
    [[ "$command_line" == *'-chardev socket,id=qsf_agent,path=/run/q-sunshine/100/qsf-agent.sock,server=on,wait=off'* ]] ||
        die 'VM 100 QEMU process does not own the reviewed QSF agent socket'
}

assert_qemu_telemetry_process() {
    local pid command_line
    pid="$(read_pid_file "/run/qemu-server/$VMID.pid" 'VM 100 QEMU pid file')"
    command_line="$(process_command "$pid")" || die 'VM 100 QEMU process disappeared'
    [[ "$command_line" == *'-chardev socket,id=qsf_telemetry,path=/run/q-sunshine/100/qsf-telemetry.sock,server=on,wait=off'* ]] ||
        die 'VM 100 QEMU process does not own the reviewed guest telemetry socket'
    [[ "$command_line" == *'-device virtserialport,chardev=qsf_telemetry,name=org.qsunshine.virgl.wayland.telemetry'* ]] ||
        die 'VM 100 QEMU process lacks the reviewed guest telemetry virtserial port'
}

assert_manual_dbus_process() {
    local pid command_line
    pid="$(read_pid_file "/run/q-sunshine/$VMID/dbus.pid" 'manual Display1 D-Bus pid file')"
    command_line="$(process_command "$pid")" || die 'manual Display1 D-Bus process disappeared'
    [[ "$command_line" == *dbus-daemon* ]] &&
        [[ "$command_line" == *'--session'* ]] &&
        [[ "$command_line" == *"--address=unix:path=/run/q-sunshine/$VMID/qemu-display1.bus"* ]] ||
        die 'manual D-Bus pid is not the reviewed VM 100 Display1 bus'
    printf '%s\n' "$pid"
}

assert_manual_qsf_process() {
    local pid command_line
    pid="$(read_pid_file "/run/q-sunshine/$VMID/qsf-control.pid" 'manual QSF-control pid file')"
    command_line="$(process_command "$pid")" || die 'manual QSF-control process disappeared'
    [[ "$command_line" == *qsf_control.py* ]] &&
        [[ "$command_line" == *"--agent-socket /run/q-sunshine/$VMID/qsf-agent.sock"* ]] &&
        [[ "$command_line" == *"--control-socket /run/q-sunshine/$VMID/qsf-control.sock"* ]] &&
        [[ "$command_line" == *"--token-file /run/q-sunshine/$VMID/qsf-control.token"* ]] ||
        die 'manual QSF pid is not the reviewed VM 100 QSF control process'
    assert_root_regular "/run/q-sunshine/$VMID/qsf-control.token" 'manual QSF control token file'
    [[ -S "/run/q-sunshine/$VMID/qsf-control.sock" && ! -L "/run/q-sunshine/$VMID/qsf-control.sock" ]] ||
        die 'manual QSF control socket is not a socket'
    [[ "$(stat -Lc '%u' -- "/run/q-sunshine/$VMID/qsf-control.sock")" == '0' ]] ||
        die 'manual QSF control socket is not root-owned'
    printf '%s\n' "$pid"
}

stop_validated_process() {
    local pid=$1 label=$2 command_line attempts
    [[ -n "$pid" ]] || return 0
    if ! command_line="$(process_command "$pid")"; then
        return 0
    fi
    case "$label" in
        qsf)
            [[ "$command_line" == *qsf_control.py* ]] &&
                [[ "$command_line" == *"--agent-socket /run/q-sunshine/$VMID/qsf-agent.sock"* ]] ||
                die 'refusing to signal a PID no longer identified as manual QSF control'
            ;;
        dbus)
            [[ "$command_line" == *dbus-daemon* ]] &&
                [[ "$command_line" == *"--address=unix:path=/run/q-sunshine/$VMID/qemu-display1.bus"* ]] ||
                die 'refusing to signal a PID no longer identified as manual Display1 D-Bus'
            ;;
        *) die 'internal process label is invalid' ;;
    esac
    kill -TERM "$pid"
    for attempts in $(seq 1 12); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
    done
    die "manual $label process did not stop after SIGTERM"
}

remove_exact_root_socket() {
    local path=$1 label=$2
    [[ ! -L "$path" ]] || die "$label is a symlink"
    [[ -e "$path" ]] || return 0
    [[ -S "$path" ]] || die "$label is not a socket"
    [[ "$(stat -Lc '%u' -- "$path")" == '0' ]] || die "$label is not root-owned"
    # This is an exact socket path under the fixed VM runtime directory, after
    # QEMU and the reviewed manual daemon have stopped.  Never remove a
    # directory, glob, or an arbitrary caller-controlled path.
    rm -- "$path"
}

await_vm_state() {
    local expected=$1 attempts current
    for attempts in $(seq 1 60); do
        current="$(qm status "$VMID" 2>/dev/null || true)"
        [[ "$current" == "status: $expected" ]] && return 0
        sleep 1
    done
    die "VM $VMID did not reach state $expected"
}

await_socket() {
    local path=$1 label=$2 attempts
    for attempts in $(seq 1 30); do
        if [[ -S "$path" && ! -L "$path" ]] &&
            [[ "$(stat -Lc '%u' -- "$path" 2>/dev/null || true)" == '0' ]]; then
            return 0
        fi
        sleep 1
    done
    die "$label did not become a root-owned socket"
}

assert_managed_or_absent() {
    local path=$1 label=$2
    if [[ -e "$path" || -L "$path" ]]; then
        assert_root_regular "$path" "$label"
        grep -Fqx -- "$MANAGED_MARKER" "$path" ||
            die "$label exists but is not this lab's managed configuration"
    fi
}

write_terminal_configuration() {
    local path=/etc/q-sunshine/terminal.conf temporary
    assert_managed_or_absent "$path" 'terminal configuration'
    assert_root_directory /etc/q-sunshine 'q-sunshine configuration directory'
    temporary="$(mktemp /etc/q-sunshine/.terminal.conf.lab.XXXXXX)"
    {
        printf '%s\n' "$MANAGED_MARKER"
        printf '%s\n' 'QSUNSHINE_TERMINAL_SERVER_CERT=/etc/pve/local/pve-ssl.pem'
        printf '%s\n' 'QSUNSHINE_TERMINAL_SERVER_KEY=/etc/pve/local/pve-ssl.key'
        printf '%s\n' 'QSUNSHINE_TERMINAL_DESCRIPTOR_CA_FILE=/etc/pve/pve-root-ca.pem'
        printf '%s\n' 'QSUNSHINE_TERMINAL_TICKET_KEY=/etc/q-sunshine/terminal/ticket.key'
        printf '%s\n' 'QSUNSHINE_TERMINAL_NODE_ENDPOINTS_FILE=/etc/pve/q-sunshine-node-endpoints.json'
        printf '%s\n' 'QSUNSHINE_TERMINAL_LISTEN_HOST=0.0.0.0'
        printf '%s\n' 'QSUNSHINE_TERMINAL_LISTEN_PORT=48123'
        printf '%s\n' 'QSUNSHINE_TERMINAL_TRANSPORT_BIND_HOST=0.0.0.0'
        printf '%s\n' 'QSUNSHINE_TERMINAL_LOCAL_NODE=qsm-pve9-lab'
        printf '%s\n' 'QSUNSHINE_TERMINAL_VM_RUNTIME_DIRECTORY=/run/q-sunshine'
    } >"$temporary"
    chown root:root -- "$temporary"
    chmod 0600 -- "$temporary"
    mv -f -- "$temporary" "$path"
}

write_vm_configuration() {
    local directory=/etc/q-sunshine/instances.d path=/etc/q-sunshine/instances.d/100.conf temporary
    install -d -o root -g root -m 0750 -- "$directory"
    assert_root_directory "$directory" 'q-sunshine VM-policy directory'
    assert_managed_or_absent "$path" 'VM 100 terminal policy'
    temporary="$(mktemp "$directory/.100.conf.lab.XXXXXX")"
    {
        printf '%s\n' "$MANAGED_MARKER"
        printf '%s\n' 'SUNSHINE_QEMU_DBUS_ADDRESS=unix:path=/run/q-sunshine/100/qemu-display1.bus'
        printf '%s\n' 'SUNSHINE_QEMU_DBUS_DESTINATION=org.qemu'
        printf '%s\n' 'SUNSHINE_QEMU_DBUS_RENDER_NODE=/dev/dri/renderD128'
        printf '%s\n' 'QSUNSHINE_MEDIA_PORT=47989'
        printf '%s\n' 'QSUNSHINE_QSF_PORT=48122'
        printf '%s\n' 'QSUNSHINE_QSF_AGENT_SOCKET=/run/q-sunshine/100/qsf-agent.sock'
        printf '%s\n' 'QSUNSHINE_ADVERTISE_HOST=127.0.0.1'
        printf '%s\n' 'QSUNSHINE_PVE_NODE=qsm-pve9-lab'
        printf '%s\n' 'QSUNSHINE_ENCODER=software'
        printf '%s\n' 'QSUNSHINE_QSF_HOST_MAX_WIDTH=1920'
        printf '%s\n' 'QSUNSHINE_QSF_HOST_MAX_HEIGHT=1080'
        printf '%s\n' 'QSUNSHINE_QSF_HOST_MAX_FPS=60'
        printf '%s\n' 'QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS=16000'
        printf '%s\n' 'QSUNSHINE_QSF_HOST_ENCODER_CODECS=H264'
        printf '%s\n' 'QSUNSHINE_GAMESTREAM_LEASE_ISSUER=/usr/lib/q-sunshine/bin/q-sunshine-lease-issuer'
        printf '%s\n' 'QSUNSHINE_GAMESTREAM_LEASE_CA_CERT=/etc/q-sunshine/vms/100/gamestream-ca.crt'
        printf '%s\n' 'QSUNSHINE_GAMESTREAM_LEASE_CA_KEY=/etc/q-sunshine/vms/100/gamestream-ca.key'
        printf '%s\n' 'QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_CERT=/etc/q-sunshine/vms/100/sunshine-server.crt'
        printf '%s\n' 'QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_KEY=/etc/q-sunshine/vms/100/sunshine-server.key'
        printf '%s\n' 'QSUNSHINE_GAMESTREAM_LEASE_TTL_SECONDS=300'
    } >"$temporary"
    chown root:root -- "$temporary"
    chmod 0600 -- "$temporary"
    mv -f -- "$temporary" "$path"
}

write_node_map() {
    local path=/etc/pve/q-sunshine-node-endpoints.json temporary
    assert_root_directory /etc/pve 'PVE cluster configuration directory'
    if [[ -e "$path" || -L "$path" ]]; then
        assert_root_regular "$path" 'shared PVE node endpoint map'
        cmp -s <(printf '%s\n' "$NODE_MAP_JSON") "$path" ||
            die 'shared PVE node endpoint map exists but is not this lab map'
    fi
    temporary="$(mktemp /etc/pve/.q-sunshine-node-endpoints.lab.XXXXXX)"
    printf '%s\n' "$NODE_MAP_JSON" >"$temporary"
    # pmxcfs deliberately materializes new cluster files as root:www-data
    # (normally mode 0640) and rejects chown, even from root.  That default
    # is readable by pveproxy and meets the map's root-owned/non-writable
    # trust boundary; do not turn an expected pmxcfs policy into a failed lab
    # deployment by forcing host-filesystem ownership semantics.
    assert_root_regular "$temporary" 'temporary shared PVE node endpoint map'
    mv -f -- "$temporary" "$path"
    assert_root_regular "$path" 'shared PVE node endpoint map'
}

ensure_ticket_key() {
    local directory=/etc/q-sunshine/terminal path=/etc/q-sunshine/terminal/ticket.key temporary
    install -d -o root -g root -m 0700 -- "$directory"
    assert_root_directory "$directory" 'terminal key directory'
    if [[ ! -e "$path" && ! -L "$path" ]]; then
        temporary="$(mktemp "$directory/.ticket.key.lab.XXXXXX")"
        umask 077
        head -c 32 /dev/urandom >"$temporary"
        chown root:root -- "$temporary"
        chmod 0600 -- "$temporary"
        mv -f -- "$temporary" "$path"
    fi
    assert_root_regular "$path" 'terminal ticket key'
    [[ "$(stat -Lc '%a' -- "$path")" == '600' ]] || die 'terminal ticket key must be mode 0600'
    [[ "$(wc -c <"$path")" == '32' ]] || die 'terminal ticket key must contain exactly 32 bytes'
}

validate_vm_material() {
    local material_dir=/etc/q-sunshine/vms/100 key certificate
    assert_root_directory "$material_dir" 'VM 100 native GameStream material directory'
    for key in gamestream-ca.key sunshine-server.key; do
        assert_root_regular "$material_dir/$key" "VM 100 $key"
        [[ "$(stat -Lc '%a' -- "$material_dir/$key")" == '600' ]] ||
            die "VM 100 $key must be mode 0600"
    done
    for certificate in gamestream-ca.crt sunshine-server.crt; do
        assert_root_regular "$material_dir/$certificate" "VM 100 $certificate"
        [[ "$(stat -Lc '%a' -- "$material_dir/$certificate")" == '644' ]] ||
            die "VM 100 $certificate must be mode 0644"
        openssl x509 -in "$material_dir/$certificate" -noout >/dev/null 2>&1 ||
            die "VM 100 $certificate is not a valid X.509 certificate"
    done
}

ensure_vm_material() {
    local material_dir=/etc/q-sunshine/vms/100 path present=0 total=0 name
    for name in gamestream-ca.crt gamestream-ca.key sunshine-server.crt sunshine-server.key; do
        path="$material_dir/$name"
        ((total += 1))
        if [[ -e "$path" || -L "$path" ]]; then
            ((present += 1))
        fi
    done
    if ((present == 0)); then
        q-sunshine-provision-vm "$VMID" >/dev/null
    elif ((present != total)); then
        die 'VM 100 GameStream material is incomplete; refusing to replace it'
    fi
    validate_vm_material
}

assert_vm_layout() {
    local config status
    config="$(qm config "$VMID")" || die 'VM 100 does not exist'
    [[ "$config" == *'vga: none'* ]] || die 'VM 100 must have vga: none'
    [[ "$config" == *'-display dbus,addr=unix:path=/run/q-sunshine/100/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128'* ]] ||
        die 'VM 100 does not contain the reviewed Display1 VirGL args'
    [[ "$config" == *'-device virtio-vga-gl'* ]] || die 'VM 100 lacks virtio-vga-gl'
    [[ "$config" == *'-chardev socket,id=qsf_agent,path=/run/q-sunshine/100/qsf-agent.sock,server=on,wait=off'* ]] ||
        die 'VM 100 does not contain the reviewed QSF socket args'
    [[ "$config" == *'-device virtio-serial-pci'* ]] &&
        [[ "$config" == *'-device virtserialport,chardev=qsf_agent,'* ]] ||
        die 'VM 100 lacks the reviewed virtio-serial QSF device'
    status="$(qm status "$VMID")"
    case "$status" in
        'status: running'|'status: stopped') ;;
        *) die "VM 100 is in an unsafe state: $status" ;;
    esac
}

assert_vm_telemetry_layout() {
    local config
    config="$(qm config "$VMID")" || die 'VM 100 does not exist'
    [[ "$config" == *'-chardev socket,id=qsf_telemetry,path=/run/q-sunshine/100/qsf-telemetry.sock,server=on,wait=off'* ]] ||
        die 'VM 100 does not contain the reviewed guest telemetry socket args'
    [[ "$config" == *'-device virtserialport,chardev=qsf_telemetry,name=org.qsunshine.virgl.wayland.telemetry'* ]] ||
        die 'VM 100 lacks the reviewed guest telemetry virtserial port'
}

ensure_vm_telemetry_layout() {
    local config args updated
    config="$(qm config "$VMID")" || die 'VM 100 does not exist'
    args="$(sed -n 's/^args: //p' <<<"$config")"
    [[ -n "$args" ]] || die 'VM 100 has no reviewed QEMU args line'
    if [[ "$args" == *'qsf_telemetry'* || "$args" == *'org.qsunshine.virgl.wayland.telemetry'* ]]; then
        assert_vm_telemetry_layout
        return 0
    fi
    # This lab owns exactly VM 100's reviewed argument string.  Preserve the
    # already validated Display1/QSF/input layout and append the independent
    # one-way guest telemetry chardev only while the VM is stopped.  Do not
    # attempt to infer or rewrite arbitrary user QEMU arguments.
    [[ "$(qm status "$VMID")" == 'status: stopped' ]] ||
        die 'VM 100 must be stopped before adding the guest telemetry port'
    [[ "$args" == *'-display dbus,addr=unix:path=/run/q-sunshine/100/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128'* ]] &&
        [[ "$args" == *'-chardev socket,id=qsf_agent,path=/run/q-sunshine/100/qsf-agent.sock,server=on,wait=off'* ]] &&
        [[ "$args" == *'-device virtio-serial-pci,id=qsf_virtio_serial'* ]] &&
        [[ "$args" == *'-device virtserialport,chardev=qsf_agent,'* ]] ||
        die 'VM 100 args are not the reviewed base topology; refusing to append telemetry'
    updated="$args -chardev socket,id=qsf_telemetry,path=/run/q-sunshine/100/qsf-telemetry.sock,server=on,wait=off -device virtserialport,chardev=qsf_telemetry,name=org.qsunshine.virgl.wayland.telemetry"
    qm set "$VMID" --args "$updated" >/dev/null
    assert_vm_layout
    assert_vm_telemetry_layout
}

assert_no_live_legacy_units() {
    local unit
    for unit in \
        q-sunshine@100.service \
        q-sunshine-auth@100.service \
        q-sunshine-qsf-control@100.service \
        q-sunshine-qsf-gateway@100.service \
        q-sunshine-qsf-system-auth-gateway@100.service; do
        systemctl is-active --quiet "$unit" &&
            die "legacy unit is active: $unit"
    done
    systemctl is-active --quiet q-sunshine-terminal.service &&
        die 'q-sunshine-terminal is already active; refusing an in-place replacement'
    # A false `is-active` is the successful expected case.  Without this
    # explicit return, the final false condition becomes this function's
    # status and `set -e` silently terminates the remote deployment before
    # package installation on a clean lab node.
    return 0
}

assert_lab_target() {
    [[ "$(id -u)" -eq 0 ]] || die 'remote deployment must run as root'
    [[ "$(hostname -s)" == "$LAB_NODE" ]] || die 'refusing a node other than qsm-pve9-lab'
    [[ -e /var/lib/qsm-lab-ready ]] || die 'nested PVE lab has not reached its ready marker'
    pveversion | grep -Eq '^pve-manager/9\.' || die 'this is not a PVE 9 lab node'
    [[ -c /dev/kvm ]] || die 'nested lab has no /dev/kvm'
    [[ -c "$RENDER_NODE" ]] || die "nested lab has no VirGL render node: $RENDER_NODE"
}

# This pre-install set is deliberately limited to commands supplied by the
# validated PVE lab itself.  The q-sunshine commands are part of the artifact
# being installed below, so requiring them here would make the first deploy
# impossible and falsely reject a clean node.
for command in apt-get awk cmp dpkg-deb dpkg-query grep head install journalctl mktemp mv openssl \
               qm rm sed seq sha256sum sleep stat systemctl tr wc; do
    require_command "$command"
done
assert_lab_target

[[ "$artifact" =~ ^/var/tmp/q-sunshine-lab-deploy/deploy-[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*-[0-9]+/q-sunshine-pve\.deb$ ]] ||
    die 'unsafe staged artifact path'
assert_root_regular "$artifact" 'staged artifact'
[[ "$(sha256sum -- "$artifact" | awk '{print $1}')" == "$expected_sha256" ]] ||
    die 'staged artifact SHA-256 differs from the explicitly supplied artifact'
[[ "$(dpkg-deb -f "$artifact" Package)" == 'q-sunshine-pve' ]] ||
    die 'staged artifact is not q-sunshine-pve'
[[ "$(dpkg-deb -f "$artifact" Architecture)" == 'amd64' ]] ||
    die 'staged artifact architecture is not amd64'
[[ "$(dpkg-deb -f "$artifact" Version)" == "$expected_version" ]] ||
    die 'staged artifact version differs from the explicitly supplied artifact'

# Everything through this point is read-only aside from the private upload
# directory.  Validate the target and reject any active old systemd topology
# before package/configuration work, and validate it once more immediately
# before stopping the known manual processes.
assert_vm_layout
assert_no_live_legacy_units

# Resolve the package's real Debian/Trixie runtime dependencies through the
# node's configured PVE/Debian repositories.  `dpkg -i` alone leaves a clean
# PVE lab unpacked when the minimal image has not previously needed Sunshine's
# libevdev/miniupnpc libraries, then every later terminal check is misleading.
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends --fix-broken "$artifact"
installed_version="$(dpkg-query -W -f='${Version}' q-sunshine-pve)" ||
    die 'q-sunshine-pve did not install'
[[ "$installed_version" == "$expected_version" ]] ||
    die 'installed q-sunshine-pve version differs from the supplied artifact'

for command in q-sunshine-preflight q-sunshine-provision-vm q-sunshine-pve-ui; do
    require_command "$command"
done

SUNSHINE_QEMU_DBUS_RENDER_NODE="$RENDER_NODE" q-sunshine-preflight --virgl >/dev/null
ensure_ticket_key
ensure_vm_material
write_terminal_configuration
write_vm_configuration
write_node_map

# Reconcile only after the authoritative map exists.  The UI helper safely
# restores stock PVE markup on an unsupported version; treat that non-error
# state as a failed lab gate instead of proceeding without Console integration.
q-sunshine-pve-ui reconcile >/dev/null
[[ "$(q-sunshine-pve-ui status)" == 'q-sunshine PVE UI: active' ]] ||
    die 'PVE Console q-sunshine overlay is not active'
systemctl daemon-reload

# Re-read the QEMU topology directly before touching the manual harness.  A
# running lab must expose exactly the known temporary QEMU, QSF, and D-Bus
# processes; an already-stopped VM may have no such processes after a prior
# interrupted deployment.
assert_vm_layout
vm_state="$(qm status "$VMID")"
manual_qsf_pid=''
manual_dbus_pid=''
if [[ "$vm_state" == 'status: running' ]]; then
    assert_qemu_process
    [[ -S "/run/q-sunshine/$VMID/qsf-agent.sock" && ! -L "/run/q-sunshine/$VMID/qsf-agent.sock" ]] ||
        die 'running VM 100 does not expose the reviewed QSF agent socket'
    [[ "$(stat -Lc '%u' -- "/run/q-sunshine/$VMID/qsf-agent.sock")" == '0' ]] ||
        die 'running VM 100 QSF agent socket is not root-owned'
    manual_qsf_pid="$(assert_manual_qsf_process)"
    manual_dbus_pid="$(assert_manual_dbus_process)"
    printf 'QSM_LAB_MANUAL_STACK_VALIDATED vmid=%s components=qemu,qsf,dbus\n' "$VMID"
    qm stop "$VMID" --timeout 60
    await_vm_state stopped
else
    # A prior failed run can leave the lab VM stopped while a manual helper is
    # still alive.  Only stop it if its PID file and complete command line are
    # still the expected VM-100 process; otherwise stop and ask for review.
    if [[ -e "/run/q-sunshine/$VMID/qsf-control.pid" || -L "/run/q-sunshine/$VMID/qsf-control.pid" ]]; then
        manual_qsf_pid="$(assert_manual_qsf_process)"
    fi
    if [[ -e "/run/q-sunshine/$VMID/dbus.pid" || -L "/run/q-sunshine/$VMID/dbus.pid" ]]; then
        manual_dbus_pid="$(assert_manual_dbus_process)"
    fi
fi

stop_validated_process "$manual_qsf_pid" qsf
stop_validated_process "$manual_dbus_pid" dbus
remove_exact_root_socket "/run/q-sunshine/$VMID/qsf-agent.sock" 'stale VM 100 QSF agent socket'
remove_exact_root_socket "/run/q-sunshine/$VMID/qemu-display1.bus" 'stale VM 100 Display1 D-Bus socket'
remove_exact_root_socket "/run/q-sunshine/$VMID/qsf-telemetry.sock" 'stale VM 100 guest telemetry socket'
# The guest input watcher writes its raw evdev evidence only to this separate
# virtserial channel.  Add it after the known manual QEMU instance has stopped
# and before the terminal-owned bus starts the replacement VM.
ensure_vm_telemetry_layout
printf 'QSM_LAB_MANUAL_STACK_RETIRED vmid=%s\n' "$VMID"

# The terminal service owns the replacement private D-Bus daemon before QEMU
# is restarted.  It does not start a Sunshine/QSF network worker until the
# later PVE-authorized descriptor request.
systemctl enable --now q-sunshine-terminal.service >/dev/null
systemctl is-enabled --quiet q-sunshine-terminal.service ||
    die 'q-sunshine-terminal was not enabled'
systemctl is-active --quiet q-sunshine-terminal.service ||
    die 'q-sunshine-terminal is not active'
await_socket "/run/q-sunshine/$VMID/qemu-display1.bus" 'terminal-owned Display1 D-Bus socket'
[[ "$(stat -Lc '%a' -- "/run/q-sunshine/$VMID/qemu-display1.bus")" == '700' ]] ||
    die 'terminal-owned Display1 D-Bus socket must be mode 0700'
journalctl -u q-sunshine-terminal.service --no-pager -n 80 | grep -Fq 'Q_SUNSHINE_TERMINAL_READY' ||
    die 'terminal service did not emit its bounded readiness marker'

qm start "$VMID"
await_vm_state running
await_socket "/run/q-sunshine/$VMID/qsf-agent.sock" 'VM 100 QSF agent socket after restart'
await_socket "/run/q-sunshine/$VMID/qsf-telemetry.sock" 'VM 100 guest telemetry socket after restart'
assert_qemu_telemetry_process

printf 'QSM_LAB_TERMINAL_DEPLOY_READY node=%s vmid=%s package=%s terminal=active pve_ui=active display1_bus=ready qemu=running qsf_agent=ready guest_telemetry=ready\n' \
    "$LAB_NODE" "$VMID" "$installed_version"
printf 'QSM_LAB_TERMINAL_DEPLOY_ARTIFACT sha256=%s\n' "$expected_sha256"
printf 'QSM_LAB_TERMINAL_DEPLOY_STAGING_RETAINED path=%s\n' "${artifact%/q-sunshine-pve.deb}"
REMOTE_DEPLOY
