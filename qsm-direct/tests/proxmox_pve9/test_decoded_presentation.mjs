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
// Pictures decoded ahead of the playout buffer replace the track reader, keep
// its bounded queue and one draw per tick, close every picture and fall back.
{
    const h = harness(), shown = [], retired = [];
    const p = new sandbox.Presenter(h.scope, (image, rtp) => { shown.push(rtp); }, () => retired.push(1));
    let fallbacks = 0; p.onDirectFailed = () => { fallbacks++; p.configure(h.sourceTrack, true); };
    p.configure(h.sourceTrack, true);
    assert.equal(h.readers.length, 1);
    const denied = frame(1); p.pushDirect(denied, 1, false);
    assert.equal(denied.closed, 1); assert.equal(h.readers[0].cancellations, 0, 'a denied picture keeps the reader');
    const invalid = frame(2); p.pushDirect(invalid, -1, true); assert.equal(invalid.closed, 1);
    // A TCP burst of three keeps every picture; a fourth overflows the bound.
    const images = [frame(3), frame(4), frame(5), frame(6)];
    for (const [i, image] of images.entries()) { p.pushDirect(image, 3 + i, true); }
    assert.equal(h.readers[0].cancellations, 1, 'first direct picture retires the track reader');
    assert.equal(h.clones[0].stops, 1);
    assert.equal(images[0].closed, 1, 'queue stays bounded to three pictures');
    h.tick(16); h.tick(16); h.tick(16);
    assert.deepEqual(shown, [4, 5, 6]); assert.equal(images[2].closed, 1); assert.equal(images[3].closed, 0);
    // An older picture past the age bound is not drawn.
    const stale = [frame(8), frame(9)];
    p.pushDirect(stale[0], 8, true); h.tick(80); assert.equal(stale[0].closed, 1);
    assert.deepEqual(shown, [4, 5, 6]);
    p.configure(h.sourceTrack, true);
    assert.equal(h.readers.length, 1, 'an active direct path is not replaced by the reader');
    // Short silence lends the display to the browser path; fresh pictures return.
    for (let i = 0; i < 4; ++i) { h.timeout(50); }
    assert.equal(p.active, false, 'the browser path presents while direct is silent');
    assert.equal(p.directUsable, true);
    const fresh = frame(10); p.pushDirect(fresh, 10, true);
    assert.equal(p.active, true, 'a fresh direct picture takes the display back');
    h.tick(16); assert.deepEqual(shown.at(-1), 10);
    // Long silence retires the path permanently and restores the browser's own picture.
    for (let i = 0; i < 101; ++i) { h.timeout(50); }
    assert.equal(fallbacks, 1); assert.equal(p.directUsable, false); assert.equal(p.directFailure, 'silent'); assert.equal(fresh.closed, 1); stale[1].close();
    assert.equal(h.readers.length, 2);
    const late = frame(6); p.pushDirect(late, 6, true);
    assert.equal(late.closed, 1, 'a failed direct path never returns');
    assert.equal(h.readers[1].cancellations, 0);
    p.stop(); assert.equal(h.rafs.size + h.timers.size, 0);
    assert.ok(retired.length >= 2);
}
// An interrupted direct path lends the display to the browser and stays usable:
// its next picture takes the display back from the track reader.
{
    const h = harness(), shown = [];
    let updates = 0;
    const p = new sandbox.Presenter(h.scope, (image, rtp) => { shown.push(rtp); });
    p.onDirectFailed = () => { updates++; p.configure(h.sourceTrack, true); };
    const first = frame(20); p.pushDirect(first, 20, true);
    h.tick(16); assert.deepEqual(shown, [20]);
    p.directInterrupted('Decoding error.');
    assert.equal(first.closed, 1); assert.equal(updates, 1);
    assert.equal(p.directUsable, true); assert.equal(p.directFailure, undefined);
    assert.equal(p.directRestarts, 1); assert.equal(p.directLastError, 'Decoding error.');
    assert.equal(h.readers.length, 1, 'the browser path presents meanwhile');
    const next = frame(30); p.pushDirect(next, 30, true);
    assert.equal(h.readers[0].cancellations, 1); assert.equal(p.active, true);
    h.tick(16); assert.deepEqual(shown, [20, 30]);
    p.stop(); assert.equal(next.closed, 1); assert.equal(h.rafs.size + h.timers.size, 0);
}
// Disabling presentation stops direct pictures and releases them.
{
    const h = harness();
    const p = new sandbox.Presenter(h.scope, () => {});
    const image = frame(7); p.pushDirect(image, 7, true);
    p.configure(h.sourceTrack, false);
    assert.equal(image.closed, 1); assert.equal(p.active, false); assert.equal(h.rafs.size + h.timers.size, 0);
}
// The actual worker source against a fake decoder: the original frame always
// passes unchanged; pictures follow the SPS; every failure leaves the browser path.
{
    const startAt = source.indexOf('const source = `', source.indexOf('createVideoReceiveFeedback')) + 'const source = `'.length;
    const workerSource = source.slice(startAt, source.indexOf('`;', startAt));
    assert.ok(!workerSource.includes('${'), 'worker source has no interpolation');
    const run = async ({ noDecoder = false, frames, onDecode = null, show = true }) => {
        const messages = [], decoders = [];
        class FakeDecoder {
            constructor(init) { this.init = init; this.configs = []; this.chunks = []; this.closed = 0; this.decodeQueueSize = 0; decoders.push(this); }
            configure(config) { this.configs.push(config); }
            decode(chunk) { this.chunks.push(chunk); onDecode?.(this, chunk); }
            close() { this.closed++; }
        }
        const clock = { offset: 0 };
        const context = vm.createContext({ TransformStream, performance: { now: () => performance.now() + clock.offset }, Map, String, Uint8Array, Number,
            EncodedVideoChunk: class { constructor(v) { Object.assign(this, v); } },
            self: { postMessage: (m, t) => messages.push({ m, t }) } });
        if (!noDecoder) { context.VideoDecoder = FakeDecoder; }
        vm.runInContext(workerSource, context);
        const self = context.self;
        self.onmessage({ data: show ? 'show' : 'hide' });
        const outgoing = [], stream = new TransformStream();
        const writable = new WritableStream({ write(frame) { outgoing.push(frame); } });
        self.onrtctransform({ transformer: { readable: stream.readable, writable } });
        const writer = stream.writable.getWriter();
        for (const f of frames) { await writer.write(f); }
        await writer.close();
        for (let i = 0; i < 8; ++i) { await new Promise(r => setImmediate(r)); }
        return { messages, decoders, outgoing, self, clock };
    };
    const key = (rtp, sps) => ({ type: 'key', data: Uint8Array.from([0, 0, 0, 1, 0x67, ...sps, 0, 0, 1, 0x68, 1, 0, 0, 1, 0x65, 9]).buffer, getMetadata: () => ({ rtpTimestamp: rtp }) });
    const delta = rtp => ({ type: 'delta', data: Uint8Array.from([0, 0, 0, 1, 0x41, 7, 7, 7, 7, 7, 7, 7]).buffer, getMetadata: () => ({ rtpTimestamp: rtp }) });
    const directFailed = r => r.messages.filter(x => x.m.op === 'direct-failed').length;
    // Deltas before the first key frame cannot start a decoder; all still pass.
    {
        const frames = [delta(1), key(2, [0x42, 0xe0, 0x1f]), delta(3), key(4, [0x42, 0xe0, 0x1f]), key(5, [0x64, 0, 0x28, 1]), delta(6)];
        const r = await run({ frames });
        assert.deepEqual(r.outgoing, frames, 'originals pass unchanged and in order');
        assert.equal(r.decoders.length, 1);
        assert.deepEqual(r.decoders[0].chunks.map(c => [c.timestamp, c.type]), [[2, 'key'], [3, 'delta'], [4, 'key'], [5, 'key'], [6, 'delta']]);
        assert.deepEqual(r.decoders[0].configs.map(c => c.codec), ['avc1.42e01f', 'avc1.640028'], 'only a changed SPS reconfigures');
        assert.ok(r.decoders[0].configs.every(c => c.optimizeForLatency === true));
        assert.equal(directFailed(r), 0);
        // Decoded pictures reach the page by transfer, only while shown.
        const picture = { timestamp: 2, closed: 0, close() { this.closed++; } };
        r.decoders[0].init.output(picture);
        assert.equal(r.messages.at(-1).m.op, 'frame'); assert.equal(r.messages.at(-1).m.rtp, 2);
        assert.equal(r.messages.at(-1).t[0], picture); assert.equal(picture.closed, 0);
        r.self.onmessage({ data: 'hide' });
        const hidden = { timestamp: 3, closed: 0, close() { this.closed++; } };
        r.decoders[0].init.output(hidden); assert.equal(hidden.closed, 1);
    }
    // HEVC starts the same direct path. Its profile fields are read past emulation
    // prevention, and only a changed SPS reconfigures.
    {
        const hevcKey = (rtp, ptl) => ({ type: 'key', data: Uint8Array.from([0, 0, 0, 1, 0x40, 1, 0x0c, 1, 0, 0, 0, 1, 0x42, 1, ...ptl, 0, 0, 0, 1, 0x44, 1, 0xc0, 0, 0, 0, 1, 0x26, 1, 9]).buffer, getMetadata: () => ({ rtpTimestamp: rtp }) });
        const main = [1, 1, 0x60, 0, 0, 3, 0, 0xb0, 0, 0, 3, 0, 0, 3, 0, 0x5d, 0xa0, 2, 0x80];
        const main10High = [1, 0x22, 0x20, 0, 0, 3, 0, 0xb0, 0, 0, 3, 0, 0, 3, 0, 0x78, 0xa0, 2, 0x80];
        const inner = [1, 1, 0x60, 0, 0, 3, 0, 0x90, 0, 0, 3, 0, 0x12, 0, 0x5d, 0xa0, 2, 0x80];
        const frames = [hevcKey(2, main), delta(3), hevcKey(4, main), hevcKey(5, main10High), hevcKey(6, inner)];
        const r = await run({ frames });
        assert.deepEqual(r.outgoing, frames);
        assert.equal(r.decoders.length, 1);
        assert.deepEqual(r.decoders[0].configs.map(c => c.codec), ['hvc1.1.6.L93.B0', 'hvc1.2.4.H120.B0', 'hvc1.1.6.L93.90.00.00.00.12']);
        assert.deepEqual(r.decoders[0].chunks.map(c => c.type), ['key', 'delta', 'key', 'key', 'key']);
        assert.equal(directFailed(r), 0);
    }
    // A key frame with no SPS of either codec ends the optional path; frames still pass.
    {
        const bare = { type: 'key', data: Uint8Array.from([0, 0, 0, 1, 0x65, 1, 2, 3, 4, 5, 6, 7]).buffer, getMetadata: () => ({ rtpTimestamp: 1 }) };
        const r = await run({ frames: [bare] });
        assert.deepEqual(r.outgoing, [bare]);
        assert.equal(r.decoders.length, 0);
        assert.equal(directFailed(r), 1);
    }
    // No decoder API: frames pass, nothing is reported as a failure.
    {
        const frames = [key(1, [0x42, 0xe0, 0x1f]), delta(2)];
        const r = await run({ noDecoder: true, frames });
        assert.deepEqual(r.outgoing, frames); assert.equal(directFailed(r), 0);
    }
    // A key frame with only a VPS has no SPS of either codec and ends direct decode once.
    {
        const hevc = { type: 'key', data: Uint8Array.from([0, 0, 0, 1, 0x40, 1, 2, 3, 4, 5, 6, 7]).buffer, getMetadata: () => ({ rtpTimestamp: 1 }) };
        const frames = [hevc, hevc, delta(2)];
        const r = await run({ frames });
        assert.deepEqual(r.outgoing, frames); assert.equal(directFailed(r), 1); assert.equal(r.decoders.length, 0);
    }
    // A throwing decoder or a backlog falls back; media keeps flowing.
    for (const onDecode of [() => { throw new Error('decode'); }, d => { d.decodeQueueSize = 61; }]) {
        const frames = [key(1, [0x42, 0xe0, 0x1f]), delta(2), delta(3), delta(4)];
        const r = await run({ frames, onDecode });
        assert.deepEqual(r.outgoing, frames); assert.equal(directFailed(r), 1); assert.equal(r.decoders[0].closed, 1);
    }
    // A decode error restarts at the next key frame and media keeps flowing.
    const decodeError = name => d => queueMicrotask(() => d.init.error(Object.assign(new Error('Decoding error.'), { name })));
    const restarted = r => r.messages.filter(x => x.m.op === 'direct-restart').length;
    {
        const sps = [0x42, 0xe0, 0x1f];
        const frames = [key(1, sps), delta(2), delta(3), delta(4), key(5, sps), delta(6)];
        const r = await run({ frames, onDecode: (d, c) => { if (c.timestamp === 3) { decodeError('EncodingError')(d); } } });
        assert.deepEqual(r.outgoing, frames);
        assert.equal(directFailed(r), 0); assert.equal(restarted(r), 1);
        assert.equal(r.decoders.length, 2, 'a closed decoder is replaced');
        assert.deepEqual(r.decoders[0].chunks.map(c => c.timestamp), [1, 2, 3]);
        assert.deepEqual(r.decoders[1].chunks.map(c => c.timestamp), [5, 6]);
        assert.equal(r.decoders[1].configs.length, 1, 'the new decoder is configured again from the same SPS');
    }
    // Errors that keep returning end the path, and so does an unsupported stream.
    {
        const sps = [0x42, 0xe0, 0x1f];
        const frames = [1, 2, 3, 4, 5, 6].map(n => key(n, sps));
        const r = await run({ frames, onDecode: decodeError('EncodingError') });
        assert.deepEqual(r.outgoing, frames);
        assert.equal(restarted(r), 3); assert.equal(directFailed(r), 1); assert.equal(r.decoders.length, 4);
        const unsupported = await run({ frames: [key(1, sps), key(2, sps)], onDecode: decodeError('NotSupportedError') });
        assert.equal(restarted(unsupported), 0); assert.equal(directFailed(unsupported), 1);
    }
    // A picture decoded too late never reaches the page; a current one does.
    {
        const r = await run({ frames: [key(100, [0x42, 0xe0, 0x1f]), delta(101)] });
        r.clock.offset = 500;
        const late = { timestamp: 100, closed: 0, close() { this.closed++; } };
        const count = r.messages.length;
        r.decoders[0].init.output(late);
        r.clock.offset = 0;
        assert.equal(late.closed, 1); assert.equal(r.messages.length, count);
        assert.equal(directFailed(r), 0, 'a late picture alone is not a failure');
        const current = { timestamp: 101, closed: 0, close() { this.closed++; } };
        r.decoders[0].init.output(current);
        assert.equal(current.closed, 0); assert.equal(r.messages.at(-1).m.op, 'frame');
    }
    // A picture that cannot be transferred closes and falls back.
    {
        const r = await run({ frames: [key(1, [0x42, 0xe0, 0x1f])] });
        const post = r.self.postMessage;
        r.self.postMessage = (m, t) => { if (t) { throw new Error('not transferable'); } post(m); };
        const picture = { timestamp: 1, closed: 0, close() { this.closed++; } };
        r.decoders[0].init.output(picture);
        assert.equal(picture.closed, 1); assert.equal(r.decoders[0].closed, 1); assert.equal(directFailed(r), 1);
    }
}
console.log('QSM_DECODED_PRESENTATION_OK');
