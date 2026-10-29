import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const begin = source.indexOf('    class DecodedVideoPresenter {');
const end = source.indexOf('    // Sample the base picture', begin);
assert.ok(begin > 0 && end > begin);
const sandbox = vm.createContext({});
vm.runInContext(source.slice(begin, end) + '\nglobalThis.Presenter = DecodedVideoPresenter; globalThis.Region = VideoRegionRenderer;', sandbox);
const flush = async () => { for (let i = 0; i < 8; ++i) { await Promise.resolve(); } };
const frame = rtp => ({ displayWidth: 640, displayHeight: 448, closed: 0,
    metadata: () => ({ rtpTimestamp: rtp }),
    close() { assert.equal(this.closed++, 0, 'each decoded picture must close exactly once'); },
});
const harness = () => {
    let time = 0, serial = 0;
    const rafs = new Map(), timers = new Map(), readers = [], clones = [];
    const sourceTrack = { readyState: 'live', stops: 0, stop() { this.stops++; }, clone() {
        const clone = { stops: 0, stop() { this.stops++; } }; clones.push(clone); return clone;
    } };
    const scope = {
        performance: { now: () => time },
        requestAnimationFrame: fn => { rafs.set(++serial, fn); return serial; },
        cancelAnimationFrame: id => rafs.delete(id),
        setTimeout: (fn, ms) => { timers.set(++serial, { fn, at: time + ms }); return serial; },
        clearTimeout: id => timers.delete(id),
        MediaStreamTrackProcessor: class {
            constructor({ track, maxBufferSize }) {
                assert.ok(clones.includes(track)); assert.equal(maxBufferSize, 1);
                const reader = { pending: null, cancellations: 0,
                    read() { return new Promise((resolve, reject) => { this.pending = { resolve, reject }; }); },
                    cancel() { this.cancellations++; return Promise.resolve(); },
                    emit(value, done = false) { const pending = this.pending; this.pending = null; pending.resolve({ value, done }); },
                };
                readers.push(reader); this.readable = { getReader: () => reader };
            }
        },
    };
    return { scope, sourceTrack, readers, clones, rafs, timers,
        tick(ms) { time += ms; const callbacks = [...rafs.values()]; rafs.clear(); callbacks.forEach(fn => fn(time)); },
        timeout(ms) { time += ms; for (const [id, item] of [...timers]) {
            if (item.at <= time) { timers.delete(id); item.fn(); }
        } },
    };
};

