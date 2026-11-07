// The file manager's logic against a simulated helper that keeps the real rules.
import assert from 'node:assert/strict';
import { Blob } from 'node:buffer';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import vm from 'node:vm';

// Arrays made inside the sandbox have that realm's prototype.
const same = (actual, expected, message) => assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected, message);
const source = fs.readFileSync(process.env.QSM_FILES_SOURCE || new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const begin = source.indexOf('    // QSM_FILES_BEGIN');
const end = source.indexOf('    // QSM_FILES_END');
assert.ok(begin > 0 && end > begin);
const sandbox = vm.createContext({
    ArrayBuffer, DataView, Uint8Array, Map, Set, Error, Number, Promise, JSON, Math, Object, Array, String, Date, Intl,
    TextEncoder, TextDecoder, setTimeout, clearTimeout, setInterval, clearInterval, console,
    gettext: (text) => text, waitForIce: async () => {},
});
vm.runInContext(source.slice(begin, end) + `
Object.assign(globalThis, { qfmEncode, qfmDecode, qfmSafeName, qfmValidEntries, qfmJoin, qfmParent, qfmSize, qfmTarHeader,
    QfmRate, QfmLink, QfmTransfers, QfmJob, QfmError, QfmStagedFiles, QfmSpool, qfmFormat, QfmConsoleDemand });`, sandbox);
const { qfmEncode, qfmDecode, qfmSafeName, qfmValidEntries, qfmJoin, qfmParent, qfmSize, qfmTarHeader,
    QfmRate, QfmLink, QfmTransfers, QfmJob, QfmError, QfmStagedFiles, QfmSpool, qfmFormat, QfmConsoleDemand } = sandbox;

// ---- frames
{
    const payload = new Uint8Array([1, 2, 3]).buffer;
    const frame = qfmDecode(qfmEncode({ op: 'put', id: 7, path: '/tmp/имя\udc80' }, payload));
    assert.equal(frame.header.path, '/tmp/имя\udc80', 'non-ASCII and a lone surrogate survive');
    same([...frame.payload], [1, 2, 3]);
    assert.equal(qfmDecode(qfmEncode({ id: 1 })).payload, null);
    const encoded = new Uint8Array(qfmEncode({ path: 'é' }));
    assert.ok(encoded.slice(2).every((byte) => byte < 128), 'the header is ASCII');
    for (const bad of [new ArrayBuffer(0), new ArrayBuffer(1), 'text', new Uint8Array([0, 9, 1]).buffer, new Uint8Array([0, 2, 91, 93]).buffer]) {
        assert.throws(() => qfmDecode(bad));
    }
}

// ---- names a guest may choose
{
    for (const name of ['a.txt', 'имя', '.hidden', 'x y', '..a']) { assert.equal(qfmSafeName(name), true, name); }
    for (const name of ['', '.', '..', 'a/b', '../x', 'a\\b', 'a\u0000b', 'a\nb', 7, null, 'x'.repeat(4097)]) { assert.equal(qfmSafeName(name), false, String(name)); }
    const rows = qfmValidEntries([['ok', 'f', 3, 4, 420, null], ['a/b', 'f', 1, 1, 1, null], ['', 'f', 1, 1, 1, null],
        ['t', 'q', 1, 1, 1, null], 'x', ['short', 'f'], ['n', 'f', -1, 'x', 'y', 'bad'], ['l', 'l', 1, 1, 1, 'd']]);
    same(rows.map((row) => row.name), ['ok', 'n', 'l']);
    assert.equal(rows[1].size, 0);
    assert.equal(rows[1].target, null);
    assert.equal(rows[2].target, 'd');
    assert.throws(() => qfmValidEntries('x'));
    assert.equal(qfmJoin('/', 'a'), '/a');
    assert.equal(qfmJoin('/a', 'b'), '/a/b');
    assert.equal(qfmParent('/a/b'), '/a');
    assert.equal(qfmParent('/a'), '/');
    assert.equal(qfmParent('/'), '/');
    assert.equal(qfmSize(1536), '1.5 KB');
    assert.equal(qfmFormat('{0} of {1}', 1, 2), '1 of 2');
    assert.equal(QfmTransfers.alternative('/d/a.txt', 2), '/d/a (2).txt');
    assert.equal(QfmTransfers.alternative('a', 1), 'a (1)');
    assert.equal(QfmTransfers.alternative('/d/.bashrc', 1), '/d/.bashrc (1)');
}

// ---- yielding to the console, and taking all the rest
{
    assert.ok(QfmRate.TARGET_BACKGROUND < 80, 'below the standing queue where video cuts its rate');
    const step = QfmRate.SAMPLE_MS;
    // A path that carries `capacity` bytes per second, with a queue the echoes show.
    const path = (rate, { capacity, base = 20, samples = 600, noise = () => 0, start = 1000, consoleShare = () => 0 }) => {
        let now = start;
        let queue = 0;
        let moved = 0;
        for (let i = 0; i < samples; i += 1) {
            now += step;
            const free = capacity * Math.max(0, 1 - consoleShare(i));
            queue += rate.rate * step / 1000;
            const out = Math.min(queue, free * step / 1000);
            queue -= out;
            rate.deliver(out);
            if (i >= samples / 2) { moved += out; }
            rate.sample(base + (free > 0 ? queue / free * 1000 : 1000) + noise(i), now);
        }
        return { now, queue, used: moved / (capacity * (samples / 2) * step / 1000), delay: capacity ? queue / capacity * 1000 : 0 };
    };

    for (const capacity of [250e3, 2.5e6, 12.5e6, 125e6]) {
        for (const target of [QfmRate.TARGET_BACKGROUND, QfmRate.TARGET_FOREGROUND]) {
            const rate = new QfmRate();
            rate.target = target;
            const { used } = path(rate, { capacity, samples: 1500 });
            assert.ok(used > .9, `takes all of an empty path: ${capacity} B/s, target ${target}, used ${used.toFixed(2)}`);
        }
    }

    // It leaves what the console takes, and takes it back when the console is done.
    const shared = new QfmRate();
    let run = path(shared, { capacity: 2.5e6, samples: 600 });
    assert.ok(run.used > .9);
    run = path(shared, { capacity: 2.5e6, samples: 600, start: run.now, consoleShare: () => .5 });
    assert.ok(run.used > .42 && run.used < .55, `gives way to the console, used ${run.used.toFixed(2)}`);
    assert.ok(run.delay < 100, `without a standing queue, ${Math.round(run.delay)} ms`);
    run = path(shared, { capacity: 2.5e6, samples: 600, start: run.now });
    assert.ok(run.used > .85, `takes the freed bandwidth back, used ${run.used.toFixed(2)}`);

    // A busy main thread delays most echoes but rarely all: that is not a queue.
    const busy = new QfmRate();
    const noisy = path(busy, { capacity: 2.5e6, samples: 1000, noise: (i) => (i % 5 === 0 ? 0 : 37) });
    assert.ok(noisy.used > .85, `a busy page does not hold a transfer back, used ${noisy.used.toFixed(2)}`);

    // Nothing to send: no growth past the starting rate, and an old high rate fades.
    const idle = new QfmRate();
    let idleNow = 1000;
    for (let i = 0; i < 400; i += 1) { idleNow += step; idle.sample(20, idleNow); }
    assert.ok(idle.rate <= QfmRate.START * 1.1, 'an idle link does not build up a rate');
    idle.rate = 8 * 1024 * 1024;
    for (let i = 0; i < 300; i += 1) { idleNow += step; idle.sample(20, idleNow); }
    assert.ok(idle.rate < 8 * 1024 * 1024 * .1, 'a rate nothing uses fades');

    // At a low rate arrivals come in rare frames. Chance must not read as a full path.
    const grainy = new QfmRate();
    grainy.rate = QfmRate.MIN;
    grainy.capacity = 200 * 1024;
    let grainNow = 1000;
    let owed = 0;
    for (let i = 0; i < 300; i += 1) {
        grainNow += step;
        owed += grainy.rate * step / 1000;
        while (owed >= grainy.chunk()) { grainy.deliver(grainy.chunk()); owed -= grainy.chunk(); }
        grainy.sample(i % 40 < 8 ? 141 : 100, grainNow);
    }
    assert.ok(grainy.capacity >= 100 * 1024, `a low rate with spikes does not teach a low width, saw ${Math.round(grainy.capacity)}`);
    assert.ok(grainy.rate > 100 * 1024, `it climbs back beside them, reached ${Math.round(grainy.rate)}`);

    // At a very low rate a chunk spans many samples, and that still counts as wanting more.
    const slow = new QfmRate();
    slow.rate = QfmRate.MIN;
    const low = path(slow, { capacity: 2.5e6, samples: 1200 });
    assert.ok(low.used > .85, `it climbs from the floor, used ${low.used.toFixed(2)}`);

    // A full path: the rate goes to what it carries, not to half of anything.
    const full = new QfmRate();
    full.rate = 4e6;
    let clock = 1000;
    full.deliver(2.5e5);
    full.sample(20, clock);
    for (let i = 0; i < 2; i += 1) { clock += step; full.deliver(2.5e5); full.sample(120, clock); }
    assert.ok(full.rate >= 1.9e6 && full.rate < 2.5e6, `below what was carried, by little, reached ${Math.round(full.rate)}`);
    const held = full.rate;
    for (let i = 0; i < 2; i += 1) { clock += step; full.deliver(2.5e5); full.sample(120, clock); }
    assert.equal(full.rate, held, 'one queue is paid for once, not on every sample');

    // Its own delay is the path's business: when that grows, the transfer finds out soon.
    const longer = new QfmRate();
    let run2 = path(longer, { capacity: 2.5e6, base: 15, samples: 400 });
    assert.ok(run2.used > .85);
    run2 = path(longer, { capacity: 2.5e6, base: 100, samples: 300, start: run2.now });
    assert.ok(run2.used > .75, `a longer path is not a queue, used ${run2.used.toFixed(2)}`);
    run2 = path(longer, { capacity: 2.5e6, base: 15, samples: 300, start: run2.now });
    assert.ok(run2.used > .8, `and neither is a shorter one, used ${run2.used.toFixed(2)}`);

    // Every so often it steps aside, and the delay it then sees is the path's own.
    const drain = new QfmRate();
    let drainNow = 1000;
    let lowest = Infinity;
    let drains = 0;
    let wasDraining = false;
    for (let i = 0; i < 400; i += 1) {
        drainNow += step;
        drain.deliver(drain.current() * step / 1000);
        drain.sample(drain.draining ? 18 : 18 + (i % 2), drainNow);
        lowest = Math.min(lowest, drain.current() / drain.rate);
        if (drain.draining && !wasDraining) { drains += 1; }
        wasDraining = drain.draining;
    }
    assert.ok(drains >= 2 && drains <= 3, `a drain every 15 s, saw ${drains}`);
    assert.ok(lowest <= .25, 'and while it lasts the pace falls');
    assert.equal(drain.floorDelay, 18);

    const floor = new QfmRate();
    floor.pinged(0);
    for (let t = 1000; t < 60000; t += 1000) { floor.tick(t); }
    assert.equal(floor.rate, QfmRate.MIN, 'it keeps a floor, so the transfer does not stop for good');
    assert.equal(floor.window(), 64 * 1024);
    assert.equal(floor.chunk(), 2048, 'on a narrow path frames are small');
    floor.rate = 100 * 1024 * 1024;
    assert.equal(floor.window(), 4 * 1024 * 1024);
    assert.equal(floor.chunk(), 32768);

    const silent = new QfmRate();
    silent.sample(20, 1000);
    silent.pinged(1100);
    const heard = silent.rate;
    silent.tick(1350);
    assert.equal(silent.rate, heard, 'an echo may take its time');
    silent.tick(1500);
    assert.ok(silent.rate < heard, 'an echo that never returns counts as congestion');

    // In a hidden tab pings are a second apart, and each echo returns at once.
    const hidden = new QfmRate();
    hidden.rate = 1024 * 1024;
    clock = 1000;
    for (let i = 0; i < 60; i += 1) {
        hidden.pinged(clock);
        clock += 20;
        hidden.deliver(hidden.rate);
        hidden.sample(20, clock);
        clock += 980;
        hidden.tick(clock);
    }
    assert.ok(hidden.rate > 1024 * 1024 * 2, `a hidden tab goes on and even grows, reached ${Math.round(hidden.rate)}`);
}

// ---- video the path holds back goes first
await (async () => {
    let now = 0;
    let bytes = 0;
    let rate = { bitrate: 1800, maximum: 8000 };
    const demand = new QfmConsoleDemand({ videoRate: () => rate, now: () => now,
        stats: async () => new Map([['a', { type: 'inbound-rtp', kind: 'video', bytesReceived: bytes }], ['b', { type: 'inbound-rtp', kind: 'audio', bytesReceived: 1e9 }]]) });
    assert.equal(await demand.wants(), false, 'nothing to compare with yet');
    now += 1000; bytes += 225000; // 1800 kbit/s in one second
    assert.equal(await demand.wants(), false, 'one busy second is a repaint or a key frame');
    now += 1000; bytes += 225000;
    assert.equal(await demand.wants(), false);
    now += 1000; bytes += 225000;
    assert.equal(await demand.wants(), true, 'a clip below its maximum');
    now += 1000; bytes += 225000;
    assert.equal(await demand.wants(), true);
    now += 1000; bytes += 225000;
    assert.equal(await demand.wants(), true);
    now += 1000; bytes += 2000;
    assert.equal(await demand.wants(), true, 'one dip of a clip under pressure changes nothing');
    let idleSeconds = 0;
    for (; idleSeconds < 20; idleSeconds += 1) {
        now += 1000; bytes += 2000;
        if (!await demand.wants()) { break; }
    }
    assert.ok(idleSeconds >= 1 && idleSeconds < 20, `a clip that really stopped lets go, after ${idleSeconds} s`);
    now += 1000; bytes += 2000;
    assert.equal(await demand.wants(), false, 'a low target on an idle desktop holds nothing up');
    rate = { bitrate: 500, maximum: 8000 };
    now += 1000; bytes += 37500; // 300 kbit/s, as an idle desktop can send
    assert.equal(await demand.wants(), false, 'even past half of a very low target');
    rate = { bitrate: 1800, maximum: 8000 };
    rate = { bitrate: 8000, maximum: 8000 };
    now += 1000; bytes += 1000000;
    assert.equal(await demand.wants(), false, 'a clip with all it wants leaves the rest');
    rate = null;
    now += 1000; bytes += 225000;
    assert.equal(await demand.wants(), false);

    const aside = new QfmRate();
    aside.rate = 2e6;
    aside.sample(20, 1000);
    const before = aside.rate;
    aside.standingAside = true;
    assert.equal(aside.current(), QfmRate.MIN);
    for (let i = 1; i < 30; i += 1) { aside.deliver(1600); aside.sample(i % 2 ? 80 : 20, 1000 + i * 100); }
    assert.equal(aside.rate, before, 'nothing is learned about the path meanwhile');
    aside.standingAside = false;
    assert.equal(aside.current(), before, 'and it goes on from where it was');
})();

// ---- a tar that a real tar reads
{
    const chunks = [];
    const push = (bytes) => chunks.push(Buffer.from(bytes));
    const body = Buffer.from('hello tar');
    push(qfmTarHeader('dir/', 0, 0, '5'));
    push(qfmTarHeader('dir/a.txt', body.length, 1700000000000, '0'));
    push(body); push(new Uint8Array(512 - body.length));
    const longName = `dir/${'n'.repeat(180)}-имя.txt`;
    push(qfmTarHeader(longName, 3, 1700000000000, '0'));
    push(Buffer.from('abc')); push(new Uint8Array(509));
    push(new Uint8Array(1024));
    const tar = spawnSync('tar', ['-tvf', '-'], { input: Buffer.concat(chunks) });
    if (tar.error) {
        console.log('tar is not available, archive listing not checked');
    } else {
        assert.equal(tar.status, 0, tar.stderr.toString());
        const listing = tar.stdout.toString();
        assert.ok(listing.includes('dir/a.txt'));
        assert.ok(listing.includes(`${'n'.repeat(180)}-`), 'a long name survives through PAX');
        const extracted = spawnSync('tar', ['-xOf', '-', 'dir/a.txt'], { input: Buffer.concat(chunks) });
        assert.equal(extracted.stdout.toString(), 'hello tar');
    }
    const large = qfmTarHeader('big', 9 * 1024 * 1024 * 1024, 0, '0');
    assert.equal(large.length, 1536, 'past 8 GiB the size goes in a PAX record');
}

// ---- simulated connection and helper
const settle = () => new Promise((resolve) => setImmediate(resolve));

class Helper {
    constructor() {
        this.files = new Map();
        this.dirs = new Set(['/', '/home', '/home/u']);
        this.locked = false;
        this.puts = new Map();
        this.gets = new Map();
        this.granted = [];
        this.maxUnacked = 0;
        this.ackEvery = 64 * 1024;
        this.stallGets = false;
    }

    attach(channel) { this.channel = channel; }
    reply(header, payload) { this.channel.deliver(qfmEncode(header, payload)); }

    handle(frameBuffer) {
        const { header, payload } = qfmDecode(frameBuffer);
        const { op, id } = header;
        if (this.mute && ['put', 'data', 'end'].includes(op)) { return; }
        const fail = (err) => this.reply({ id, ok: false, err });
        const guarded = ['list', 'stat', 'mkdir', 'rename', 'delete', 'put', 'get'];
        if (this.locked && guarded.includes(op)) { fail('ELOCKED'); return; }
        if (op === 'hello') {
            this.reply({ id, ok: true, v: 1, home: '/home/u', user: 'u', rights: 'rw', locked: this.locked, chunk: 32768 });
        } else if (op === 'mkdir') {
            this.dirs.add(header.path);
            this.reply({ id, ok: true });
        } else if (op === 'list') {
            const entries = [];
            for (const [path, data] of this.files) {
                if (qfmParent(path) === header.path) { entries.push([path.slice(path.lastIndexOf('/') + 1), 'f', data.length, 1000, 420, null]); }
            }
            for (const path of this.dirs) {
                if (path !== '/' && qfmParent(path) === header.path) { entries.push([path.slice(path.lastIndexOf('/') + 1), 'd', 0, 1000, 493, null]); }
            }
            this.reply({ id, part: entries });
            this.reply({ id, ok: true, path: header.path, total: entries.length, truncated: false });
        } else if (op === 'put') {
            if (header.overwrite === 'fail' && this.files.has(header.path)) { this.puts.set(id, { failed: true }); fail('EEXIST'); return; }
            const upload = { path: header.path, size: header.size, parts: [], written: 0, acked: 0 };
            this.puts.set(id, upload);
            if (payload) { upload.parts.push(Buffer.from(payload)); upload.written += payload.length; }
            if (header.last) { this.finish(id, upload); }
        } else if (op === 'data') {
            const upload = this.puts.get(id);
            if (!upload || upload.failed) { return; }
            if (header.o !== upload.written) { this.puts.delete(id); fail('EINVAL'); return; }
            upload.parts.push(Buffer.from(payload));
            upload.written += payload.length;
            this.maxUnacked = Math.max(this.maxUnacked, upload.written - upload.acked);
            if (upload.written - upload.acked >= this.ackEvery) { upload.acked = upload.written; this.reply({ id, ack: upload.written }); }
        } else if (op === 'end') {
            const upload = this.puts.get(id);
            if (upload && !upload.failed) { this.finish(id, upload); }
        } else if (op === 'get') {
            const data = this.files.get(header.path);
            if (!data) { fail('ENOENT'); return; }
            const download = { data, offset: 0, credit: header.credit, id };
            this.gets.set(id, download);
            this.granted.push(header.credit);
            this.reply({ id, meta: true, size: data.length, mtime: 1000, mode: 420 });
            this.pumpGet(download);
        } else if (op === 'credit') {
            const download = this.gets.get(id);
            if (download) { download.credit += header.n; this.granted.push(header.n); this.pumpGet(download); }
        } else if (op === 'cancel') {
            if (this.puts.delete(id)) { fail('ECANCELED'); return; }
            if (this.gets.delete(id)) { fail('ECANCELED'); }
        } else {
            fail('EINVAL');
        }
    }

    finish(id, upload) {
        const data = Buffer.concat(upload.parts);
        this.puts.delete(id);
        if (data.length !== upload.size) { this.reply({ id, ok: false, err: 'EINVAL' }); return; }
        this.files.set(upload.path, data);
        this.reply({ id, ack: data.length });
        this.reply({ id, ok: true, size: data.length });
    }

    pumpGet(download) {
        if (this.stallGets) { return; }
        while (download.offset < download.data.length && download.credit > 0) {
            const length = Math.min(32768, download.credit, download.data.length - download.offset);
            const chunk = download.data.subarray(download.offset, download.offset + length);
            const payload = chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.length);
            this.reply({ id: download.id, o: download.offset }, payload);
            download.offset += length;
            download.credit -= length;
        }
        if (download.offset >= download.data.length) {
            this.gets.delete(download.id);
            this.reply({ id: download.id, ok: true, size: download.data.length, changed: false });
        }
    }
}

