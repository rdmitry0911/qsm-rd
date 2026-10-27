import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import zlib from 'node:zlib';

const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const begin = source.indexOf('    class ExactUiRenderer {');
const end = source.indexOf('    const defaultConsoleSettings', begin);
assert.ok(begin > 0 && end > begin);
const sandbox = vm.createContext({ ArrayBuffer, DataView, Uint8Array, Map, Error });
vm.runInContext(source.slice(begin, end) + '\nglobalThis.Renderer = ExactUiRenderer;', sandbox);
const record = (op, { epoch = 1, serial = 1, width = 64, height = 64, x = 0, y = 0, w = 0, h = 0, pixels, encoding = 0 } = {}) => {
    const data = pixels ? zlib.deflateSync(pixels) : new Uint8Array();
    const buffer = new ArrayBuffer(28 + data.length), view = new DataView(buffer);
    view.setUint32(0, 0x51554931); view.setUint8(4, op);
    view.setUint8(5, encoding);
    view.setUint32(8, epoch); view.setUint32(12, serial);
    for (const [i, value] of [width, height, x, y, w, h].entries()) { view.setUint16(16 + 2 * i, value); }
    new Uint8Array(buffer, 28).set(data); return buffer;
};
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
r.configure(true); await r.receive(record(1, { width: 16384, height: 16384 }));
assert.equal(r.enabled, false); assert.equal(errors.length, 1);
assert.equal(clears.length, 1);
console.log('QSM_EXACT_UI_RENDERER_OK');
