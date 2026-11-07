#!/usr/bin/env python3
"""Drive the real file manager UI in real browsers over the real file path.

browser (Playwright) -- WebSocket -- relay -- qsm_file_peer.py -- FileSession -- QsmGuestChannel --
[qsm-desktop-agent over a pty, or a VM's virtio port] -- qsm_desktop_files.py -- files

Only the page around the manager is a stand-in for PVE. In Chromium the folder picker
returns the origin private file system: a real directory handle a test can reach.

Usage: qualify-file-manager.py OUTPUT.json [--browser chromium|firefox|both] [--large MIB]
       [--small COUNT] [--channel unix|agent] [--agent PATH]
       qualify-file-manager.py OUTPUT.json --chardev SOCKET --ssh 'ssh ... user@host' [--large MIB]
           runs the transfer scenarios against a VM's real agent and virtio port.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.server
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'extensions'))
sys.path.insert(0, str(ROOT))
from direct_guest.qsm_guest_channel import QsmGuestChannel  # noqa: E402
from direct_terminal.qsm_file_session import FileSession  # noqa: E402
from extensions.signal.qsm_direct_signal import relay, websocket_accept  # noqa: E402

CHROME = '/tmp/qsm-rdp-hybrid-20261028/chrome-stand/root/opt/google/chrome/chrome'
FIREFOX = str(Path.home() / '.cache/ms-playwright/firefox-1555/firefox/firefox')
MIB = 1024 * 1024
WAIT_FOR_ICE = """
const waitForIce = (peer) => new Promise((resolve) => {
    if (peer.iceGatheringState === 'complete') { resolve(); return; }
    peer.addEventListener('icegatheringstatechange', () => { if (peer.iceGatheringState === 'complete') { resolve(); } });
    setTimeout(resolve, 3000);
});
"""


def manager_source() -> str:
    source = (ROOT / 'integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
    begin, end = source.index('    // QSM_FILES_BEGIN'), source.index('    // QSM_FILES_END')
    return source[begin:end]


PAGE = """<!doctype html><meta charset=utf-8><title>files</title><body style="background:#000">
<script>
const gettext = (text) => text;
%s
%s
window.qfm = { QfmRate };
window.progress = [];
window.manager = new QsmFileManager({
    win: window, doc: document,
    getToken: () => 'a'.repeat(32),
    createSocket: (token) => (window.lastSocket = new WebSocket(`ws://${location.host}/socket/${token}`)),
    offer: async (token) => {
        const response = await fetch('/files', { method: 'POST', body: JSON.stringify({ token }) });
        if (!response.ok) { const error = new Error('signal ' + response.status); error.status = response.status; throw error; }
        return response.json();
    },
    onVisible: () => {},
    onProgress: (value) => { window.progress.push(value); },
});
if (location.search.includes('opfs')) {
    window.showDirectoryPicker = async () => navigator.storage.getDirectory();
    window.manager.local.pickerAvailable = true;
}
window.manager.show();
</script>"""


class Stand:
    """One helper, one guest link and the page's little web server."""

    def __init__(self, directory: Path, args) -> None:
        self.directory = directory
        self.home = directory / 'guest-home'
        self.home.mkdir()
        self.socket_path = directory / 'files.sock'
        self.rights = 'rw'
        self.session: FileSession | None = None
        self.answer_status = 200
        self.lock_file = directory / 'locked'
        self.environment = {**os.environ, 'HOME': str(self.home)}
        self.helper = None
        self.agent = None
        self.args = args
        self.start_helper()
        if args.channel == 'agent':
            self.start_agent()
        self.link = self.new_link()
        self.server = None

    def start_helper(self) -> None:
        self.helper = subprocess.Popen([sys.executable, '-I', str(ROOT / 'guest/qsm_desktop_files.py'), '--listen', str(self.socket_path),
                                        '--lock-state-file', str(self.lock_file), '--lock-interval', '0.2', '--home', str(self.home)],
                                       env=self.environment, cwd=self.home, stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not self.socket_path.exists():
            if time.monotonic() > deadline or self.helper.poll() is not None:
                raise RuntimeError('the helper did not start')
            time.sleep(.02)

    def stop_helper(self) -> None:
        if self.helper is not None:
            self.helper.terminate()
            self.helper.wait(timeout=10)
            self.helper = None

    def start_agent(self) -> None:
        """The real agent, with a socketpair in place of QEMU's chardev, as a pty's master."""
        import pty
        import tty
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        name = os.ttyname(slave)
        self.slave = slave
        self.agent = subprocess.Popen([self.args.agent, '--device', name, '--state-dir', str(self.directory)],
                                      stdout=subprocess.DEVNULL, env=self.environment)
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.agent_end = theirs
        master = self.master

        def write_all(data: bytes) -> None:
            view = memoryview(data)
            while view:
                view = view[os.write(master, view):]

        def forward(name, read, write) -> None:
            try:
                while block := read(1 << 16):
                    write(block)
            except OSError as error:
                print(f'stand: the pty bridge {name} ended: {error!r}', file=sys.stderr, flush=True)

        threading.Thread(target=forward, args=('up', lambda size: os.read(master, size), theirs.sendall), daemon=True).start()
        threading.Thread(target=forward, args=('down', theirs.recv, write_all), daemon=True).start()
        self.agent_socket = ours

    def new_link(self) -> QsmGuestChannel:
        if self.args.channel == 'agent':
            taken = [self.agent_socket]

            def connect(_timeout: float) -> socket.socket:
                if not taken:
                    raise OSError('the agent stream was already taken')
                return taken.pop()
            return QsmGuestChannel(connector=connect, files=True)

        def connect(timeout: float) -> socket.socket:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            connection.connect(str(self.socket_path))
            return connection
        return QsmGuestChannel(connector=connect, files=True)

    def serve(self, page: str) -> str:
        stand = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args) -> None:
                pass

            def reply(self, status: int, kind: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith('/socket/'):
                    network = stand.session.take_network() if stand.session is not None else None
                    if network is None or stand.session.token != self.path.rsplit('/', 1)[-1]:
                        self.reply(404, 'application/json', b'{}')
                        return
                    self.close_connection = True
                    self.send_response(101)
                    self.send_header('Upgrade', 'websocket')
                    self.send_header('Connection', 'Upgrade')
                    self.send_header('Sec-WebSocket-Accept', websocket_accept(self.headers['Sec-WebSocket-Key']))
                    self.end_headers()
                    with network:
                        relay(self.connection, network)
                    return
                self.reply(200, 'text/html; charset=utf-8', page.encode())

            def do_POST(self) -> None:  # noqa: N802
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if stand.answer_status != 200:
                    self.reply(stand.answer_status, 'application/json', b'{}')
                    return
                session = FileSession(stand.link, stand.rights)
                stand.session = session
                try:
                    session.start()
                except Exception:  # noqa: BLE001 - as the signal service does
                    self.reply(502, 'application/json', b'{}')
                    return
                self.reply(200, 'application/json', json.dumps({'socket': session.token, 'rights': stand.rights}).encode())

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f'http://127.0.0.1:{self.server.server_address[1]}/'

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        if self.server is not None:
            self.server.shutdown()
        self.link.close()
        self.stop_helper()
        if self.agent is not None:
            self.agent.kill()
            self.agent.wait(timeout=5)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(MIB):
            value.update(block)
    return value.hexdigest()


def tree(root: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    for path in sorted(root.rglob('*')):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            continue
        result[relative] = digest(path) if path.is_file() else 'dir'
    return result


def make_bytes(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)


# Page helpers: seed and read the origin private file system that plays the local disk.
OPFS = """
window.opfs = {
    async dir(parts, create = true) {
        let handle = await navigator.storage.getDirectory();
        for (const part of parts) { handle = await handle.getDirectoryHandle(part, { create }); }
        return handle;
    },
    async write(path, size, seed) {
        const parts = path.split('/');
        const dir = await this.dir(parts.slice(0, -1));
        const handle = await dir.getFileHandle(parts[parts.length - 1], { create: true });
        const writer = await handle.createWritable();
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
    async list(path = '') {
        const parts = path ? path.split('/') : [];
        const dir = await this.dir(parts, false);
        const out = [];
        for await (const [name, handle] of dir.entries()) { out.push(handle.kind === 'directory' ? name + '/' : name); }
        return out.sort();
    },
    async clear() {
        const root = await navigator.storage.getDirectory();
        for await (const [name] of root.entries()) { await root.removeEntry(name, { recursive: true }); }
    },
};
"""


class Checks:
    def __init__(self, name: str) -> None:
        self.name = name
        self.results: list[dict] = []

    def add(self, name: str, ok: bool, detail: object = None) -> None:
        self.results.append({'check': name, 'ok': bool(ok), 'detail': detail})
        print(f"[{self.name}] {'ok  ' if ok else 'FAIL'} {name}{'' if detail is None else ' ' + json.dumps(detail, ensure_ascii=False, default=str)[:1800]}", flush=True)


async def pane_rows(page, side: str) -> list[str]:
    return await page.evaluate("""(side) => window.manager[side].visible.map((entry) => entry.name)""", side)


async def wait_pane(page, side: str, name: str, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in await pane_rows(page, side):
            return True
        await asyncio.sleep(.05)
    return False


async def jobs_done(page, timeout: float = 600) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await page.evaluate("""() => window.manager.jobs.map((job) => ({ label: job.label, state: job.state, done: job.done,
            total: job.total, files: job.files, filesDone: job.filesDone, skipped: job.skipped,
            errors: job.errors.map(({ path, error }) => ({ path, code: error.code, message: error.message })) }))""")
        if state and all(item['state'] not in ('queued', 'preparing', 'running', 'locked') for item in state):
            return state
        await asyncio.sleep(.1)
    stuck = await page.evaluate("""() => { const l = window.manager.link; return l && { hidden: document.hidden, visibility: document.visibilityState,
        rate: Math.round(l.rate.rate), inflight: l.inflight, window: l.rate.window(), waiters: l.waiters.length, pending: l.pending.size, tokens: Math.round(l.tokens),
        buffered: l.files.bufferedAmount, state: l.files.readyState, closed: l.closed, jobs: window.manager.jobs.map((job) => [job.state, job.done, job.total]) }; }""")
    raise TimeoutError(f'the transfer did not finish: {stuck}')


async def clear_jobs(page) -> None:
    await page.evaluate("() => { window.manager.jobs.length = 0; window.manager.drawNow(); }")


async def select(page, side: str, *names: str) -> None:
    await page.evaluate("""([side, names]) => {
        const pane = window.manager[side];
        pane.selected = new Set(pane.visible.filter((entry) => names.includes(entry.name)));
        pane.cursor = Math.max(0, pane.visible.findIndex((entry) => names.includes(entry.name)));
        pane.render();
        pane.focus();
    }""", [side, list(names)])


async def row(page, side: str, name: str):
    await page.evaluate("""([side, name]) => {
        const pane = window.manager[side];
        const index = pane.visible.findIndex((entry) => entry.name === name);
        pane.scroller.scrollTop = Math.max(0, index * 26 - 26);
        pane.render();
    }""", [side, name])
    await asyncio.sleep(.05)
    return page.locator(f'.qfm-{side} .qfm-row').filter(has=page.locator('.qfm-name', has_text=re.compile('^' + re.escape(name) + '$'))).first


async def chromium_scenarios(browser, stand: Stand, args, checks: Checks, report: dict) -> None:
    home = stand.home
    (home / 'docs').mkdir()
    (home / 'docs' / 'readme.txt').write_bytes(make_bytes(5000, 1))
    (home / '.secret').write_text('hidden')
    os.symlink('/nonexistent', home / 'dangling')
    os.symlink(str(home / 'docs'), home / 'docs-link')
    os.mkfifo(home / 'a-fifo')
    (home / '<img src=x onerror=window.pwned=1>').write_text('name is data')
    (home / os.fsdecode(b'bad-\xff-name')).write_bytes(b'non utf-8 name')
    (home / 'back\\slash').write_text('x')
    context = await browser.new_context(viewport={'width': 1280, 'height': 800})
    page = await context.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
    await page.add_init_script(OPFS)
    page_html = PAGE % (WAIT_FOR_ICE, manager_source())
    url = stand.serve(page_html)
    await page.goto(url + '?opfs')
    await page.evaluate('window.opfs.clear()')

    # ---- browsing
    opened = await wait_pane(page, 'guest', 'docs')
    checks.add('guest pane opens in the home folder', opened, None if opened else {
        'message': await page.locator('.qfm-guest .qfm-message').inner_text(), 'errors': errors[:5],
        'state': await page.evaluate("() => { const m = window.manager, p = window.lastSocket; return { link: !!m.link, closed: m.link && m.link.closed, reason: m.link && m.link.endReason, socket: p && p.readyState, rows: m.guest.entries.length, path: m.guest.path }; }")})
    if not opened:
        raise RuntimeError('the guest pane did not open')
    rows = await pane_rows(page, 'guest')
    checks.add('hidden files start hidden', '.secret' not in rows)
    await page.get_by_role('button', name='Show hidden files').last.click()
    checks.add('hidden toggle shows them', await wait_pane(page, 'guest', '.secret'))
    await page.get_by_role('button', name='Show hidden files').last.click()
    checks.add('a hostile file name is only text', await page.evaluate("() => window.pwned === undefined && !document.querySelector('.qfm img')"))
    bad = await page.evaluate("() => window.manager.guest.visible.find((entry) => entry.name.startsWith('bad-')).name.length")
    checks.add('a non-UTF-8 name is listed', bad > 0)
    await (await row(page, 'guest', 'docs')).dblclick()
    checks.add('double click enters a folder', await wait_pane(page, 'guest', 'readme.txt'))
    await page.get_by_role('button', name='Up one folder (Backspace)').last.click()
    checks.add('up returns', await wait_pane(page, 'guest', 'docs'))
    await (await row(page, 'guest', 'docs-link')).dblclick()
    checks.add('a link to a folder opens', await wait_pane(page, 'guest', 'readme.txt'))
    await page.get_by_role('button', name='Up one folder (Backspace)').last.click()
    await wait_pane(page, 'guest', 'docs')

    # ---- local pane
    await page.evaluate('window.opfs.write("seed/small.bin", 1000, 11)')
    await page.get_by_role('button', name='Choose folder…').click()
    checks.add('the picked folder is listed', await wait_pane(page, 'local', 'seed'))

    # ---- a tree to the guest
    plan = {'tree/a.bin': 1, 'tree/exact.bin': 60000, 'tree/over.bin': 60001, 'tree/sub/deep/b.bin': 1024 * 1024 + 7, 'tree/empty.bin': 0}
    for index, (path, size) in enumerate(plan.items()):
        await page.evaluate('([p, s, i]) => window.opfs.write(p, s, i)', [path, size, index + 100])
    await page.evaluate('window.opfs.dir(["tree", "void"])')
    await page.get_by_role('button', name='Refresh').first.click()
    await wait_pane(page, 'local', 'tree')
    await select(page, 'local', 'tree')
    await page.get_by_role('button', name='Copy to guest →').click()
    state = (await jobs_done(page))[-1]
    checks.add('a folder uploads', state['state'] == 'done' and state['files'] == len(plan), state)
    same = True
    for path in plan:
        local = await page.evaluate('(p) => window.opfs.hash(p)', path)
        same = same and local['sha256'] == digest(home / path) and local['size'] == (home / path).stat().st_size
    checks.add('every uploaded file has the same SHA-256', same)
    checks.add('an empty folder arrives', (home / 'tree/void').is_dir())
    checks.add('no temporary files remain', not [p.name for p in home.rglob('*') if '.qsm-part-' in p.name])
    await clear_jobs(page)

    # ---- the same name again: Replace, then Keep both, with the real dialog
    await page.evaluate('window.opfs.write("again.bin", 2000, 7)')
    (home / 'again.bin').write_bytes(b'old')
    await page.get_by_role('button', name='Refresh').first.click()
    await wait_pane(page, 'local', 'again.bin')
    await page.get_by_role('button', name='Refresh').last.click()
    await wait_pane(page, 'guest', 'again.bin')
    await select(page, 'local', 'again.bin')
    await page.get_by_role('button', name='Copy to guest →').click()
    await page.get_by_role('button', name='Keep both').click()
    await jobs_done(page)
    checks.add('Keep both leaves the old file', (home / 'again.bin').read_bytes() == b'old' and (home / 'again (1).bin').stat().st_size == 2000)
    await clear_jobs(page)
    await select(page, 'local', 'again.bin')
    await page.get_by_role('button', name='Copy to guest →').click()
    await page.get_by_role('button', name='Replace').click()
    await jobs_done(page)
    checks.add('Replace replaces', (home / 'again.bin').stat().st_size == 2000)
    await clear_jobs(page)

    # ---- a folder from the guest, with links, a FIFO and hostile names inside
    source = home / 'export'
    (source / 'sub').mkdir(parents=True)
    (source / 'x.bin').write_bytes(make_bytes(70000, 2))
    (source / 'sub' / 'y.bin').write_bytes(make_bytes(1 << 20, 3))
    (source / 'empty').touch()
    (source / 'void').mkdir()
    os.mkfifo(source / 'pipe')
    os.symlink('/etc/hostname', source / 'link-out')
    (source / 'back\\slash').write_text('skipped')
    await page.get_by_role('button', name='Refresh').last.click()
    await wait_pane(page, 'guest', 'export')
    await select(page, 'guest', 'export')
    await page.get_by_role('button', name='← Copy here').click()
    state = (await jobs_done(page))[-1]
    checks.add('a folder downloads and reports what it skipped', state['state'] == 'done' and state['files'] == 3 and state['skipped'] == 3, state)
    same = True
    for name in ('x.bin', 'sub/y.bin', 'empty'):
        result = await page.evaluate('(p) => window.opfs.hash(p)', f'export/{name}')
        same = same and result['sha256'] == digest(source / name)
    checks.add('every downloaded file has the same SHA-256', same)
    listing = await page.evaluate('window.opfs.list("export")')
    checks.add('a link, a FIFO and an unsafe name are not copied, an empty folder is', listing == ['empty', 'sub/', 'void/', 'x.bin'], listing)
    await clear_jobs(page)

    # ---- what the user chose survives a refresh that finishes on its own
    await select(page, 'guest', 'export')
    await page.get_by_role('button', name='Refresh').last.click()
    await asyncio.sleep(.5)
    kept = await page.evaluate("""() => { const p = window.manager.guest; return { chosen: [...p.selected].map((e) => e.name), under: p.visible[p.cursor].name }; }""")
    checks.add('a refresh of the same folder keeps the selection', kept == {'chosen': ['export'], 'under': 'export'}, kept)

    # ---- keyboard and file operations
    await page.locator('.qfm-guest .qfm-list').focus()
    await page.keyboard.press('F7')
    await page.locator('.qfm-dialog input[type=text]').fill('made-by-f7')
    await page.keyboard.press('Enter')
    checks.add('F7 makes a folder', await wait_pane(page, 'guest', 'made-by-f7') and (home / 'made-by-f7').is_dir())
    await select(page, 'guest', 'made-by-f7')
    await page.keyboard.press('F2')
    await page.locator('.qfm-dialog input[type=text]').fill('renamed')
    await page.keyboard.press('Enter')
    checks.add('F2 renames', await wait_pane(page, 'guest', 'renamed') and (home / 'renamed').is_dir())
    await select(page, 'guest', 'renamed')
    await page.keyboard.press('F2')
    await page.locator('.qfm-dialog input[type=text]').fill('../escape')
    await page.keyboard.press('Enter')
    await asyncio.sleep(.2)
    checks.add('a name with a slash is refused', (home / 'renamed').is_dir() and not (home.parent / 'escape').exists())
    await select(page, 'guest', 'renamed')
    await page.keyboard.press('Delete')
    await page.get_by_role('button', name='Delete', exact=True).last.click()
    deadline = time.monotonic() + 10
    while (home / 'renamed').exists() and time.monotonic() < deadline:
        await asyncio.sleep(.05)
    checks.add('Delete removes after asking', not (home / 'renamed').exists())
    await select(page, 'guest', 'tree')
    await page.keyboard.press('Delete')
    await page.get_by_role('button', name='Delete', exact=True).last.click()
    deadline = time.monotonic() + 10
    while (home / 'tree').exists() and time.monotonic() < deadline:
        await asyncio.sleep(.05)
    checks.add('a whole folder deletes', not (home / 'tree').exists())
    target = home / 'keep-target'
    target.write_text('keep')
    os.symlink(str(target), home / 'link-to-delete')
    await page.get_by_role('button', name='Refresh').last.click()
    await wait_pane(page, 'guest', 'link-to-delete')
    await select(page, 'guest', 'link-to-delete')
    await page.keyboard.press('Delete')
    await page.get_by_role('button', name='Delete', exact=True).last.click()
    await asyncio.sleep(.5)
    checks.add('deleting a link leaves its target', target.read_text() == 'keep' and not (home / 'link-to-delete').is_symlink())
    await page.locator('.qfm-local .qfm-list').focus()
    await page.keyboard.press('Tab')
    checks.add('Tab moves to the other pane', await page.evaluate("() => document.activeElement.closest('.qfm-guest') !== null"))

    # ---- a large file both ways
    await page.evaluate('([s]) => window.opfs.write("large.bin", s, 77)', [args.large * MIB])
    await page.get_by_role('button', name='Refresh').first.click()
    await wait_pane(page, 'local', 'large.bin')
    await select(page, 'local', 'large.bin')
    started = time.monotonic()
    cpu_before = time.process_time()
    await page.get_by_role('button', name='Copy to guest →').click()
    state = (await jobs_done(page, 1800))[-1]
    upload = time.monotonic() - started
    local = await page.evaluate('window.opfs.hash("large.bin")')
    checks.add(f'{args.large} MiB uploads intact', state['state'] == 'done' and local['sha256'] == digest(home / 'large.bin'), {'seconds': round(upload, 1), 'MiB_per_s': round(args.large / upload, 1)})
    report['upload_mib_per_s'] = round(args.large / upload, 1)
    await clear_jobs(page)
    await page.evaluate('() => navigator.storage.getDirectory().then((root) => root.removeEntry("large.bin"))')
    await page.get_by_role('button', name='Refresh').last.click()
    await wait_pane(page, 'guest', 'large.bin')
    await page.get_by_role('button', name='Refresh').first.click()
    await asyncio.sleep(.3)
    await select(page, 'guest', 'large.bin')
    started = time.monotonic()
    await page.get_by_role('button', name='← Copy here').click()
    state = (await jobs_done(page, 1800))[-1]
    download = time.monotonic() - started
    local = await page.evaluate('window.opfs.hash("large.bin")')
    checks.add(f'{args.large} MiB downloads intact', state['state'] == 'done' and local['sha256'] == digest(home / 'large.bin'), {'seconds': round(download, 1), 'MiB_per_s': round(args.large / download, 1)})
    report['download_mib_per_s'] = round(args.large / download, 1)
    await clear_jobs(page)
    del cpu_before

    # ---- many small files
    await page.evaluate("""async (count) => {
        const root = await window.opfs.dir(['many']);
        const body = new Uint8Array(2048);
        for (let i = 0; i < count; i += 1) {
            const writer = await (await root.getFileHandle('f' + i, { create: true })).createWritable();
            body[0] = i & 255; body[1] = i >> 8;
            await writer.write(body);
            await writer.close();
        }
    }""", args.small)
    await page.get_by_role('button', name='Refresh').first.click()
    await wait_pane(page, 'local', 'many')
    await select(page, 'local', 'many')
    started = time.monotonic()
    await page.get_by_role('button', name='Copy to guest →').click()
    state = (await jobs_done(page, 900))[-1]
    elapsed = time.monotonic() - started
    count = len(list((home / 'many').iterdir()))
    checks.add(f'{args.small} small files upload', state['state'] == 'done' and count == args.small, {'seconds': round(elapsed, 1), 'files_per_s': round(args.small / elapsed)})
    report['small_files_per_s'] = round(args.small / elapsed)
    await clear_jobs(page)

    # ---- cancel leaves nothing behind, in both directions
    await page.evaluate('window.opfs.write("cancel.bin", 67108864, 5)')
    await page.get_by_role('button', name='Refresh').first.click()
    await wait_pane(page, 'local', 'cancel.bin')
    await select(page, 'local', 'cancel.bin')
    await page.get_by_role('button', name='Copy to guest →').click()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not await page.evaluate("() => window.manager.jobs[0] && window.manager.jobs[0].done > 1048576"):
        await asyncio.sleep(.02)
    await page.get_by_role('button', name='Cancel', exact=True).click()
    state = (await jobs_done(page))[-1]
    await asyncio.sleep(.3)
    leftovers = [p.name for p in home.iterdir() if '.qsm-part-' in p.name]
    checks.add('cancelling an upload leaves no file', state['state'] == 'cancelled' and not (home / 'cancel.bin').exists() and not leftovers, {'state': state['state'], 'left': leftovers})
    await clear_jobs(page)
    await select(page, 'guest', 'large.bin')
    await page.get_by_role('button', name='← Copy here').click()
    await page.get_by_role('button', name='Replace').click() if await page.get_by_role('button', name='Replace').count() else None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and await page.locator('.qfm-dialog').count() == 0 and not await page.evaluate("() => window.manager.jobs[0] && window.manager.jobs[0].done > 1048576"):
        await asyncio.sleep(.02)
    if await page.locator('.qfm-dialog').count():
        await page.get_by_role('button', name='Replace').click()
        while time.monotonic() < deadline and not await page.evaluate("() => window.manager.jobs[0] && window.manager.jobs[0].done > 1048576"):
            await asyncio.sleep(.02)
    await page.get_by_role('button', name='Cancel', exact=True).click()
    state = (await jobs_done(page))[-1]
    local = await page.evaluate('window.opfs.hash("large.bin")')
    checks.add('cancelling a download keeps the existing local file whole', state['state'] == 'cancelled' and local['sha256'] == digest(home / 'large.bin'), state['state'])
    await clear_jobs(page)

    # ---- lock: browsing is refused and a transfer waits, then goes on
    stand.lock_file.write_text('1')
    await asyncio.sleep(2.5)
    await page.get_by_role('button', name='Refresh').last.click()
    await asyncio.sleep(.5)
    checks.add('a locked guest refuses browsing and says why', 'locked' in (await page.locator('.qfm-report, .qfm-guest .qfm-message').all_inner_texts() or [''])[0] + ' '.join(await page.locator('.qfm-guest .qfm-message, .qfm-report').all_inner_texts()))
    await page.evaluate('window.opfs.write("after-lock.bin", 4096, 9)')
    await page.get_by_role('button', name='Refresh').first.click()
    await wait_pane(page, 'local', 'after-lock.bin')
    await select(page, 'local', 'after-lock.bin')
    await page.get_by_role('button', name='Copy to guest →').click()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not await page.evaluate("() => window.manager.jobs[0] && window.manager.jobs[0].state === 'locked'"):
        await asyncio.sleep(.05)
    checks.add('a transfer pauses while locked and writes nothing', await page.evaluate("() => window.manager.jobs[0].state") == 'locked' and not (home / 'after-lock.bin').exists())
    # A folder that cannot be opened must not move the pane: an action names files by its path.
    shown = await page.evaluate('window.manager.guest.path')
    await (await row(page, 'guest', 'docs')).dblclick()
    await asyncio.sleep(.8)
    checks.add('a folder that fails to open leaves the pane where it was',
               await page.evaluate('window.manager.guest.path') == shown and 'docs' in await pane_rows(page, 'guest'))
    stand.lock_file.unlink()
    state = (await jobs_done(page, 60))[-1]
    checks.add('it continues after unlock', state['state'] == 'done' and (home / 'after-lock.bin').stat().st_size == 4096, state['state'])
    await clear_jobs(page)
    await asyncio.sleep(.5)

    # A download goes where the user was when asking, even if the pane moves on.
    (home / 'moved.bin').write_bytes(make_bytes(400000, 44))
    await page.evaluate('window.opfs.dir(["elsewhere"])')
    await page.get_by_role('button', name='Refresh').first.click()
    await wait_pane(page, 'local', 'elsewhere')
    await page.get_by_role('button', name='Refresh').last.click()
    await wait_pane(page, 'guest', 'moved.bin')
    await select(page, 'guest', 'moved.bin')
    await page.get_by_role('button', name='← Copy here').click()
    await (await row(page, 'local', 'elsewhere')).dblclick()
    await jobs_done(page, 60)
    await clear_jobs(page)
    in_root = await page.evaluate('window.opfs.list("")')
    in_other = await page.evaluate('window.opfs.list("elsewhere")')
    checks.add('a download lands in the folder it was asked for', 'moved.bin' in in_root and 'moved.bin' not in in_other, {'other': in_other})
    await page.get_by_role('button', name='Up one folder (Backspace)').first.click()
    await wait_pane(page, 'local', 'elsewhere')
    await page.evaluate("() => { window.manager.reportBar.style.display = 'none'; }")

    # ---- read-only rights: the helper enforces them, not only the buttons
    await page.evaluate('window.manager.link.close()')
    stand.rights = 'r'
    await page.get_by_role('button', name='Refresh').last.click()
    await page.wait_for_function("window.manager.link && !window.manager.link.closed && window.manager.rights === 'r'", timeout=30000)
    await wait_pane(page, 'guest', 'docs')
    await asyncio.sleep(.2)
    checks.add('read-only: write buttons are off', await page.get_by_role('button', name='Copy to guest →').is_disabled())
    forced = await page.evaluate("""async () => { try { await window.manager.link.call('mkdir', { path: window.manager.guest.path + '/forced', parents: false }); return 'allowed'; } catch (error) { return error.code; } }""")
    checks.add('read-only: the helper refuses a forged write', forced == 'ERIGHTS' and not (home / 'forced').exists(), forced)
    await select(page, 'guest', 'docs')
    await page.get_by_role('button', name='← Copy here').click()
    state = (await jobs_done(page))[-1]
    checks.add('read-only: downloads still work', state['state'] == 'done', state['state'])
    await clear_jobs(page)

    # ---- no helper, and a signal refusal: the page says what to do
    async def next_message() -> str:
        """What the page says after the current text has been cleared."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = await page.evaluate("""() => [window.manager.reportBar.style.display === 'none' ? '' : window.manager.reportText.textContent,
                window.manager.guest.message.style.display === 'none' ? '' : window.manager.guest.message.textContent].join(' ')""")
            if message.strip():
                return message.strip()
            await asyncio.sleep(.05)
        return ''

    clear = "() => { const m = window.manager; m.reportBar.style.display = 'none'; m.guest.showMessage(''); }"
    await page.evaluate('window.manager.link.close()')
    stand.rights = 'rw'
    stand.stop_helper()
    await asyncio.sleep(.3)
    await page.evaluate(clear)
    await page.get_by_role('button', name='Refresh').last.click()
    message = await next_message()
    wanted = 'file service' if args.channel == 'agent' else 'could not start'
    checks.add('a guest without its helper says so and what to do', wanted in message, message[:220])
    report['no_helper_message'] = message[:300]
    stand.start_helper()
    await page.evaluate(clear)
    await page.get_by_role('button', name='Refresh').last.click()
    await page.wait_for_function('window.manager.link && !window.manager.link.closed', timeout=30000)
    checks.add('it recovers once the helper is back', await wait_pane(page, 'guest', 'docs'))
    await page.evaluate('window.manager.link.close()')
    await page.evaluate(clear)
    stand.answer_status = 403
    await page.get_by_role('button', name='Refresh').last.click()
    message = await next_message()
    checks.add('a role without the file privilege is told which one', 'VM.GuestAgent.FileRead' in message, message[:220])
    stand.answer_status = 200

    progress = await page.evaluate('window.progress')
    checks.add('the toolbar saw progress and an end', any(isinstance(item, int) and item > 0 for item in progress) and progress[-1] is None, progress[-5:])
    errors = [e for e in errors if 'Failed to load resource' not in e]  # The refusals this run asked for.
    report['chromium_page_errors'] = errors
    checks.add('no page errors', not errors, errors[:5])
    await page.evaluate('window.opfs.clear()')
    await context.close()


async def firefox_scenarios(browser, stand: Stand, args, checks: Checks, report: dict) -> None:
    home = stand.home
    (home / 'ff').mkdir()
    (home / 'ff' / 'one.bin').write_bytes(make_bytes(300000, 21))
    (home / 'ff' / 'sub').mkdir()
    (home / 'ff' / 'sub' / 'two.bin').write_bytes(make_bytes(70000, 22))
    context = await browser.new_context(viewport={'width': 1280, 'height': 800}, accept_downloads=True)
    page = await context.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.add_init_script(OPFS)
    url = stand.serve(PAGE % (WAIT_FOR_ICE, manager_source()))
    await page.goto(url)
    checks.add('the guest pane opens', await wait_pane(page, 'guest', 'ff'))
    checks.add('no folder picker here, so items are added', await page.get_by_role('button', name='Add files…').count() == 1)
    with tempfile.TemporaryDirectory() as directory:
        picked = Path(directory) / 'picked.bin'
        picked.write_bytes(make_bytes(100000, 23))
        await page.locator('.qfm-local input[type=file]').first.set_input_files(str(picked))
        checks.add('an added file is listed', await wait_pane(page, 'local', 'picked.bin'))
        await select(page, 'local', 'picked.bin')
        await page.get_by_role('button', name='Copy to guest →').click()
        state = (await jobs_done(page))[-1]
        checks.add('an added file uploads intact', state['state'] == 'done' and digest(home / 'picked.bin') == digest(picked), state['state'])
    await clear_jobs(page)
    await page.get_by_role('button', name='Refresh').last.click()
    await wait_pane(page, 'guest', 'picked.bin')
    await select(page, 'guest', 'picked.bin')
    async with page.expect_download(timeout=60000) as info:
        await page.get_by_role('button', name='← Download').click()
    download = await info.value
    saved = Path(await download.path())
    checks.add('one file arrives as itself', download.suggested_filename == 'picked.bin' and digest(saved) == digest(home / 'picked.bin'), download.suggested_filename)
    await clear_jobs(page)
    await select(page, 'guest', 'ff')
    async with page.expect_download(timeout=120000) as info:
        await page.get_by_role('button', name='← Download').click()
    download = await info.value
    saved = Path(await download.path())
    with tarfile.open(saved) as archive:
        names = sorted(member.name for member in archive.getmembers())
        one = hashlib.sha256(archive.extractfile('ff/one.bin').read()).hexdigest()
        two = hashlib.sha256(archive.extractfile('ff/sub/two.bin').read()).hexdigest()
    checks.add('a folder arrives as one tar', download.suggested_filename == 'ff.tar' and names == ['ff', 'ff/one.bin', 'ff/sub', 'ff/sub/two.bin'], names)
    checks.add('the tar holds the same bytes', one == digest(home / 'ff/one.bin') and two == digest(home / 'ff/sub/two.bin'))
    await clear_jobs(page)
    started = time.monotonic()
    (home / 'ff-large.bin').write_bytes(make_bytes(64 * MIB, 24))
    await page.get_by_role('button', name='Refresh').last.click()
    await wait_pane(page, 'guest', 'ff-large.bin')
    await select(page, 'guest', 'ff-large.bin')
    started = time.monotonic()
    async with page.expect_download(timeout=300000) as info:
        await page.get_by_role('button', name='← Download').click()
    download = await info.value
    elapsed = time.monotonic() - started
    checks.add('64 MiB downloads through the spool', digest(Path(await download.path())) == digest(home / 'ff-large.bin'), {'MiB_per_s': round(64 / elapsed, 1)})
    report['firefox_download_mib_per_s'] = round(64 / elapsed, 1)
    checks.add('no page errors', not errors, errors[:5])
    report['firefox_page_errors'] = errors
    await context.close()


class VmStand:
    """A VM whose agent and helper already run; only the chardev and SSH are on this side."""

    def __init__(self, args) -> None:
        self.args = args
        self.rights = 'rw'
        self.answer_status = 200
        self.session = None
        self.server = None
        descriptor = os.open(args.chardev, os.O_PATH | os.O_CLOEXEC)

        def connect(timeout: float) -> socket.socket:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            connection.connect(f'/proc/self/fd/{descriptor}')
            return connection
        self.link = QsmGuestChannel(connector=connect, files=True)

    serve = Stand.serve

    def ssh(self, command: str) -> str:
        return subprocess.run([*self.args.ssh.split(), command], check=True, capture_output=True, text=True, timeout=600).stdout

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
        if self.server is not None:
            self.server.shutdown()
        self.link.close()


async def vm_scenarios(browser, stand: VmStand, args, checks: Checks, report: dict) -> None:
    stand.ssh('rm -rf ~/vmtest && mkdir -p ~/vmtest/down && head -c %d /dev/urandom > ~/vmtest/down/large.bin' % (args.large * MIB))
    context = await browser.new_context(viewport={'width': 1280, 'height': 800})
    page = await context.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.add_init_script(OPFS)
    await page.goto(stand.serve(PAGE % (WAIT_FOR_ICE, manager_source())) + '?opfs')
    await page.evaluate('window.opfs.clear()')
    checks.add('the guest pane opens over the virtio port', await wait_pane(page, 'guest', 'vmtest', 40))
    await page.get_by_role('button', name='Choose folder…').click()
    await (await row(page, 'guest', 'vmtest')).dblclick()
    await wait_pane(page, 'guest', 'down')
    await (await row(page, 'guest', 'down')).dblclick()
    await wait_pane(page, 'guest', 'large.bin')

    async def clipboard_during(operation) -> dict:
        """Clipboard round trips share the port with file frames. How long do they wait now?"""
        stop = threading.Event()
        samples: list[float] = []
        failures: list[str] = []

        def probe() -> None:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    stand.link.dispatch({'op': 'status'})
                    samples.append((time.monotonic() - started) * 1000)
                except Exception as error:  # noqa: BLE001
                    failures.append(type(error).__name__)
                time.sleep(.05)
        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        try:
            await operation()
        finally:
            stop.set()
            thread.join(timeout=10)
        samples.sort()
        return {'count': len(samples), 'median_ms': round(samples[len(samples) // 2], 1) if samples else None,
                'p95_ms': round(samples[int(len(samples) * .95)], 1) if samples else None, 'failures': failures[:5]}

    # ---- guest to browser
    await select(page, 'guest', 'large.bin')
    started = time.monotonic()

    async def download() -> None:
        await page.get_by_role('button', name='← Copy here').click()
        report['vm_down_state'] = (await jobs_done(page, 1800))[-1]
    report['agent_requests_during_download'] = await clipboard_during(download)
    elapsed = time.monotonic() - started
    local = await page.evaluate('window.opfs.hash("large.bin")')
    remote = stand.ssh('sha256sum ~/vmtest/down/large.bin').split()[0]
    checks.add(f'{args.large} MiB leaves the VM intact', report['vm_down_state']['state'] == 'done' and local['sha256'] == remote, {'MiB_per_s': round(args.large / elapsed, 1)})
    report['vm_download_mib_per_s'] = round(args.large / elapsed, 1)
    await clear_jobs(page)

    # ---- browser to guest
    await page.evaluate('() => navigator.storage.getDirectory().then((root) => root.removeEntry("large.bin"))')
    await page.evaluate('([s]) => window.opfs.write("up.bin", s, 77)', [args.large * MIB])
    await page.get_by_role('button', name='Refresh').first.click()
    await wait_pane(page, 'local', 'up.bin')
    await select(page, 'local', 'up.bin')
    started = time.monotonic()

    async def upload() -> None:
        await page.get_by_role('button', name='Copy to guest →').click()
        report['vm_up_state'] = (await jobs_done(page, 1800))[-1]
    report['agent_requests_during_upload'] = await clipboard_during(upload)
    elapsed = time.monotonic() - started
    local = await page.evaluate('window.opfs.hash("up.bin")')
    remote = stand.ssh('sha256sum ~/vmtest/down/up.bin').split()[0]
    checks.add(f'{args.large} MiB enters the VM intact', report['vm_up_state']['state'] == 'done' and local['sha256'] == remote, {'MiB_per_s': round(args.large / elapsed, 1)})
    report['vm_upload_mib_per_s'] = round(args.large / elapsed, 1)
    await clear_jobs(page)

    # ---- many small files, where each one costs round trips through the port
    await page.evaluate("""async (count) => {
        const root = await window.opfs.dir(['many']);
        const body = new Uint8Array(2048);
        for (let i = 0; i < count; i += 1) {
            const writer = await (await root.getFileHandle('f' + i, { create: true })).createWritable();
            body[0] = i & 255; body[1] = i >> 8;
            await writer.write(body);
            await writer.close();
        }
    }""", args.small)
    await page.get_by_role('button', name='Refresh').first.click()
    await wait_pane(page, 'local', 'many')
    await select(page, 'local', 'many')
    started = time.monotonic()
    await page.get_by_role('button', name='Copy to guest →').click()
    state = (await jobs_done(page, 900))[-1]
    elapsed = time.monotonic() - started
    count = int(stand.ssh('ls ~/vmtest/down/many | wc -l'))
    checks.add(f'{args.small} small files enter the VM', state['state'] == 'done' and count == args.small, {'files_per_s': round(args.small / elapsed)})
    report['vm_small_files_per_s'] = round(args.small / elapsed)
    left = stand.ssh('find ~/vmtest -name ".*qsm-part-*" | wc -l').strip()
    checks.add('no temporary files remain in the VM', left == '0', left)
    report['vm_page_errors'] = errors
    checks.add('no page errors', not errors, errors[:5])
    await page.evaluate('window.opfs.clear()')
    await context.close()
    stand.ssh('rm -rf ~/vmtest')


async def run_vm(args) -> dict:
    from playwright.async_api import async_playwright
    report: dict = {'date': time.strftime('%Y-%m-%d %H:%M:%S'), 'channel': 'virtio', 'large_mib': args.large, 'small_files': args.small}
    checks = Checks('vm')
    stand = VmStand(args)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=CHROME, headless=False,
                env={**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':97')}, args=['--no-sandbox', '--no-first-run'])
            report['chromium_version'] = browser.version
            await vm_scenarios(browser, stand, args, checks, report)
            await browser.close()
    finally:
        report['checks'] = checks.results
        report['failed'] = [item for item in checks.results if not item['ok']]
        stand.close()
    return report


async def run(args) -> dict:
    from playwright.async_api import async_playwright
    report: dict = {'date': time.strftime('%Y-%m-%d %H:%M:%S'), 'channel': args.channel, 'large_mib': args.large, 'small_files': args.small}
    all_checks = []
    async with async_playwright() as playwright:
        for name in (['chromium', 'firefox'] if args.browser == 'both' else [args.browser]):
            directory = Path(tempfile.mkdtemp(prefix='qfm-', dir=args.work))
            stand = Stand(directory, args)
            checks = Checks(name)
            try:
                if name == 'chromium':
                    browser = await playwright.chromium.launch(executable_path=CHROME, headless=False,
                        env={**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':97')}, args=['--no-sandbox', '--no-first-run'])
                    report['chromium_version'] = browser.version
                    await chromium_scenarios(browser, stand, args, checks, report)
                else:
                    browser = await playwright.firefox.launch(executable_path=FIREFOX, headless=True,
                        firefox_user_prefs={'media.peerconnection.ice.obfuscate_host_addresses': False, 'media.peerconnection.ice.loopback': True})
                    report['firefox_version'] = browser.version
                    await firefox_scenarios(browser, stand, args, checks, report)
                await browser.close()
            except Exception as error:  # noqa: BLE001 - the report must say how far it got
                checks.add('the run itself finished', False, f'{type(error).__name__}: {error}')
            finally:
                all_checks.extend({**item, 'browser': name} for item in checks.results)
                report['checks'] = all_checks
                stand.close()
                shutil.rmtree(directory, ignore_errors=True)
    report['failed'] = [item for item in all_checks if not item['ok']]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--browser', default='both', choices=('chromium', 'firefox', 'both'))
    parser.add_argument('--large', type=int, default=256)
    parser.add_argument('--small', type=int, default=2000)
    parser.add_argument('--channel', default='unix', choices=('unix', 'agent'))
    parser.add_argument('--agent', default='')
    parser.add_argument('--work', default=None)
    parser.add_argument('--chardev', default=None, help='a VM agent chardev socket')
    parser.add_argument('--ssh', default=None, help='the command that reaches that VM')
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    report = {}
    try:
        report = asyncio.run(run_vm(args) if args.chardev else run(args))
    finally:
        args.output.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    print(json.dumps({key: report[key] for key in report if key != 'checks'}, indent=1, ensure_ascii=False, default=str))
    return 1 if report.get('failed') else 0


if __name__ == '__main__':
    raise SystemExit(main())
