#!/usr/bin/env python3
"""The file manager through a real PVE login, console popup, signal service and terminal.

Nothing installed on the node changes. A private terminal and signal service run from a
private directory with their own sockets, and know only the dedicated test container. The
browser loads the new console script over the node's installed one and sends its signal
requests to the private service through an SSH tunnel. The helper runs by hand in the
container as a test account, on the socket path the terminal connects to.

Usage: qualify-file-manager-pve.py OUTPUT.json [--node root@HOST] [--vmid 108] [--account USER]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
CHROME = '/tmp/qsm-rdp-hybrid-20261028/chrome-stand/root/opt/google/chrome/chrome'
PRIVATE = '/root/qsm-file-e2e'
RUN = '/run/qsm-file-e2e'
SIGNAL_PORT = 18107
TUNNEL_PORT = 18108
GUEST_DIR = '/opt/qsm-file-e2e'

LIBRARY = {
    'browser_bridge': ['__init__.py', 'qsm_browser_bridge.py', 'qsm_lossless_ui.py', 'qsm_transport_feedback.py', 'qsm_file_peer.py'],
    'direct_guest': ['__init__.py', 'qsm_guest_channel.py', 'qsm_desktop_metadata.py', 'qsm_desktop_priority.py'],
    'direct_terminal': ['qsm_direct_terminal.py', 'qsm_direct_encoder_probe.py', 'qsm_shared_migration.py', 'qsm_file_session.py'],
    'signal': ['__init__.py', 'qsm_direct_signal.py', 'qsm_direct_endpoint.py', 'qsm_direct_endpoint_cli.py', 'qsm_gateway.py'],
}
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


class Checks:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def add(self, name: str, ok: bool, detail: object = None) -> None:
        self.results.append({'check': name, 'ok': bool(ok), 'detail': detail})
        suffix = '' if detail is None else ' ' + json.dumps(detail, ensure_ascii=False, default=str)[:700]
        print(f"{'ok  ' if ok else 'FAIL'} {name}{suffix}", flush=True)


class Node:
    def __init__(self, args) -> None:
        self.host, self.vmid, self.account = args.node, args.vmid, args.account

    def ssh(self, command: str, *, check: bool = True, timeout: float = 120) -> str:
        return subprocess.run(['ssh', '-o', 'BatchMode=yes', self.host, command], check=check, capture_output=True,
                              text=True, timeout=timeout).stdout.strip()

    def guest(self, command: str, *, check: bool = True) -> str:
        return self.ssh(f'pct exec {self.vmid} -- sh -c {shlex.quote(command)}', check=check)

    def as_account(self, command: str, *, check: bool = True) -> str:
        return self.guest(f'runuser -u {self.account} -- env HOME=/home/{self.account} sh -c {shlex.quote(command)}', check=check)

    def production(self) -> dict:
        return {name: self.ssh(f'systemctl show -p MainPID --value {name}') for name in
                ('qsm-pve-direct-terminal', 'qsm-pve-direct-signal', 'pveproxy')}

    def stage(self) -> str:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            for package, names in LIBRARY.items():
                target = work / ('signal_service' if package == 'signal' else package)
                target.mkdir()
                for name in names:
                    (target / name).write_bytes((ROOT / 'extensions' / package / name).read_bytes())
            if os.environ.get('QSM_E2E_PEER_DEBUG'):
                # What SCTP holds, in the stderr that reaches the terminal's log. Not in the product.
                path = work / 'browser_bridge' / 'qsm_file_peer.py'
                text = path.read_text()
                marker = '        relay = asyncio.ensure_future(self._from_terminal())\n'
                assert marker in text
                path.write_text(text.replace(marker, marker + '''        async def qfm_debug():
            while True:
                await asyncio.sleep(5)
                t = self._pc.sctp
                print("QFMHEAD", {k: [(c.tsn - t._last_received_tsn, c.flags, c.stream_seq, len(c.user_data)) for c in v.reassembly[:8]] for k, v in t._inbound_streams.items()}, "forward_tsn", getattr(t, "_qfm_forward", 0), file=sys.stderr, flush=True)
                print("QFMDBG", {"last_tsn": t._last_received_tsn, "misordered": len(t._sack_misordered), "dups": len(t._sack_duplicates), "rwnd": t._advertised_rwnd,
                    "streams": {k: (v.sequence_number, len(v.reassembly)) for k, v in t._inbound_streams.items()}, "state": t._association_state.name, "wbuf": self._writer.transport.get_write_buffer_size(),
                    "cwnd": t._cwnd, "flight": t._flight_size, "out": len(t._outbound_queue), "sent": len(t._sent_queue)}, file=sys.stderr, flush=True)
        _t = self._pc.sctp
        _orig = _t._receive_forward_tsn_chunk
        async def _counted(chunk):
            _t._qfm_forward = getattr(_t, "_qfm_forward", 0) + 1
            await _orig(chunk)
        _t._receive_forward_tsn_chunk = _counted
        asyncio.ensure_future(qfm_debug())
'''))
            (work / 'browser_bridge' / 'qsm_gateway.py').write_bytes((ROOT / 'extensions/signal/qsm_gateway.py').read_bytes())
            (work / 'qsm_desktop_files.py').write_bytes((ROOT / 'guest/qsm_desktop_files.py').read_bytes())
            self.ssh(f'rm -rf {PRIVATE}; mkdir -p {PRIVATE}')
            subprocess.run(['scp', '-q', '-r', '-o', 'BatchMode=yes', *[str(path) for path in work.iterdir()], f'{self.host}:{PRIVATE}/'], check=True)
        name = self.ssh('hostname')
        self.ssh(f"""set -e
