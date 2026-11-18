// Headless two-Console fixture for tests/browser_bridge/test_two_console_headless.py
// (contract CONSOLE-ACTIVE-SIZE-1).
//
// Runs the console's own resize, role, control-channel and decoded-frame code
// (the same production parts test_resize_reference.mjs extracts) for several
// Consoles on one fake clock and ONE fake display: qdev-host keeps the last
// accepted size (a repeat is UNCHANGED) and the guest presents an accepted
// size after its hot-plug cycle, to every Console.  The terminal is not faked
// here: every message a Console sends goes to the Python driver, which feeds
// it into real BrowserWebRtcBridge peers sharing one UnixInputEgress, and the
// display applies only the size requests that egress forwards.  Data channels,
// clock and display are doubles: not end-to-end evidence.
//
// Line protocol: one JSON command per stdin line, one JSON answer per line.
import fs from 'node:fs';
import readline from 'node:readline';

const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const part = (a, b) => {
    const start = source.indexOf(a);
    if (start < 0) { throw new Error(`console source lacks ${a}`); }
    const end = source.indexOf(b, start);
    if (end <= start) { throw new Error(`console source lacks ${b} after ${a}`); }
    return source.slice(start, end);
};
const make = new Function('env', `
const {video, control, popup, send, settings, status, peer, clock, guest, counters, ResizeObserver} = env;
const Date = { now: () => clock.now };
const HTMLMediaElement = { HAVE_CURRENT_DATA: 2 };
const gettext = (text) => text;
const sessionFps = 60;
let closed = false, guestAsleepHint = false, firstFrameTimer = null, observer = null;
let lastResize = '', lastAppliedSize = '', resizeConsole = () => undefined;
let exactUiRequested = null;
const updateExactPreference = () => {};
const remoteVideoWidth = () => guest.decoded ? guest.width : 0;
const remoteVideoHeight = () => guest.decoded ? guest.height : 0;
const guestFrameIsDark = () => false;
const revealToolbar = () => { counters.reveals += 1; };
const stopDiag = () => {}, startLivenessMonitor = () => {}, hideToolbarSoon = () => {};
const syncExactGeometry = () => {}, placeGuestCursor = () => {}, reflowView = () => {};
${part('    const dimensions = (video, fps) => {', '    // kind: ')}
${part('let resizeTimer = null;', 'let firstFrameTimer = null;')}
${part('const updateMediaStatus = () => {', 'const iceConfiguration = async')}
${part('const resize = (immediate = false) => {', 'const flushPointer = (reliable = false) => {')}
${part("control.addEventListener('open',", "control.addEventListener('message',")}
${part("control.addEventListener('message', (event) => {", '// aiortc closes every server-side data channel')}
${part('observer = new ResizeObserver(', '// A VM has one Display1 scanout')}
${part("video.addEventListener('resize', () => {", '// Control follows the pointer into the console')}
return { input: () => noteGuestInput() };
`);

const target = () => {
    const listeners = new Map();
    return {
        addEventListener(name, fn) { if (!listeners.has(name)) { listeners.set(name, []); } listeners.get(name).push(fn); },
        emit(name, event) { for (const fn of listeners.get(name) || []) { fn(event); } },
    };
};

const clock = { now: 1_000_000 };
const timers = new Map();
let timerId = 0;
const consoles = new Map();
let outbox = [];
const guest = { width: 1920, height: 1080, decoded: true };
const display = { accepted: '1920x1080', modeChanges: 0, unchanged: 0, log: [] };
const APPLY_MS = 1500;

const setTimeout_ = (fn, ms) => { timers.set(++timerId, { fn, at: clock.now + ms }); return timerId; };
const tick = (ms) => {
    const end = clock.now + ms;
    for (;;) {
        let next = null;
        for (const [n, timer] of timers) { if (timer.at <= end && (!next || timer.at < next[1].at)) { next = [n, timer]; } }
        if (!next) { break; }
        timers.delete(next[0]);
        clock.now = Math.max(clock.now, next[1].at);
        next[1].fn();
    }
    clock.now = end;
};
const present = (width, height) => {
    if (guest.width === width && guest.height === height) { return; }
    guest.width = width; guest.height = height;
    for (const c of consoles.values()) { if (!c.closed) { c.video.emit('resize'); } }
};
const apply = (width, height) => {
    const size = `${width}x${height}`;
    if (size === display.accepted) { display.unchanged += 1; return; }
    display.accepted = size;
    display.modeChanges += 1;
    display.log.push({ at: clock.now - 1_000_000, size });
    setTimeout_(() => { if (display.accepted === size) { present(width, height); } }, APPLY_MS);
};

const attach = (name, [width, height]) => {
    const video = { ...target(), width: 0, height: 0, readyState: 4, box: { width, height },
        getBoundingClientRect() { return this.box; } };
    const control = target();
    const observers = [];
    class FakeResizeObserver { constructor(fn) { this.fn = fn; observers.push(this); } observe() {} disconnect() {} }
    const popup = { ...target(), setTimeout: setTimeout_, clearTimeout: (n) => timers.delete(n) };
    const c = { name, video, control, popup, observers, closed: false, sequence: 0, status: { textContent: '' } };
    c.state = make({ video, control, popup, status: c.status, guest, clock, counters: { reveals: 0 },
        ResizeObserver: FakeResizeObserver, peer: { connectionState: 'connected' },
        settings: { resizeDebounceMs: 400, resizeSettleMs: 1000 },
        send: (value) => {
            if (c.closed) { return false; }
            outbox.push({ peer: name, channel: 'control', data: JSON.stringify(value) });
            return true;
        } });
    consoles.set(name, c);
    control.emit('open');
};
const layout = (c, [width, height]) => {
    c.video.box = { width, height };
    c.popup.emit('resize');
    for (const observer of c.observers) { observer.fn([]); }
};
// The user moves the pointer over this Console's video (qsm-pointer channel).
const input = (c) => {
    c.state.input();
    c.sequence += 1;
    outbox.push({ peer: c.name, channel: 'pointer', data: JSON.stringify({ op: 'mouse_position', x: 10, y: 10,
        width: guest.width, height: guest.height, sequence: c.sequence }) });
};

const handle = (command) => {
    const c = command.peer !== undefined ? consoles.get(command.peer) : null;
    switch (command.cmd) {
    case 'attach': attach(command.peer, command.box); break;
    case 'deliver': if (!c.closed) { c.control.emit('message', { data: command.data }); } break;
    case 'apply': apply(command.width, command.height); break;
    case 'layout': layout(c, command.box); break;
    case 'input': input(c); break;
    case 'tick': tick(command.ms); break;
    case 'close': c.closed = true; break;
    case 'state': break;
    default: throw new Error(`unknown command ${command.cmd}`);
    }
    const answer = { outbox, guest: `${guest.width}x${guest.height}`, display };
    outbox = [];
    return answer;
};

const lines = readline.createInterface({ input: process.stdin });
lines.on('line', (line) => {
    let answer;
    try { answer = handle(JSON.parse(line)); } catch (error) { answer = { error: String(error && error.stack || error) }; }
    process.stdout.write(`${JSON.stringify(answer)}\n`);
});
