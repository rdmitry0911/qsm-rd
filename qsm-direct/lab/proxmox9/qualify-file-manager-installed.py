#!/usr/bin/env python3
"""The file manager on what is installed on a node, through a real desktop login.

Nothing is staged. The browser talks to the node's own pveproxy, console script, signal and
terminal services over the way a user would, with no route override and no tunnel. The
helper in the container is whatever the session started: a throwaway account logs in
through the SDDM greeter in the console picture, with mouse and keyboard, and the session
brings qsm_desktop_files.py up by itself. The screen lock is the session's real locker.

The account's password is generated here, kept in memory and only ever crosses stdin of
chpasswd and the console's keyboard. The account, its session and its files are removed at
the end, also after a failure. No service on the node or in the container is restarted.

Run it holding the lane's lock:
  flock /tmp/qsm-rdp-hybrid-20261028/lane-pve-216.lock \\
    uv run --offline --no-project --python /usr/bin/python3 --with playwright python \\
    qualify-file-manager-installed.py OUTPUT_DIR [--browser chromium|firefox|both]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import string
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit

CHROME = '/tmp/qsm-rdp-hybrid-20261028/chrome-stand/root/opt/google/chrome/chrome'
FIREFOX = str(Path.home() / '.cache/ms-playwright/firefox-1555/firefox/firefox')
MIB = 1024 * 1024
SERVICES = {'qsm-pve-direct-terminal': '/usr/bin/qsm-pve-direct-terminal',
            'qsm-pve-direct-signal': '/usr/bin/qsm-pve-direct-signal',
            'pveproxy': '/usr/bin/pveproxy'}
NODE_FILES = ('/usr/share/pve-manager/js/qsm-pve-direct-console.js',
              '/usr/lib/qsm-pve-direct/browser_bridge/qsm_file_peer.py',
              '/usr/lib/qsm-pve-direct/direct_terminal/qsm_direct_terminal.py',
              '/usr/lib/qsm-pve-direct/direct_terminal/qsm_file_session.py',
              '/usr/lib/qsm-pve-direct/signal_service/qsm_direct_signal.py')
GUEST_FILES = ('/usr/lib/qsm-console/qsm_desktop_files.py', '/usr/lib/qsm-console/qsm-session-launch',
               '/usr/lib/qsm-console/qsm-native-seat')
LOCKED_TEXT = 'The guest screen is locked'
TICKET = ("perl -MPVE::AccessControl -MPVE::Cluster -MJSON -e 'PVE::Cluster::cfs_update(); "
          "print encode_json({ticket=>scalar PVE::AccessControl::assemble_ticket(q{root@pam})})'")
OPFS_PICKER = """
window.showDirectoryPicker = async () => navigator.storage.getDirectory();
window.opfs = {
    async dir(parts, create = true) {
        let handle = await navigator.storage.getDirectory();
        for (const part of parts) { handle = await handle.getDirectoryHandle(part, { create }); }
        return handle;
    },
    async write(path, size, seed) {
        const parts = path.split('/');
        const dir = await this.dir(parts.slice(0, -1));
        const writer = await (await dir.getFileHandle(parts[parts.length - 1], { create: true })).createWritable();
        let state = seed >>> 0 || 1;
        const block = new Uint8Array(Math.min(1 << 20, Math.max(size, 1)));
        for (let done = 0; done < size;) {
            const length = Math.min(block.length, size - done);
            for (let i = 0; i < length; i += 4) {
                state ^= state << 13; state ^= state >>> 17; state ^= state << 5; state >>>= 0;
                block[i] = state; block[i + 1] = state >>> 8; block[i + 2] = state >>> 16; block[i + 3] = state >>> 24;
            }
            await writer.write(block.subarray(0, length));
            done += length;
        }
        await writer.close();
    },
    async hash(path) {
        const parts = path.split('/');
        const dir = await this.dir(parts.slice(0, -1), false);
        const file = await (await dir.getFileHandle(parts[parts.length - 1])).getFile();
        const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
        return { size: file.size, sha256: [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('') };
    },
    async clear() {
        const root = await navigator.storage.getDirectory();
        for await (const [name] of root.entries()) { await root.removeEntry(name, { recursive: true }); }
    },
};
"""
RECORD_PEERS = """window.qsmPeers = [];
const NativePeer = window.RTCPeerConnection;
window.RTCPeerConnection = class extends NativePeer { constructor(...args) { super(...args); window.qsmPeers.push(this); } };
window.qsmSockets = [];
const NativeSocket = window.WebSocket;
window.WebSocket = class extends NativeSocket { constructor(...args) { super(...args); window.qsmSockets.push(this); } };"""
FILE_SOCKETS = """() => (window.qsmSockets || []).filter((item) => item.url.includes('qsm-direct-files'))
    .map((item) => ({ url: item.url.replace(/[0-9a-f]{32}$/, '<token>'), state: item.readyState }))"""
# Types, protocols and URLs only. No ufrag, no credential.
PEER_STATS = """async () => {
    const out = [];
    for (const peer of window.qsmPeers || []) {
        if (peer.connectionState === 'closed') { continue; }
        const stats = [...(await peer.getStats()).values()];
        const pair = stats.find((item) => item.type === 'candidate-pair' && (item.selected || (item.nominated && item.state === 'succeeded')));
        const local = pair && stats.find((item) => item.id === pair.localCandidateId);
        const remote = pair && stats.find((item) => item.id === pair.remoteCandidateId);
        const config = peer.getConfiguration();
        out.push({ state: peer.connectionState, policy: config.iceTransportPolicy,
            urls: (config.iceServers || []).flatMap((server) => [].concat(server.urls || [])),
            local_type: local && local.candidateType, relay_protocol: local && local.relayProtocol, local_protocol: local && local.protocol,
            turn_url: local && local.url, remote_type: remote && remote.candidateType,
            labels: stats.filter((item) => item.type === 'data-channel').map((item) => item.label),
            video: stats.filter((item) => item.type === 'inbound-rtp' && (item.kind === 'video' || item.mediaType === 'video')).map((item) => ({ frames: item.framesDecoded, decoder: item.decoderImplementation })),
            bytes_sent: pair && pair.bytesSent, bytes_received: pair && pair.bytesReceived });
    }
    return out;
}"""
# What the session's helper itself answers, over the socket the terminal uses. Read only.
HELPER_HELLO = r"""
import base64, json, socket, struct, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(10)
s.connect(sys.argv[1])
f = s.makefile('rwb')
greeting = f.readline().strip().decode()
sid = 'c0de'
f.write(('FS %s O r\n' % sid).encode()); f.flush()
opened = f.readline().strip().decode()
header = json.dumps({'op': 'hello', 'id': 1}).encode()
frame = struct.pack('>H', len(header)) + header
f.write(('FS %s D %s\n' % (sid, base64.b64encode(frame).decode())).encode()); f.flush()
line = f.readline().split()
raw = base64.b64decode(line[3])
length = struct.unpack('>H', raw[:2])[0]
reply = json.loads(raw[2:2 + length])
f.write(('FS %s C\n' % sid).encode()); f.flush()
print(json.dumps({'greeting': greeting, 'opened': opened, 'hello': reply}))
"""
SECRETS: set[str] = set()


def scrub(text: object) -> str:
    out = str(text)
    for secret in SECRETS:
        out = out.replace(secret, '[hidden]')
    return out


def say(text: object) -> None:
    print(scrub(text), flush=True)


class Checks:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def add(self, name: str, ok: bool, detail: object = None) -> None:
        detail = json.loads(scrub(json.dumps(detail, ensure_ascii=False, default=str)))
        self.results.append({'check': name, 'ok': bool(ok), 'detail': detail})
        suffix = '' if detail is None else ' ' + json.dumps(detail, ensure_ascii=False)[:900]
        say(f"{'ok  ' if ok else 'FAIL'} {name}{suffix}")


class Node:
    def __init__(self, host: str, vmid: int, account: str) -> None:
        self.host, self.vmid, self.account = host, vmid, account
        self.uid: int | None = None
        self.strays: set[str] = set()
        self.clip_found = ''
        self.started = 0

    def ssh(self, command: str, *, check: bool = True, timeout: float = 120, stdin: str | None = None) -> str:
        return subprocess.run(['ssh', '-o', 'BatchMode=yes', self.host, command], check=check, capture_output=True,
                              text=True, timeout=timeout, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL).stdout.strip()

    def guest(self, command: str, *, check: bool = True, timeout: float = 120, stdin: str | None = None) -> str:
        return self.ssh(f'pct exec {self.vmid} -- sh -c {shlex.quote(command)}', check=check, timeout=timeout, stdin=stdin)

    def as_account(self, command: str, *, check: bool = True, timeout: float = 120) -> str:
        environment = (f'HOME=/home/{self.account} XDG_RUNTIME_DIR=/run/user/{self.uid} '
                       f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{self.uid}/bus')
        return self.guest(f'runuser -u {self.account} -- env {environment} sh -c {shlex.quote(command)}', check=check, timeout=timeout)

    # ---- what must not move
    def production(self) -> dict:
        pids = {name: self.ssh(f'systemctl show -p MainPID --value {name}') for name in SERVICES}
        hashes = self.ssh('sha256sum ' + ' '.join([*SERVICES.values(), *NODE_FILES]))
        guest_hashes = self.guest('sha256sum ' + ' '.join(GUEST_FILES))
        fixtures = self.guest(r"pgrep -a -x sway; pgrep -a -x sddm; pgrep -a -x kwin_wayland; pgrep -af 'qsm-rdp-hybrid|qsm-seat-keeper|qsm-native-seat control' | grep -v pgrep", check=False)
        return {'main_pids': pids, 'sha256': hashes.splitlines(), 'guest_sha256': guest_hashes.splitlines(),
                'fixture_processes': sorted(line for line in fixtures.splitlines() if self.account not in line),
                'seat2_sessions': self.guest('loginctl show-seat seatqsm2 -p Sessions --value', check=False),
                'display2_windows': self.display2_windows()}

    def display2_windows(self) -> list:
        """Read only: which windows the fixture's display holds, and which one is on top."""
        text = self.guest('runuser -u qsm-display -- /opt/qsm-native-sway/bin/swaymsg -s "$(ls /run/qsm-display-2/sway-ipc.*.sock)" -t get_tree', check=False)
        found: list = []

        def walk(item: dict) -> None:
            if item.get('type') in ('con', 'floating_con') and item.get('pid'):
                found.append({'window': item.get('app_id') or (item.get('name') or '')[:40], 'pid': item['pid'], 'visible': item.get('visible'),
                              'focused': item.get('focused'), 'fullscreen': item.get('fullscreen_mode')})
            for child in item.get('nodes', []) + item.get('floating_nodes', []):
                walk(child)
        try:
            walk(json.loads(text))
        except ValueError:
            return [text[:200]]
        return found

    def slots(self) -> list[dict]:
        """The terminal's own record of this container's consoles. Read only."""
        try:
            entries = json.loads(self.ssh('cat /run/qsm-pve-direct-terminal/sessions/lxc-slots.json', check=False))
        except ValueError:
            return []
        return [entry for entry in entries if isinstance(entry, dict) and entry.get('vmid') == self.vmid]

    def greeter_ready(self, slot: int) -> bool:
        if self.guest(f'test -S /run/qsm-native/greeter-seatqsm{slot}/wayland-0 && echo yes', check=False) != 'yes':
            return False
        for identifier in self.guest(f'loginctl show-seat seatqsm{slot} -p Sessions --value', check=False).split():
            text = self.guest(f'loginctl show-session {identifier} -p Service -p State', check=False)
            if 'Service=qsm-sddm-greeter' in text and 'State=active' in text:
                return True
        return False

    def packages(self) -> dict:
        query = "dpkg-query -W -f '${Package} ${Version}\\n' 'qsm-*'"
        return {'node': self.ssh(query).splitlines(), 'guest': self.guest(query).splitlines()}

    def journal(self, unit: str = 'qsm-pve-direct-terminal') -> list[str]:
        return self.ssh(f"journalctl -u {unit} --since @{self.started} --no-pager -o cat", check=False, timeout=60).splitlines()

    # ---- the throwaway account
    def create_account(self) -> str:
        if self.guest(f'getent passwd {self.account}', check=False):
            self.remove_account()
        self.guest(f'useradd -m -s /bin/bash {self.account}')
        self.uid = int(self.guest(f'id -u {self.account}'))
        self.strays = set(self.find_strays())
        # A login replaces this directory, and qualify-file-manager-pve.py expects the one it found.
        self.clip_found = self.guest('stat -c "%U %G %a" /run/qsm-login/clip-1 2>/dev/null', check=False)
        # Letters and digits only: the keyboard path is what is under test, not a layout.
        password = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))
        SECRETS.add(password)
        self.guest('exec chpasswd', stdin=f'{self.account}:{password}\n')
        status = self.guest(f'passwd -S {self.account}').split()
        if len(status) < 2 or status[1] != 'P':
            raise RuntimeError(f'the account did not get a password: status {status[1:2]}')
        return password

    def find_strays(self) -> list[str]:
        if self.uid is None:
            return []
        return self.guest(f'find /tmp /var/tmp /dev/shm /run /var/lib/AccountsService /var/mail /var/spool -xdev -uid {self.uid} 2>/dev/null', check=False, timeout=60).splitlines()

    def end_session(self) -> None:
        """Only this account's processes. Its PAM close must run before the account goes."""
        self.guest(f'getent passwd {self.account} >/dev/null && loginctl terminate-user {self.account}', check=False)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            left = self.guest(f'loginctl list-sessions --no-legend | grep -w {self.account}; '
                              f'pgrep -u {self.account} | head -3', check=False)
            records = self.guest(f'grep -lw "{self.uid}" /run/qsm-native/*/*.json 2>/dev/null', check=False) if self.uid else ''
            if not left and not records:
                return
            time.sleep(1)
        say(f'the session did not end in time: {left} {records}')

    def remove_account(self) -> dict:
        if self.uid is None:
            value = self.guest(f'id -u {self.account}', check=False)
            self.uid = int(value) if value.isdigit() else None
        self.end_session()
        result = {'userdel': self.guest(f'userdel -r {self.account} 2>&1; echo rc=$?', check=False)}
        if self.uid is not None:
            strays = [path for path in self.find_strays() if path not in self.strays]
            # Files of the session that outlived it, by the uid only this run used.
            for path in strays:
                self.guest(f'[ "$(stat -c %u {shlex.quote(path)} 2>/dev/null)" = "{self.uid}" ] && rm -rf {shlex.quote(path)}', check=False)
            result['session_leftovers_removed'] = strays
            result['still_owned_by_uid'] = [path for path in self.find_strays() if path not in self.strays]
        if self.clip_found:
            owner, group, mode = self.clip_found.split()
            self.guest(f'[ -e /run/qsm-login/clip-1 ] || install -d -m {mode} -o {owner} -g {group} /run/qsm-login/clip-1', check=False)
            result['clip_1_restored_as'] = self.guest('stat -c "%U %G %a" /run/qsm-login/clip-1 2>&1', check=False)
        # accounts-daemon's record of a login is root's and is named after the account.
        result['accounts_service_record'] = self.guest(f'ls /var/lib/AccountsService/users/{self.account} /var/lib/AccountsService/icons/{self.account} 2>/dev/null', check=False)
        self.guest(f'rm -f /var/lib/AccountsService/users/{self.account} /var/lib/AccountsService/icons/{self.account}', check=False)
        result['passwd'] = self.guest(f'getent passwd {self.account}', check=False)
        result['home'] = self.guest(f'ls -d /home/{self.account} 2>/dev/null', check=False)
        result['processes'] = self.guest(f'[ -n "{self.uid}" ] && pgrep -U {self.uid} | wc -l', check=False)
        return result

    # ---- the session, read from inside
    def desktop_session(self) -> dict | None:
        for line in self.guest('loginctl list-sessions --no-legend', check=False).splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[2] == self.account:
                text = self.guest(f'loginctl show-session {fields[0]} -p Id -p Name -p Seat -p Service -p Class -p Type -p State -p Leader -p LockedHint -p Scope', check=False)
                data = dict(item.split('=', 1) for item in text.splitlines() if '=' in item)
                if data.get('State') != 'closing':
                    return data
        return None

    def helper_evidence(self, slot: int, session: dict) -> dict:
        socket_path = f'/run/qsm-login/clip-{slot}/files.sock'
        out: dict = {'socket': self.guest(f'stat -c "%F %U %a %n" {socket_path} 2>&1', check=False)}
        pid = self.guest(f'pgrep -u {self.account} -f "/usr/lib/qsm-console/[q]sm_desktop_files.py" | head -1', check=False)
        out['pid'] = pid
        if not pid:
            out['all_of_account'] = self.guest(f'ps -u {self.account} -o pid=,ppid=,args= | cut -c1-130 | head -40', check=False)
            return out
        out['command'] = self.guest(f'tr "\\0" " " < /proc/{pid}/cmdline')
        out['user'] = self.guest(f'ps -o user= -p {pid}')
        out['cgroup'] = self.guest(f'cat /proc/{pid}/cgroup')
        out['audit_session'] = self.guest(f'cat /proc/{pid}/sessionid', check=False)
        out['chain'] = self.guest(f'p={pid}; while [ "$p" -gt 1 ]; do ps -o user=,pid=,ppid=,args= -p $p | cut -c1-150; p=$(ps -o ppid= -p $p | tr -d " "); done').splitlines()
        parent = self.guest(f'ps -o ppid= -p {pid}').strip()
        out['siblings'] = self.guest(f'ps --ppid {parent} -o pid=,args= | cut -c1-110', check=False).splitlines()
        return out

    def helper_hello(self, slot: int) -> dict:
        code = shlex.quote(HELPER_HELLO)
        text = self.as_account(f'python3 -c {code} /run/qsm-login/clip-{slot}/files.sock', check=False, timeout=30)
        try:
            return json.loads(text)
        except ValueError:
            return {'unreadable': text[:300]}

    def lock_state(self, session_id: str) -> dict:
        hint = self.guest(f'loginctl show-session {session_id} -p LockedHint --value', check=False)
        active = self.as_account('busctl --user --timeout=2 --auto-start=no call org.freedesktop.ScreenSaver /ScreenSaver org.freedesktop.ScreenSaver GetActive 2>&1', check=False)
        return {'LockedHint': hint, 'ScreenSaver.GetActive': active,
                'locker_processes': self.guest(f'pgrep -u {self.account} -a "kscreenlocker|plasmashell|ksmserver" | cut -c1-90', check=False).splitlines()}


