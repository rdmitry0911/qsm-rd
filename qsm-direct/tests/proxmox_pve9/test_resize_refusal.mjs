// Contract UIINFO-REFUSAL-1 in the console: the display's error reply to a
// size request (qsm_resize_result, from Display1 Console.SetUIInfo) is the
// terminal answer for that size.  Runs the console's own resize, retry,
// status and control-message code with fake timers and input.
import assert from 'node:assert/strict';
import fs from 'node:fs';
const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const part = (a, b) => {
    const start = source.indexOf(a);
    assert.ok(start >= 0, `console source lacks ${a}`);
    return source.slice(start, source.indexOf(b, start));
};
const make = new Function('env', `
const {video, control, popup, send, settings, status, peer, clock, guest, counters} = env;
const Date = { now: () => clock.now };
const HTMLMediaElement = { HAVE_CURRENT_DATA: 2 };
const gettext = (text) => text;
const sessionFps = 60;
let closed = false, guestAsleepHint = false, firstFrameTimer = null;
let lastResizeSentAt = 0, lastResize = '', lastAppliedSize = '', resizeConsole = () => undefined;
const remoteVideoWidth = () => guest.width, remoteVideoHeight = () => guest.height;
const guestFrameIsDark = () => false;
const revealToolbar = () => { counters.reveals += 1; };
const stopDiag = () => {}, startLivenessMonitor = () => {}, hideToolbarSoon = () => {};
const syncExactGeometry = () => {}, placeGuestCursor = () => {}, reflowView = () => {};
${part('    const dimensions = (video, fps) => {', '    // kind: ')}
${part('let resizeTimer = null;', 'let lastResizeSentAt = 0;')}
${part('const updateMediaStatus = () => {', 'const iceConfiguration = async')}
${part('const resize = (immediate = false) => {', 'const flushPointer = (reliable = false) => {')}
${part("control.addEventListener('message', (event) => {", '// aiortc closes every server-side data channel')}
${part("video.addEventListener('resize', () => {", '// Control follows the pointer into the console')}
return { resize: (immediate) => resizeConsole(immediate), input: () => noteGuestInput(), updateMediaStatus };
`);

const target = () => {
    const listeners = new Map();
    return {
        addEventListener(name, fn) { if (!listeners.has(name)) { listeners.set(name, []); } listeners.get(name).push(fn); },
        emit(name, event) { for (const fn of listeners.get(name) || []) { fn(event); } },
    };
};

function harness({ box = [1630, 572], guestSize = [1280, 800] } = {}) {
    const clock = { now: 1_000_000 };
    let id = 0;
    const timers = new Map();
    const sent = [];
    const guest = { width: guestSize[0], height: guestSize[1] };
    const video = { ...target(), width: 0, height: 0, readyState: 4, box: { width: box[0], height: box[1] },
        getBoundingClientRect() { return this.box; } };
    const control = target();
    const status = { textContent: '' };
    const counters = { reveals: 0 };
    const popup = {
        setTimeout(fn, ms) { timers.set(++id, { fn, at: clock.now + ms }); return id; },
        clearTimeout(n) { timers.delete(n); },
    };
    const state = make({ video, control, popup, status, guest, clock, counters,
        peer: { connectionState: 'connected' },
        settings: { resizeDebounceMs: 400, resizeSettleMs: 1000 },
        send: (value) => { sent.push(value); return true; } });
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
    const resizes = (width, height) => sent.filter((m) => m.op === 'resize' &&
        (width === undefined || (m.width === width && m.height === height)));
    const result = (fields) => control.emit('message', { data: JSON.stringify({ op: 'qsm_resize_result', ...fields }) });
    // A user who keeps working in the console: pointer input every 100 ms and
    // a layout callback (ResizeObserver/popup resize) every second.
    const work = (ms) => {
        for (let t = 0; t < ms; t += 100) {
            state.input();
            if (t % 1000 === 0) { state.resize(false); }
            tick(100);
        }
    };
    return { ...state, clock, sent, guest, video, status, counters, tick, resizes, result, work };
}

