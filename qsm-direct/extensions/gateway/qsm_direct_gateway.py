#!/usr/bin/env python3
"""Opt-in, reversible single-node stock-PVE HTTPS + TURN/TCP gateway.

Never modifies Proxmox binaries, generic HAProxy/coturn configs or host ingress
rules. Enable/disable restarts pveproxy: run during a maintenance window.
"""
from __future__ import annotations
import argparse
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import socket
import subprocess
import sys

ETC = Path('/etc/qsm-pve-direct')
STATE = Path('/var/lib/qsm-pve-direct-gateway')
RUN = Path('/run/qsm-pve-direct-gateway')
PVE = Path('/etc/default/pveproxy')
UNITS = ('qsm-pve-direct-gateway.service', 'qsm-pve-direct-turn.service')
TABLE = 'qsm_direct_gateway'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def atomic(path, data, mode=0o600):
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def proxy_config(original):
    text = original.decode()
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith('#'):
            continue
        raise ValueError('existing pveproxy settings require manual review; automatic setup accepts comments only and never weakens a source ACL')
    return original + b'\n# QSM single-port gateway; restored byte-for-byte by disable.\nLISTEN_IP="127.0.0.1"\nPROXY_REAL_IP_HEADER="X-Forwarded-For"\nPROXY_REAL_IP_ALLOW_FROM="127.0.0.1"\n'


def haproxy_config(signal_port, address):
    return f'''global
    maxconn 1024
    user haproxy
    group haproxy
    tune.ssl.default-dh-param 2048
defaults
    mode tcp
    timeout connect 10s
    timeout client 1h
    timeout server 1h
frontend public
    bind {address}:8006
    bind [::]:8006 v6only
    tcp-request inspect-delay 5s
    acl tls req.ssl_hello_type 1
    acl turn req.payload(0,1),hex 00
    acl stun_cookie req.payload(4,4),hex 2112A442
    tcp-request content accept if tls
    tcp-request content accept if turn stun_cookie
    tcp-request content reject if WAIT_END
    use_backend https if tls
    default_backend turn
backend https
    server local 127.0.0.1:18006 send-proxy-v2
backend turn
    server local 127.0.0.1:13479 send-proxy-v2
frontend https
    mode http
    bind 127.0.0.1:18006 ssl crt {RUN}/proxy.pem accept-proxy
    timeout http-request 15s
    http-request del-header X-Forwarded-For
    http-request del-header Forwarded
    http-request set-header X-Forwarded-For %[src]
    http-request set-header X-Forwarded-Proto https
    acl qsm path_beg /qsm-direct/
    use_backend signal if qsm
    default_backend pve
backend signal
    mode http
    http-request replace-path ^/qsm-direct/(.*)$ /\\1
    server local 127.0.0.1:{signal_port} ssl verify required ca-file {RUN}/ca.pem
backend pve
    mode http
    timeout tunnel 1h
    server local 127.0.0.1:8006 ssl verify required ca-file {RUN}/ca.pem
'''


def turn_config(address, secret):
    return f'''listening-ip=127.0.0.1
listening-port=13480
tcp-proxy-port=13479
relay-ip={address}
realm=qsm-direct-node
server-name=qsm-direct-node
use-auth-secret
static-auth-secret={secret}
userdb=/var/lib/qsm-pve-direct-turn/turndb
fingerprint
no-udp
no-tls
no-dtls
no-tcp-relay
no-cli
no-multicast-peers
no-rfc5780
no-stun-backward-compatibility
no-stun
stale-nonce=600
user-quota=8
total-quota=128
max-bps=15000000
bps-capacity=250000000
denied-peer-ip=0.0.0.0-255.255.255.255
denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
allowed-peer-ip={address}
log-file=stdout
simple-log
no-stdout-log
'''


def firewall(uid):
    return f'''add table inet {TABLE}
add set inet {TABLE} peers {{ type ipv4_addr . inet_service; flags timeout; timeout 15s; }}
add chain inet {TABLE} output {{ type filter hook output priority -5; policy accept; }}
add rule inet {TABLE} output meta skuid {uid} meta l4proto udp ip daddr . udp dport @peers counter accept
add rule inet {TABLE} output meta skuid {uid} meta l4proto udp counter drop
'''


def certificates():
    RUN.mkdir(mode=0o750, exist_ok=True)
    cert = Path('/etc/pve/local/pveproxy-ssl.pem')
    key = Path('/etc/pve/local/pveproxy-ssl.key')
    if not cert.exists():
        cert, key = Path('/etc/pve/local/pve-ssl.pem'), Path('/etc/pve/local/pve-ssl.key')
    # HAProxy loads private keys as root before accepting connections.
    atomic(RUN / 'proxy.pem', cert.read_bytes() + b'\n' + key.read_bytes())
    atomic(RUN / 'ca.pem', Path('/etc/ssl/certs/ca-certificates.crt').read_bytes() +
           b'\n' + Path('/etc/pve/pve-root-ca.pem').read_bytes(), 0o644)


def snapshot():
    value = {}
    for name, path in (('proxy', PVE), ('signal', ETC / 'signal.json')):
        value[name] = base64.b64encode(path.read_bytes()).decode() if path.exists() else None
    value['signal_active'] = subprocess.run(['systemctl', 'is-active', '--quiet', 'qsm-pve-direct-signal']).returncode == 0
    value['signal_enabled'] = subprocess.run(['systemctl', 'is-enabled', '--quiet', 'qsm-pve-direct-signal'], stdout=subprocess.DEVNULL).returncode == 0
    return value