class FakeChannel extends EventTarget {
    constructor(onSend) {
        super();
        this.readyState = 'open';
        this.bufferedAmount = 0;
        this.onSend = onSend;
    }
    send(data) { this.onSend(data); }
    deliver(data) { setImmediate(() => this.dispatchEvent(Object.assign(new Event('message'), { data }))); }
}

class FakePeer extends EventTarget {
    constructor(helper) {
        super();
        this.helper = helper;
        this.localDescription = { sdp: 'v=0' };
        this.connectionState = 'connected';
    }
    createDataChannel(label) {
        if (label === 'qsm-files') {
            const channel = new FakeChannel((data) => {
                if (typeof data !== 'string') { setImmediate(() => this.helper.handle(data)); }
            });
            this.helper.attach(channel);
            setImmediate(() => channel.deliver('{"op":"status","state":"ready","reason":null}'));
            return channel;
        }
        const rtt = new FakeChannel((data) => {
            const value = JSON.parse(data);
            if ('t' in value) { rtt.deliver(data); }
        });
        return rtt;
    }
    async createOffer() { return { type: 'offer', sdp: 'v=0' }; }
    async setLocalDescription() {}
    async setRemoteDescription() {}
    close() { this.closed = true; }
}

const timers = { now: () => performance.now(), setTimeout, clearTimeout, setInterval, clearInterval };
const openLink = async (helper) => {
    const link = new QfmLink({ createPeer: async () => new FakePeer(helper), offer: async () => ({ sdp: 'v=0' }), timers, isHidden: () => false });
    link.rate.rate = 256 * 1024 * 1024;
    assert.equal((await link.open()).home, '/home/u');
    return link;
};
const transfersFor = (link, { ask = async () => ({ choice: 'skip', all: false }), spool } = {}) => new QfmTransfers({
    link: () => link, now: () => performance.now(), sleep: (ms) => new Promise((resolve) => setTimeout(resolve, Math.min(ms, 5))),
    ask, notify: () => {}, spool,
});
const blob = (size, seed = 1) => {
    const data = Buffer.alloc(size);
    for (let i = 0; i < size; i += 1) { data[i] = (i * seed + (i >> 8)) & 255; }
    return data;
};
class File extends Blob {
    constructor(parts, name, { lastModified }) {
        super(parts);
        this.name = name;
        this.lastModified = lastModified;
    }
}
const staged = (files) => {
    const tree = new QfmStagedFiles();
    for (const [path, data] of Object.entries(files)) {
        tree.add(path, data === null ? null : new File([data], path.split('/').pop(), { lastModified: 5000 }));
    }
    return tree;
};