// Short decoded bursts preserve RTP identity and consume at most one picture
// per display tick. Overflow closes history while the displayed frame stays
// alive for a region mapping that arrives after the picture.
{
    const h = harness(), shown = [], refs = [];
    const p = new sandbox.Presenter(h.scope, (image, rtp) => { shown.push(rtp); refs.push(image); });
    p.configure(h.sourceTrack, true);
    const images = [frame(0xfffffffe), frame(0xffffffff), frame(0)];
    for (const image of images) { h.readers[0].emit(image); await flush(); }
    assert.equal(images[0].closed, 1); assert.equal(p.active, true);
    h.tick(16); assert.deepEqual(shown, [0xffffffff]); assert.equal(refs[0].closed, 0);
    h.tick(16); assert.deepEqual(shown, [0xffffffff, 0]); assert.equal(refs[0].closed, 1);
    p.stop(); assert.equal(images[2].closed, 1);
    assert.equal(h.clones[0].stops, 1); assert.equal(h.sourceTrack.stops, 0);
    assert.equal(h.rafs.size + h.timers.size, 0);
}
// A hidden/blocked display cannot replay a stale queue when RAF resumes.
{
    const h = harness(), shown = [], p = new sandbox.Presenter(h.scope, (_, rtp) => shown.push(rtp));
    p.configure(h.sourceTrack, true); const image = frame(12);
    h.readers[0].emit(image); await flush(); h.tick(51);
    assert.deepEqual(shown, []); assert.equal(image.closed, 1); p.stop();
}
// Cancellation races include the very first read, a pending read after a
// successful draw, and a reader from a superseded session.
{
    const h = harness(), shown = [], p = new sandbox.Presenter(h.scope, (_, rtp) => shown.push(rtp));
    p.configure(h.sourceTrack, true); p.configure(null, false);
    p.configure(h.sourceTrack, true);
    const old = frame(10), fresh = frame(20);
    h.readers[0].emit(old); h.readers[1].emit(fresh); await flush(); h.tick(16);
    assert.equal(old.closed, 1); assert.deepEqual(shown, [20]);
    p.stop(); const late = frame(21); h.readers[1].emit(late); await flush();
    assert.equal(late.closed, 1); assert.equal(fresh.closed, 1);
    assert.equal(p.active, false); assert.equal(h.rafs.size + h.timers.size, 0);
    assert.deepEqual(h.clones.map(x => x.stops), [1, 1]);
}
// Unsupported APIs/identity, processor failure, silence, EOF and drawing
// failure all return ownership to HTML video without stopping its track.
for (const failure of ['api', 'constructor', 'metadata', 'bad-rtp', 'silence', 'eof', 'read', 'draw']) {
    const h = harness(), p = new sandbox.Presenter(h.scope, () => { if (failure === 'draw') { throw Error('draw failed'); } });
    if (failure === 'api') { delete h.scope.MediaStreamTrackProcessor; }
    if (failure === 'constructor') { h.scope.MediaStreamTrackProcessor = class { constructor() { throw Error('unsupported'); } }; }
    p.configure(h.sourceTrack, true);
    let image;
    if (failure === 'silence') { h.timeout(2000); }
    else if (failure === 'eof') { h.readers[0].emit(undefined, true); }
    else if (failure === 'read') { h.readers[0].pending.reject(Error('track stopped')); }
    else if (['metadata', 'bad-rtp', 'draw'].includes(failure)) {
        image = frame(failure === 'bad-rtp' ? 0x100000000 : 30);
        if (failure === 'metadata') { delete image.metadata; }
        h.readers[0].emit(image); await flush(); h.tick(16);
    }
    await flush(); assert.equal(p.active, false, failure);
    assert.equal(h.sourceTrack.stops, 0, failure);
    assert.ok(h.clones.every(x => x.stops === 1), failure);
    assert.equal(h.rafs.size + h.timers.size, 0, failure);
    if (image) { assert.equal(image.closed, 1, failure); }
    const clones = h.clones.length; p.configure(h.sourceTrack, true);
    assert.equal(h.clones.length, clones, 'a failed track must not cause a retry loop');
}
// The region renderer borrows the exact decoded frame until retirement.
// Late metadata cannot substitute a newer, unrelated HTML video picture.
{
    const drawn = [], video = { videoWidth: 1, videoHeight: 1, style: {} };
    const canvas = { width: 0, height: 0, style: {}, getContext: () => ({ drawImage: image => drawn.push(image) }) };
    const region = new sandbox.Region(canvas, video), image = frame(44);
    region.frame(44, image); assert.equal(drawn.length, 0);
    region.map(44, [1280, 720, 640, 64, 640, 448]);
    assert.equal(drawn[0], image); assert.equal(region.painted, 44);
    region.retireDecoded(); image.close();
    video.videoWidth = 640; video.videoHeight = 448;
    region.map(44, [1280, 720, 640, 64, 640, 448]);
    assert.equal(drawn.length, 1, 'retired RTP cannot repaint with HTML video');
    region.frame(45); region.map(45, [1280, 720, 640, 64, 640, 448]);
    assert.equal(drawn[1], video);
}
// Exercise the production handoff itself: delayed mapping cannot ACK an
// undrawn frame, and returning to HTML cannot move backwards across RTP wrap.
{
    const reports = [], captures = [], advances = [];
    const video = { videoWidth: 640, videoHeight: 448, style: {} };
    const canvas = { style: {}, getContext: () => ({ drawImage() {} }) };
    const region = new sandbox.Region(canvas, video);
    const handoff = vm.createContext({ videoRegions: region, document: { hidden: false },
        exactRenderer: { enabled: true, videoFrame: rtp => advances.push(rtp) },
        exactCompositor: { capture: () => captures.push(true) },
        syncExactGeometry() {}, reportVideoPresented: rtp => reports.push(rtp),
    });
    const start = source.indexOf('        let presentationRtp = null');
    const finish = source.indexOf('        const decodedPresenter = ', start);
    assert.ok(start > 0 && finish > start);
    vm.runInContext(source.slice(start, finish) + '\nglobalThis.present = presentVideoFrame; globalThis.finish = finishVideoPresentation;', handoff);
    const mapping = [1280, 720, 640, 64, 640, 448];
    region.map(0xfffffffe, mapping);
    assert.equal(handoff.present(0xfffffffe, 10, frame(0xfffffffe)), true);
    assert.deepEqual(reports, [0xfffffffe]);
    assert.equal(handoff.present(1, 20, frame(1)), true);
    assert.deepEqual(reports, [0xfffffffe], 'missing mapping cannot report presentation');
    region.map(0xfffffffe, mapping); handoff.finish(0xfffffffe, 21);
    assert.equal(reports.length, 1, 'late metadata for an older base cannot advance fences');
    region.map(1, mapping); handoff.finish(1, 22);
    assert.deepEqual(reports, [0xfffffffe, 1]);
    region.retireDecoded();
    assert.equal(handoff.present(0xffffffff, 23), false);
    assert.equal(handoff.present(1, 24), false);
    region.map(2, mapping); assert.equal(handoff.present(2, 25), true);
    assert.deepEqual(reports, [0xfffffffe, 1, 2]);
    assert.deepEqual(advances, reports); assert.equal(captures.length, reports.length);
}
console.log('QSM_DECODED_PRESENTATION_OK');
