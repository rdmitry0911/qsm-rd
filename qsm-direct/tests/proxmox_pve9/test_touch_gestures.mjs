import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const mapStart = source.indexOf('const GESTURE_ACTIONS =');
const mapEnd = source.indexOf('// A media worker is shared', mapStart);
const loadMap = new Function('browserStorage', `${source.slice(mapStart, mapEnd)}; return { gestureMap, gestureToAction };`);
const defaults = loadMap(null).gestureToAction;
assert.equal(defaults.drag1, 'move');
assert.equal(defaults.holdDrag, 'drag');
const storedMap = value => loadMap({ getItem: () => JSON.stringify(value) }).gestureToAction;
const migrated = storedMap({ leftClick: 'tap', rightClick: 'longPress', middleClick: 'tap3', drag: 'drag1', scroll: 'drag2', zoom: 'pinch' });
assert.equal(migrated.drag1, 'move', 'saved legacy defaults must also stop selecting on ordinary motion');
assert.equal(migrated.holdDrag, 'drag');
const custom = storedMap({ drag: 'drag2', scroll: 'drag1' });
assert.equal(custom.drag1, 'scroll', 'keep explicit custom bindings');
assert.equal(custom.drag2, 'drag');

const start = source.indexOf('const touchPoints = new Map();');
const end = source.indexOf("video.addEventListener('paste'", start);
assert.ok(start > 0 && end > start);
const recognizer = new Function('env', `
    const {video, document, popup, gestureToAction, queuePointer, flushPointer,
        pointerForMouseEvent, sendMouseButton, send, releaseHeldInput} = env;
    let usingTouchInput = false, toolbarVisible = false, toolbarPinned = false;
    let zoomScale = 1, zoomTx = 0, zoomTy = 0;
    const MIN_ZOOM = 1, MAX_ZOOM = 4;
    const ensurePlaying = () => {}, hideToolbarNow = () => {},
        showTouchMarker = () => {}, placeGuestCursor = () => {},
        clampZoom = () => {}, applyViewTransform = () => {};
    let diagLastInput = '';
    ${source.slice(start, end)}
    return { zoom: () => zoomScale };
`);

function harness(map = defaults) {
    const target = () => ({
        listeners: new Map(),
        addEventListener(name, callback) {
            if (!this.listeners.has(name)) { this.listeners.set(name, []); }
            this.listeners.get(name).push(callback);
        },
        emit(name, event) { for (const callback of this.listeners.get(name) || []) { callback(event); } },
    });
    const video = { ...target(), focus() {}, setPointerCapture() {}, releasePointerCapture() {} };
    const document = { ...target(), hidden: false };
    const timers = new Map();
    let clock = 0, nextTimer = 0;
    const popup = { ...target(),
        setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { at: clock + delay, callback }); return id; },
        clearTimeout(id) { timers.delete(id); },
    };
    const messages = [], points = [], held = new Set();
    const button = (number, down) => {
        if (down === held.has(number)) { return; }
        if (down) { held.add(number); } else { held.delete(number); }
        messages.push({ button: number, down });
    };
    const instance = recognizer({ video, document, popup, gestureToAction: map,
        queuePointer: point => points.push(point), flushPointer() {},
        pointerForMouseEvent: event => ({ x: event.clientX, y: event.clientY }),
        sendMouseButton: button, send: message => messages.push(message),
        releaseHeldInput: () => { for (const n of [...held]) { button(n, false); } },
    });
    return { video, document, popup, points, messages, held, ...instance,
        tick(ms) {
            clock += ms;
            for (const [id, timer] of [...timers]) {
                if (timer.at <= clock && timers.has(id)) { timers.delete(id); timer.callback(); }
            }
        },
        touch(name, x, y, id = 1) {
            const event = { pointerType: 'touch', pointerId: id, clientX: x, clientY: y,
                preventDefault() {}, stopPropagation() {} };
            video.emit(name, event);
            if (['pointerup', 'pointercancel'].includes(name)) { document.emit(name, event); }
        },
    };
}

{
    const h = harness();
    h.touch('pointerdown', 20, 30);
    h.touch('pointermove', 23, 32);
    assert.deepEqual(h.points.at(-1), { x: 23, y: 32 }, 'small motion must already move the cursor');
    h.touch('pointermove', 80, 90);
    h.tick(1000);
    h.touch('pointermove', 100, 120);
    h.touch('pointerup', 100, 120);
    assert.deepEqual(h.messages, [], 'ordinary slides send no mouse button, even when slow');
    assert.deepEqual(h.points.at(-1), { x: 100, y: 120 });
}
{
    const h = harness(); h.touch('pointerdown', 20, 30); h.touch('pointerup', 20, 30);
    assert.deepEqual(h.messages, [{ button: 1, down: true }, { button: 1, down: false }]);
}
{
    const h = harness(); h.touch('pointerdown', 20, 30); h.tick(500);
    assert.deepEqual(h.messages, [], 'holding must not open a context menu before a selection drag');
    h.touch('pointermove', 80, 90); h.touch('pointermove', 100, 120); h.touch('pointerup', 100, 120);
    assert.deepEqual(h.messages, [{ button: 1, down: true }, { button: 1, down: false }]);
    assert.equal(h.held.size, 0);
}
{
    const h = harness(); h.touch('pointerdown', 20, 30); h.tick(500); h.touch('pointerup', 20, 30);
    assert.deepEqual(h.messages, [{ button: 3, down: true }, { button: 3, down: false }]);
}
for (const cancel of ['pointercancel', 'lostpointercapture', 'blur', 'hidden']) {
    const h = harness(); h.touch('pointerdown', 20, 30); h.tick(500); h.touch('pointermove', 80, 90);
    if (cancel === 'blur') { h.popup.emit('blur'); }
    else if (cancel === 'hidden') { h.document.hidden = true; h.document.emit('visibilitychange'); }
    else { h.touch(cancel, 80, 90); }
    h.touch('pointerup', 80, 90);
    assert.equal(h.held.size, 0, `${cancel} must release selection`);
    assert.deepEqual(h.messages, [{ button: 1, down: true }, { button: 1, down: false }]);
}
{
    const h = harness(); h.touch('pointerdown', 20, 30); h.tick(500); h.popup.emit('blur');
    h.touch('pointerup', 20, 30);
    assert.deepEqual(h.messages, [], 'an interrupted hold must not later right-click');
}
{
    const h = harness(); h.touch('pointerdown', 20, 30); h.touch('pointerdown', 60, 30, 2);
    h.touch('pointermove', 20, 60); h.touch('pointermove', 60, 60, 2);
    h.touch('pointerup', 20, 60); h.touch('pointerup', 60, 60, 2);
    assert.ok(h.messages.some(m => m.op === 'scroll'));
    assert.ok(h.messages.every(m => m.button === undefined), 'two-finger scrolling never selects');
}
{
    const h = harness(); h.touch('pointerdown', 20, 30); h.tick(500); h.touch('pointermove', 80, 90);
    h.touch('pointerdown', 100, 90, 2);
    assert.equal(h.held.size, 0, 'a second finger ends selection before a new gesture');
    h.touch('pointerup', 80, 90); h.touch('pointerup', 100, 90, 2);
    assert.equal(h.messages.filter(m => m.down).length, 1);
}
console.log('QSM_TOUCH_GESTURES_OK');