def restore():
    saved = json.loads((STATE / 'before.json').read_text())
    # Stop accepting new TURN allocations before opening the original listener.
    for unit in UNITS:
        subprocess.run(['systemctl', 'disable', '--now', unit], check=False, stdout=subprocess.DEVNULL)
    subprocess.run(['systemctl', 'disable', '--now', 'qsm-pve-direct-gateway-certs.timer'], check=False)
    for name, path in (('proxy', PVE), ('signal', ETC / 'signal.json')):
        previous = saved[name]
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, base64.b64decode(previous), 0o644 if name == 'proxy' else 0o600)
    (ETC / 'gateway.json').unlink(missing_ok=True)
    subprocess.run(['nft', 'delete', 'table', 'inet', TABLE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run('/usr/sbin/qsm-pve-direct-ui', 'reconcile')
    run('systemctl', 'restart', 'pveproxy')
    if saved['signal_active']:
        run('systemctl', 'restart', 'qsm-pve-direct-signal')
    else:
        run('systemctl', 'stop', 'qsm-pve-direct-signal')
    run('systemctl', 'enable' if saved['signal_enabled'] else 'disable', 'qsm-pve-direct-signal')
    (STATE / 'before.json').unlink()


def enable(address):
    if (STATE / 'before.json').exists():
        raise ValueError('gateway state already exists; use status or disable first')
    ui = Path('/usr/share/pve-manager/js/pvemanagerlib.js')
    if ui.exists() and b'openWebRTCViewer' in ui.read_bytes():
        raise ValueError('automatic gateway setup supports the standalone stock-PVE UI, not the native patched viewer')
    if len(list(Path('/etc/pve/nodes').iterdir())) != 1:
        raise ValueError('automatic setup supports a single-node PVE installation only')
    for binary in ('haproxy', 'turnserver', 'nft'):
        if shutil.which(binary) is None:
            raise ValueError(f'missing {binary}; see README.gateway for installation without enabling generic services')
    address = str(ipaddress.IPv4Address(address))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as bound:
        bound.bind((address, 0))  # Must be an address assigned to this node.
    if ipaddress.ip_address(address).is_loopback:
        raise ValueError('use a non-loopback node IPv4 address')
    original = PVE.read_bytes() if PVE.exists() else b''
    updated = proxy_config(original)
    for port in (18006, 13479, 13480):
        with socket.socket() as test:
            test.bind(('127.0.0.1', port))
    if subprocess.run(['nft', 'list', 'table', 'inet', TABLE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise ValueError('gateway firewall table already exists; refusing to replace it')
    account = pwd.getpwnam('qsm-turn')
    STATE.mkdir(mode=0o700, exist_ok=True)
    secret = secrets.token_hex(32)
    atomic(STATE / 'before.json', json.dumps(snapshot()).encode())
    try:
        run('systemctl', 'stop', 'qsm-pve-direct-signal')
        with socket.socket() as test:
            test.bind(('127.0.0.1', 18007))
        atomic(ETC / 'gateway.json', json.dumps({'secret': secret, 'relay_ipv4': address}).encode())
        atomic(ETC / 'signal.json', json.dumps({'listen': '127.0.0.1', 'port': 18007, 'gateway': True}).encode())
        atomic(STATE / 'haproxy.cfg', haproxy_config(18007, address).encode())
        atomic(STATE / 'turn.conf', turn_config(address, secret).encode(), 0o640)
        os.chown(STATE / 'turn.conf', 0, account.pw_gid)
        # Only the dedicated TURN user can read its config; no other file in
        # this root state directory contains a publicly accessible secret.
        STATE.chmod(0o750)
        os.chown(STATE, 0, account.pw_gid)
        certificates()
        run('haproxy', '-c', '-f', str(STATE / 'haproxy.cfg'), stdout=subprocess.DEVNULL)
        run('nft', '-f', '-', input=firewall(account.pw_uid).encode())
        atomic(PVE, updated, 0o644)
        run('/usr/sbin/qsm-pve-direct-ui', 'reconcile')
        run('systemctl', 'restart', 'pveproxy')
        run('systemctl', 'restart', 'qsm-pve-direct-signal')
        for unit in reversed(UNITS):
            run('systemctl', 'enable', '--now', unit)
        run('systemctl', 'enable', '--now', 'qsm-pve-direct-gateway-certs.timer')
        print('Gateway enabled on TCP 8006. Reload the PVE web page. UDP ingress is not required.')
    except BaseException:
        restore()
        raise


def prepare():
    # Reboot entry point: create an empty fail-closed allowlist, then peers
    # refresh it. Never flush a global table or add any ingress ACCEPT rule.
    certificates()
    if subprocess.run(['nft', 'list', 'table', 'inet', TABLE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run('nft', '-f', '-', input=firewall(pwd.getpwnam('qsm-turn').pw_uid).encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('enable', 'disable', 'status', 'prepare', 'certificates'))
    parser.add_argument('--relay-ip')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    if args.action == 'prepare':
        prepare()
        return 0
    Path('/run/lock').mkdir(exist_ok=True)
    with open('/run/lock/qsm-direct-gateway.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'enable':
            if not args.relay_ip:
                parser.error('enable needs --relay-ip <node IPv4>')
            enable(args.relay_ip)
        elif args.action == 'disable':
            restore()
        elif args.action == 'prepare':
            prepare()
        elif args.action == 'certificates':
            if (ETC / 'gateway.json').exists():
                previous = (RUN / 'proxy.pem').read_bytes() if (RUN / 'proxy.pem').exists() else b''
                certificates()
                if previous != (RUN / 'proxy.pem').read_bytes():
                    run('systemctl', 'reload', 'qsm-pve-direct-gateway')
        else:
            print('enabled' if (ETC / 'gateway.json').exists() else 'disabled')
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'qsm gateway: {error}', file=sys.stderr)
        sys.exit(1)