# ---- the page
# Where the greeter draws its controls in a 1280x800 console, read from its screenshots.
GREETER_EMPTY = (700, 600)
GREETER_OTHER = (788, 640)
GREETER_USERNAME = (640, 476)
GREETER_PASSWORD = (620, 515)
FILES_BUTTON = 'Files: copy between this computer and the guest'
JOB_ENDED = re.compile(r' — (Done|Cancelled|\d+ failed) ')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(MIB):
            digest.update(chunk)
    return digest.hexdigest()


async def poll(check, timeout: float, interval: float = 1.0):
    """The first true answer of check, or its last answer after the timeout."""
    deadline = time.monotonic() + timeout
    while True:
        value = await asyncio.to_thread(check)
        if value or time.monotonic() > deadline:
            return value
        await asyncio.sleep(interval)


class Run:
    def __init__(self, args, node: Node, checks: Checks, report: dict, password: str) -> None:
        self.args, self.node, self.checks, self.report = args, node, checks, report
        self.password = password
        self.out: Path = args.output
        self.host = node.host.split('@')[-1]

    async def shot(self, page, name: str) -> None:
        try:
            await page.screenshot(path=str(self.out / f'{name}.png'), timeout=20000)
        except Exception as error:  # noqa: BLE001 - a picture is evidence, not a condition
            say(f'no screenshot {name}: {type(error).__name__}')

    def add_observers(self, context, tag: str) -> dict:
        seen: dict = {'signal': [], 'script': {}, 'errors': []}

        async def on_response(response) -> None:
            path = urlsplit(response.url).path
            if '/qsm-direct' in path or 'qsm-direct' in path.rsplit('/', 1)[-1]:
                seen['signal'].append((path.rsplit('/', 1)[-1], response.status))
            if path.endswith('qsm-pve-direct-console.js'):
                try:
                    seen['script'][path] = hashlib.sha256(await response.body()).hexdigest()
                except Exception as error:  # noqa: BLE001
                    seen['script'][path] = f'unreadable: {type(error).__name__}'

        def on_page(item) -> None:
            item.on('pageerror', lambda error: seen['errors'].append(f'{error.name}: {error.message}'[:300]))
            item.on('console', lambda message: seen['errors'].append(message.text[:300])
                    if message.type == 'error' and 'Failed to load' not in message.text else None)
        context.on('response', on_response)
        context.on('page', on_page)
        self.report[f'{tag}_observed'] = seen
        return seen

    async def open_console(self, context, tag: str, picture_timeout: float = 60):
        """Node's own login page, resource tree and console button. Nothing is routed."""
        await self.console_may_open()
        ticket = json.loads(self.node.ssh(TICKET))['ticket']
        SECRETS.add(ticket)
        page = await context.new_page()
        await page.goto(f'https://{self.host}:8006/', wait_until='domcontentloaded')
        await page.locator('input[name=username]').fill('root')
        await page.locator('input[name=password]').fill(ticket)
        await page.evaluate("void Ext.ComponentQuery.query('field[name=realm]')[0].setValue('pam')")
        await page.get_by_role('button', name='Login', exact=True).click()
        await page.wait_for_function("window.Ext&&Ext.ComponentQuery.query('pveResourceTree')[0]", timeout=30000)
        notice = page.locator('.x-message-box:visible')
        if await notice.count():
            ok = notice.locator('.x-btn').filter(has_text='OK')
            if await ok.count():
                await ok.last.click()
        vmid = self.node.vmid
        await page.wait_for_function(f"Ext.ComponentQuery.query('pveResourceTree')[0].getStore().getRootNode().findChild('id','lxc/{vmid}',true)", timeout=30000)
        await page.evaluate(f"void Ext.ComponentQuery.query('pveResourceTree')[0].selectById('lxc/{vmid}')")
        await page.wait_for_function("Ext.ComponentQuery.query('[itemId=qsm-direct-console-button]').some(b=>b.down('#qsm-direct')&&!b.down('#qsm-direct').disabled)", timeout=30000)
        async with page.expect_popup(timeout=30000) as popup:
            await page.evaluate("(()=>{const b=Ext.ComponentQuery.query('[itemId=qsm-direct-console-button]').find(b=>b.consoleType==='lxc');b.down('#qsm-direct').handler()})()")
        console = await popup.value
        picture = True
        try:
            await console.wait_for_function("document.querySelector('video')?.videoWidth>0", timeout=picture_timeout * 1000)
        except Exception:  # noqa: BLE001 - what the page then shows is the finding
            picture = False
        return page, console, picture

    def console_slot(self) -> tuple[int, str]:
        def find():
            lines = [line for line in self.node.journal() if re.search(r'(opens|resumes) console \d+ of container', line)]
            return lines[-1] if lines else ''
        deadline = time.monotonic() + 10
        line = find()
        while not line and time.monotonic() < deadline:
            time.sleep(.5)
            line = find()
        match = re.search(r'console (\d+) of container', line)
        if not match:
            raise RuntimeError('the terminal did not say which console this is')
        return int(match[1]), line

    async def click_files(self, console) -> None:
        button = console.get_by_role('button', name=FILES_BUTTON)
        handle = console.locator('button[aria-controls="qsm-direct-toolbar"]')
        for _ in range(150):
            # A folded toolbar is moved off the page, where it still counts as visible.
            box = await button.bounding_box() if await button.count() and await button.is_visible() else None
            if box and box['x'] >= 0 and box['y'] >= 0:
                break
            if await handle.count() and await handle.get_attribute('aria-expanded') == 'false':
                await handle.click()
            await asyncio.sleep(.1)
        await button.click(timeout=10000)

    async def console_may_open(self) -> None:
        """A new console takes the first free one, and console 2 carries this container's fixture.

        Opening it would do harm even without a login: leaving a login screen resets its
        greeter, and the new greeter covers the fixture's window. A closed browser holds its
        console until ICE fails, about 30 s, so wait for that. Console 1 must be either this
        user's own left desktop or a free, ready login screen, or nothing is opened at all.
        """
        node = self.node

        def settled() -> bool:
            entries = node.slots()
            if any(entry.get('slot') != 1 or entry.get('owner') != 'root@pam' for entry in entries):
                raise RuntimeError(f'another console of this container is in use: {[(e.get("slot"), e.get("owner")) for e in entries]}')
            if entries:
                return entries[0].get('detached_wall') is not None
            return node.greeter_ready(1) and not node.desktop_session()
        if not await poll(settled, 75):
            raise RuntimeError(f'console 1 did not become free, not opening a console that would land on console 2: {node.slots()}')

    async def wait_greeter(self, slot: int) -> bool:
        return bool(await poll(lambda: self.node.greeter_ready(slot), 60))

    async def greeter_login(self, console, tag: str) -> dict:
        """As a user would: click, Other..., name, password, Enter, all into the picture."""
        await self.shot(console, f'{tag}-1-greeter')
        await console.mouse.click(*GREETER_EMPTY)
        await asyncio.sleep(1.5)
        await console.mouse.click(*GREETER_OTHER)
        await asyncio.sleep(1.5)
        await self.shot(console, f'{tag}-2-other')
        await console.mouse.click(*GREETER_USERNAME)
        await asyncio.sleep(.3)
        await console.keyboard.type(self.node.account, delay=60)
        await console.mouse.click(*GREETER_PASSWORD)
        await asyncio.sleep(.3)
        await console.keyboard.type(self.password, delay=60)
        await asyncio.sleep(.6)
        await self.shot(console, f'{tag}-3-typed')
        await console.keyboard.press('Enter')
        session = await poll(self.node.desktop_session, 60)
        await self.shot(console, f'{tag}-4-after-enter')
        if not session:
            raise RuntimeError('the login through the picture brought no session')
        return session

    async def verify_session(self, console, session: dict, slot: int, tag: str) -> None:
        checks, node = self.checks, self.node
        sid = session.get('Id', '')
        checks.add(f'{tag}: a real session of the account on the console seat',
                   session.get('Name') == node.account and session.get('Seat') == f'seatqsm{slot}' and session.get('Class') == 'user'
                   and session.get('Service') in ('sddm', 'qsm-sddm') and session.get('Type') == 'wayland', session)
        socket_path = f'/run/qsm-login/clip-{slot}/files.sock'
        appeared = await poll(lambda: node.guest(f'test -S {socket_path} && echo yes', check=False) == 'yes', 60)
        checks.add(f'{tag}: files.sock exists after the login', bool(appeared), socket_path)
        evidence = await asyncio.to_thread(node.helper_evidence, slot, session)
        self.report[f'{tag}_helper'] = evidence
        chain = evidence.get('chain', [])
        chain_text = '\n'.join(chain[1:])
        command = evidence.get('command', '')
        checks.add(f'{tag}: the helper runs as the account, on the socket of this session',
                   evidence.get('user') == node.account and f'--listen {socket_path}' in command, {'user': evidence.get('user'), 'command': command, 'pid': evidence.get('pid')})
        # qsm-session-launch ends in `exec startplasma-wayland`, so its pid shows that name now.
        checks.add(f'{tag}: its parents go back to the session, nobody started it by hand',
                   f'session-{sid}.scope' in evidence.get('cgroup', '') and ('startplasma-wayland' in chain_text or 'qsm-session-launch' in chain_text)
                   and 'sddm-helper' in chain_text and any('qsm-clipboard-agent' in line for line in evidence.get('siblings', [])),
                   {'chain': chain, 'cgroup': evidence.get('cgroup'), 'siblings': evidence.get('siblings')})
        shell = await poll(lambda: node.guest(f'pgrep -u {node.account} -x plasmashell', check=False), 60)
        self.report[f'{tag}_plasmashell'] = bool(shell)
        await asyncio.sleep(6)
        await self.shot(console, f'{tag}-5-desktop')

    # ---- panes
    async def row(self, console, side: str, name: str):
        return console.locator(f'.qfm-{side} .qfm-row').filter(has=console.locator('.qfm-name', has_text=re.compile('^' + re.escape(name) + '$'))).first

    async def names(self, console, side: str) -> list[str]:
        return await console.evaluate("(side) => [...document.querySelectorAll(`.qfm-${side} .qfm-name`)].map((e) => e.textContent)", side)

    async def wait_name(self, console, side: str, name: str, timeout: float = 30, present: bool = True) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (name in await self.names(console, side)) == present:
                return True
            await asyncio.sleep(.1)
        return False

    async def refresh_guest(self, console) -> None:
        await console.locator('.qfm-guest').get_by_role('button', name='Refresh').click()

    async def select(self, console, side: str, name: str) -> None:
        await (await self.row(console, side, name)).click()

    async def ask_name(self, console, button: str, name: str) -> None:
        await console.locator('.qfm-guest').get_by_role('button', name=button).click()
        await console.locator('.qfm-dialog input[type=text]').fill(name)
        await console.locator('.qfm-dialog button', has_text='OK').click()

    async def job_end(self, console, timeout: float) -> dict:
        started = time.monotonic()
        items: list = []
        while time.monotonic() - started < timeout:
            items = await console.evaluate("""() => [...document.querySelectorAll('.qfm-job')].map((row) => ({
                text: row.querySelector('.qfm-job-text')?.textContent, errors: row.querySelector('.qfm-job-errors')?.textContent }))""")
            if items and all(JOB_ENDED.search(item['text'] or '') for item in items):
                return {'done': all(' — Done ' in item['text'] for item in items), 'seconds': round(time.monotonic() - started, 1), 'jobs': items}
            await asyncio.sleep(.2)
        return {'done': False, 'seconds': round(time.monotonic() - started, 1), 'jobs': items, 'timed_out': True}

    async def clear_jobs(self, console) -> None:
        buttons = console.locator('.qfm-job button', has_text='Remove')
        for _ in range(await buttons.count()):
            await buttons.first.click()

    async def report_text(self, console) -> str:
        bar = console.locator('.qfm-report')
        if await bar.count() and await bar.is_visible():
            return await console.locator('.qfm-report span').inner_text()
        return await console.evaluate("document.querySelector('.qfm-guest .qfm-message')?.textContent || ''")

    def part_files(self) -> list[str]:
        return self.node.as_account("find ~ -xdev -name '.*qsm-part-*' 2>/dev/null", check=False).splitlines()

    async def sampling_parts(self, seen: set[str], stop: asyncio.Event) -> None:
        while not stop.is_set():
            seen.update(await asyncio.to_thread(self.part_files))
            await asyncio.sleep(.5)

    async def copy_up(self, console, name: str) -> dict:
        """Start a copy from the local pane and watch the guest's temporary file meanwhile."""
        seen: set[str] = set()
        stop = asyncio.Event()
        sampler = asyncio.ensure_future(self.sampling_parts(seen, stop))
        await self.select(console, 'local', name)
        await console.get_by_role('button', name='Copy to guest →').click()
        result = await self.job_end(console, self.args.transfer_timeout)
        stop.set()
        await sampler
        result['temporary_seen'] = sorted(path.rsplit('/', 1)[-1] for path in seen)
        result['temporary_after'] = await asyncio.to_thread(self.part_files)
        return result

    async def wait_home(self, console, tag: str, marker: str) -> None:
        home = f'/home/{self.node.account}'
        location = console.locator('.qfm-guest .qfm-location')

        async def filled() -> bool:
            return await location.count() > 0 and await location.inner_text() == home and marker in await self.names(console, 'guest')
        for attempt in range(3):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not await filled():
                await asyncio.sleep(.2)
            if await filled():
                break
            if attempt < 2:
                await self.refresh_guest(console)
        ok = await filled()
        state = {'location': await location.inner_text() if await location.count() else None, 'rows': (await self.names(console, 'guest'))[:12],
                 'message': await self.report_text(console), 'on_disk': self.node.as_account('ls ~ | head -12', check=False).splitlines()}
        self.checks.add(f'{tag}: the guest pane shows the account\'s home', ok, state)
        if not ok:
            await self.shot(console, f'{tag}-guest-pane-failed')
            raise RuntimeError('the guest pane did not fill')

    async def relay_check(self, page, tag: str) -> None:
        peers = await page.evaluate(PEER_STATS)
        controller = self.node.ssh("ss -Htn state established '( sport = :8006 )' | awk '{print $4}' | cut -d: -f1 | sort | uniq -c", check=False).splitlines()
        self.report[f'{tag}_relay'] = {'peers': peers, 'established_on_8006_by_address': controller}
        sockets = await page.evaluate(FILE_SOCKETS)
        self.report[f'{tag}_relay']['file_sockets'] = sockets
        console_peers = [peer for peer in peers if peer.get('state') == 'connected']

        def through_8006(peer: dict) -> bool:
            return (peer.get('local_type') == 'relay' and peer.get('relay_protocol') == 'tcp' and peer.get('policy') == 'relay'
                    and any(url.endswith(':8006?transport=tcp') for url in peer.get('urls', [])))
        self.checks.add(f'{tag}: the console is relayed on TCP 8006', len(console_peers) == 1 and through_8006(console_peers[0]),
                        [{key: peer.get(key) for key in ('state', 'policy', 'urls', 'local_type', 'relay_protocol', 'turn_url', 'bytes_sent', 'bytes_received')} for peer in peers])
        self.checks.add(f'{tag}: files have their own WebSocket on TCP 8006',
                        any(item['state'] == 1 and ':8006/qsm-direct/qsm-direct-files/socket/' in item['url'] for item in sockets), sockets)
        users = self.node.ssh("pgrep -f '/[q]sm_file_peer' | xargs -r ps -o user= -p | sort | uniq -c", check=False)
        self.checks.add(f'{tag}: the file peer runs unprivileged', bool(users) and 'root' not in users and 'nobody' in users, users)

    async def folder_steps(self, console, tag: str) -> None:
        """New folder, rename and delete in the guest, through the buttons."""
        node, checks = self.node, self.checks
        first, second = f'fm-{tag}-new', f'fm-{tag}-renamed'
        await self.ask_name(console, 'New folder (F7)', first)
        shown = await self.wait_name(console, 'guest', first)
        disk = node.as_account(f'stat -c "%F %U" ~/{first} 2>&1', check=False)
        checks.add(f'{tag}: new folder', shown and disk == f'directory {node.account}', disk)
        await self.select(console, 'guest', first)
        await self.ask_name(console, 'Rename (F2)', second)
        shown = await self.wait_name(console, 'guest', second)
        disk = node.as_account(f'ls -d ~/{first} ~/{second} 2>/dev/null', check=False)
        checks.add(f'{tag}: rename', shown and disk.endswith(f'/{second}') and first not in disk, disk)
        await self.select(console, 'guest', second)
        await console.locator('.qfm-guest').get_by_role('button', name='Delete (F8)').click()
        await console.locator('.qfm-dialog button', has_text='Delete').click()
        gone = await self.wait_name(console, 'guest', second, present=False)
        disk = node.as_account(f'test -e ~/{second} && echo present || echo absent', check=False)
        checks.add(f'{tag}: delete', gone and disk == 'absent', disk)

    async def delete_guest_file(self, console, name: str) -> bool:
        await self.select(console, 'guest', name)
        await console.locator('.qfm-guest').get_by_role('button', name='Delete (F8)').click()
        await console.locator('.qfm-dialog button', has_text='Delete').click()
        return await self.wait_name(console, 'guest', name, present=False)

    # ---- Chromium
    async def chromium(self, playwright) -> None:
        checks, node, size = self.checks, self.node, self.args.size_mib * MIB
        browser = await playwright.chromium.launch(executable_path=CHROME, headless=False,
            env={**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':97')}, args=['--no-sandbox', '--no-first-run', '--autoplay-policy=no-user-gesture-required'])
        self.report['chromium'] = browser.version
        try:
            context = await browser.new_context(ignore_https_errors=True, viewport={'width': 1280, 'height': 800})
            await context.add_init_script(OPFS_PICKER)
            await context.add_init_script(RECORD_PEERS)
            seen = self.add_observers(context, 'chromium')
            page, console, picture = await self.open_console(context, 'chromium')
            checks.add('chromium: the console opened on the installed services', picture)
            if not picture:
                raise RuntimeError('no picture in Chromium')
            slot, line = await asyncio.to_thread(self.console_slot)
            self.report['slot'], self.report['slot_line'] = slot, line
            if slot == 2:
                raise RuntimeError(f'console 2 carries the fixture of this container, not logging in there: {line}')
            installed = node.ssh('sha256sum /usr/share/pve-manager/js/qsm-pve-direct-console.js').split()[0]
            checks.add('chromium: the page runs the installed console script, unmodified', list(seen['script'].values()) == [installed] or set(seen['script'].values()) == {installed},
                       {'browser': seen['script'], 'node': installed})
            await asyncio.sleep(2)

            # ---- at the login screen there is no service
            await self.click_files(console)
            await console.wait_for_selector('.qfm-guest .qfm-message', state='visible', timeout=30000)
            message = await console.locator('.qfm-guest .qfm-message').inner_text()
            listed = await self.names(console, 'guest')
            checks.add('chromium: before a login there are no files to see', not listed and ('qsm-direct-files', 200) not in seen['signal'],
                       {'rows': listed, 'file_requests': [item for item in seen['signal'] if 'files' in item[0]]})
            checks.add('chromium: before a login the page names the cause (nobody logged in)', 'Log in' in message or 'not running' in message, message[:220])
            await console.get_by_role('button', name='Back to console').click()
            await asyncio.sleep(1)

            # ---- the login
            session = await self.greeter_login(console, 'chromium')
            await self.verify_session(console, session, slot, 'chromium')
            sid = session['Id']

            # ---- the home
            marker = f'fm-marker-{secrets.token_hex(3)}.txt'
            node.as_account(f'echo marker > ~/{marker}')
            await self.click_files(console)
            await self.wait_home(console, 'chromium', marker)
            checks.add('chromium: the signal service answered the file request', ('qsm-direct-files', 200) in seen['signal'],
                       [item for item in seen['signal'] if 'files' in item[0]][-3:])
            await self.relay_check(page, 'chromium')
            hello = await asyncio.to_thread(node.helper_hello, slot)
            self.report['chromium_helper_hello'] = hello
            rights_line = [line for line in node.journal() if 'opened files' in line]
            self.report['chromium_files_lines'] = rights_line[-3:]

            # ---- upload
            await console.evaluate('window.opfs.clear()')
            await console.evaluate('([s]) => window.opfs.write("up-chromium.bin", s, 3)', [size])
            await console.get_by_role('button', name='Choose folder…').click()
            await self.wait_name(console, 'local', 'up-chromium.bin')
            result = await self.copy_up(console, 'up-chromium.bin')
            local = await console.evaluate('window.opfs.hash("up-chromium.bin")')
            remote = node.as_account('sha256sum ~/up-chromium.bin; stat -c "%s %U %a" ~/up-chromium.bin', check=False).splitlines()
            ok = result['done'] and local['size'] == size and bool(remote) and remote[0].split()[0] == local['sha256']
            checks.add(f'chromium: {self.args.size_mib} MiB upload, SHA-256 equal', ok,
                       {'browser': local, 'guest': remote, 'seconds': result['seconds'], 'MiB_per_s': round(self.args.size_mib / max(result['seconds'], .1), 1), 'job': result['jobs'][-1:]})
            checks.add('chromium: the temporary file existed during the upload and is gone', bool(result['temporary_seen']) and not result['temporary_after'],
                       {'during': result['temporary_seen'], 'after': result['temporary_after']})
            await self.clear_jobs(console)

            # ---- download
            node.as_account(f'head -c {size} /dev/urandom > ~/down-chromium.bin')
            await self.refresh_guest(console)
            await self.wait_name(console, 'guest', 'down-chromium.bin')
            await self.select(console, 'guest', 'down-chromium.bin')
            await console.get_by_role('button', name='← Copy here').click()
            result = await self.job_end(console, self.args.transfer_timeout)
            local = await console.evaluate('window.opfs.hash("down-chromium.bin")') if result['done'] else {}
            remote = node.as_account('sha256sum ~/down-chromium.bin').split()[0]
            checks.add(f'chromium: {self.args.size_mib} MiB download, SHA-256 equal', result['done'] and local.get('sha256') == remote,
                       {'browser': local, 'guest': remote, 'seconds': result['seconds'], 'MiB_per_s': round(self.args.size_mib / max(result['seconds'], .1), 1), 'job': result['jobs'][-1:]})
            await self.clear_jobs(console)

            await self.folder_steps(console, 'chromium')
            deleted = [await self.delete_guest_file(console, name) for name in ('up-chromium.bin', 'down-chromium.bin')]
            left = node.as_account('ls -a ~ | grep -E "chromium|qsm-part"', check=False)
            checks.add('chromium: copies deleted through the pane, no temporary file left', all(deleted) and not left and not self.part_files(), left)

            await self.lock_steps(page, console, slot, sid)

            pageerrors = [text for text in seen['errors']]
            checks.add('chromium: no page errors', not pageerrors, pageerrors[:6])
        finally:
            # The desktop stays for Firefox: this closes the console, not the session.
            await browser.close()

    # ---- the real lock
    async def lock_steps(self, page, console, slot: int, sid: str) -> None:
        checks, node = self.checks, self.node
        before = await asyncio.to_thread(node.lock_state, sid)
        hello = await asyncio.to_thread(node.helper_hello, slot)
        self.report['lock_before'] = {'state': before, 'helper': hello}
        checks.add('lock: before, the session is not locked, by logind, the locker and the helper',
                   before['LockedHint'] == 'no' and hello.get('hello', {}).get('locked') is False,
                   {'state': before, 'helper_locked': hello.get('hello', {}).get('locked')})
        self.report['lock_bus_names'] = node.as_account('busctl --user --no-pager list 2>&1 | grep -iE "ScreenSaver|kscreenlocker|ksld" | cut -c1-90', check=False).splitlines()
        node.guest(f'loginctl lock-session {sid}')

        def engaged() -> dict | None:
            state = node.lock_state(sid)
            return state if state['LockedHint'] == 'yes' or state['ScreenSaver.GetActive'].strip() == 'b true' else None
        state = await poll(engaged, 25)
        how = 'loginctl lock-session'
        if not state:
            # The session's own locker, in case logind's signal does not reach it.
            how = 'org.freedesktop.ScreenSaver.Lock'
            attempt = node.as_account('busctl --user --timeout=5 call org.freedesktop.ScreenSaver /ScreenSaver org.freedesktop.ScreenSaver Lock 2>&1', check=False)
            self.report['lock_direct_call'] = attempt
            state = await poll(engaged, 15)
        await asyncio.sleep(2.5)  # the helper reads the state at most every 2 s
        now = await asyncio.to_thread(node.lock_state, sid)
        hello = await asyncio.to_thread(node.helper_hello, slot)
        self.report['lock_engaged'] = {'by': how, 'state': now, 'helper': hello}
        if not state:
            journal = node.guest(f'journalctl _UID={node.uid} --since @{node.started} --no-pager -o cat | grep -iE "lock|ksld|kscreen|greet" | tail -15 | cut -c1-180', check=False)
            self.report['lock_not_engaged_journal'] = journal.splitlines()
            checks.add('lock: the locker engaged', False, {'state': now, 'helper_locked': hello.get('hello', {}).get('locked'), 'journal': journal.splitlines()[-8:]})
            return
        checks.add('lock: the locker engaged', True, {'by': how, 'state': now})
        checks.add('lock: the helper reports locked', hello.get('hello', {}).get('locked') is True, hello.get('hello'))
        await console.get_by_role('button', name='Back to console').click()
        await asyncio.sleep(2)
        await self.shot(console, 'lock-1-locked-picture')
        await self.click_files(console)
        # Coming back reloads the pane, which is refused as well. Its message must not pass for the next one's.
        await asyncio.sleep(3)
        self.report['lock_reopen_message'] = await self.report_text(console)
        dismiss = console.locator('.qfm-report button', has_text='Dismiss')
        if await console.locator('.qfm-report').is_visible():
            await dismiss.click()

        # A new operation through the page.
        await self.ask_name(console, 'New folder (F7)', 'fm-while-locked')
        await console.wait_for_selector('.qfm-report', state='visible', timeout=15000)
        text = await self.report_text(console)
        disk = node.as_account('test -e ~/fm-while-locked && echo present || echo absent', check=False)
        checks.add('lock: a new folder is refused with the page\'s message', LOCKED_TEXT in text and disk == 'absent', {'message': text, 'on_disk': disk})
        await self.shot(console, 'lock-2-refused')
        await console.locator('.qfm-report button', has_text='Dismiss').click()
        await self.refresh_guest(console)
        await console.wait_for_selector('.qfm-report', state='visible', timeout=15000)
        text = await self.report_text(console)
        checks.add('lock: browsing is refused too', LOCKED_TEXT in text, text)
        await console.locator('.qfm-report button', has_text='Dismiss').click()

        # A copy waits for the unlock.
        await console.evaluate('window.opfs.write("while-locked.bin", 1048576, 11)')
        await console.locator('.qfm-local').get_by_role('button', name='Refresh').click()
        await self.wait_name(console, 'local', 'while-locked.bin')
        await self.select(console, 'local', 'while-locked.bin')
        await console.get_by_role('button', name='Copy to guest →').click()
        paused = ''
        for _ in range(60):
            paused = await console.evaluate("document.querySelector('.qfm-job-text')?.textContent || ''")
            if 'locked' in paused:
                break
            await asyncio.sleep(.25)
        disk = node.as_account('test -e ~/while-locked.bin && echo present || echo absent', check=False)
        checks.add('lock: a copy is paused with the page\'s message', 'Paused: the guest screen is locked' in paused and disk == 'absent', {'job': paused, 'on_disk': disk})

        # Unlock.
        node.guest(f'loginctl unlock-session {sid}')

        def released() -> dict | None:
            state = node.lock_state(sid)
            return state if state['LockedHint'] == 'no' and state['ScreenSaver.GetActive'].strip() != 'b true' else None
        state = await poll(released, 25)
        await asyncio.sleep(2.5)
        hello = await asyncio.to_thread(node.helper_hello, slot)
        self.report['lock_released'] = {'state': await asyncio.to_thread(node.lock_state, sid), 'helper': hello}
        checks.add('lock: unlock-session releases it, by logind, the locker and the helper', bool(state) and hello.get('hello', {}).get('locked') is False,
                   self.report['lock_released'])
        result = await self.job_end(console, 60)
        local = await console.evaluate('window.opfs.hash("while-locked.bin")')
        remote = node.as_account('sha256sum ~/while-locked.bin 2>&1', check=False).split()[0]
        checks.add('lock: the paused copy continues after the unlock, intact', result['done'] and local['sha256'] == remote, {'job': result['jobs'][-1:], 'seconds': result['seconds']})
        await self.clear_jobs(console)
        await self.refresh_guest(console)
        await self.wait_name(console, 'guest', 'while-locked.bin')
        await self.ask_name(console, 'New folder (F7)', 'fm-after-unlock')
        shown = await self.wait_name(console, 'guest', 'fm-after-unlock', timeout=15)
        disk = node.as_account('stat -c "%F" ~/fm-after-unlock 2>&1', check=False)
        checks.add('lock: a new folder works again', shown and disk == 'directory', {'shown': shown, 'on_disk': disk, 'message': await self.report_text(console)})
        for name in ('fm-after-unlock', 'while-locked.bin'):
            await self.delete_guest_file(console, name)
        await console.get_by_role('button', name='Back to console').click()
        await asyncio.sleep(1.5)
        await self.shot(console, 'lock-3-unlocked-picture')
        await self.click_files(console)

    # ---- Firefox
    async def firefox(self, playwright) -> None:
        checks, node, size = self.checks, self.node, self.args.size_mib * MIB
        options: dict = {}
        if self.args.firefox_headed:
            options['env'] = {**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':97')}
        browser = await playwright.firefox.launch(executable_path=FIREFOX, headless=not self.args.firefox_headed, **options,
            firefox_user_prefs={'media.autoplay.default': 0, 'media.autoplay.blocking_policy': 0})
        self.report['firefox'] = browser.version
        try:
            context = await browser.new_context(ignore_https_errors=True, viewport={'width': 1280, 'height': 800}, accept_downloads=True)
            await context.add_init_script(RECORD_PEERS)
            seen = self.add_observers(context, 'firefox')
            page, console, picture = await self.open_console(context, 'firefox')
            await asyncio.sleep(2)
            await self.shot(console, 'firefox-0-first')
            peers = await page.evaluate(PEER_STATS)
            page_state = await console.evaluate("""() => { const v = document.querySelector('video');
                return { width: v && v.videoWidth, height: v && v.videoHeight, time: v && v.currentTime, paused: v && v.paused, text: document.body.innerText.slice(0, 300) }; }""")
            self.report['firefox_console'] = {'picture': picture, 'page': page_state, 'peers': peers}
            checks.add('firefox: the console shows a picture through PVE and TURN', picture,
                       {'video': page_state, 'stats': [{key: peer.get(key) for key in ('state', 'local_type', 'relay_protocol', 'video', 'bytes_received')} for peer in peers]})
            slot, line = await asyncio.to_thread(self.console_slot)
            self.report['firefox_slot_line'] = line
            if slot == 2:
                raise RuntimeError(f'console 2 carries the fixture of this container, not logging in there: {line}')
            session = await asyncio.to_thread(node.desktop_session)
            if picture:
                if session:
                    # A fresh login, so Firefox's mouse and keyboard are what gets the desktop.
                    node.guest(f'loginctl terminate-session {session["Id"]}', check=False)
                    def closed() -> bool:
                        return not node.desktop_session() and not node.guest(f'grep -lw "{node.uid}" /run/qsm-native/*/*.json 2>/dev/null', check=False)
                    gone = await poll(closed, 60)
                    self.report['firefox_previous_session_closed'] = bool(gone)
                checks.add('firefox: the login screen is back on the console', await self.wait_greeter(slot))
                await asyncio.sleep(3)
                session = await self.greeter_login(console, 'firefox')
            elif not session:
                raise RuntimeError('no picture in Firefox and no desktop to resume')
            await self.verify_session(console, session, slot, 'firefox')

            marker = f'fm-marker-ff-{secrets.token_hex(3)}.txt'
            node.as_account(f'echo marker > ~/{marker}')
            await self.click_files(console)
            await self.wait_home(console, 'firefox', marker)
            checks.add('firefox: no folder picker, so files are added', await console.get_by_role('button', name='Add files…').count() == 1 and await console.get_by_role('button', name='Choose folder…').count() == 0)
            await self.relay_check(page, 'firefox')

            with tempfile.TemporaryDirectory(dir=self.out) as directory:
                picked = Path(directory) / 'up-firefox.bin'
                with picked.open('wb') as target:
                    for _ in range(self.args.size_mib):
                        target.write(os.urandom(MIB))
                local = sha256_file(picked)
                await console.locator('.qfm-local input[type=file]').first.set_input_files(str(picked))
                await self.wait_name(console, 'local', 'up-firefox.bin')
                result = await self.copy_up(console, 'up-firefox.bin')
            remote = node.as_account('sha256sum ~/up-firefox.bin 2>&1', check=False).split()[0]
            checks.add(f'firefox: {self.args.size_mib} MiB upload through the file input, SHA-256 equal', result['done'] and remote == local,
                       {'browser': local, 'guest': remote, 'seconds': result['seconds'], 'MiB_per_s': round(self.args.size_mib / max(result['seconds'], .1), 1), 'job': result['jobs'][-1:]})
            checks.add('firefox: the temporary file existed during the upload and is gone', bool(result['temporary_seen']) and not result['temporary_after'],
                       {'during': result['temporary_seen'], 'after': result['temporary_after']})
            await self.clear_jobs(console)

            node.as_account(f'head -c {size} /dev/urandom > ~/down-firefox.bin')
            await self.refresh_guest(console)
            await self.wait_name(console, 'guest', 'down-firefox.bin')
            await self.select(console, 'guest', 'down-firefox.bin')
            started = time.monotonic()
            async with console.expect_download(timeout=self.args.transfer_timeout * 1000) as info:
                await console.get_by_role('button', name='← Download').click()
            download = await info.value
            seconds = time.monotonic() - started
            saved = sha256_file(Path(await download.path()))
            remote = node.as_account('sha256sum ~/down-firefox.bin').split()[0]
            checks.add(f'firefox: {self.args.size_mib} MiB download through the spool, SHA-256 equal', saved == remote and download.suggested_filename == 'down-firefox.bin',
                       {'browser': saved, 'guest': remote, 'name': download.suggested_filename, 'seconds': round(seconds, 1), 'MiB_per_s': round(self.args.size_mib / max(seconds, .1), 1)})
            await self.job_end(console, 30)
            await self.clear_jobs(console)

            node.as_account('mkdir -p ~/fm-ff/sub; head -c 300000 /dev/urandom > ~/fm-ff/one.bin; head -c 70000 /dev/urandom > ~/fm-ff/sub/two.bin')
            await self.refresh_guest(console)
            await self.wait_name(console, 'guest', 'fm-ff')
            await self.select(console, 'guest', 'fm-ff')
            async with console.expect_download(timeout=120000) as info:
                await console.get_by_role('button', name='← Download').click()
            download = await info.value
            with tarfile.open(await download.path()) as archive:
                members = sorted(member.name for member in archive.getmembers())
                one = hashlib.sha256(archive.extractfile('fm-ff/one.bin').read()).hexdigest()
                two = hashlib.sha256(archive.extractfile('fm-ff/sub/two.bin').read()).hexdigest()
            guest_one = node.as_account('sha256sum ~/fm-ff/one.bin').split()[0]
            guest_two = node.as_account('sha256sum ~/fm-ff/sub/two.bin').split()[0]
            checks.add('firefox: a folder arrives as one tar with the same bytes',
                       download.suggested_filename == 'fm-ff.tar' and members == ['fm-ff', 'fm-ff/one.bin', 'fm-ff/sub', 'fm-ff/sub/two.bin'] and one == guest_one and two == guest_two,
                       {'name': download.suggested_filename, 'members': members})
            await self.job_end(console, 30)
            await self.clear_jobs(console)

            await self.folder_steps(console, 'firefox')
            deleted = [await self.delete_guest_file(console, name) for name in ('up-firefox.bin', 'down-firefox.bin', 'fm-ff')]
            left = node.as_account('ls -a ~ | grep -E "firefox|fm-ff|qsm-part"', check=False)
            checks.add('firefox: copies deleted through the pane, no temporary file left', all(deleted) and not left and not self.part_files(), left)
            checks.add('firefox: no page errors', not seen['errors'], seen['errors'][:6])
            await self.shot(console, 'firefox-6-end')
        finally:
            # With the console still attached the seat frees itself the moment it closes.
            await asyncio.to_thread(node.end_session)
            await browser.close()