mkdir -p -m 700 {RUN} {RUN}/sessions {RUN}/instances {RUN}/containers {RUN}/pve/qemu-server {RUN}/pve/lxc
cp /etc/qsm-pve-direct/containers.d/{self.vmid}.conf {RUN}/containers/
cp /etc/pve/lxc/{self.vmid}.conf {RUN}/pve/lxc/
cd {PRIVATE}
QSM_DIRECT_EXACT_UI_DEFAULT=1 nohup setsid python3 direct_terminal/qsm_direct_terminal.py --instance-directory {RUN}/instances --runtime-directory {RUN}/sessions \
  --vm-runtime-directory {RUN}/vm --pve-config-directory {RUN}/pve/qemu-server --qemu-pid-directory {RUN}/qemu --container-instance-directory {RUN}/containers \
  --pve-socket {RUN}/pve-webrtc.sock --webrtc-socket {RUN}/webrtc/offer.sock --local-node {name} > {RUN}/terminal.log 2>&1 < /dev/null &
echo $! > {RUN}/terminal.pid
for i in $(seq 1 100); do [ -S {RUN}/pve-webrtc.sock ] && break; sleep .1; done
[ -S {RUN}/pve-webrtc.sock ]
nohup setsid python3 -c 'import sys; sys.path.insert(0, "{PRIVATE}"); from signal_service.qsm_direct_signal import main; sys.exit(main())' \
  --listen 127.0.0.1 --port {SIGNAL_PORT} --terminal-socket {RUN}/pve-webrtc.sock --instance-directory {RUN}/instances --container-instance-directory {RUN}/containers \
  --cert /etc/pve/local/pve-ssl.pem --key /etc/pve/local/pve-ssl.key --local-node {name} > {RUN}/signal.log 2>&1 < /dev/null &
