import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import zlib from 'node:zlib';

const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const begin = source.indexOf('    class ExactUiRenderer {');
const end = source.indexOf('    const defaultConsoleSettings', begin);
assert.ok(begin > 0 && end > begin);
const sandbox = vm.createContext({ ArrayBuffer, DataView, Uint8Array, Map, Error, Number, queueMicrotask });
const regionBegin = source.indexOf('    class VideoRegionRenderer {');
vm.runInContext(source.slice(regionBegin, end) + '\nglobalThis.Renderer = ExactUiRenderer; globalThis.Region = VideoRegionRenderer;', sandbox);
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
await fence.receive(record(2, { serial: 2, capture: 120, w: 64, h: 64, pixels }));
await fence.receive(record(3, { serial: 3, capture: 130, w: 64, h: 64 }));
assert.equal(fencedClears.length, 0);
fence.mapVideoFrame(500, 125); fence.videoFrame(500);
assert.equal(fencedClears.length, 0, 'older video cannot uncover stale content');
fence.mapVideoFrame(501, 130); fence.videoFrame(501);
assert.equal(fencedClears.length, 1, 'matching presented video releases the overlay');
await fence.receive(record(2, { serial: 4, capture: 140, w: 64, h: 64, pixels }));
await fence.receive(record(1, { epoch: 2, capture: 150 }));
assert.equal(fenceCanvas.width, 64); assert.equal(fence.active.size, 1, 'same-size reset retains the old backing until the fence');
await fence.receive(record(2, { epoch: 2, serial: 2, capture: 160, w: 64, h: 64, pixels }));
fence.mapVideoFrame(502, 150); fence.videoFrame(502);
assert.equal(fencedClears.length, 1, 'a replacement patch survives the pending reset');
await fence.receive(record(3, { epoch: 2, serial: 3, capture: 170, w: 64, h: 64 }));
fence.videoFrame(503); fence.mapVideoFrame(503, 170);
assert.equal(fencedClears.length, 2, 'late control mapping also releases its presented frame');
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
base.map(700, full); base.frame(700); coupled.mapVideoFrame(700, 10, full); coupled.videoFrame(700);
await coupled.receive(record(2, { width: 128, x: 64, w: 64, h: 64, serial: 2, capture: 11, pixels }));
await coupled.receive(record(3, { width: 128, x: 64, w: 64, h: 64, serial: 3, capture: 20 }));
base.map(701, crop); base.frame(701); coupled.mapVideoFrame(701, 20, crop); coupled.videoFrame(701);
assert.equal(base.painted, 700);
assert.equal(coupled.videoCapture, 10);
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