await (async () => {
    const helper = new Helper();
    const link = await openLink(helper);

    // A tree with small, chunk-sized and large files, and an empty folder.
    {
        const tree = staged({ 'top/small.txt': blob(100), 'top/exact.bin': blob(32768), 'top/sub/big.bin': blob(300000, 3), 'top/empty': null });
        const job = new QfmJob('up', 'up');
        await transfersFor(link).upload(job, tree, await tree.readdir(), '/home/u');
        same(job.errors, []);
        assert.equal(job.filesDone, 3);
        assert.equal(job.done, job.total);
        assert.ok(helper.dirs.has('/home/u/top/empty'));
        assert.ok(helper.dirs.has('/home/u/top/sub'));
        assert.ok(helper.files.get('/home/u/top/sub/big.bin').equals(blob(300000, 3)));
        assert.ok(helper.files.get('/home/u/top/exact.bin').equals(blob(32768)));
        assert.equal(link.inflight, 0, 'the whole window is free again');
        assert.ok(helper.maxUnacked <= link.rate.window());
    }

    // Conflicts: skip, keep both, replace, and "the same for the rest".
    {
        helper.files.set('/home/u/c.txt', blob(10));
        const send = async (choice, all = false) => {
            const tree = staged({ 'c.txt': blob(20, 7) });
            const job = new QfmJob('up', 'up');
            const asked = [];
            await transfersFor(link, { ask: async (path) => { asked.push(path); return { choice, all }; } })
                .upload(job, tree, await tree.readdir(), '/home/u');
            same(asked, ['/home/u/c.txt']);
            return job;
        };
        const skipped = await send('skip');
        assert.equal(skipped.skipped, 1);
        assert.equal(skipped.total, 0);
        assert.equal(helper.files.get('/home/u/c.txt').length, 10);
        await send('rename');
        assert.equal(helper.files.get('/home/u/c (1).txt').length, 20);
        await send('replace');
        assert.equal(helper.files.get('/home/u/c.txt').length, 20);
        const cancelled = await send('cancel');
        assert.equal(cancelled.cancelled, true);

        // A large file that exists: the failure stops its data and frees the window.
        helper.files.set('/home/u/large', blob(10));
        const tree = staged({ large: blob(500000) });
        const job = new QfmJob('up', 'up');
        await transfersFor(link).upload(job, tree, await tree.readdir(), '/home/u');
        assert.equal(job.skipped, 1);
        assert.equal(helper.files.get('/home/u/large').length, 10);
        await settle();
        assert.equal(link.inflight, 0);

        // One answer covers the rest of the copy, and the question is asked once.
        helper.files.set('/home/u/m1', blob(1));
        helper.files.set('/home/u/m2', blob(1));
        const many = staged({ m1: blob(5), m2: blob(5) });
        const manyJob = new QfmJob('up', 'up');
        let asked = 0;
        await transfersFor(link, { ask: async () => { asked += 1; await settle(); return { choice: 'replace', all: true }; } })
            .upload(manyJob, many, await many.readdir(), '/home/u');
        assert.equal(asked, 1);
        assert.equal(helper.files.get('/home/u/m2').length, 5);
    }

    // Cancelling an upload stops it and frees the window at once, even with no acks coming.
    {
        helper.ackEvery = 1 << 40;
        link.rate.rate = 256 * 1024;
        const tree = staged({ stuck: blob(2000000) });
        const job = new QfmJob('up', 'up');
        const run = transfersFor(link).upload(job, tree, await tree.readdir(), '/home/u');
        while (link.inflight === 0) { await settle(); }
        await new Promise((resolve) => setTimeout(resolve, 30));
        job.cancel();
        await run;
        assert.equal(helper.files.has('/home/u/stuck'), false);
        assert.equal(link.inflight, 0);
        assert.equal(job.done, 0, 'progress of a cancelled file is taken back');
        helper.ackEvery = 64 * 1024;
        link.rate.rate = 256 * 1024 * 1024;
    }

    // Download into a folder sink, with credit.
    {
        helper.files.set('/home/u/d/a.bin', blob(700000, 5));
        helper.dirs.add('/home/u/d');
        const written = new Map();
        const rootFiles = new Map();
        const folder = (name) => ({
            kind: 'directory', name, children: new Map(),
            async getDirectoryHandle(child, options) {
                if (!this.children.has(child)) {
                    if (!options?.create) { throw new Error('NotFound'); }
                    this.children.set(child, folder(child));
                }
                return this.children.get(child);
            },
            async getFileHandle(child, options) {
                if (!this.children.has(child)) {
                    if (!options?.create) { throw new Error('NotFound'); }
                    const handle = { kind: 'file', name: child, data: null, async createWritable() {
                        const parts = [];
                        return {
                            write: async ({ position, data }) => { parts.push([position, Buffer.from(data)]); },
                            close: async () => { handle.data = Buffer.concat(parts.map((part) => part[1])); written.set(child, handle.data); },
                            abort: async () => { parts.length = 0; handle.aborted = true; },
                        };
                    } };
                    this.children.set(child, handle);
                }
                return this.children.get(child);
            },
        });
        const target = { handle: folder('root') };
        const job = new QfmJob('down', 'down');
        helper.granted.length = 0;
        const entry = { name: 'd', type: 'd', size: 0, mtime: 0, mode: 0, target: null };
        await transfersFor(link).downloadToFolders(job, [{ entry, path: '/home/u/d' }], target);
        same(job.errors, []);
        assert.ok(written.get('a.bin').equals(blob(700000, 5)));
        assert.ok(helper.granted[0] <= 128 * 1024, 'the first credit is small');
        assert.ok(helper.granted.length > 1, 'more credit follows what was written');
        assert.equal(job.done, 700000);

        // An existing local file is asked about; "keep both" leaves it alone.
        const second = new QfmJob('down', 'down');
        written.clear();
        await transfersFor(link, { ask: async () => ({ choice: 'rename', all: false }) })
            .downloadToFolders(second, [{ entry, path: '/home/u/d' }], target);
        assert.ok(written.has('a (1).bin'));

        // A broken local write ends that file with its own reason, and the guest stops sending.
        const brokenTarget = { handle: folder('broken') };
        const realGet = brokenTarget.handle.getFileHandle.bind(brokenTarget.handle);
        brokenTarget.handle.getFileHandle = async (child, options) => {
            const handle = await realGet(child, options);
            if (options?.create) {
                handle.createWritable = async () => ({
                    write: async () => { throw new QfmError('ENOSPC'); },
                    close: async () => {}, abort: async () => {},
                });
            }
            return handle;
        };
        const broken = new QfmJob('down', 'down');
        const flat = { name: 'a.bin', type: 'f', size: 700000, mtime: 0, mode: 0, target: null };
        await transfersFor(link).downloadToFolders(broken, [{ entry: flat, path: '/home/u/d/a.bin' }], brokenTarget);
        assert.equal(broken.errors.length, 1);
        assert.equal(broken.errors[0].error.code, 'ENOSPC');
        assert.equal(broken.done, 0);
        await settle();
        assert.equal(helper.gets.size, 0);

        // Cancelling a download that receives nothing still ends it.
        helper.stallGets = true;
        const stalled = new QfmJob('down', 'down');
        const run = transfersFor(link).downloadToFolders(stalled, [{ entry: flat, path: '/home/u/d/a.bin' }], { handle: folder('stalled') });
        while (helper.gets.size === 0) { await settle(); }
        stalled.cancel();
        await run;
        assert.equal(stalled.errors.length, 0);
        helper.stallGets = false;
    }

    // A guest's hostile names do not reach local APIs or an archive.
    {
        helper.dirs.add('/home/u/evil');
        const job = new QfmJob('down', 'down');
        const plan = await transfersFor(link).planDownload(job, [
            { entry: { name: '..', type: 'd' }, path: '/' },
            { entry: { name: 'bad\\name', type: 'f', size: 1 }, path: '/x' },
            { entry: { name: 'pipe', type: 'o', size: 0 }, path: '/home/u/pipe' },
        ], null);
        same(plan.files, []);
        same(plan.directories, []);
        assert.equal(job.skipped, 3);
    }

    // A guest cannot fill this disk: more than granted, or than it declared, ends the file.
    {
        // Writes take a moment, so no new credit goes out while the frames arrive.
        const sink = () => async () => ({ write: () => new Promise((resolve) => setTimeout(resolve, 30)), close: async () => {}, abort: async () => {} });
        const flooding = (head) => {
            const original = helper.handle.bind(helper);
            helper.handle = (buffer) => {
                const { header } = qfmDecode(buffer);
                if (header.op !== 'get') { original(buffer); return; }
                helper.handle = original;
                head(header.id);
            };
        };
        const chunk = new ArrayBuffer(32768);
        flooding((id) => {
            helper.reply({ id, meta: true, size: 1 << 30, mtime: 1, mode: 420 });
            for (let i = 0; i < 6; i += 1) { helper.reply({ id, o: i * 32768 }, chunk); }
        });
        const greedy = new QfmJob('down', 'down');
        await assert.rejects(transfersFor(link).getFile(greedy, '/x', sink()), (error) => error.code === 'EPROTO');
        assert.equal(greedy.done, 0, 'what was written of a failed file does not count');
        flooding((id) => {
            helper.reply({ id, meta: true, size: 10, mtime: 1, mode: 420 });
            helper.reply({ id, o: 0 }, chunk);
        });
        await assert.rejects(transfersFor(link).getFile(new QfmJob('down', 'down'), '/x', sink()), (error) => error.code === 'ECHANGED');
        flooding((id) => { helper.reply({ id, meta: true, size: -5, mtime: 1, mode: 420 }); });
        await assert.rejects(transfersFor(link).getFile(new QfmJob('down', 'down'), '/x', sink()), (error) => error.code === 'EPROTO');
        await settle();
        helper.gets.clear();

        // A file that changed while it was read does not replace anything.
        const events = [];
        const changing = (head) => {
            const original = helper.handle.bind(helper);
            helper.handle = (buffer) => {
                const { header } = qfmDecode(buffer);
                if (header.op !== 'get') { original(buffer); return; }
                helper.handle = original;
                helper.reply({ id: header.id, meta: true, size: 4, mtime: 1, mode: 420 });
                helper.reply({ id: header.id, o: 0 }, new Uint8Array(4).buffer);
                helper.reply({ id: header.id, ok: true, size: 4, changed: head });
            };
        };
        changing(true);
        await assert.rejects(transfersFor(link).getFile(new QfmJob('down', 'down'), '/x', async () => ({
            write: async () => events.push('write'), close: async () => events.push('close'), abort: async () => events.push('abort'),
        })), (error) => error.code === 'ECHANGED');
        same(events, ['write', 'abort']);
    }

    // A listing cut short is an error of the job, not a complete copy.
    {
        const realList = link.list.bind(link);
        link.list = async (path) => ({ entries: [], path, truncated: true });
        const job = new QfmJob('down', 'down');
        await transfersFor(link).planDownload(job, [{ entry: { name: 'huge', type: 'd', size: 0 }, path: '/huge' }], null);
        same(job.errors.map(({ error }) => error.code), ['ETRUNCATED']);
        link.list = realList;
        const many = [];
        for (let i = 0; i < 2; i += 1) { many.push(Array.from({ length: 150000 }, (_value, index) => [`n${i}-${index}`, 'f', 0, 0, 0, null])); }
        const original = helper.handle.bind(helper);
        helper.handle = (buffer) => {
            const { header } = qfmDecode(buffer);
            if (header.op !== 'list') { original(buffer); return; }
            helper.handle = original;
            link.pending.get(header.id).onFrame({ id: header.id, part: many[0] }, null);
            link.pending.get(header.id).onFrame({ id: header.id, part: many[1] }, null);
            helper.reply({ id: header.id, ok: true, path: header.path, total: 2, truncated: false });
        };
        const capped = await link.list('/big');
        assert.equal(capped.entries.length, 200000, 'a guest cannot grow a listing past its limit');
        assert.equal(capped.truncated, true);
    }

    // Two guest names that are one name here: both files survive, with no question asked.
    {
        helper.dirs.add('/home/u/cases');
        helper.files.set('/home/u/cases/A.txt', blob(30, 1));
        helper.files.set('/home/u/cases/a.txt', blob(40, 2));
        const stored = new Map();
        const folded = {
            async getDirectoryHandle() { return folded; },
            async getFileHandle(name, options) {
                const key = name.toLowerCase();
                if (!stored.has(key) && !options?.create) { throw new Error('NotFound'); }
                await settle();
                if (!stored.has(key)) { stored.set(key, { name, data: null }); }
                const item = stored.get(key);
                return { async createWritable() {
                    const parts = [];
                    return { write: async ({ data }) => { parts.push(Buffer.from(data)); }, close: async () => { item.data = Buffer.concat(parts); }, abort: async () => {} };
                } };
            },
            async removeEntry(name) { stored.delete(name.toLowerCase()); },
        };
        let asked = 0;
        const job = new QfmJob('down', 'down');
        await transfersFor(link, { ask: async () => { asked += 1; return { choice: 'replace', all: true }; } })
            .downloadToFolders(job, [{ entry: { name: 'cases', type: 'd', size: 0 }, path: '/home/u/cases' }], { handle: folded });
        assert.equal(asked, 0);
        same(job.errors, []);
        same([...stored.keys()].sort(), ['cases', 'cases (1)'].length ? [...stored.keys()].sort() : []);
        const sizes = [...stored.values()].map((item) => (item.data ? item.data.length : 0)).sort((a, b) => a - b);
        same(sizes.filter(Boolean), [30, 40], 'neither file was lost');
        helper.files.delete('/home/u/cases/A.txt');
        helper.files.delete('/home/u/cases/a.txt');
    }

    // A new local file that fails does not stay behind empty, and one that existed stays.
    {
        helper.files.set('/home/u/fails.bin', blob(10));
        const fresh = new Map();
        const place = {
            async getDirectoryHandle() { return place; },
            async getFileHandle(name, options) {
                if (!fresh.has(name)) {
                    if (!options?.create) { throw new Error('NotFound'); }
                    fresh.set(name, 'empty');
                }
                return { async createWritable() { return { write: async () => { throw new QfmError('ENOSPC'); }, close: async () => {}, abort: async () => {} }; } };
            },
            async removeEntry(name) { fresh.delete(name); },
        };
        const entry = { name: 'fails.bin', type: 'f', size: 10, mtime: 0, mode: 0, target: null };
        const job = new QfmJob('down', 'down');
        await transfersFor(link).downloadToFolders(job, [{ entry, path: '/home/u/fails.bin' }], { handle: place });
        same(job.errors.map(({ error }) => error.code), ['ENOSPC']);
        assert.equal(fresh.has('fails.bin'), false, 'the empty new file is gone');
        fresh.set('fails.bin', 'mine');
        await transfersFor(link, { ask: async () => ({ choice: 'replace', all: false }) })
            .downloadToFolders(new QfmJob('down', 'down'), [{ entry, path: '/home/u/fails.bin' }], { handle: place });
        assert.equal(fresh.get('fails.bin'), 'mine', 'a file that was there before is left alone');
    }

    // "Cancel the copy" ends an upload that another worker has in progress.
    {
        helper.files.set('/home/u/clash', blob(1));
        helper.ackEvery = 1 << 40;
        link.rate.rate = 256 * 1024;
        const tree = staged({ clash: blob(10), running: blob(4000000) });
        const job = new QfmJob('up', 'up');
        let finished = false;
        const run = transfersFor(link, { ask: async () => { while (link.inflight === 0) { await settle(); } return { choice: 'cancel', all: false }; } })
            .upload(job, tree, await tree.readdir(), '/home/u').then(() => { finished = true; });
        await Promise.race([run, new Promise((resolve) => setTimeout(resolve, 5000))]);
        assert.equal(finished, true, 'the job ended');
        assert.equal(job.cancelled, true);
        assert.equal(link.inflight, 0);
        assert.equal(helper.files.has('/home/u/running'), false);
        helper.ackEvery = 64 * 1024;
        link.rate.rate = 256 * 1024 * 1024;
    }

    // A request that the guest never answers can still be cancelled.
    {
        const original = helper.handle.bind(helper);
        let held = 0;
        helper.handle = (buffer) => {
            const { header } = qfmDecode(buffer);
            if (header.op === 'mkdir') { held = header.id; return; }
            if (header.op === 'cancel' && header.id === held) { helper.reply({ id: held, ok: false, err: 'ECANCELED' }); return; }
            original(buffer);
        };
        const job = new QfmJob('up', 'up');
        const tree = staged({ 'hung/x': blob(1) });
        const run = transfersFor(link).upload(job, tree, await tree.readdir(), '/home/u');
        while (!held) { await settle(); }
        job.cancel();
        await run;
        assert.equal(job.aborts.size, 0);
        same(job.errors, []);
        helper.handle = original;
    }

    // Symlinks: followed when selected, skipped inside a folder.
    {
        const listings = new Map([['/l', [['inner', 'l', 0, 0, 0, 'd'], ['file', 'f', 5, 0, 0, null]]]]);
        const realList = link.list.bind(link);
        link.list = async (path) => ({ entries: qfmValidEntries(listings.get(path)), path, truncated: false });
        const job = new QfmJob('down', 'down');
        const plan = await transfersFor(link).planDownload(job, [{ entry: { name: 'link', type: 'l', target: 'd', size: 0 }, path: '/l' }], null);
        same(plan.directories.map((parts) => parts.join('/')), ['link']);
        same(plan.files.map((file) => file.relative.join('/')), ['link/file']);
        assert.equal(job.skipped, 1);
        link.list = realList;
    }

    // A locked guest pauses a job, which goes on after unlock.
    {
        helper.locked = true;
        const tree = staged({ later: blob(50) });
        const job = new QfmJob('up', 'up');
        const run = transfersFor(link).upload(job, tree, await tree.readdir(), '/home/u');
        for (let i = 0; i < 20 && job.state !== 'locked'; i += 1) { await new Promise((resolve) => setTimeout(resolve, 2)); }
        assert.equal(job.state, 'locked');
        helper.locked = false;
        await run;
        same(job.errors, []);
        assert.equal(helper.files.get('/home/u/later').length, 50);
        assert.equal(job.state, 'running');
    }

    // Spooled downloads: one file as it is, several as one tar a real tar reads.
    {
        helper.files.set('/home/u/one.txt', blob(1000));
        const saved = [];
        const spoolFactory = async (name) => {
            const parts = [];
            return {
                name,
                append: async (data) => { parts.push(Buffer.from(data)); },
                pad: async (count) => { parts.push(Buffer.alloc(count)); },
                finish: async () => { saved.push({ name, data: Buffer.concat(parts) }); },
                abort: async () => { parts.length = 0; saved.push({ name, aborted: true }); },
            };
        };
        const single = new QfmJob('down', 'down');
        await transfersFor(link, { spool: spoolFactory }).downloadToSpool(single, [{ entry: { name: 'one.txt', type: 'f', size: 1000 }, path: '/home/u/one.txt' }], 'x.tar');
        assert.equal(saved[0].name, 'one.txt');
        assert.ok(saved[0].data.equals(blob(1000)));
        assert.equal(single.filesDone, 1);

        helper.files.set('/home/u/d/b.txt', blob(1234, 9));
        const archive = new QfmJob('down', 'down');
        await transfersFor(link, { spool: spoolFactory }).downloadToSpool(archive,
            [{ entry: { name: 'd', type: 'd', size: 0 }, path: '/home/u/d' }], 'd.tar');
        same(archive.errors, []);
        const tarFile = saved[1];
        assert.equal(tarFile.name, 'd.tar');
        const listing = spawnSync('tar', ['-tf', '-'], { input: tarFile.data });
        if (!listing.error) {
            assert.equal(listing.status, 0, listing.stderr.toString());
            same(listing.stdout.toString().trim().split('\n').sort(), ['d/', 'd/a.bin', 'd/b.txt']);
            const content = spawnSync('tar', ['-xOf', '-', 'd/b.txt'], { input: tarFile.data, maxBuffer: 1 << 24 });
            assert.ok(content.stdout.equals(blob(1234, 9)));
        }
    }

    // A connection that stops moving data is replaced, and the file goes through on the new one.
    {
        const silent = new Helper();
        const stuck = await openLink(silent);
        silent.mute = true;
        const good = new Helper();
        let current = stuck;
        const originalStall = QfmLink.STALL_MS;
        QfmLink.STALL_MS = 300;
        let relinks = 0;
        const transfers = new QfmTransfers({ link: () => current, now: () => performance.now(), sleep: async () => {},
            ask: async () => ({ choice: 'skip', all: false }), notify: () => {},
            relink: async () => { relinks += 1; current = await openLink(good); } });
        const tree = staged({ moved: blob(400000) });
        const job = new QfmJob('up', 'up');
        await transfers.upload(job, tree, await tree.readdir(), '/home/u');
        assert.equal(relinks, 1);
        assert.equal(stuck.closed, true);
        same(job.errors, []);
        assert.ok(good.files.get('/home/u/moved').equals(blob(400000)));
        assert.equal(job.done, 400000, 'progress of the first try is not counted twice');

        // A connection that goes away is the same: a brief loss of the path costs a retry.
        const lostFirst = new Helper();
        current = await openLink(lostFirst);
        lostFirst.mute = true;
        relinks = 0;
        const flap = new QfmJob('up', 'up');
        const flapTree = staged({ flapped: blob(400000) });
        const flapRun = transfers.upload(flap, flapTree, await flapTree.readdir(), '/home/u');
        const firstLink = current;
        while (firstLink.inflight === 0) { await settle(); }
        firstLink.end('ELINK');
        await flapRun;
        assert.equal(relinks, 1);
        same(flap.errors, []);
        assert.ok(good.files.get('/home/u/flapped').equals(blob(400000)));

        // A guest that never gets anywhere ends in an error, not in a loop.
        let again = 0;
        const hopeless = new QfmTransfers({ link: () => current, now: () => performance.now(), sleep: async () => {},
            ask: async () => ({ choice: 'skip', all: false }), notify: () => {},
            relink: async () => { again += 1; const helper = new Helper(); current = await openLink(helper); helper.mute = true; } });
        const dead = new Helper();
        current = await openLink(dead);
        dead.mute = true;
        const lost = new QfmJob('up', 'up');
        const lostTree = staged({ never: blob(400000) });
        await hopeless.upload(lost, lostTree, await lostTree.readdir(), '/home/u');
        assert.equal(again, 3);
        same(lost.errors.map(({ error }) => error.code), ['ESTALLED']);
        current.close();
        QfmLink.STALL_MS = originalStall;
    }

    // The link ending fails what is waiting with one reason and halts the job.
    {
        link.rate.rate = 64 * 1024;
        const tree = staged({ gone1: blob(1000000), gone2: blob(1000000) });
        const job = new QfmJob('up', 'up');
        const run = transfersFor(link).upload(job, tree, await tree.readdir(), '/home/u');
        while (link.inflight === 0) { await settle(); }
        link.end('ELINK');
        await run;
        assert.ok(job.errors.length >= 1);
        assert.ok(job.errors.every(({ error }) => error.code === 'ELINK'));
        assert.equal(job.halted, true);
        await assert.rejects(link.call('hello'), (error) => error.code === 'ELINK');
    }
})();

// A guest without its helper, and hostile frames, end the link rather than hang it.
await (async () => {
    const peer = new FakePeer(new Helper());
    const original = peer.createDataChannel.bind(peer);
    peer.createDataChannel = (label) => {
        const channel = original(label);
        if (label === 'qsm-files') {
            channel.deliver = () => {};
            setImmediate(() => channel.dispatchEvent(Object.assign(new Event('message'), { data: '{"op":"status","state":"closed","reason":"nohelper"}' })));
        }
        return channel;
    };
    const link = new QfmLink({ createPeer: async () => peer, offer: async () => ({ sdp: 'v=0' }), timers, isHidden: () => false });
    await assert.rejects(link.open(), (error) => error.code === 'ENOHELPER');
    assert.equal(link.closed, true);

    const helper = new Helper();
    const open = await openLink(helper);
    const pending = open.call('list', { path: '/' }, null, () => {});
    helper.channel.deliver(new Uint8Array([255, 255, 1]).buffer);
    await assert.rejects(pending, (error) => error.code === 'ELINK');
})();

console.log('file manager tests passed');
process.exit(0);
