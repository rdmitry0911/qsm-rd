// Contract UIINFO-CADENCE-1: when the console asks the display for its window
// size (op 'resize' -> media worker -> Display1 Console.SetUIInfo).
//
// Reference: Apple Virtualization.framework VZVirtualMachineView, macOS 26
// (25C56-era x86_64h), decompiled on the decomp host
// (ghidra_output/vz_display_resize_20261118/h1s.log); recorded in apple-virgl
// analysis/20261118-FIX-CANDIDATE-UIINFO-HID-STOP.md sections 5 and 7.
//   R1 -[VZVirtualMachineView setFrameSize:] @0x7ffb1666d990 sends no display
//      reconfiguration while [[self window] inLiveResize].
//   R2 -[VZVirtualMachineView viewDidEndLiveResize] @0x7ffb16662c70 sends exactly
//      one _resizeDisplayMatchingFrame: for the whole drag.
//   R3 $_15 @0x7ffb166677e0 (_matchesSize:) sends nothing for a size equal to the
//      display's current configuration.
//   R4 nothing is re-sent on a timer or on input; the presented-frame lambda
//      @0x7ffb1665e700 compares the next frame and does not re-send.
// Error answers follow UIINFO-REFUSAL-1 (test_resize_refusal.mjs): Invalid is the
// terminal answer for that size, Unsupported ends size requests, Failed is
// retried within a fixed bound.
// Browser analog of inLiveResize: a browser reports no live-resize begin or end,
// so a layout size change starts or continues the live resize and the window
// size being stable for the settle window ends it.
//
// Runs the console's own resize, layout-callback, control-channel and decoded
// frame code with fake timers.  The fake display stands in for the media worker
// and qdev-host only (SetUIInfo answer, last-accepted-size dedupe, the presented
// picture after the guest's hot-plug cycle); it is not end-to-end evidence.
import assert from 'node:assert/strict';
import fs from 'node:fs';
const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const part = (a, b) => {
    const start = source.indexOf(a);
    assert.ok(start >= 0, `console source lacks ${a}`);
    const end = source.indexOf(b, start);
    assert.ok(end > start, `console source lacks ${b} after ${a}`);
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
return { input: () => noteGuestInput(), fullscreen: () => resizeConsole(true) };
`);

const target = () => {
    const listeners = new Map();
    return {
        addEventListener(name, fn) { if (!listeners.has(name)) { listeners.set(name, []); } listeners.get(name).push(fn); },
        emit(name, event) { for (const fn of listeners.get(name) || []) { fn(event); } },
    };
};

// display: the guest's size when the console starts (the previous session's
// size on a reconnect).  decoded: whether a picture was decoded before the
// control channel opened.  opened: whether the control channel is open (the
// console's send() fails before).  answer(size): how the display answers a
// SetUIInfo.
function harness({ box, display, decoded = true, opened = true, answer = () => 'apply', applyMs = 1500 }) {
    const clock = { now: 1_000_000 };
    let id = 0;
    const timers = new Map();
    const sent = [];
    const guest = { width: display[0], height: display[1], decoded };
    const channel = { open: opened };
    const video = { ...target(), width: 0, height: 0, readyState: 4, box: { width: box[0], height: box[1] },
        getBoundingClientRect() { return this.box; } };
    const control = target();
    const status = { textContent: '' };
    const counters = { reveals: 0 };
    const observers = [];
    class FakeResizeObserver {
        constructor(fn) { this.fn = fn; observers.push(this); }
        observe() {}
        disconnect() {}
    }
    const popup = {
        ...target(),
        setTimeout(fn, ms) { timers.set(++id, { fn, at: clock.now + ms }); return id; },
        clearTimeout(n) { timers.delete(n); },
    };
    // qdev-host keeps the last accepted size and answers a repeat UNCHANGED; any
    // other accepted size is one guest display hot-plug cycle, after which the
    // guest presents that size.
    const display_ = { accepted: `${display[0]}x${display[1]}`, modeChanges: 0, unchanged: 0 };
    const present = (width, height) => {
        const firstFrame = !guest.decoded;
        guest.decoded = true;
        if (!firstFrame && guest.width === width && guest.height === height) { return; }
        guest.width = width; guest.height = height;
        video.emit('resize');
    };
    const reply = (result, error, width, height) => popup.setTimeout(() => control.emit('message', { data: JSON.stringify({
        op: 'qsm_resize_result', result, width, height, error, message: `${width}x${height} ${result}` }) }), 20);
    const setUiInfo = ({ width, height }) => {
        const size = `${width}x${height}`;
        const kind = answer(size);
        if (kind === 'apply') {
            if (size === display_.accepted) { display_.unchanged += 1; return; }
            display_.accepted = size;
            display_.modeChanges += 1;
            popup.setTimeout(() => { if (display_.accepted === size) { present(width, height); } }, applyMs);
        } else if (kind === 'invalid') {
            reply('refused', 'org.qemu.Display1.Error.Invalid', width, height);
        } else if (kind === 'failed') {
            reply('failed', 'org.qemu.Display1.Error.Failed', width, height);
        } else if (kind === 'unsupported') {
            reply('unsupported', 'org.qemu.Display1.Error.Unsupported', width, height);
        }
        // 'silent': the deployed qdev-host below 800x600 logs a refusal and
        // returns success; nothing changes and no error reaches the console.
    };
    const state = make({ video, control, popup, status, guest, clock, counters, ResizeObserver: FakeResizeObserver,
        peer: { connectionState: 'connected' },
        settings: { resizeDebounceMs: 400, resizeSettleMs: 1000 },
        send: (value) => {
            if (!channel.open) { return false; }
            sent.push(value);
            if (value.op === 'resize') { setUiInfo(value); }
            return true;
        } });
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
    // The browser reports a new window size: the popup 'resize' event and the
    // ResizeObserver of the video element.
    const layout = (width, height) => {
        video.box = { width, height };
        popup.emit('resize');
        for (const observer of observers) { observer.fn([]); }
    };
    const sizes = () => sent.filter((m) => m.op === 'resize').map((m) => `${m.width}x${m.height}`);
    // A user working in the console: pointer input every 100 ms, a layout
    // callback without a size change (toolbar reflow) every second.
    const work = (ms) => {
        for (let t = 0; t < ms; t += 100) {
            state.input();
            if (t % 1000 === 0) { layout(video.box.width, video.box.height); }
            tick(100);
        }
    };
    const open = () => { channel.open = true; control.emit('open'); };
    const firstFrame = () => present(guest.width, guest.height);
    return { ...state, clock, sent, guest, video, status, counters, display: display_, tick, layout, sizes, work,
        open, firstFrame, present };
}

// An OS window drag: a new window size every stepMs.  rests maps a step index to
// a pause with the mouse button still held (the hand slows down mid-drag).
const even = (value) => Math.floor(value / 2) * 2;
const path = ([w0, h0], [w1, h1], steps) => Array.from({ length: steps }, (_, i) =>
    [even(w0 + (w1 - w0) * (i + 1) / steps), even(h0 + (h1 - h0) * (i + 1) / steps)]);
const drag = (h, sizes, { stepMs = 50, rests = new Map() } = {}) => {
    sizes.forEach(([width, height], i) => {
        h.layout(width, height);
        h.tick(stepMs);
        if (rests.has(i)) { h.tick(rests.get(i)); }
    });
};


const scenarios = [];
const scenario = (name, fn) => scenarios.push([name, fn]);

scenario('continuous drag: one request', () => {
    // R1/R2: a continuous 3 s drag (60 sizes) is one request, for the final size.
    const h = harness({ box: [1280, 800], display: [1280, 800] });
    drag(h, path([1280, 800], [1630, 572], 60));
    assert.deepEqual(h.sizes(), [], 'R1: no size is requested while the window is being resized');
    h.tick(1_000);
    assert.deepEqual(h.sizes(), ['1630x572'], 'R2: the end of the drag requests the final size once');
    h.work(20_000);
    assert.deepEqual(h.sizes(), ['1630x572'],
        'R4: the request is not re-sent while the guest runs its hot-plug cycle or afterwards');
    assert.equal(h.display.modeChanges, 1, 'one guest display mode change for the whole drag');
    assert.deepEqual([h.guest.width, h.guest.height], [1630, 572]);
    assert.equal(h.status.textContent, '', 'a converged request leaves the status line alone');
});

scenario('drag with rests: one request', () => {
    // R1/R2: the hand rests twice mid-drag (button held) for 600 ms: still one
    // drag, one request.
    const h = harness({ box: [1920, 1080], display: [1920, 1080] });
    drag(h, path([1920, 1080], [1324, 626], 60), { stepMs: 50, rests: new Map([[19, 600], [39, 600]]) });
    h.tick(1_000);
    h.work(20_000);
    assert.deepEqual(h.sizes(), ['1324x626'], 'R2: a drag with rests is still one request for its final size');
    assert.equal(h.display.modeChanges, 1, 'no intermediate size of the drag reaches the guest');
});

scenario('two drags: two requests', () => {
    // Two drags separated by a stable window are two requests, one per drag.
    const h = harness({ box: [1280, 800], display: [1280, 800] });
    drag(h, path([1280, 800], [1600, 900], 30));
    h.work(5_000);
    drag(h, path([1600, 900], [1440, 900], 30));
    h.work(5_000);
    assert.deepEqual(h.sizes(), ['1600x900', '1440x900'], 'each drag ends in exactly one request');
    assert.equal(h.display.modeChanges, 2);
});

scenario('equal size: no request', () => {
    // R3: the window is the size the guest presents: nothing is requested, for a
    // layout callback, a full-screen/maximise transition or input.
    const h = harness({ box: [1600, 900], display: [1600, 900] });
    h.layout(1600, 900);
    h.tick(2_000);
    h.fullscreen();
    h.work(30_000);
    assert.deepEqual(h.sizes(), [], 'R3: a size equal to the current one is not requested');

    // A drag that ends where it started is no request either.
    drag(h, [...path([1600, 900], [1200, 700], 20), ...path([1200, 700], [1600, 900], 20)]);
    h.work(5_000);
    assert.deepEqual(h.sizes(), [], 'R3: a drag back to the current size requests nothing');
    assert.equal(h.display.modeChanges, 0);
});

scenario('equal compares with the configuration', () => {
    // R3 compares with the display's configuration, not with the old picture:
    // while 1600x900 is being applied, a drag back to the old 1280x800 is a new
    // request; a further drag ending at 1280x800 is not.
    const h = harness({ box: [1280, 800], display: [1280, 800], applyMs: 3000 });
    drag(h, path([1280, 800], [1600, 900], 20));
    h.tick(1_000);
    assert.deepEqual(h.sizes(), ['1600x900']);
    drag(h, path([1600, 900], [1280, 800], 10));
    h.tick(1_000);
    assert.deepEqual(h.sizes(), ['1600x900', '1280x800'], 'R3: the configured size was 1600x900, so 1280x800 is sent');
    h.work(10_000);
    drag(h, [...path([1280, 800], [1300, 820], 4), [1280, 800]]);
    h.work(5_000);
    assert.deepEqual(h.sizes(), ['1600x900', '1280x800'], 'R3: then 1280x800 is current and not sent again');
    assert.deepEqual([h.guest.width, h.guest.height], [1280, 800]);
});

scenario('refused size (Invalid): no retry loop', () => {
    // UIINFO-REFUSAL-1 Invalid after a drag: one request, no retry loop.
    const h = harness({ box: [1280, 800], display: [1280, 800],
        answer: (size) => (size === '640x400' ? 'invalid' : 'apply') });
    drag(h, path([1280, 800], [640, 400], 30));
    h.work(60_000);
    assert.deepEqual(h.sizes(), ['640x400'], 'a refused size is asked for once');
    assert.match(h.status.textContent, /refused this window size/);
});

scenario('unapplied size (deployed 800x600 floor): no retry loop', () => {
    // R4 with the deployed qdev-host floor: a too-small size answered with success
    // but never applied is not re-sent on a timer, on input or on layout callbacks.
    const h = harness({ box: [1280, 800], display: [1280, 800],
        answer: (size) => (size === '640x400' ? 'silent' : 'apply') });
    drag(h, path([1280, 800], [640, 400], 30));
    h.work(60_000);
    assert.deepEqual(h.sizes(), ['640x400'], 'R4: an unapplied request is not retried');
    assert.equal(h.display.modeChanges, 0);
    assert.match(h.status.textContent, /kept its own size/, 'the console says that the guest kept its size');
    // The next drag is a new request as usual.
    drag(h, path([640, 400], [1024, 640], 10));
    h.work(5_000);
    assert.deepEqual(h.sizes(), ['640x400', '1024x640']);
});

scenario('Failed: bounded retry', () => {
    // UIINFO-REFUSAL-1 Failed: retried only on a Failed answer, within a fixed
    // bound, then never again on timers, input or layout callbacks.
    const h = harness({ box: [1280, 800], display: [1280, 800], answer: () => 'failed' });
    drag(h, path([1280, 800], [1600, 900], 20));
    h.work(60_000);
    const count = h.sizes().length;
    assert.ok(count > 1, 'a Failed answer is retried');
    assert.ok(count <= 4, `Failed retries are bounded (sent ${count})`);
    assert.ok(h.sizes().every((size) => size === '1600x900'));
    h.work(120_000);
    assert.equal(h.sizes().length, count, 'no retry loop');
});

scenario('reconnect resizes once', () => {
    // Reconnect (the console reloads): the previous session left the guest at
    // 1630x572; the reopened window is 1280x800.  The ResizeObserver reports
    // the window while ICE/DTLS still negotiate (send() fails then); the new
    // control channel asks for the window size once, at once, before any
    // picture is decoded.
    const h = harness({ box: [1280, 800], display: [1630, 572], decoded: false, opened: false });
    h.layout(1280, 800);
    h.tick(1_500);
    assert.deepEqual(h.sizes(), []);
    h.open();
    assert.deepEqual(h.sizes(), ['1280x800'], 'a reconnected console requests its window size when the channel opens');
    h.tick(50);
    h.firstFrame();
    h.work(20_000);
    assert.deepEqual(h.sizes(), ['1280x800'], 'and only once');
    assert.deepEqual([h.guest.width, h.guest.height], [1280, 800], 'the guest follows the reconnected window');
    // A drag after the reconnect is gated as before.
    drag(h, path([1280, 800], [1600, 900], 40));
    h.work(5_000);
    assert.deepEqual(h.sizes(), ['1280x800', '1600x900']);
    assert.equal(h.display.modeChanges, 2);
});

scenario('reconnect at the current size: no request', () => {
    // Reconnect when the guest already presents the window size: nothing to ask.
    const h = harness({ box: [1600, 900], display: [1600, 900], decoded: true, opened: false });
    h.layout(1600, 900);
    h.tick(1_500);
    h.open();
    h.work(10_000);
    assert.deepEqual(h.sizes(), [], 'R3: a reconnected console at the current size requests nothing');
});

scenario('another console: followed, not fought', () => {
    // R4 with two consoles: another console moves the shared guest; this one does
    // not push its own size back, and asks again only when its own window changes.
    const h = harness({ box: [1280, 800], display: [1280, 800] });
    drag(h, path([1280, 800], [1600, 900], 20));
    h.work(5_000);
    h.present(1920, 1080);
    h.work(30_000);
    assert.deepEqual(h.sizes(), ['1600x900'], 'R4: another console\'s size is followed, not fought');
    drag(h, path([1600, 900], [1500, 900], 10));
    h.work(5_000);
    assert.deepEqual(h.sizes(), ['1600x900', '1500x900']);
});

let failed = 0;
for (const [name, fn] of scenarios) {
    try { fn(); console.log(`ok   ${name}`); } catch (error) {
        failed += 1;
        console.log(`FAIL ${name}: ${error.message}`);
    }
}
assert.equal(failed, 0, `${failed} of ${scenarios.length} resize cadence scenarios failed`);
console.log(`resize reference cadence: ${scenarios.length} scenarios passed`);
