import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import zlib from 'node:zlib';

const source = fs.readFileSync(process.env.QSM_EXACT_UI_SOURCE || new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const begin = source.indexOf('    class ExactUiRenderer {');
const end = source.indexOf('    const defaultConsoleSettings', begin);
assert.ok(begin > 0 && end > begin);
const sandbox = vm.createContext({ ArrayBuffer, DataView, Uint8Array, Map, Error, Number, queueMicrotask });
const regionBegin = source.indexOf('    class VideoRegionRenderer {');
vm.runInContext(source.slice(regionBegin, end) + '\nglobalThis.Renderer = ExactUiRenderer; globalThis.Region = VideoRegionRenderer; globalThis.Compositor = ExactUiCompositor;', sandbox);
const record = (op, { epoch = 1, serial = 1, width = 64, height = 64, x = 0, y = 0, w = 0, h = 0, pixels, encoding = 0, capture = 0 } = {}) => {
    const data = pixels ? zlib.deflateSync(pixels) : new Uint8Array();
    const buffer = new ArrayBuffer(28 + (capture ? 8 : 0) + data.length), view = new DataView(buffer);
    view.setUint32(0, 0x51554931); view.setUint8(4, op);
    view.setUint8(5, encoding);
    if (capture) { view.setUint16(6, 8); view.setUint32(28, Math.floor(capture / 4294967296)); view.setUint32(32, capture >>> 0); }
    view.setUint32(8, epoch); view.setUint32(12, serial);
    for (const [i, value] of [width, height, x, y, w, h].entries()) { view.setUint16(16 + 2 * i, value); }
    new Uint8Array(buffer, 28 + (capture ? 8 : 0)).set(data); return buffer;
};
const regionDraws = [], regionAcks = [];
const regionCanvas = { width: 0, height: 0, style: {}, getContext: () => ({ drawImage: (...args) => regionDraws.push(args.slice(1)) }) };
const regionVideo = { videoWidth: 1280, videoHeight: 720, style: {} };
const regionRenderer = new sandbox.Region(regionCanvas, regionVideo, rtp => regionAcks.push(rtp));
assert.equal(regionRenderer.map(1, [1280, 720, 0, 0, 1280, 720]), true);
regionRenderer.frame(1); assert.deepEqual(regionAcks, [1]);
regionRenderer.map(2, [1280, 720, 640, 64, 640, 448]); regionRenderer.frame(2);
assert.equal(regionDraws.length, 1, 'a different-sized decoded picture cannot be stretched into the crop');
regionVideo.videoWidth = 640; regionVideo.videoHeight = 448; regionRenderer.frame(2);
assert.deepEqual(regionDraws.at(-1), [640, 64, 640, 448]);
assert.equal(regionCanvas.width, 1280); assert.equal(regionVideo.style.opacity, '0');
assert.equal(regionRenderer.map(3, [1280, 720, 1000, 0, 640, 448]), false);
// UI-only repaint must use the last presented base, even if the media element
// already exposes a newer frame. Both images are combined before CSS scaling.
const raster = (w, h, value) => {
    const image = { width: w, height: h, style: {}, data: new Uint8Array(w * h * 4) };
    if (value !== undefined) {
        for (let i = 0; i < image.data.length; i += 4) { image.data.set([value, value, value, 255], i); }
    }
    image.getContext = () => ({ drawImage: other => {
        for (let i = 0; i < other.data.length; i += 4) {
            if (other.data[i + 3] === 255) { image.data.set(other.data.subarray(i, i + 4), i); }
        }
    } });
    return image;
};
const native = { ...raster(128, 64, 10), videoWidth: 128, videoHeight: 64 };
const backing = raster(128, 64), exact = raster(128, 64), composed = raster(128, 64);
exact.data.set([200, 30, 40, 255], 64 * 4);
const regionState = { canvas: backing, width: 0, painted: null };
const compositor = new sandbox.Compositor(composed, native, exact, regionState);
compositor.capture(); compositor.present(true);
assert.deepEqual([...composed.data.subarray(64 * 4, 64 * 4 + 4)], [200, 30, 40, 255]);
assert.equal(composed.data[0], 10);
native.data.fill(255); compositor.present(true);
assert.equal(composed.data[0], 10, 'an unfenced future video frame cannot leak into a UI-only repaint');
compositor.capture(); compositor.present(true);
assert.equal(composed.data[0], 255, 'the next presented base advances the complete composition');
regionState.width = 128; regionState.painted = 42; backing.data.fill(20);
for (let i = 3; i < backing.data.length; i += 4) { backing.data[i] = 255; }
compositor.capture(); compositor.present(true);
assert.equal(composed.data[0], 20, 'a crop must use the retained full-desktop base, not stretch the media element');
compositor.present(false);
assert.equal(composed.style.display, 'none');
assert.equal(backing.style.display, 'block');
regionState.width = 0; compositor.present(false);
assert.equal(native.style.opacity, '');
assert.equal(backing.style.display, 'none');
compositor.present(true);
assert.equal(composed.style.display, 'none', 're-enabling must not briefly display a base retained from the previous session');
compositor.capture(); compositor.present(true);
assert.equal(composed.style.display, 'block', 're-enabling becomes visible after a fresh presented base');
const painted = [], clears = [], errors = [], pending = [];
const context = { createImageData: (w, h) => ({ data: new Uint8Array(w * h * 4) }),
    putImageData: (image, x, y) => painted.push({ pixels: image.data, x, y }),
    clearRect: (...r) => clears.push(r) };
const canvas = { width: 0, height: 0, style: {}, getContext: () => context };
const inflate = data => new Promise(resolve => pending.push(() => resolve(zlib.inflateSync(data))));
const r = new sandbox.Renderer(canvas, inflate, e => errors.push(e));
r.configure(true); await r.receive(record(1));
const pixels = new Uint8Array(64 * 64 * 4);
for (let n = 0; n < pixels.length; n += 4) { pixels.set([12, 34, 56, 255], n); }
const first = r.receive(record(2, { serial: 2, w: 64, h: 64, pixels }));
await r.receive(record(3, { serial: 3, w: 64, h: 64 }));
pending.shift()(); await first;
assert.equal(painted.length, 0, 'late inflate cannot resurrect a cleared tile');
const second = r.receive(record(2, { serial: 4, w: 64, h: 64, pixels }));
pending.shift()(); await second;
assert.deepEqual(painted[0].pixels, pixels, 'RGBA pixels must arrive exactly');
const third = r.receive(record(2, { serial: 5, w: 64, h: 64, pixels }));
await r.receive(record(1, { epoch: 2, width: 128, height: 64 }));
pending.shift()(); await third;
assert.equal(painted.length, 1, 'old geometry cannot repaint after resize');
const fourth = r.receive(record(2, { epoch: 2, serial: 2, width: 128, w: 64, h: 64, pixels }));
r.configure(false); pending.shift()(); await fourth;
assert.equal(painted.length, 1, 'disabling drops in-flight exact pixels');
assert.equal(canvas.width, 0);
r.configure(true); await r.receive(record(1, { epoch: 3 }));
const neutral = r.receive(record(2, { epoch: 3, serial: 2, w: 64, h: 64,
    pixels: new Uint8Array(4096).fill(123), encoding: 1 }));
pending.shift()(); await neutral;
assert.deepEqual([...painted.at(-1).pixels.slice(0, 4)], [123, 123, 123, 255]);
// Event-driven sending may deliver many tiny records in one network burst.
// Decode concurrency stays bounded, and queued history of one tile coalesces.
const burstJobs = [], burstInflate = [], burstPaints = [], burstErrors = [];
const burstCanvas = { width: 0, height: 0, style: {}, getContext: () => ({ ...context,
    putImageData: image => burstPaints.push(image.data[0]),
}) };
const burst = new sandbox.Renderer(burstCanvas,
    data => new Promise(resolve => burstInflate.push(() => resolve(zlib.inflateSync(data)))),
    e => burstErrors.push(e));
burst.configure(true); await burst.receive(record(1));
for (let serial = 2; serial <= 65; ++serial) {
    burstJobs.push(burst.receive(record(2, { serial, w: 64, h: 64,
        encoding: 1, pixels: new Uint8Array(4096).fill(serial) })));
}
assert.equal(burst.inflight, 8); assert.equal(burst.pending.size, 1);
for (let turn = 0; turn < 4; ++turn) {
    for (const ready of burstInflate.splice(0)) { ready(); }
    for (let i = 0; i < 10; ++i) { await Promise.resolve(); }
}
await Promise.all(burstJobs);
assert.deepEqual(burstPaints, [65], 'only the newest tile survives queued/in-flight history');
assert.ok(burst.enabled); assert.equal(burstErrors.length, 0);
// Compact commands use only processed, epoch-local immutable cache entries.
const compactPaints = [];
const compactCanvas = { width: 0, height: 0, style: {}, getContext: () => ({ ...context,
    putImageData: image => compactPaints.push(new Uint8Array(image.data)),
}) };
const compact = new sandbox.Renderer(compactCanvas, data => Promise.resolve(zlib.inflateSync(data)));
const command = (op, serial, payload, encoding = 0) => {
    const base = record(op, { serial, w: 64, h: 64, encoding });
    const result = new Uint8Array(base.byteLength + payload.length);
    result.set(new Uint8Array(base)); result.set(payload, base.byteLength); return result.buffer;
};
compact.configure(true); await compact.receive(record(1));
const original = new Uint8Array(64 * 64 * 4);
for (let y = 0; y < 64; ++y) {
    for (let x = 0; x < 64; ++x) { original.set([x % 4, y, x % 4 + y, 255], (y * 64 + x) * 4); }
}
const u32 = id => { const b = Buffer.alloc(4); b.writeUInt32BE(id); return b; };
await compact.receive(command(7, 2, Buffer.concat([u32(1), zlib.deflateSync(original)])));
await compact.receive(command(8, 3, u32(1)));
assert.deepEqual(compactPaints.at(-1), original);
const scrollHeader = Buffer.alloc(10); scrollHeader.writeUInt32BE(2); scrollHeader.writeUInt32BE(1, 4); scrollHeader.writeInt16BE(-5, 8);
const exposed = new Uint8Array(5 * 64 * 4).fill(255);
await compact.receive(command(9, 4, Buffer.concat([scrollHeader, zlib.deflateSync(exposed)])));
assert.deepEqual(compactPaints.at(-1).subarray(0, 59 * 64 * 4), original.subarray(5 * 64 * 4));
assert.deepEqual(compact.cache.get(1).image.data, original, 'overlap reads the immutable source bitmap');
await compact.receive(command(6, 5, new Uint8Array([19, 23, 29, 255])));
assert.deepEqual([...compactPaints.at(-1).subarray(0, 4)], [19, 23, 29, 255]);
await compact.receive(record(1, { epoch: 2 }));
assert.equal(compact.cache.size, 0);
// A clear/reset cannot reveal video captured before the newest exact UI.
const fencedClears = [], fencedPaints = [];
const fenceCanvas = { width: 0, height: 0, style: {}, getContext: () => ({ ...context,
    clearRect: (...r) => fencedClears.push(r), putImageData: image => fencedPaints.push(image.data[0]),
}) };
const fence = new sandbox.Renderer(fenceCanvas, data => Promise.resolve(zlib.inflateSync(data)));
fence.configure(true); await fence.receive(record(1, { capture: 100 }));
fence.mapVideoFrame(490, 120); fence.videoFrame(490);
await fence.receive(record(2, { serial: 2, capture: 120, w: 64, h: 64, pixels }));
await fence.receive(record(3, { serial: 3, capture: 130, w: 64, h: 64 }));
assert.equal(fencedClears.length, 0);
fence.mapVideoFrame(500, 125); fence.videoFrame(500);
assert.equal(fencedClears.length, 0, 'older video cannot uncover stale content');
fence.mapVideoFrame(501, 130); fence.videoFrame(501);
assert.equal(fencedClears.length, 1, 'matching presented video releases the overlay');
fence.mapVideoFrame(502, 140); fence.videoFrame(502);
await fence.receive(record(2, { serial: 4, capture: 140, w: 64, h: 64, pixels }));
await fence.receive(record(1, { epoch: 2, capture: 150 }));
assert.equal(fenceCanvas.width, 64); assert.equal(fence.active.size, 1, 'same-size reset retains the old backing until the fence');
await fence.receive(record(2, { epoch: 2, serial: 2, capture: 160, w: 64, h: 64, pixels }));
fence.mapVideoFrame(503, 150); fence.videoFrame(503);
assert.equal(fencedClears.length, 2, 'reset releases the old bitmap while the future replacement still waits');
assert.equal(fence.active.size, 0);
fence.mapVideoFrame(504, 160); fence.videoFrame(504);
assert.equal(fence.active.size, 1, 'the replacement appears with its corresponding base picture');
await fence.receive(record(3, { epoch: 2, serial: 3, capture: 170, w: 64, h: 64 }));
fence.videoFrame(505); fence.mapVideoFrame(505, 170);
assert.equal(fencedClears.length, 3, 'late control mapping also releases its presented frame');
// Under TURN/TCP congestion, reliable UI records can lead displayed video
// by seconds. Future snapshots must not form a mosaic over its older scene.
const futurePaints = [];
const futureCanvas = { width: 0, height: 0, style: {}, getContext: () => ({ ...context,
    putImageData: image => futurePaints.push(image.data[0]),
}) };
const future = new sandbox.Renderer(futureCanvas, data => Promise.resolve(zlib.inflateSync(data)));
future.configure(true); await future.receive(record(1, { capture: 10 }));
future.mapVideoFrame(610, 10); future.videoFrame(610);
await future.receive(record(2, { serial: 2, capture: 20, w: 64, h: 64, pixels }));
assert.equal(futurePaints.length, 0, 'a future exact bitmap cannot cover an older video scene');
future.mapVideoFrame(611, 19); future.videoFrame(611);
assert.equal(futurePaints.length, 0);
future.mapVideoFrame(612, 20); future.videoFrame(612);
assert.equal(futurePaints.length, 1, 'the exact bitmap appears when its base picture is displayed');
await future.receive(record(2, { serial: 3, capture: 30, w: 64, h: 64, pixels }));
await future.receive(record(3, { serial: 4, capture: 31, w: 64, h: 64 }));
future.mapVideoFrame(613, 30); future.videoFrame(613);
assert.equal(futurePaints.length, 1, 'a newer invalidation cancels a waiting bitmap');
future.configure(false); future.mapVideoFrame(614, 40); future.videoFrame(614);
assert.equal(futurePaints.length, 1, 'disable drops bitmaps waiting for video presentation');
// A received replacement is not necessarily a painted replacement. Its
// pending inflation must not cancel the clearing of an older visible tile.
const lateInflations = [], lateClears = [];
const lateCanvas = { width: 0, height: 0, style: {}, getContext: () => ({ ...context,
    clearRect: (...args) => lateClears.push(args),
}) };
const late = new sandbox.Renderer(lateCanvas,
    data => new Promise(resolve => lateInflations.push(() => resolve(zlib.inflateSync(data)))));
late.configure(true); await late.receive(record(1, { capture: 10 }));
late.mapVideoFrame(599, 20); late.videoFrame(599);
const oldBitmap = late.receive(record(2, { serial: 2, capture: 20, w: 64, h: 64, pixels }));
lateInflations.shift()(); await oldBitmap;
await late.receive(record(3, { serial: 3, capture: 30, w: 64, h: 64 }));
const newBitmap = late.receive(record(2, { serial: 4, capture: 40, w: 64, h: 64, pixels }));
late.mapVideoFrame(600, 30); late.videoFrame(600);
assert.equal(lateClears.length, 1, 'an unpainted replacement cannot leave the old square over newer video');
assert.equal(late.active.size, 0);
lateInflations.shift()(); await newBitmap;
assert.equal(late.active.size, 0, 'inflating a future replacement does not paint it early');
late.mapVideoFrame(601, 40); late.videoFrame(601);
assert.equal(late.active.get('0:0').serial, 4);
await late.receive(record(1, { epoch: 2, capture: 50 }));
const afterReset = late.receive(record(2, { epoch: 2, serial: 2, capture: 60, w: 64, h: 64, pixels }));
late.mapVideoFrame(602, 50); late.videoFrame(602);
assert.equal(lateClears.length, 2, 'reset fencing also inspects the painted epoch, not pending patch serials');
lateInflations.shift()(); await afterReset;
late.mapVideoFrame(603, 60); late.videoFrame(603);
assert.equal(late.active.get('0:0').epoch, 2);
// A decoded-size transition can prevent the base canvas from being updated.
// Its RTP callback alone must not reveal the previously painted old window.
const baseCanvas = { width: 0, height: 0, style: {}, getContext: () => ({ drawImage() {} }) };
const baseVideo = { videoWidth: 128, videoHeight: 64, style: {} };
const base = new sandbox.Region(baseCanvas, baseVideo);
const coupledClears = [];
const overlay = { width: 0, height: 0, style: {}, getContext: () => ({ ...context,
    clearRect: (...args) => coupledClears.push(args),
}) };
const coupled = new sandbox.Renderer(overlay, data => Promise.resolve(zlib.inflateSync(data)),
    () => {}, () => {}, (rtp, region) => !region || base.painted === rtp);
const full = [128, 64, 0, 0, 128, 64], crop = [128, 64, 64, 0, 64, 64];
coupled.configure(true); await coupled.receive(record(1, { width: 128, capture: 10 }));
base.map(700, full); base.frame(700); coupled.mapVideoFrame(700, 11, full); coupled.videoFrame(700);
await coupled.receive(record(2, { width: 128, x: 64, w: 64, h: 64, serial: 2, capture: 11, pixels }));
await coupled.receive(record(3, { width: 128, x: 64, w: 64, h: 64, serial: 3, capture: 20 }));
base.map(701, crop); base.frame(701); coupled.mapVideoFrame(701, 20, crop); coupled.videoFrame(701);
assert.equal(base.painted, 700);
assert.equal(coupled.videoCapture, 11);
assert.equal(coupledClears.length, 0, 'unpainted crop metadata cannot uncover the old base canvas');
baseVideo.videoWidth = 64;
base.frame(702); coupled.videoFrame(702);
// Control metadata arrives after presentation, as in the real viewer handler.
base.map(702, crop); coupled.mapVideoFrame(702, 21, crop);
assert.equal(base.painted, 702);
assert.equal(coupledClears.length, 1, 'only a successfully painted matching crop releases old exact pixels');
r.configure(true); await r.receive(record(1, { width: 16384, height: 16384 }));
assert.equal(r.enabled, false); assert.equal(errors.length, 1);
assert.equal(clears.length, 1);
console.log('QSM_EXACT_UI_RENDERER_OK');