const REFUSED = { result: 'refused', width: 1630, height: 572, error: 'org.qemu.Display1.Error.Invalid',
    message: '1630x572 refused: no matching device mode' };

{
    // Invalid: one request for the size, then nothing on timers or input.
    const h = harness();
    h.resize(true);
    assert.equal(h.resizes(1630, 572).length, 1, 'the console asks for its window size once');
    h.tick(100);
    h.result(REFUSED);
    h.work(60_000);
    assert.equal(h.resizes(1630, 572).length, 1,
        'a refused size is not re-sent by the verification timer or by input');
    assert.match(h.status.textContent, /refused this window size/);
    assert.match(h.status.textContent, /1630x572 refused: no matching device mode/,
        'the device reason is shown on the status line');
    assert.equal(h.counters.reveals, 1, 'the reason is shown once, without blocking the console');
    assert.equal(h.video.width, 1630, 'the scaled view keeps the window presentation box');
    h.updateMediaStatus();
    assert.match(h.status.textContent, /refused this window size/,
        'a later media status update keeps the reason instead of a bare Connected');

    // A new window size is a new request.
    h.video.box = { width: 1600, height: 572 };
    h.resize(false);
    h.tick(500);
    assert.equal(h.resizes(1600, 572).length, 1, 'a different size is requested again');
    assert.equal(h.status.textContent, 'Connected', 'the refusal of the old size is withdrawn');
    h.guest.width = 1600; h.guest.height = 572;
    h.video.emit('resize');
    h.work(10_000);
    assert.equal(h.resizes(1600, 572).length, 1, 'an accepted size converges as before');

    // Returning to the refused size is again a new request.
    h.video.box = { width: 1630, height: 572 };
    h.resize(false);
    h.tick(500);
    assert.equal(h.resizes(1630, 572).length, 2, 'the earlier refused size is asked for once more');
    h.result(REFUSED);
    h.work(30_000);
    assert.equal(h.resizes(1630, 572).length, 2, 'and is terminal again after the second refusal');
}

{
    // Another Console's refused size does not stop this Console's request.
    const h = harness();
    h.resize(true);
    h.result({ ...REFUSED, width: 1500, height: 500, message: '1500x500 refused' });
    h.tick(1_000);
    assert.ok(h.resizes(1630, 572).length > 1, 'a refusal of a different size leaves the retry in charge');
    assert.doesNotMatch(h.status.textContent, /refused/);
}

{
    // Unsupported: no further size request in this session, for any size.
    const h = harness();
    h.resize(true);
    h.result({ result: 'unsupported', width: 1630, height: 572,
        error: 'org.qemu.Display1.Error.Unsupported', message: 'SetUIInfo is not supported' });
    h.work(30_000);
    h.video.box = { width: 1600, height: 700 };
    h.resize(false);
    h.tick(2_000);
    h.resize(true);
    h.work(30_000);
    assert.equal(h.resizes().length, 1, 'an unsupported display is never asked again');
    assert.match(h.status.textContent, /does not follow the window size.*SetUIInfo is not supported/);
    assert.equal(h.video.width, 1600, 'the view follows the window, scaled');
}

{
    // Failed: transient, so the console retries, but within a fixed budget.
    const h = harness();
    h.resize(true);
    h.result({ result: 'failed', width: 1630, height: 572,
        error: 'org.qemu.Display1.Error.Failed', message: 'device not ready' });
    h.work(60_000);
    const budget = h.resizes(1630, 572).length;
    assert.ok(budget > 1, 'a failed request is retried');
    assert.ok(budget <= 1 + 16 + 6 * 9, `retries are bounded (sent ${budget})`);
    h.work(120_000);
    assert.equal(h.resizes(1630, 572).length, budget, 'no retry loop runs on input forever');
}

{
    // Success path: unchanged convergence on the decoded picture.
    const h = harness();
    h.resize(true);
    h.guest.width = 1630; h.guest.height = 572;
    h.video.emit('resize');
    h.work(10_000);
    assert.equal(h.resizes().length, 1);
    h.updateMediaStatus();
    assert.equal(h.status.textContent, 'Connected');
}

console.log('resize refusal: 5 scenarios passed');
