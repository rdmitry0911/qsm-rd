import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import zlib from 'node:zlib';

const source = fs.readFileSync(process.env.QSM_EXACT_UI_SOURCE || new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const begin = source.indexOf('    class ExactUiRenderer {');
const end = source.indexOf('    const defaultConsoleSettings', begin);
assert.ok(begin > 0 && end > begin);
const sandbox = vm.createContext({ ArrayBuffer, DataView, Uint8Array, Map, Error, Number, queueMicrotask, performance, setTimeout });
const regionBegin = source.indexOf('    class VideoRegionRenderer {');
vm.runInContext(source.slice(regionBegin, end) + '\nglobalThis.Renderer = ExactUiRenderer; globalThis.Region = VideoRegionRenderer; globalThis.Compositor = ExactUiCompositor;', sandbox);
const record = (op, { epoch = 1, serial = 1, width = 64, height = 64, x = 0, y = 0, w = 0, h = 0, pixels, encoding = 0, capture = 0, layout = null, early = false } = {}) => {
    const data = pixels ? zlib.deflateSync(pixels) : new Uint8Array();
    const stamp = layout !== null ? 16 : capture ? 8 : 0;
    const buffer = new ArrayBuffer(28 + stamp + data.length), view = new DataView(buffer);
    view.setUint32(0, 0x51554931); view.setUint8(4, op);
    view.setUint8(5, encoding | (early ? 2 : 0));
    if (stamp) { view.setUint16(6, stamp); view.setUint32(28, Math.floor(capture / 4294967296)); view.setUint32(32, capture >>> 0); }
    if (layout !== null) { view.setUint32(36,Math.floor(layout / 4294967296));view.setUint32(40,layout >>> 0); }
    view.setUint32(8, epoch); view.setUint32(12, serial);
    for (const [i, value] of [width, height, x, y, w, h].entries()) { view.setUint16(16 + 2 * i, value); }
    new Uint8Array(buffer, 28 + stamp).set(data); return buffer;
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

// A small edit inside an established layout can lead a delayed video. A
// fresh tile, dense repaint or changed layout must still wait for its base.
const priorityPaints=[], priorityClears=[], priorityErrors=[];
const priorityCanvas={width:0,height:0,style:{},getContext:()=>({
 createImageData:(w,h)=>({data:new Uint8Array(w*h*4)}),
 putImageData:image=>priorityPaints.push(new Uint8Array(image.data)),
 clearRect:(...a)=>priorityClears.push(a)})};
const priority=new sandbox.Renderer(priorityCanvas,data=>Promise.resolve(zlib.inflateSync(data)),e=>priorityErrors.push(e));
priority.configure(true);
const white=new Uint8Array(64*64*4).fill(255), one=new Uint8Array(white);one[0]=0;
await priority.receive(record(1,{capture:100,layout:7}));
await priority.receive(record(2,{serial:2,w:64,h:64,pixels:white,capture:100,layout:7,early:true}));
assert.equal(priorityPaints.length,0,'new tiles never lead video');
priority.mapVideoFrame(1,100);priority.videoFrame(1);
assert.equal(priorityPaints.length,1);
await priority.receive(record(2,{serial:3,w:64,h:64,pixels:one,capture:200,layout:7,early:true}));
assert.equal(priority.videoCapture,100);assert.equal(priorityPaints.length,2,'small edit paints before unrelated future video');
const dense=new Uint8Array(white);dense.fill(20);
for(let i=3;i<dense.length;i+=4)dense[i]=255;
await priority.receive(record(2,{serial:4,w:64,h:64,pixels:dense,capture:300,layout:7,early:true}));
assert.equal(priorityPaints.length,2,'dense repaint remains capture-fenced even if producer marks it early');
await priority.receive(record(5,{serial:5,capture:300,layout:8}));
assert.equal(priorityClears.length,0,'old early tile is retained until a corresponding base exists');
priority.mapVideoFrame(2,300);priority.videoFrame(2);
assert.equal(priorityClears.length,1,'changed layout retires old early pixels at its capture boundary');
assert.equal(priorityPaints.length,2,'queued old-layout image cannot resurrect after the new base');
await priority.receive(record(2,{serial:6,w:64,h:64,pixels:white,capture:400,layout:8,early:true}));
assert.equal(priorityPaints.length,2,'first image in a new layout waits for video');
priority.mapVideoFrame(3,400);priority.videoFrame(3);
await priority.receive(record(2,{serial:7,w:64,h:64,pixels:one,capture:500,layout:8,early:true}));
assert.equal(priorityPaints.length,4);
await priority.receive(record(5,{serial:8,capture:500,layout:0}));
const two=new Uint8Array(one);two[4]=0;
await priority.receive(record(2,{serial:9,w:64,h:64,pixels:two,capture:600,layout:8,early:true}));
assert.equal(priorityPaints.length,4,'revoked metadata cannot authorize another early patch');
priority.mapVideoFrame(4,500);priority.videoFrame(4);
assert.equal(priorityClears.length,2);
assert.deepEqual(priorityErrors,[]);

// An ordinary capture-fenced tile can still be inflating/queued when the
// compositor reports a new layout. Unchanged ordinary tiles are not sent
// again by the worker, so dropping them would leave a permanent hole.
const normalPaints=[],normalClears=[];
const normalCanvas={width:0,height:0,style:{},getContext:()=>({
 createImageData:(w,h)=>({data:new Uint8Array(w*h*4)}),
 putImageData:image=>normalPaints.push(new Uint8Array(image.data)),
 clearRect:(...args)=>normalClears.push(args)})};
const normal=new sandbox.Renderer(normalCanvas,data=>Promise.resolve(zlib.inflateSync(data)),e=>{throw e});
normal.configure(true);
await normal.receive(record(1,{capture:100,layout:0}));
normal.mapVideoFrame(1,100);normal.videoFrame(1);
await normal.receive(record(2,{serial:2,w:64,h:64,pixels:white,capture:200,layout:0}));
assert.equal(normalPaints.length,0,'ordinary tile still waits for its captured base');
await normal.receive(record(5,{serial:3,capture:200,layout:8}));
normal.mapVideoFrame(2,300);normal.videoFrame(2);
assert.equal(normalPaints.length,1,'unchanged ordinary tile survives a metadata layout boundary');
assert.deepEqual(normalPaints[0],white);
await normal.receive(record(2,{serial:4,w:64,h:64,pixels:one,capture:400,layout:8}));
await normal.receive(record(5,{serial:5,capture:500,layout:9}));
await normal.receive(record(3,{serial:6,w:64,h:64,capture:500,layout:9}));
normal.mapVideoFrame(3,500);normal.videoFrame(3);
assert.equal(normalPaints.length,1,'newer invalidation still cancels an old queued ordinary tile');
assert.equal(normal.active.size,0);

// A copy reads the canvas: earlier inflations finish first, later records wait.
{
    const ops = [], inflations = [];
    const ctx = { createImageData: (w, h) => ({ data: new Uint8Array(w * h * 4) }),
        putImageData: (image, x, y) => ops.push(['paint', image.data[0], x, y]), clearRect: (...a) => ops.push(['clear', ...a]),
        save: () => ops.push(['save']), restore: () => ops.push(['restore']), beginPath() {}, clip() {},
        rect: (...a) => ops.push(['rect', ...a]), drawImage: (source, ...a) => ops.push(['draw', source === copyCanvas, ctx.globalCompositeOperation, ...a]) };
    const copyCanvas = { width: 0, height: 0, style: {}, getContext: () => ctx };
    const errors = [];
    const renderer = new sandbox.Renderer(copyCanvas, data => new Promise(resolve => inflations.push(() => resolve(zlib.inflateSync(data)))), e => errors.push(e));
    renderer.configure(true);
    const copy = (serial, x, y, w, h, sx, sy, capture = 100, flags = 0) => {
        const buffer = new ArrayBuffer(28 + 16 + 4), v = new DataView(buffer);
        v.setUint32(0, 0x51554931); v.setUint8(4, 10); v.setUint8(5, flags); v.setUint16(6, 16);
        v.setUint32(8, 1); v.setUint32(12, serial);
        [256, 128, x, y, w, h].forEach((n, i) => v.setUint16(16 + 2 * i, n));
        v.setUint32(32, capture); v.setUint16(44, sx); v.setUint16(46, sy);
        return buffer;
    };
    const solid = value => { const p = new Uint8Array(64 * 64 * 4); for (let i = 0; i < p.length; i += 4) { p.set([value, value, value, 255], i); } return p; };
    const tile = (serial, x, value, extra = {}) => { const b = record(2, { serial, width: 256, height: 128, x, y: 0, w: 64, h: 64, pixels: solid(value), capture: 50, layout: 0, ...extra }); return b; };
    await renderer.receive(record(1, { width: 256, height: 128 }));
    renderer.mapVideoFrame(1, 1000); renderer.videoFrame(1);
    const first = renderer.receive(tile(2, 0, 7));
    assert.equal(inflations.length, 1);
    await renderer.receive(copy(3, 64, 0, 64, 64, 0, 0));
    const later = renderer.receive(tile(4, 128, 9));
    assert.equal(ops.filter(o => o[0] === 'draw').length, 0, 'a copy waits for earlier inflation');
    assert.equal(inflations.length, 1, 'a later record cannot start before the copy');
    assert.equal(renderer.serial, 3);
    inflations.shift()(); await first;
    assert.deepEqual(ops.slice(0, 3).map(o => o[0]), ['paint', 'save', 'rect'], 'the earlier tile is painted, then copied');
    const draw = ops.find(o => o[0] === 'draw');
    assert.deepEqual(draw, ['draw', true, 'copy', 0, 0, 64, 64, 64, 0, 64, 64], 'replace destination with an integer same-size self copy');
    assert.equal(ctx.imageSmoothingEnabled, false);
    assert.equal(renderer.active.get('64:0').image, null, 'a copied bitmap is unknown, so it cannot bypass a fence');
    assert.equal(renderer.copies, 1);
    await new Promise(resolve => setTimeout(resolve));
    assert.equal(inflations.length, 1, 'the queued record starts after the copy');
    inflations.shift()(); await later;
    assert.equal(ops.at(-1)[0], 'paint');
    assert.equal(ops.at(-1)[2], 128);
    // A tile waiting for its video fence is part of what a copy moves.
    const fenced = renderer.receive(tile(5, 0, 11, { capture: 5000 }));
    inflations.shift()(); await fenced;
    const paintsBefore = ops.filter(o => o[0] === 'paint').length;
    assert.ok(renderer.ready.has('0:0'), 'an ordinary tile still waits for its capture');
    await renderer.receive(copy(6, 64, 0, 64, 64, 0, 0));
    assert.equal(ops.filter(o => o[0] === 'paint').length, paintsBefore + 1);
    assert.equal(renderer.ready.size, 0);
    // A band record presents without waiting for video; others keep the fence.
    const band = record(2, { serial: 7, width: 256, height: 128, x: 0, y: 64, w: 64, h: 64, pixels: solid(13), capture: 9000, layout: 0 });
    new DataView(band).setUint8(5, 4);
    const paintsBand = ops.filter(o => o[0] === 'paint').length;
    const banded = renderer.receive(band);
    inflations.shift()(); await banded;
    assert.equal(ops.filter(o => o[0] === 'paint').length, paintsBand + 1, 'a copy band is not fenced');
    // A layout boundary retires only leading layout-dependent bitmaps. The worker
    // does not publish a copy band again, so erasing it would leave stale pixels.
    assert.equal(renderer.active.get('0:64').earlyPainted, false);
    const clearsBefore = ops.filter(o => o[0] === 'clear').length;
    renderer.scene = { layout: 77, capture: 1 }; renderer.retireEarlyLayout();
    assert.equal(ops.filter(o => o[0] === 'clear').length, clearsBefore, 'a band survives a layout change');
    assert.ok(renderer.active.has('0:64'));
    // Invalid geometry disables the layer rather than drawing.
    await renderer.receive(copy(8, 250, 0, 64, 64, 0, 0));
    assert.equal(errors.length, 1);
    assert.equal(renderer.enabled, false);
}
console.log('QSM_EXACT_UI_COPY_OK');
{
    // History records reference pixels of earlier records, in record order.
    const paints = [], errors = [], deferred = [];
    const canvas = { width: 0, height: 0, style: {}, getContext: () => ({
        createImageData: (w, h) => ({ data: new Uint8Array(w * h * 4) }),
        putImageData: (image, x, y) => paints.push({ x, y, pixels: new Uint8Array(image.data) }),
        clearRect: () => {},
    }) };
    let hold = false;
    const inflate = data => hold ? new Promise(resolve => deferred.push(() => resolve(zlib.inflateSync(data))))
        : Promise.resolve(zlib.inflateSync(data));
    const fresh = () => {
        const renderer = new sandbox.Renderer(canvas, inflate, e => errors.push(e));
        renderer.configure(true);
        return renderer;
    };
    const varint = n => { const o = []; while (n >= 128) { o.push((n & 127) | 128); n >>>= 7; } o.push(n); return o; };
    const checksum = pixels => { let s = 2166136261; for (const v of pixels) { s = Math.imul(s ^ v, 16777619); } return s >>> 0; };
    // plan: [literals, matchLength, distance, literals, matchLength, distance, ..., literals]
    const tokens = (pixels, plan, { sum = checksum(pixels), extra = [] } = {}) => {
        const ops = [], distances = [], literals = [];
        let at = 0;
        for (let n = 0; n < plan.length; n += 3) {
            ops.push(...varint(plan[n]));
            for (let i = 0; i < plan[n]; ++i) { literals.push(pixels[at + i]); }
            at += plan[n];
            if (n + 1 < plan.length) { ops.push(...varint(plan[n + 1])); distances.push(...varint(plan[n + 2])); at += plan[n + 1]; }
        }
        ops.push(...extra);
        const out = Buffer.alloc(8 + ops.length + distances.length + literals.length * 3);
        out.writeUInt32BE(sum); out.writeUInt16BE(ops.length, 4); out.writeUInt16BE(distances.length, 6);
        out.set(ops, 8); out.set(distances, 8 + ops.length);
        const base = 8 + ops.length + distances.length;
        literals.forEach((v, i) => {
            const r = v & 255;
            out[base + i] = r;
            out[base + literals.length + i] = ((v >>> 8) - r) & 255;
            out[base + 2 * literals.length + i] = ((v >>> 16) - r) & 255;
        });
        return out;
    };
    const historyRecord = (serial, inflated, { x = 0, flags = 8 } = {}) => {
        const data = Buffer.concat([Buffer.alloc(4), zlib.deflateSync(inflated)]);
        data.writeUInt32BE(inflated.length);
        const base = record(2, { serial, width: 128, height: 64, x, w: 64, h: 64 });
        const out = new Uint8Array(base.byteLength + data.length);
        out.set(new Uint8Array(base)); out.set(data, base.byteLength);
        new DataView(out.buffer).setUint8(5, flags);
        return out.buffer;
    };
    const reset = serial => record(1, { serial, width: 128, height: 64 });
    const rgba = pixels => { const o = new Uint8Array(pixels.length * 4); pixels.forEach((v, i) => o.set([v & 255, (v >>> 8) & 255, (v >>> 16) & 255, 255], i * 4)); return o; };
    const noise = seed => Array.from({ length: 4096 }, (_, i) => (Math.imul(i + seed, 2654435761) >>> 8) & 0xffffff);
    const renderer = fresh();
    await renderer.receive(reset(1));
    // A uniform tile: one pixel, then a match overlapping itself.
    const uniform = new Array(4096).fill(0x332211);
    await renderer.receive(historyRecord(2, tokens(uniform, [1, 4095, 1])));
    assert.deepEqual(paints.at(-1).pixels, rgba(uniform));
    // Literals, in all channels.
    const first = noise(1);
    await renderer.receive(historyRecord(3, tokens(first, [4096]), { x: 64 }));
    assert.deepEqual(paints.at(-1).pixels, rgba(first));
    assert.equal(paints.at(-1).x, 64);
    // The same pixels again, wherever they are, take one match.
    await renderer.receive(historyRecord(4, tokens(first, [0, 4096, 4096])));
    assert.deepEqual(paints.at(-1).pixels, rgba(first));
    assert.equal(tokens(first, [0, 4096, 4096]).length < 16, true);
    // Reference only part of them, then literals.
    const mixed = [...first.slice(1000, 3000), ...noise(2).slice(0, 2096)];
    await renderer.receive(historyRecord(5, tokens(mixed, [0, 2000, 4096 + 3096, 2096])));
    assert.deepEqual(paints.at(-1).pixels, rgba(mixed));
    // Inflation finishing in the other order cannot change which pixels are referenced.
    hold = true;
    const older = noise(3), newer = [...older];
    const a = renderer.receive(historyRecord(6, tokens(older, [4096])));
    const b = renderer.receive(historyRecord(7, tokens(newer, [0, 4096, 4096]), { x: 64 }));
    deferred.pop()(); await new Promise(resolve => setTimeout(resolve, 5));
    deferred.pop()(); hold = false;
    await a; await b;
    assert.deepEqual(paints.at(-1).pixels, rgba(newer));
    assert.deepEqual(paints.at(-2).pixels, rgba(older));
    // A clear drops a tile's paint, not what later records refer to.
    hold = true;
    const replaced = noise(4);
    const dropped = renderer.receive(historyRecord(8, tokens(replaced, [4096])));
    await renderer.receive(record(3, { serial: 9, width: 128, height: 64, w: 64, h: 64 }));
    const paintsBefore = paints.length;
    deferred.shift()(); hold = false; await dropped;
    assert.equal(paints.length, paintsBefore, 'a cleared tile is not painted');
    await renderer.receive(historyRecord(10, tokens(replaced, [0, 4096, 4096]), { x: 64 }));
    assert.deepEqual(paints.at(-1).pixels, rgba(replaced));
    assert.equal(errors.length, 0);
    // A new epoch restarts history: older pixels are gone.
    await renderer.receive(record(1, { epoch: 2, serial: 1, width: 128, height: 64 }));
    assert.equal(renderer.history, null);
    const mismatch = { epoch: 2 };
    const stale = historyRecord(2, tokens(replaced, [0, 4096, 4096]));
    new DataView(stale).setUint32(8, mismatch.epoch);
    await renderer.receive(stale);
    assert.equal(errors.length, 1, 'a source before the epoch cannot decode');
    assert.equal(renderer.enabled, false);
    // Whatever disagrees disables the layer rather than showing wrong pixels.
    for (const [name, inflated] of [
        ['checksum', tokens(first, [4096], { sum: 12345 })],
        ['too few pixels', tokens(first.slice(0, 4095), [4095])],
        ['trailing ops', tokens(first, [4096], { extra: [1] })],
        ['match past the start', tokens(uniform, [1, 4095, 2])],
        ['zero distance', tokens(uniform, [1, 4095, 0])],
        ['overlong varint', Buffer.concat([tokens(uniform.slice(0, 1), [1]).subarray(0, 4), Buffer.from([0, 6, 0, 0, 255, 255, 255, 255, 255, 1])])],
        ['literal bytes', Buffer.concat([tokens(first, [4096]), Buffer.from([1])])],
    ]) {
        const count = errors.length, target = fresh();
        await target.receive(reset(1));
        await target.receive(historyRecord(2, inflated));
        assert.equal(errors.length, count + 1, name);
        assert.equal(target.enabled, false, name);
    }
    {
        // Unknown flags and a history flag on anything else are not accepted.
        const count = errors.length, target = fresh();
        await target.receive(reset(1));
        await target.receive(historyRecord(2, tokens(first, [4096]), { flags: 24 }));
        assert.equal(errors.length, count + 1);
        const other = fresh();
        await other.receive(reset(1));
        const cleared = record(3, { serial: 2, width: 128, height: 64, w: 64, h: 64 });
        new DataView(cleared).setUint8(5, 8);
        await other.receive(cleared);
        assert.equal(errors.length, count + 2);
    }
    // A failed reference still fails later ones waiting behind it.
    {
        const target = fresh(), count = errors.length;
        await target.receive(reset(1));
        await Promise.all([target.receive(historyRecord(2, tokens(first, [4096], { sum: 1 }))),
            target.receive(historyRecord(3, tokens(first, [4096]), { x: 64 }))]);
        assert.ok(errors.length > count);
        assert.equal(target.enabled, false);
    }
}
{
    // A burst of records is answered by far fewer replies: each one draws an SCTP
    // acknowledgement over the same narrow path. An isolated record is answered at once.
    const replies = [];
    const renderer = new sandbox.Renderer(raster(64, 64), async d => d, () => {}, reply => replies.push(reply));
    renderer.enabled = true; renderer.epoch = 1; renderer.serial = 1;
    renderer.report();
    await Promise.resolve();
    assert.equal(replies.length, 1);
    for (const serial of [2, 3, 4, 5]) { renderer.serial = serial; renderer.report(); await Promise.resolve(); }
    assert.equal(replies.length, 1, 'no reply within the spacing');
    await new Promise(resolve => setTimeout(resolve, 15));
    assert.equal(replies.length, 2);
    assert.equal(replies[1].serial, 5, 'the delayed reply states the newest record');
    await new Promise(resolve => setTimeout(resolve, 15));
    renderer.serial = 6; renderer.report();
    await Promise.resolve();
    assert.equal(replies.length, 3, 'a record after a pause is answered at once');
}
console.log('QSM_EXACT_UI_HISTORY_OK');