echo $! > {RUN}/signal.pid
for i in $(seq 1 100); do ss -ltnH | grep -q :{SIGNAL_PORT} && break; sleep .1; done
ss -ltnH | grep -q :{SIGNAL_PORT}
""")
        self.ssh(f'pct exec {self.vmid} -- mkdir -p {GUEST_DIR}; pct push {self.vmid} {PRIVATE}/qsm_desktop_files.py {GUEST_DIR}/qsm_desktop_files.py')
        return name

    def start_helper(self, slot: int) -> None:
        directory = f'/run/qsm-login/clip-{slot}'
        self.as_account(f'mkdir -p ~/qfm-e2e/inbox; rm -f {directory}/files.sock; '
                        f'nohup python3 {GUEST_DIR}/qsm_desktop_files.py --listen {directory}/files.sock --home "$HOME/qfm-e2e" > /tmp/qfm-helper.log 2>&1 < /dev/null & sleep 1; ls -la {directory}/files.sock')

    def clean(self) -> None:
        for name in ('signal', 'terminal'):
            self.ssh(f'test -f {RUN}/{name}.pid && kill $(cat {RUN}/{name}.pid); true', check=False)
        self.ssh(f'sleep 1; pgrep -f "{PRIVATE}/[b]rowser_bridge/qsm_file_peer" | xargs -r kill; pgrep -f "[q]sm-direct-media-worker.*{RUN}" | xargs -r kill', check=False)
        self.guest(f'pkill -f "{GUEST_DIR}/[q]sm_desktop_files"; rm -rf {GUEST_DIR} /run/qsm-login/clip-*/files.sock; rm -rf /home/{self.account}/qfm-e2e', check=False)
        self.ssh(f'cp -a {RUN}/terminal.log {RUN}/signal.log /var/tmp/ 2>/dev/null; rm -rf {RUN} {PRIVATE}', check=False)


async def run(args, node: Node, checks: Checks, report: dict) -> None:
    from playwright.async_api import async_playwright
    before = node.production()
    report['production_before'] = before
    node_name = node.stage()
    checks.add('the private terminal and signal service came up', True, node_name)
    tunnel = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-N', '-L', f'{TUNNEL_PORT}:127.0.0.1:{SIGNAL_PORT}', node.host])
    await asyncio.sleep(1)
    host = node.host.split('@')[-1]
    cmd = "perl -MPVE::AccessControl -MPVE::Cluster -MJSON -e 'PVE::Cluster::cfs_update(); print encode_json({ticket=>scalar PVE::AccessControl::assemble_ticket(q{root@pam})})'"
    auth = json.loads(node.ssh(cmd))
    console_source = (ROOT / 'integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
    # Only for what a stalled run reports: the manager lives in the console's closure.
    assert 'fileManager = new QsmFileManager({' in console_source
    console_source = console_source.replace('fileManager = new QsmFileManager({', 'fileManager = popup.__qsmFiles = new QsmFileManager({')
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=CHROME, headless=False,
                env={**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':97')}, args=['--no-sandbox', '--no-first-run', '--autoplay-policy=no-user-gesture-required'])
            report['chromium'] = browser.version
            context = await browser.new_context(ignore_https_errors=True, viewport={'width': 1280, 'height': 800})
            await context.add_init_script(OPFS_PICKER)
            await context.add_init_script("""window.qsmPeers = [];
                const NativePeer = window.RTCPeerConnection;
                window.RTCPeerConnection = class extends NativePeer { constructor(...args) { super(...args); window.qsmPeers.push(this); } };""")
            errors: list[str] = []
            signal_calls: list[tuple[str, int]] = []
            context.on('page', lambda item: item.on('pageerror', lambda error: errors.append(f'{error.name}: {error.message}')))

            async def serve_console(route) -> None:
                await route.fulfill(status=200, content_type='application/javascript', body=console_source)
            await context.route('**/pve2/js/qsm-pve-direct-console.js*', serve_console)

            async def to_private_signal(route) -> None:
                request = route.request
                path = request.url.split('/qsm-direct', 1)[1]
                response = await route.fetch(url=f'https://127.0.0.1:{TUNNEL_PORT}{path}')
                signal_calls.append((path.rsplit('/', 1)[-1], response.status))
                await route.fulfill(response=response)
            await context.route('**/qsm-direct/**', to_private_signal)

            page = await context.new_page()
            await page.goto(f'https://{host}:8006/', wait_until='domcontentloaded')
            await page.locator('input[name=username]').fill('root')
            await page.locator('input[name=password]').fill(auth['ticket'])
            auth = None
            await page.evaluate("void Ext.ComponentQuery.query('field[name=realm]')[0].setValue('pam')")
            await page.get_by_role('button', name='Login', exact=True).click()
            await page.wait_for_function("window.Ext&&Ext.ComponentQuery.query('pveResourceTree')[0]", timeout=30000)
            notice = page.locator('.x-message-box:visible')
            if await notice.count():
                ok = notice.locator('.x-btn').filter(has_text='OK')
                if await ok.count():
                    await ok.last.click()
            await page.wait_for_function(f"Ext.ComponentQuery.query('pveResourceTree')[0].getStore().getRootNode().findChild('id','lxc/{node.vmid}',true)", timeout=30000)
            await page.evaluate(f"void Ext.ComponentQuery.query('pveResourceTree')[0].selectById('lxc/{node.vmid}')")
            await page.wait_for_function("Ext.ComponentQuery.query('[itemId=qsm-direct-console-button]').some(b=>b.down('#qsm-direct')&&!b.down('#qsm-direct').disabled)", timeout=30000)
            async with page.expect_popup(timeout=30000) as popup:
                await page.evaluate("(()=>{const b=Ext.ComponentQuery.query('[itemId=qsm-direct-console-button]').find(b=>b.consoleType==='lxc');b.down('#qsm-direct').handler()})()")
            console = await popup.value
            console.on('console', lambda message: errors.append(message.text) if message.type == 'error' and 'Failed to load' not in message.text else None)
            await console.wait_for_function("document.querySelector('video')?.videoWidth>0", timeout=60000)
            checks.add('the console opened through the private terminal', True, await console.evaluate('document.querySelector("video").videoWidth'))
            slot_line = node.ssh(f'grep -oE "opens console [0-9]+|resumes console [0-9]+" {RUN}/terminal.log | tail -1', check=False)
            slot = int(slot_line.split()[-1]) if slot_line else 1
            report['slot'] = slot

            button = console.get_by_role('button', name='Files: copy between this computer and the guest')
            toolbar_opened = False
            for _ in range(100):
                if await button.count() and await button.is_visible():
                    break
                handle = console.locator('button[aria-controls="qsm-direct-toolbar"]')
                if not toolbar_opened and await handle.count() and await handle.get_attribute('aria-expanded') == 'false':
                    await handle.click()
                    toolbar_opened = True
                await asyncio.sleep(.1)
            checks.add('the token arrived and the toolbar offers Files', await button.is_visible())

            # ---- no helper yet: the real reason, on the real path
            await button.click()
            await console.wait_for_selector('.qfm-guest .qfm-message', state='visible', timeout=30000)
            message = await console.locator('.qfm-guest .qfm-message').inner_text()
            checks.add('without a helper the page says what is missing', 'could not start' in message or 'not running' in message, message[:200])
            await console.get_by_role('button', name='Back to console').click()

            node.start_helper(slot)
            await button.click()
            await console.get_by_role('button', name='Refresh').last.click()
            try:
                await console.wait_for_function("document.querySelector('.qfm-guest .qfm-name')?.textContent==='inbox'", timeout=30000)
            except Exception:
                report['guest_pane_state'] = await console.evaluate("""() => ({ message: document.querySelector('.qfm-guest .qfm-message')?.textContent,
                    report: document.querySelector('.qfm-report span')?.textContent, rows: [...document.querySelectorAll('.qfm-guest .qfm-name')].map((e) => e.textContent).slice(0, 5),
                    location: document.querySelector('.qfm-guest .qfm-location')?.textContent }) """)
                report['helper_log'] = node.as_account('cat /tmp/qfm-helper.log; ls -la /run/qsm-login/clip-*/ 2>&1 | tail -8', check=False)
                raise RuntimeError(f"the guest pane did not fill: {report['guest_pane_state']} {report['helper_log'][-400:]}")
            checks.add('the guest pane lists the session account\'s folder over TURN and the real ACL path', True,
                       await console.locator('.qfm-guest .qfm-location').inner_text())
            report['signal_calls'] = list(signal_calls)
            checks.add('the signal service answered the file request', ('qsm-direct-files', 200) in signal_calls, signal_calls[-4:])
            relay = await page.evaluate("""async () => {
                const peer = window.qsmPeers[window.qsmPeers.length - 1];
                const stats = [...(await peer.getStats()).values()];
                const pair = stats.find((item) => item.type === 'candidate-pair' && item.nominated && item.state === 'succeeded');
                const local = pair && stats.find((item) => item.id === pair.localCandidateId);
                return { peers: window.qsmPeers.length, state: peer.connectionState, candidate: local && local.candidateType, labels: stats.filter((item) => item.type === 'data-channel').map((item) => item.label) };
            }""")
            checks.add('files have their own connection, through the relay as the console', relay['peers'] >= 2 and relay['candidate'] == 'relay' and 'qsm-files' in relay['labels'], relay)

            # ---- real copies both ways, checked inside the container
            await console.evaluate('window.opfs.clear()')
            await console.evaluate('window.opfs.write("up.bin", 12000000, 3)')
            await console.get_by_role('button', name='Choose folder…').click()
            await console.wait_for_function("document.querySelector('.qfm-local .qfm-name')?.textContent==='up.bin'", timeout=20000)
            await console.locator('.qfm-local .qfm-name', has_text='up.bin').click()
            await console.get_by_role('button', name='Copy to guest →').click()
            started = time.monotonic()
            await console.wait_for_selector('.qfm-job-text:has-text("Done")', timeout=180000)
            local = await console.evaluate('window.opfs.hash("up.bin")')
            remote = node.as_account('sha256sum ~/qfm-e2e/up.bin').split()[0]
            mode = node.as_account('stat -c "%U %a" ~/qfm-e2e/up.bin')
            checks.add('an upload arrives intact as the session account', local['sha256'] == remote and mode.startswith(node.account), {'seconds': round(time.monotonic() - started, 1), 'owner_mode': mode})
            await console.get_by_role('button', name='Remove').click()

            node.as_account('head -c 9000000 /dev/urandom > ~/qfm-e2e/inbox/down.bin')
            await console.locator('.qfm-guest .qfm-name', has_text='inbox').dblclick()
            await console.wait_for_function("document.querySelector('.qfm-guest .qfm-name')?.textContent==='down.bin'", timeout=20000)
            await console.locator('.qfm-guest .qfm-name', has_text='down.bin').click()
            await console.get_by_role('button', name='← Copy here').click()
            await console.wait_for_selector('.qfm-job-text:has-text("Done")', timeout=180000)
            local = await console.evaluate('window.opfs.hash("down.bin")')
            remote = node.as_account('sha256sum ~/qfm-e2e/inbox/down.bin').split()[0]
            checks.add('a download arrives intact', local['sha256'] == remote)
            await console.get_by_role('button', name='Remove').click()

            # ---- how fast on this path, with the file manager open
            if args.throughput:
                async def finished(what: str) -> None:
                    try:
                        await console.wait_for_selector('.qfm-job-text:has-text("Done")', timeout=args.throughput * 1500 + 60000)
                    except Exception:
                        state = await console.evaluate("""() => ({ jobs: [...document.querySelectorAll('.qfm-job-text,.qfm-job-errors,.qfm-dialog,.qfm-report span')].map((e) => e.textContent),
                            local: [...document.querySelectorAll('.qfm-local .qfm-name')].map((e) => e.textContent).slice(0, 8),
                            guest: [...document.querySelectorAll('.qfm-guest .qfm-name')].map((e) => e.textContent).slice(0, 8) })""")
                        inner = await console.evaluate("""() => { const m = window.__qsmFiles, l = m.link; return { closed: l.closed, reason: l.endReason, inflight: l.inflight, waiters: l.waiters.length,
                            pending: l.pending.size, rate: Math.round(l.rate.rate), current: Math.round(l.rate.current()), window: l.rate.window(), tokens: Math.round(l.tokens), buffered: l.files.bufferedAmount,
                            lowThreshold: l.files.bufferedAmountLowThreshold, state: l.files.readyState, peer: l.peer.connectionState, ice: l.peer.iceConnectionState, pings: l.pingCount, floor: l.rate.floorDelay,
                            recent: l.rate.recent, draining: l.rate.draining, aside: l.rate.standingAside, unanswered: l.rate.unanswered, hidden: document.hidden, jobs: m.jobs.map((j) => [j.state, j.done, j.total, j.errors.length]) }; }""")
                        guest = node.as_account('ls -la ~/qfm-e2e | cut -c20-90', check=False)
                        peer = node.ssh(f"P=$(pgrep -f '{PRIVATE}/[b]rowser_bridge/qsm_file_peer' | head -1); ps -o pcpu,etimes,cputimes -p $P | tail -1; cat /proc/$P/wchan; echo; ss -xp 2>/dev/null | grep \"pid=$P\" | cut -c1-120 | head -3", check=False)
                        report['stall'] = {'page': state, 'inner': inner, 'guest': guest, 'peer': peer, 'terminal_log': node.ssh(f'grep -vE "media health|MEDIA_HEALTH|libx264" {RUN}/terminal.log | tail -14', check=False)}
                        raise RuntimeError(f"{what} did not finish: {json.dumps(report['stall'], ensure_ascii=False)[:2400]}")

                size = args.throughput * 1024 * 1024
                print('writing the local file', flush=True)
                await console.evaluate('([s]) => window.opfs.write("big.bin", s, 21)', [size])
                await console.get_by_role('button', name='Refresh').first.click()
                await console.wait_for_function("[...document.querySelectorAll('.qfm-local .qfm-name')].some((e)=>e.textContent==='big.bin')", timeout=20000)
                await console.get_by_role('button', name='Up one folder (Backspace)').last.click()
                await console.wait_for_function("[...document.querySelectorAll('.qfm-guest .qfm-name')].some((e)=>e.textContent==='inbox')", timeout=20000)
                await console.locator('.qfm-local .qfm-name', has_text='big.bin').click()
                print('uploading', flush=True)
                started = time.monotonic()
                await console.get_by_role('button', name='Copy to guest →').click()
                await finished('the upload')
                up = args.throughput / (time.monotonic() - started)
                await console.get_by_role('button', name='Remove').click()
                local = await console.evaluate('window.opfs.hash("big.bin")')
                remote = node.as_account('sha256sum ~/qfm-e2e/big.bin').split()[0]
                checks.add(f'{args.throughput} MiB up through TURN', local['sha256'] == remote, {'MiB_per_s': round(up, 1)})
                await console.locator('.qfm-local .qfm-name', has_text='big.bin').click()
                await console.keyboard.press('Delete')
                await console.get_by_role('button', name='Delete', exact=True).last.click()
                await console.wait_for_function("![...document.querySelectorAll('.qfm-local .qfm-name')].some((e)=>e.textContent==='big.bin')", timeout=20000)
                await console.get_by_role('button', name='Refresh').last.click()
                await console.wait_for_function("[...document.querySelectorAll('.qfm-guest .qfm-name')].some((e)=>e.textContent==='big.bin')", timeout=20000)
                await console.locator('.qfm-guest .qfm-name', has_text='big.bin').click()
                print('downloading', flush=True)
                started = time.monotonic()
                await console.get_by_role('button', name='← Copy here').click()
                await finished('the download')
                down = args.throughput / (time.monotonic() - started)
                listing = await console.evaluate("""async () => { const out = []; for await (const [name, handle] of (await navigator.storage.getDirectory()).entries()) { out.push(name + ':' + handle.kind); }
                    return { files: out, jobs: [...document.querySelectorAll('.qfm-job-text,.qfm-job-errors')].map((e) => e.textContent), localPath: window.__qsmFiles.local.source.location, localDepth: window.__qsmFiles.local.source.stack.length }; }""")
                print('after the download', listing, flush=True)
                report['after_download'] = listing
                await console.get_by_role('button', name='Remove').click()
                local = await console.evaluate('window.opfs.hash("big.bin")')
                checks.add(f'{args.throughput} MiB down through TURN', local['sha256'] == remote, {'MiB_per_s': round(down, 1)})
                report['lan_turn_mib_per_s'] = {'up': round(up, 1), 'down': round(down, 1)}
                await console.locator('.qfm-guest .qfm-name', has_text='inbox').dblclick()
                await console.wait_for_function("document.querySelector('.qfm-guest .qfm-location')?.textContent.endsWith('inbox')", timeout=20000)
                node.as_account('rm -f ~/qfm-e2e/big.bin')

                # A connection that stops dead mid-file: the transfer must find its own way on.
                await console.locator('.qfm-local .qfm-name', has_text='big.bin').click()
                await console.get_by_role('button', name='Copy to guest →').click()
                await console.wait_for_function('window.__qsmFiles.jobs[0] && window.__qsmFiles.jobs[0].done > 2097152', timeout=60000)
                node.ssh(f"pkill -STOP -f '{PRIVATE}/[b]rowser_bridge/qsm_file_peer'", check=False)
                stopped = time.monotonic()
                await finished('the upload over a stopped peer')
                await asyncio.sleep(.2)
                remote2 = node.as_account('sha256sum ~/qfm-e2e/inbox/big.bin').split()[0]
                checks.add('a stopped connection is replaced and the file arrives whole', remote2 == remote, {'seconds_after_the_stop': round(time.monotonic() - stopped, 1)})
                await console.get_by_role('button', name='Remove').click()
                node.as_account('rm -f ~/qfm-e2e/inbox/big.bin')

            # ---- the console around it
            await console.get_by_role('button', name='Back to console').click()
            back = await console.evaluate("""() => ({ focused: document.activeElement && document.activeElement.tagName, shown: document.querySelector('.qfm').style.display,
                width: document.querySelector('video').videoWidth, time: document.querySelector('video').currentTime })""")
            checks.add('Back to console returns focus and picture', back['focused'] == 'VIDEO' and back['shown'] == 'none' and back['width'] > 0, back)

            # ---- a transfer behind the console, then closing the console ends it
            await console.evaluate('window.opfs.write("late.bin", 60000000, 9)')
            await button.click()
            await console.get_by_role('button', name='Refresh').first.click()
            await console.wait_for_function("[...document.querySelectorAll('.qfm-local .qfm-name')].some((e)=>e.textContent==='late.bin')", timeout=20000)
            await console.locator('.qfm-local .qfm-name', has_text='late.bin').click()
            await console.get_by_role('button', name='Copy to guest →').click()
            await console.get_by_role('button', name='Back to console').click()
            await asyncio.sleep(4)
            label = await button.inner_text()
            checks.add('the toolbar shows progress while the console is in view', '%' in label, label)
            peers = node.ssh(f'pgrep -fc "{PRIVATE}/[b]rowser_bridge/qsm_file_peer"', check=False)
            user = node.ssh(f"ps -o user= -p $(pgrep -f '{PRIVATE}/[b]rowser_bridge/qsm_file_peer' | head -1)", check=False)
            checks.add('one file peer, and it runs without privileges', peers == '1' and user == 'nobody', {'peers': peers, 'user': user})
            part = node.as_account('ls -a ~/qfm-e2e/inbox | grep -c qsm-part', check=False)
            await console.close()
            await asyncio.sleep(3)
            peers = node.ssh(f'pgrep -fc "{PRIVATE}/[b]rowser_bridge/qsm_file_peer"', check=False)
            leftover = node.as_account('find ~/qfm-e2e -name ".*qsm-part-*" | wc -l', check=False)
            final = node.as_account('test -e ~/qfm-e2e/inbox/late.bin && echo present || echo absent', check=False)
            checks.add('closing the console ends the peer and removes the unfinished file', peers == '0' and leftover == '0' and final == 'absent',
                       {'peers_after': peers, 'temporary_during': part, 'temporary_after': leftover, 'final': final})
            lease = node.ssh(f"nft list set inet qsm_direct_gateway peers 2>/dev/null | grep -c 'timeout' ; true", check=False)
            report['nft_peer_lines_after_close'] = lease
            checks.add('no page errors', not errors, errors[:6])
            report['page_errors'] = errors
            await browser.close()
    finally:
        tunnel.terminate()
    after = node.production()
    report['production_after'] = after
    checks.add('the installed services were not touched', before == after, after)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--node', default='root@192.168.64.20')
    parser.add_argument('--vmid', type=int, default=108)
    parser.add_argument('--account', default='qsm-rdp-meta')
    parser.add_argument('--throughput', type=int, default=0, help='MiB to copy each way, to see this path\'s speed')
    args = parser.parse_args()
    node, checks, report = Node(args), Checks(), {'date': time.strftime('%Y-%m-%d %H:%M:%S'), 'node': args.node, 'guest': args.vmid}
    try:
        asyncio.run(run(args, node, checks, report))
    except Exception as error:  # noqa: BLE001 - the report must say how far it got
        checks.add('the run itself finished', False, f'{type(error).__name__}: {error}'[:900])
    finally:
        log = node.ssh(f'tail -25 {RUN}/terminal.log; tail -5 {RUN}/signal.log', check=False)
        report['service_log_tail'] = log
        node.clean()
        report['checks'] = checks.results
        report['failed'] = [item for item in checks.results if not item['ok']]
        args.output.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    print(json.dumps({'failed': report['failed']}, ensure_ascii=False)[:1500])
    if report['failed']:
        print(report['service_log_tail'][-1500:])
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