async def run(args, node: Node, checks: Checks, report: dict, password: str) -> None:
    from playwright.async_api import async_playwright
    work = Run(args, node, checks, report, password)
    async with async_playwright() as playwright:
        for name in (['chromium', 'firefox'] if args.browser == 'both' else [args.browser]):
            try:
                await getattr(work, name)(playwright)
            except Exception as error:  # noqa: BLE001 - the other browser still runs, the report says how far this one got
                checks.add(f'{name}: the phase finished', False, f'{type(error).__name__}: {error}'[:1200])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('output', type=Path, help='directory for report.json and screenshots')
    parser.add_argument('--node', default='root@192.168.64.20')
    parser.add_argument('--vmid', type=int, default=108)
    parser.add_argument('--account', default='qsm-fm-check')
    parser.add_argument('--browser', default='both', choices=('chromium', 'firefox', 'both'))
    parser.add_argument('--size-mib', type=int, default=40)
    parser.add_argument('--transfer-timeout', type=float, default=240)
    parser.add_argument('--firefox-headed', action='store_true', help='on DISPLAY instead of headless')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    node, checks = Node(args.node, args.vmid, args.account), Checks()
    node.started = int(node.ssh('date +%s'))
    report: dict = {'date': time.strftime('%Y-%m-%d %H:%M:%S'), 'node': args.node, 'guest': args.vmid, 'account': args.account, 'packages': node.packages()}
    before = node.production()
    report['production_before'] = before
    try:
        password = node.create_account()
        checks.add('a throwaway account with a random password', True, {'uid': node.uid, 'password_status': 'P'})
        asyncio.run(run(args, node, checks, report, password))
    except (Exception, SystemExit) as error:  # noqa: BLE001 - the report must say how far it got
        checks.add('the run itself finished', False, f'{type(error).__name__}: {error}'[:1200])
    finally:
        password = None
        released_before = sum('released' in line for line in node.journal())
        removal = node.remove_account()
        report['account_removal'] = removal
        checks.add('the account, its home and its processes are gone',
                   'rc=0' in removal['userdel'] and not removal['passwd'] and not removal['home'] and removal['processes'] in ('0', ''), removal)
        # Whatever the terminal still holds of this console: its grace period ends in a reset.
        terminal = [line for line in node.journal() if re.search(r'console|files of guest', line)]
        if not any('released' in line for line in terminal[-3:]):
            deadline = time.monotonic() + 340
            while time.monotonic() < deadline and sum('released' in line for line in node.journal()) <= released_before:
                time.sleep(5)
        terminal = [line for line in node.journal() if re.search(r'(opens|resumes) console|released|files of guest|did not confirm', line)]
        report['terminal_lines'] = [line[:200] for line in terminal]
        after = node.production()
        report['production_after'] = after
        checks.add('the installed services and fixtures did not move', before == after,
                   {key: (before[key], after[key]) for key in before if before[key] != after[key]} or after['main_pids'])
        report['seat_after'] = node.guest('loginctl list-sessions --no-legend; ls -la /run/qsm-login/; ls /var/lib/AccountsService/users/ 2>/dev/null', check=False).splitlines()
        report['checks'] = checks.results
        report['failed'] = [item['check'] for item in checks.results if not item['ok']]
        text = scrub(json.dumps(report, indent=1, ensure_ascii=False, default=str))
        (args.output / 'report.json').write_text(text)
    say(json.dumps({'failed': report['failed']}, ensure_ascii=False))
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
