/*
 * qsm-pve-direct Console menu integration
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Browser-only PVE Console entry for the direct Display1 transport.
 */
(function () {
    // Viewer mode: this file runs as the WebRTC console page that pveproxy
    // serves for '?console=kvm&webrtc=1' (index.html.tpl sets the object).
    // The page itself is the console, the offer goes to the VM's API call
    // webrtcproxy (qemu-server, dbus-display=1), and the PVE UI overrides
    // below are not registered -- their classes do not exist on that page.
    const VIEWER = (typeof window !== 'undefined' && window.QSM_WEBRTC_VIEWER) || null;
    const NATIVE_QEMU = !VIEWER && typeof PVE !== 'undefined' &&
        typeof PVE.Utils?.openWebRTCViewer === 'function';
    const defineOverride = (name, body) => {
        const legacyQemu = /\.(DisplayInputPanel|DisplayEdit|AudioInputPanel|AudioEdit|HardwareView|QemuConfig)Overlay$/.test(name);
        if (!VIEWER && !(NATIVE_QEMU && legacyQemu)) { Ext.define(name, body); }
    };
    const MIN_VMID = 100;
    const MAX_VMID = 999999999;
    const MAX_QEMU_ARGS_BYTES = 8192;
    const RUNTIME_PREFIX = '/run/qsm-pve-direct';
    const DEFAULT_RENDER_NODE = '/dev/dri/renderD128';
    const DIRECT_GPU_ID = 'qsm-direct-gpu';
    const DIRECT_AGENT_ID = 'qsm-direct-agent';
    const DIRECT_AUDIO_ID = 'qsm-direct-audio';
    // VirGL is an optional acceleration profile, not a prerequisite for the
    // browser transport. The CPU profile relies on PVE's stock VGA adapter
    // and explicitly uses QEMU's non-GL Display1 backend.
    const DISPLAY_PROFILE = Object.freeze({
        virgl: 'virgl',
        cpu: 'cpu',
    });
    // The non-GL D-Bus Display1 backend captures the active QEMU console's 2D
    // surface, which is device-agnostic: Standard VGA, VirtIO-GPU (no GL) and
    // VMware SVGA all render to an ordinary console the same way. VMware SVGA
    // therefore works with the CPU profile (it cannot drive the VirGL/GL
    // profile, which needs virtio-vga-gl). qxl is left out for now: its usual
    // home is SPICE and it has not been qualified here.
    const CPU_DISPLAY_VGA_TYPES = new Set(['std', 'virtio', 'vmware']);
    // Console preferences are deliberately browser-local.  They do not
    // contain a credential, VM identifier, SDP, or any host policy: the PVE
    // Console route remains the sole authority for those.  Keeping the UI
    // tuning here also means an administrator can use the same node with a
    // conservative desktop profile while a LAN user selects an interactive
    // one without changing a VM configuration.
    const CONSOLE_SETTINGS_STORAGE_KEY = 'qsm-direct-console-settings-v1';
    const KEYBOARD_PRIORITY = Object.freeze({
        guest: 'guest-first',
        // Raw delivery is deliberately a third choice rather than an
        // implementation detail of guest-first. It is useful while setting
        // up a VM's own shortcuts: every DOM press/release edge is sent to
        // Display1 and QSM reserves no local capture-exit chord.
        raw: 'raw-events',
        client: 'client-first',
    });
    const KEY_INPUT_MODE = Object.freeze({ typing: 'client-repeat', physical: 'guest-repeat' });
    const MOUSE_INPUT_MODE = Object.freeze({ exact: 'exact-clicks', physical: 'physical-buttons' });
    const CONSOLE_SETTING_SCHEMA = Object.freeze({
        toolbarHotZonePx: { defaultValue: 32, minimum: 4, maximum: 160 },
        toolbarNearbyPx: { defaultValue: 36, minimum: 0, maximum: 240 },
        toolbarRevealDelayMs: { defaultValue: 650, minimum: 0, maximum: 5000 },
        toolbarHideDelayMs: { defaultValue: 3000, minimum: 250, maximum: 30000 },
        targetFps: { defaultValue: 60, minimum: 10, maximum: 240 },
        playoutDelayMs: { defaultValue: 0, minimum: 0, maximum: 1000 },
        resizeDebounceMs: { defaultValue: 400, minimum: 100, maximum: 3000 },
        resizeSettleMs: { defaultValue: 1000, minimum: 250, maximum: 5000 },
    });
    // A guest compositor turns its virtual output off after an idle timeout
    // (DPMS/screen blank).  QEMU Display1 keeps emitting a scanout, but every
    // pixel is black and no mode change is applied until the guest wakes.
    // Measured on PVE 9 with a KWin/Wayland guest: the desktop returns about
    // 256 ms after the first browser mouse or key event, and never without
    // one.  This constant is how long an all-black picture is tolerated
    // before the Console explains it; a locked guest that keeps scanning out
    // its last picture is covered by the stalled-resize path instead.
    const GUEST_SLEEP_HINT_MS = 4000;

    const applyExactUiDefault = (settings, policy) => {
        if (policy.available === true && typeof policy.default_enabled === 'boolean' &&
            !settings.exactUiPreferenceSet) {
            settings.exactUi = policy.default_enabled;
        }
    };

    // Read the receiver's already decoded pictures when they retain their
    // original RTP identity. A two-picture FIFO absorbs short delivery bursts
    // without adding another decoder or an unbounded presentation backlog.
    class DecodedVideoPresenter {
        static DIRECT_DECODE = Object.freeze({ direct: true });
        constructor(scope, present, retire = () => {}) {
            this.scope = scope; this.present = present; this.retire = retire;
            this.session = null; this.failed = new WeakSet();
        }
        get active() { return this.session?.active === true; }
        get directUsable() { return !this.failed.has(DecodedVideoPresenter.DIRECT_DECODE); }
        // Pictures decoded ahead of the browser's playout buffer. Takes over
        // from the track reader on its first frame; the same bounded queue,
        // draw and order guards apply, and silence falls back to that reader.
        pushDirect(frame, rtp, allowed) {
            if (!allowed || this.failed.has(DecodedVideoPresenter.DIRECT_DECODE) || !Number.isInteger(rtp) || rtp < 0 || rtp > 0xffffffff) {
                frame.close(); return;
            }
            let s = this.session;
            if (s?.source !== DecodedVideoPresenter.DIRECT_DECODE) {
                this.stop();
                // No playout buffer smooths these: TCP delivers pictures in short
                // bursts, which one draw per tick can absorb with a little depth.
                s = { source: DecodedVideoPresenter.DIRECT_DECODE, queue: [], current: null, active: false, raf: null, timer: null,
                    lastArrival: this.scope.performance.now(), depth: 3, maxAge: 50 };
                this.session = s;
                const watchdog = () => {
                    if (this.session !== s) { return; }
                    const silent = this.scope.performance.now() - s.lastArrival;
                    // Idle desktops still send pictures. A short silence lends the
                    // display to the browser's live path until fresh pictures return.
                    if (silent >= 150) { s.active = false; }
                    if (silent >= 5000) { this.directFailed(); }
                    else { s.timer = this.scope.setTimeout(watchdog, 50); }
                };
                s.timer = this.scope.setTimeout(watchdog, 50);
            }
            this.enqueue(s, frame, rtp);
        }
        // The worker restarts its decoder from the next key frame. The browser's path
        // shows the stream until then, and the first new picture takes the display back.
        directInterrupted(reason) {
            this.directRestarts = (this.directRestarts ?? 0) + 1; this.directLastError = reason;
            if (this.session?.source === DecodedVideoPresenter.DIRECT_DECODE) { this.stop(); }
            this.onDirectFailed?.();
        }
        directFailed(reason = 'silent') {
            this.directFailure ??= reason;
            this.failed.add(DecodedVideoPresenter.DIRECT_DECODE);
            if (this.session?.source === DecodedVideoPresenter.DIRECT_DECODE) { this.stop(); }
            this.onDirectFailed?.();
        }
        enqueue(s, frame, rtp) {
            s.active = true; s.lastArrival = this.scope.performance.now();
            s.queue.push({ frame, rtp, at: s.lastArrival, closed: false });
            while (s.queue.length > (s.depth ?? 2)) { this.release(s.queue.shift()); }
            if (s.raf === null) {
                s.raf = this.scope.requestAnimationFrame(now => this.draw(s, now));
            }
        }
        release(item) {
            if (item && !item.closed) { item.closed = true; item.frame.close(); }
        }
        configure(source, enabled) {
            if (enabled && (this.session?.source === source || this.session?.source === DecodedVideoPresenter.DIRECT_DECODE)) { return; }
            this.stop();
            if (!enabled || !source || this.failed.has(source) || source.readyState === 'ended' ||
                typeof source.clone !== 'function' ||
                typeof this.scope.MediaStreamTrackProcessor !== 'function') { return; }
            const s = { source, queue: [], current: null, active: false, raf: null, timer: null,
                lastArrival: this.scope.performance.now() };
            this.session = s;
            try {
                s.track = source.clone();
                s.reader = new this.scope.MediaStreamTrackProcessor({ track: s.track, maxBufferSize: 1 })
                    .readable.getReader();
                const watchdog = () => {
                    if (this.session !== s) { return; }
                    if (this.scope.performance.now() - s.lastArrival >= 2000) { this.fail(s); }
                    else { s.timer = this.scope.setTimeout(watchdog, 1000); }
                };
                s.timer = this.scope.setTimeout(watchdog, 2000);
                void this.read(s);
            } catch (_error) { this.fail(s); }
        }
        fail(s) {
            if (this.session !== s) { return; }
            if (s.source === DecodedVideoPresenter.DIRECT_DECODE) { this.directFailed(); return; }
            this.failed.add(s.source); this.stop();
        }
        stop() {
            const s = this.session;
            if (!s) { return; }
            this.session = null; s.active = false;
            if (s.raf !== null) { this.scope.cancelAnimationFrame(s.raf); }
            if (s.timer !== null) { this.scope.clearTimeout(s.timer); }
            // Remove the renderer's borrowed reference before closing frames.
            this.retire();
            this.release(s.current);
            for (const item of s.queue) { this.release(item); }
            s.queue.length = 0;
            if (s.reader) {
                try { Promise.resolve(s.reader.cancel()).catch(() => {}); } catch (_error) { /* closed reader */ }
            }
            s.track?.stop?.();
        }
        async read(s) {
            try {
                while (this.session === s) {
                    const { value: frame, done } = await s.reader.read();
                    // Cancellation need not settle a pending read immediately.
                    if (this.session !== s) { frame?.close(); return; }
                    if (done) { frame?.close(); this.fail(s); return; }
                    let rtp;
                    try { rtp = frame.metadata().rtpTimestamp; }
                    catch (_error) { frame?.close(); this.fail(s); return; }
                    if (!Number.isInteger(rtp) || rtp < 0 || rtp > 0xffffffff) {
                        frame.close(); this.fail(s); return;
                    }
                    this.enqueue(s, frame, rtp);
                }
            } catch (_error) { this.fail(s); }
        }
        draw(s, now) {
            if (this.session !== s) { return; }
            s.raf = null;
            while (s.queue.length && now - s.queue[0].at > (s.maxAge ?? 50)) { this.release(s.queue.shift()); }
            const item = s.queue.shift();
            if (item) {
                const previous = s.current;
                s.current = item;
                try {
                    if (this.present(item.frame, item.rtp, now) === false) {
                        s.current = this.session === s ? previous : null;
                        this.release(item);
                        if (this.session !== s) { this.release(previous); }
                    } else { this.release(previous); }
                } catch (_error) { this.release(previous); this.fail(s); return; }
            }
            if (this.session === s && s.queue.length) {
                s.raf = this.scope.requestAnimationFrame(next => this.draw(s, next));
            }
        }
    }

    // Exact UI records are independent of video decode/presentation. Keep
    // geometry epochs and per-tile serials across asynchronous inflation so
    // an older patch can never paint over a newer clear, resize or disable.
    class VideoRegionRenderer {
        constructor(canvas, video, acknowledge = () => {}) {
            this.canvas = canvas; this.video = video; this.acknowledge = acknowledge;
            this.frames = new Map(); this.presented = null; this.painted = null;
            this.width = this.height = 0; this.acknowledged = false;
        }
        map(rtp, region) {
            if (!Number.isInteger(rtp) || !Array.isArray(region) || region.length !== 6 ||
                region.some(n => !Number.isInteger(n) || n < 0)) { return false; }
            const [sw, sh, x, y, w, h] = region;
            if (!sw || !sh || sw > 16384 || sh > 16384 || sw * sh > 8294400 ||
                !w || !h || x + w > sw || y + h > sh) { return false; }
            this.frames.set(rtp, region);
            if (this.frames.size > 256) { this.frames.delete(this.frames.keys().next().value); }
            if (this.presented === rtp) { this.frame(rtp, this.decoded?.image); }
            return true;
        }
        retireDecoded() {
            if (this.decoded) { this.decoded = null; this.presented = null; }
        }
        frame(rtp, image = null) {
            this.presented = rtp;
            this.decoded = image ? { rtp, image } : null;
            const region = this.frames.get(rtp);
            if (!region) { return; }
            const [sw, sh, x, y, w, h] = region;
            // Metadata and the browser-decoded picture must agree. A late
            // mapping can never stretch a different crop across the desktop.
            const source = image || this.video;
            const width = image ? image.displayWidth : this.video.videoWidth;
            const height = image ? image.displayHeight : this.video.videoHeight;
            if (width !== w || height !== h) { return; }
            if (this.width !== sw || this.height !== sh) {
                this.width = this.canvas.width = sw; this.height = this.canvas.height = sh;
                this.acknowledged = false;
            }
            const ctx = this.canvas.getContext('2d', { alpha: false, colorSpace: 'srgb' });
            ctx.drawImage(source, x, y, w, h);
            this.painted = rtp;
            this.canvas.style.display = 'block'; this.video.style.opacity = '0';
            if (!x && !y && w === sw && h === sh && !this.acknowledged) {
                this.acknowledged = true; this.acknowledge(rtp);
            }
        }
    }

    // Sample the base picture and exact pixels on one native pixel grid,
    // before CSS scaling or display colour management. A hardware video plane
    // and an independently scaled RGB canvas need not render the same colour
    // or subpixel position, even when their capture timestamps agree.
    class ExactUiCompositor {
        constructor(canvas, video, exact, regions) {
            this.canvas = canvas; this.video = video; this.exact = exact; this.regions = regions;
            this.captured = false; this.dirty = true;
        }
        capture() {
            if (this.regions.width) { this.dirty = true; return; }
            const { videoWidth: w, videoHeight: h } = this.video;
            if (!w || !h) { return; }
            const base = this.regions.canvas;
            if (base.width !== w || base.height !== h) { base.width = w; base.height = h; }
            base.getContext('2d', { alpha: false, colorSpace: 'srgb' }).drawImage(this.video, 0, 0);
            this.captured = true; this.dirty = true;
        }
        present(enabled) {
            const base = this.regions.canvas, target = this.canvas;
            const ready = enabled && (this.captured || (this.regions.width > 0 && this.regions.painted !== null)) &&
                this.exact.width > 0 && base.width === this.exact.width && base.height === this.exact.height;
            this.exact.style.display = 'none';
            if (!ready) {
                if (!enabled) { this.captured = false; }
                this.dirty = true;
                target.style.display = 'none';
                base.style.display = this.regions.width ? 'block' : 'none';
                this.video.style.opacity = this.regions.width ? '0' : '';
                return;
            }
            if (!this.dirty && target.style.display === 'block') { return; }
            if (target.width !== base.width || target.height !== base.height) {
                target.width = base.width; target.height = base.height;
            }
            const ctx = target.getContext('2d', { alpha: false, colorSpace: 'srgb' });
            ctx.drawImage(base, 0, 0); ctx.drawImage(this.exact, 0, 0);
            this.dirty = false;
            target.style.display = 'block'; base.style.display = 'none'; this.video.style.opacity = '0';
        }
    }

    // Inverse of core/pixel_history.hpp. Pixels are 0xBBGGRR.
    const decodePixelHistory = (history, tokens, count) => {
        const CAPACITY = 1 << 22, MASK = CAPACITY - 1, { ring } = history;
        const view = new DataView(tokens.buffer, tokens.byteOffset, tokens.byteLength);
        const opsSize = view.getUint16(4), distancesSize = view.getUint16(6);
        const distancesAt = 8 + opsSize, literalsAt = distancesAt + distancesSize;
        const literalBytes = tokens.length - literalsAt;
        if (literalBytes < 0 || literalBytes % 3) { throw Error('Invalid exact UI history tokens'); }
        const literals = literalBytes / 3;
        let op = 8, distance = distancesAt, literal = 0, produced = 0, position = history.position;
        const varint = (at, end) => {
            let value = 0, shift = 0, byte;
            do {
                if (at >= end || shift > 21) { throw Error('Invalid exact UI history varint'); }
                byte = tokens[at++]; value |= (byte & 127) << shift; shift += 7;
            } while (byte & 128);
            varint.at = at;
            return value;
        };
        while (produced < count) {
            const run = varint(op, distancesAt); op = varint.at;
            if (run > count - produced || literal + run > literals) { throw Error('Invalid exact UI history literals'); }
            for (let i = 0; i < run; ++i, ++literal) {
                const red = tokens[literalsAt + literal];
                ring[(position + produced + i) & MASK] = red |
                    (((tokens[literalsAt + literals + literal] + red) & 255) << 8) |
                    (((tokens[literalsAt + 2 * literals + literal] + red) & 255) << 16);
            }
            produced += run;
            if (produced >= count) { break; }
            const length = varint(op, distancesAt); op = varint.at;
            const back = varint(distance, literalsAt); distance = varint.at;
            if (!length || length > count - produced || !back || back > position + produced || back > CAPACITY - 4096) {
                throw Error('Invalid exact UI history match');
            }
            for (let i = 0; i < length; ++i) {
                const at = position + produced + i;
                ring[at & MASK] = ring[(at - back) & MASK];
            }
            produced += length;
        }
        if (op !== distancesAt || distance !== literalsAt || literal !== literals) { throw Error('Trailing exact UI history tokens'); }
        const pixels = new Uint32Array(count);
        let sum = 2166136261;
        for (let i = 0; i < count; ++i) {
            const value = ring[(position + i) & MASK];
            pixels[i] = value;
            sum = Math.imul(sum ^ value, 16777619);
        }
        // Wrong pixels here would stay on screen as if exact, and corrupt later records.
        if ((sum >>> 0) !== view.getUint32(0)) { throw Error('Exact UI history checksum mismatch'); }
        history.position = position + count;
        return pixels;
    };

    class ExactUiRenderer {
        constructor(canvas, inflate, failed = () => {}, acknowledge = () => {}, canPresent = () => true, painted = () => {}) {
            this.canvas = canvas;
            this.inflate = inflate;
            this.failed = failed;
            this.acknowledge = acknowledge;
            this.ackAt = -Infinity;
            this.canPresent = canPresent;
            this.painted = painted;
            this.active = new Map();
            this.cache = new Map(); this.cached = [];
            this.clears = new Map();
            this.videoFrames = new Map();
            this.barrier = [];
            this.copying = false;
            this.copies = 0;
            this.videoCapture = 0;
            this.presentedRtp = null;
            this.scene = null; this.baseLayout = 0;
            this.ackQueued = false;
            this.enabled = false;
            this.epoch = null;
            this.serial = null;
            this.generation = 0;
            this.width = this.height = 0;
            this.wanted = new Map();
            this.inflight = 0;
            this.pending = new Map();
            this.ready = new Map();
            this.patches = this.bytes = 0;
            this.context = null;
        }
        configure(enabled) {
            if (this.enabled === enabled) { return; }
            this.enabled = enabled;
            this.clear();
        }
        clear() {
            this.generation += 1;
            this.barrier = []; this.copying = false;
            this.history = null;
            this.scene = null; this.baseLayout = 0;
            this.epoch = this.serial = null;
            this.wanted.clear();
            this.active.clear(); this.clears.clear();
            this.ready.clear();
            this.cache.clear(); this.cached = [];
            for (const job of this.pending.values()) { job.done(); }
            this.pending.clear();
            this.width = this.height = 0;
            this.canvas.width = this.canvas.height = 0;
            this.canvas.style.display = 'none';
            this.painted();
        }
        report() {
            if (this.ackQueued) { return; }
            this.ackQueued = true;
            const send = () => {
                this.ackQueued = false;
                if (this.enabled && this.epoch !== null) {
                    this.ackAt = performance.now();
                    this.acknowledge({ op: 'qsm_ui_ack', epoch: this.epoch, serial: this.serial,
                        pending: this.pending.size + this.inflight + this.ready.size + this.barrier.length,
                        cached: this.cached.splice(0, 32).filter(id => this.cache.get(id)?.image) });
                    if (this.cached.length) { this.report(); }
                }
            };
            // A reply and the SCTP acknowledgement it draws back cost a narrow path
            // more than a small record does, so one follows many records at a burst.
            const wait = this.ackAt + 10 - performance.now();
            if (wait > 0) { setTimeout(send, wait); } else { queueMicrotask(send); }
        }
        mapVideoFrame(rtp, capture, region = null) {
            if (!Number.isInteger(rtp) || rtp < 0 || rtp > 0xffffffff ||
                !Number.isSafeInteger(capture) || capture <= 0) { return; }
            this.videoFrames.set(rtp, { capture, region });
            if (this.videoFrames.size > 256) { this.videoFrames.delete(this.videoFrames.keys().next().value); }
            if (rtp === this.presentedRtp) { this.videoFrame(rtp); }
        }
        videoFrame(rtp) {
            this.presentedRtp = rtp;
            const frame = this.videoFrames.get(rtp);
            if (!frame) { return; }
            const { capture, region } = frame;
            // A crop can change decoded dimensions before its mapped base
            // picture has been painted. Never reveal the previous canvas
            // merely because the video element reported a frame callback.
            if (!this.canPresent(rtp, region)) { return; }
            this.videoCapture = Math.max(this.videoCapture, capture);
            if (this.scene && capture >= this.scene.capture) { this.baseLayout = this.scene.layout; }
            this.retireEarlyLayout();
            this.videoRegion = region;
            let changed = false;
            for (const [key, job] of this.clears) {
                if (job.capture > capture) { continue; }
                if (region) {
                    const [, , x, y, w, h] = region;
                    if (job.x < x || job.y < y || job.x + job.w > x + w || job.y + job.h > y + h) { continue; }
                }
                this.clears.delete(key); changed = true;
                // A newer received patch may still be inflating. It cannot
                // protect the older bitmap actually covering this video.
                const tile = this.active.get(key);
                if (tile && (tile.epoch !== job.epoch ||
                    ((job.serial - tile.serial) >>> 0) < 0x80000000)) {
                    this.context.clearRect(job.x, job.y, job.w, job.h); this.active.delete(key);
                }
            }
            if (changed) { this.report(); this.painted(); }
            this.presentReady();
        }
        presentReady() {
            if (!this.enabled) { return; }
            let changed = false;
            for (const [key, job] of this.ready) {
                // Only leading jobs depend on their advisory layout. An
                // ordinary capture-fenced tile may remain unchanged across
                // a focus/layout boundary and will not be sent again.
                if (job.early && this.scene && job.layout !== this.scene.layout && this.videoCapture >= this.scene.capture &&
                    job.capture <= this.scene.capture) { this.ready.delete(key); changed = true; continue; }
                if (!job.unfenced && job.capture > this.videoCapture && !this.canPresentEarly(key, job)) { continue; }
                this.ready.delete(key); changed = true;
                if (job.generation !== this.generation || this.wanted.get(key) !== job.serial) { continue; }
                this.context.putImageData(job.image, job.x, job.y);
                const { epoch, serial, x, y, w, h, capture, layout, image } = job;
                this.active.set(key, { epoch, serial, x, y, w, h, capture, layout, image,
                    // Only a leading layout-dependent bitmap may be retired by a
                    // later layout, because only those are published again.
                    earlyPainted: capture > this.videoCapture && !job.unfenced }); this.clears.delete(key);
                this.patches += 1; this.bytes += job.bytes;
            }
            if (changed) { this.report(); this.painted(); }
        }
        retireEarlyLayout() {
            if (!this.scene || this.videoCapture < this.scene.capture) { return; }
            let changed = false;
            for (const [key, tile] of this.active) {
                if (!tile.earlyPainted || tile.layout === this.scene.layout) { continue; }
                this.context.clearRect(tile.x, tile.y, tile.w, tile.h);
                this.active.delete(key); changed = true;
            }
            if (changed) { this.report(); this.painted(); }
        }
        canPresentEarly(key, job) {
            const old = this.active.get(key);
            if (!job.early || !job.layout || job.layout !== this.baseLayout || job.layout !== this.scene?.layout ||
                this.scene.capture > this.videoCapture || !old?.image || old.epoch !== job.epoch || old.layout !== job.layout) {
                return false;
            }
            // Geometry is advisory. Independently verify that the new image
            // changes only a small part of an already displayed exact tile.
            // Dense repaints and new/relocated tiles retain capture fences.
            const a = old.image.data, b = job.image.data;
            if (a.length !== b.length) { return false; }
            const limit = Math.floor(job.w * job.h / 8);
            let changed = 0;
            for (let i = 0; i < a.length; i += 4) {
                if ((a[i] !== b[i] || a[i + 1] !== b[i + 1] || a[i + 2] !== b[i + 2]) && ++changed > limit) { return false; }
            }
            return true;
        }
        erase(key, job) {
            const r = this.videoRegion;
            const covered = !r || (job.x >= r[2] && job.y >= r[3] &&
                job.x + job.w <= r[2] + r[4] && job.y + job.h <= r[3] + r[5]);
            if ((job.capture > this.videoCapture || !covered) && this.active.has(key)) { this.clears.set(key, job); }
            else { this.context.clearRect(job.x, job.y, job.w, job.h); this.active.delete(key); }
            this.report(); this.painted();
        }
        // A copy reads the canvas, so it must observe every earlier record and
        // none later, although tile inflation is asynchronous and out of order.
        settled() {
            return !this.pending.size && !this.inflight;
        }
        releaseBarrier() {
            while (this.copying && this.settled()) {
                const copy = this.copying; this.copying = false;
                this.applyCopy(copy);
                const queued = this.barrier; this.barrier = [];
                for (const data of queued) {
                    if (this.copying) { this.barrier.push(data); } else { void this.receive(data); }
                }
            }
        }
        applyCopy({ generation, serial, epoch, x, y, w, h, sx, sy, capture, layout }) {
            if (generation !== this.generation || !this.enabled) { return; }
            const dx = x - sx, dy = y - sy;
            // Tiles still waiting for their video fence are part of the pixels
            // this copy moves. The worker already counts them as displayed.
            const overlaps = (t, ax, ay) => t.x < ax + w && ax < t.x + t.w && t.y < ay + h && ay < t.y + t.h;
            for (const [key, job] of this.ready) {
                if (!overlaps(job, sx, sy) && !overlaps(job, x, y)) { continue; }
                this.ready.delete(key);
                if (job.generation !== this.generation || this.wanted.get(key) !== job.serial) { continue; }
                this.context.putImageData(job.image, job.x, job.y);
                this.active.set(key, { epoch: job.epoch, serial: job.serial, x: job.x, y: job.y, w: job.w, h: job.h,
                    capture: job.capture, layout: job.layout, image: job.image, earlyPainted: !!job.early && job.capture > this.videoCapture });
                this.clears.delete(key);
            }
            // Transparent source pixels must remain transparent, so this copy
            // replaces its destination rather than compositing over it. The
            // canvas specification defines an overlapping self-draw as if the
            // source were read first.
            const ctx = this.context;
            ctx.save();
            ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip();
            ctx.globalCompositeOperation = 'copy';
            ctx.imageSmoothingEnabled = false;
            ctx.drawImage(this.canvas, sx, sy, w, h, x, y, w, h);
            ctx.restore();
            for (let ty = Math.floor(y / 64) * 64; ty < y + h; ty += 64) {
                for (let tx = Math.floor(x / 64) * 64; tx < x + w; tx += 64) {
                    const key = `${tx}:${ty}`;
                    this.wanted.set(key, serial);
                    this.ready.delete(key); this.clears.delete(key);
                    // The bitmap is no longer known here, so it cannot justify
                    // presenting a later sparse update ahead of its fence.
                    this.active.set(key, { epoch, serial, x: tx, y: ty, w: Math.min(64, this.width - tx),
                        h: Math.min(64, this.height - ty), capture, layout, image: null, earlyPainted: false });
                }
            }
            this.copies += 1;
            this.report(); this.painted();
        }
        async receive(data) {
            if (!this.enabled) { return; }
            if (this.copying) {
                if (this.barrier.length >= 4096) { this.configure(false); this.failed(Error('Exact UI copy backlog')); return; }
                this.barrier.push(data); return;
            }
            const operationGeneration = this.generation;
            try {
                if (!(data instanceof ArrayBuffer) || data.byteLength < 28 || data.byteLength > 6200) {
                    throw Error('Invalid exact UI record');
                }
                const v = new DataView(data);
                const flags = v.getUint8(5), encoding = flags & 1, early = !!(flags & 2), unfenced = !!(flags & 4), history = !!(flags & 8);
                const stampSize = v.getUint16(6);
                if (v.getUint32(0) !== 0x51554931 || (flags & ~15) ||
                    ![0, 8, 16].includes(stampSize) || data.byteLength < 28 + stampSize) {
                    throw Error('Unsupported exact UI record');
                }
                const payloadAt = 28 + stampSize;
                const capture = stampSize ? v.getUint32(28) * 4294967296 + v.getUint32(32) : 0;
                const layout = stampSize === 16 ? v.getUint32(36) * 4294967296 + v.getUint32(40) : 0;
                if (!Number.isSafeInteger(capture) || !Number.isSafeInteger(layout) ||
                    (early && (stampSize !== 16 || !layout || !capture))) { throw Error('Invalid capture/layout timestamp'); }
                const op = v.getUint8(4), epoch = v.getUint32(8), serial = v.getUint32(12);
                const width = v.getUint16(16), height = v.getUint16(18);
                const x = v.getUint16(20), y = v.getUint16(22), w = v.getUint16(24), h = v.getUint16(26);
                if (!epoch || !width || !height || width > 16384 || height > 16384 || width * height > 8294400) {
                    throw Error('Invalid exact UI geometry');
                }
                if (op === 1) {
                    if (flags || data.byteLength !== payloadAt || x || y || w || h) { throw Error('Invalid exact UI reset'); }
                    if (epoch === this.epoch && (serial === this.serial ||
                        ((serial - this.serial) >>> 0) >= 0x80000000)) { return; }
                    if (this.epoch !== null && epoch !== this.epoch &&
                        ((epoch - this.epoch) >>> 0) >= 0x80000000) { return; }
                    const preserve = this.width === width && this.height === height && capture > this.videoCapture;
                    if (preserve) {
                        this.generation += 1;
                        for (const job of this.pending.values()) { job.done(); }
                        this.pending.clear(); this.wanted.clear(); this.clears.clear();
                        this.ready.clear();
                        this.cache.clear(); this.cached = [];
                        for (const [key, tile] of this.active) {
                            this.wanted.set(key, serial);
                            this.erase(key, { ...tile, epoch, capture, serial });
                        }
                    } else {
                        this.clear();
                        this.width = this.canvas.width = width; this.height = this.canvas.height = height;
                        this.context = this.canvas.getContext('2d', { alpha: true, colorSpace: 'srgb' });
                    }
                    this.scene = { layout, capture };
                    this.baseLayout = capture <= this.videoCapture ? layout : 0;
                    this.history = null;
                    this.epoch = epoch; this.serial = serial; this.report();
                    return;
                }
                if (epoch !== this.epoch || width !== this.width || height !== this.height) { return; }
                if (serial === this.serial || ((serial - this.serial) >>> 0) >= 0x80000000) { return; }
                if (op === 5) {
                    if (flags || stampSize !== 16 || data.byteLength !== payloadAt || x || y || w || h) {
                        throw Error('Invalid UI layout boundary');
                    }
                    this.serial = serial; this.scene = { layout, capture };
                    this.baseLayout = capture <= this.videoCapture ? layout : 0;
                    this.retireEarlyLayout(); this.presentReady(); this.report(); return;
                }
                if (op === 10) {
                    if (flags || data.byteLength !== payloadAt + 4 || !w || !h || x + w > width || y + h > height) {
                        throw Error('Invalid exact UI copy');
                    }
                    const sx = v.getUint16(payloadAt), sy = v.getUint16(payloadAt + 2);
                    if (sx + w > width || sy + h > height) { throw Error('Invalid exact UI copy source'); }
                    this.serial = serial;
                    this.copying = { generation: this.generation, serial, epoch, x, y, w, h, sx, sy, capture, layout };
                    this.releaseBarrier();
                    return;
                }
                if (![2, 3, 6, 7, 8, 9].includes(op) || x >= width || y >= height || x % 64 || y % 64 ||
                    w !== Math.min(64, width - x) || h !== Math.min(64, height - y) ||
                    (op === 3 && (flags || data.byteLength !== payloadAt))) { throw Error('Invalid exact UI tile'); }
                this.serial = serial;
                const key = `${x}:${y}`;
                this.wanted.set(key, serial);
                this.ready.delete(key);
                let cacheId = null, reference = null, shift = 0;
                if ([6, 8].includes(op) && (encoding || data.byteLength !== payloadAt + 4)) {
                    throw Error('Invalid compact UI record');
                }
                if ([7, 9].includes(op)) {
                    if (data.byteLength < payloadAt + (op === 9 ? 11 : 5)) { throw Error('Truncated UI cache'); }
                    cacheId = v.getUint32(payloadAt);
                    if (!cacheId || this.cache.has(cacheId)) { throw Error('Invalid UI cache id'); }
                }
                if ([8, 9].includes(op)) {
                    const id = v.getUint32(payloadAt + (op === 9 ? 4 : 0));
                    reference = this.cache.get(id);
                    if (!reference?.image || reference.w !== w || reference.h !== h) { throw Error('Missing UI bitmap'); }
                    if (op === 9) {
                        shift = v.getInt16(payloadAt + 8);
                        if (encoding || !shift || Math.abs(shift) >= h) { throw Error('Invalid UI scroll'); }
                    }
                }
                if (cacheId !== null) {
                    this.cache.set(cacheId, { w, h, image: null });
                    if (this.cache.size > 256) { this.cache.delete(this.cache.keys().next().value); }
                }
                // Every history record extends what later ones refer to, even
                // when a newer patch of this tile will replace its pixels.
                let decoded = null;
                if (history) {
                    if (op !== 2 || encoding || data.byteLength < payloadAt + 5) { throw Error('Invalid exact UI history record'); }
                    decoded = this.decodeHistory(data, payloadAt, w * h);
                }
                const previous = this.pending.get(key);
                if (previous) { previous.done(); this.pending.delete(key); }
                if (op === 3) { this.erase(key, { epoch, x, y, w, h, capture, serial }); return; }
                // Network arrival and browser inflation are independently
                // paced. Keep one newest job per tile and at most eight
                // inflations instead of disabling the layer on a short burst.
                await new Promise(done => {
                    this.pending.set(key, { key, serial, generation: this.generation,
                        data, op, cacheId, reference, shift, encoding, payloadAt, capture, layout, early, unfenced, x, y, w, h, decoded, done });
                    this.pump();
                });
            } catch (error) {
                if (operationGeneration !== this.generation) { return; }
                this.configure(false); this.failed(error);
            }
        }
        pump() {
            while (this.enabled && this.inflight < 8 && this.pending.size) {
                const [key, job] = this.pending.entries().next().value;
                this.pending.delete(key);
                this.inflight += 1;
                void this.decode(job).catch(error => {
                    if (job.generation !== this.generation) { return; }
                    this.configure(false); this.failed(error);
                }).finally(() => {
                    this.inflight -= 1; job.done(); this.pump(); this.releaseBarrier(); this.report();
                });
            }
        }
        // Inflation runs concurrently and may finish in any order; history
        // grows strictly in record order, which starts here, before any await.
        decodeHistory(data, payloadAt, count) {
            const size = new DataView(data).getUint32(payloadAt);
            if (size < 8 || size > 65536) { throw Error('Invalid exact UI history size'); }
            const inflated = this.inflate(new Uint8Array(data, payloadAt + 4), size);
            const history = this.history || (this.history = { ring: new Uint32Array(1 << 22), position: 0, tail: Promise.resolve() });
            const decoded = history.tail.then(() => inflated).then(tokens => decodePixelHistory(history, tokens, count));
            // Whichever job waits reports a failure; a replaced job must not raise one on its own.
            history.tail = decoded.then(() => {});
            decoded.catch(() => {}); history.tail.catch(() => {});
            return decoded;
        }
        async decode(job) {
            const { key, serial, generation, data, op, cacheId, reference, shift,
                encoding, payloadAt, capture, layout, early, unfenced, x, y, w, h, decoded } = job;
            const image = this.context.createImageData(w, h);
            if (decoded) {
                const pixels = await decoded, out = image.data;
                for (let i = 0, at = 0; i < pixels.length; ++i, at += 4) {
                    const value = pixels[i];
                    out[at] = value & 255; out[at + 1] = (value >>> 8) & 255; out[at + 2] = (value >>> 16) & 255; out[at + 3] = 255;
                }
            } else if (op === 6) {
                const color = new Uint8Array(data, payloadAt, 4);
                if (color[3] !== 255) { throw Error('Invalid fill alpha'); }
                // Read/write native words through the same representation;
                // this preserves RGBA on either endianness and avoids 4096
                // individual typed-array calls for each opaque fill tile.
                const pixel = new Uint8Array(4); pixel.set(color);
                new Uint32Array(image.data.buffer, image.data.byteOffset, w * h)
                    .fill(new Uint32Array(pixel.buffer)[0]);
            } else if (op === 8) {
                image.data.set(reference.image.data);
            } else if (op === 9) {
                const stride = w * 4, count = (h - Math.abs(shift)) * stride;
                const dest = Math.max(shift, 0) * stride, src = Math.max(-shift, 0) * stride;
                image.data.set(reference.image.data.subarray(src, src + count), dest);
                const exposed = await this.inflate(new Uint8Array(data, payloadAt + 10), Math.abs(shift) * stride);
                if (exposed.length !== Math.abs(shift) * stride) { throw Error('Invalid scroll pixels'); }
                for (let i = 3; i < exposed.length; i += 4) {
                    if (exposed[i] !== 255) { throw Error('Invalid scroll alpha'); }
                }
                image.data.set(exposed, shift > 0 ? 0 : count);
            } else {
                const expected = w * h * (encoding ? 1 : 4);
                const pixels = await this.inflate(new Uint8Array(data, payloadAt + (op === 7 ? 4 : 0)), expected);
                if (pixels.length !== expected) { throw Error('Invalid exact UI pixel count'); }
                if (encoding) {
                    for (let i = 0; i < pixels.length; ++i) {
                        const at = i * 4;
                        image.data[at] = image.data[at + 1] = image.data[at + 2] = pixels[i];
                        image.data[at + 3] = 255;
                    }
                } else {
                    for (let i = 3; i < pixels.length; i += 4) {
                        if (pixels[i] !== 255) { throw Error('Invalid exact UI alpha'); }
                    }
                    image.data.set(pixels);
                }
            }
            if (!this.enabled || generation !== this.generation) { return; }
            if (cacheId !== null && this.cache.has(cacheId)) {
                this.cache.get(cacheId).image = image;
                this.cached.push(cacheId);
            }
            if (this.wanted.get(key) !== serial) { return; }
            // SCTP can deliver a new UI snapshot seconds before the browser
            // displays its corresponding video through a congested TURN/TCP
            // connection. Fence paints as well as clears: mixed capture times
            // otherwise turn fades and pointer-triggered transitions into
            // old/new 64-pixel squares. One latest bitmap per tile is retained;
            // the ACK decode window includes these presentation waiters.
            this.ready.set(key, { epoch: this.epoch, serial, generation, capture, layout, early, unfenced, x, y, w, h,
                image, bytes: data.byteLength });
            this.presentReady();
        }
    }

    // QSM_FILES_BEGIN: the two-pane file manager, docs/FILE_MANAGER.md.
    const QFM_CHUNK = 60000;
    const QFM_ROW = 26;
    const QFM_WORKERS = 4;
    const QFM_MAX_LIST = 200000;

    const qfmFormat = (text, ...values) => text.replace(/\{(\d+)\}/g, (_match, index) => String(values[index]));

    class QfmError extends Error {
        constructor(code, message = '') {
            super(message || code);
            this.code = code;
        }
    }

    const qfmErrorText = (error) => {
        const code = error && error.code;
        const text = {
            ENOENT: gettext('It no longer exists'),
            EACCES: gettext('Permission denied in the guest'),
            EPERM: gettext('Not permitted in the guest'),
            EEXIST: gettext('It already exists'),
            ENOTDIR: gettext('A part of the path is not a folder'),
            EISDIR: gettext('It is a folder'),
            ENOTEMPTY: gettext('The folder is not empty'),
            ENOSPC: gettext('The disk is full'),
            EDQUOT: gettext('The disk quota is exceeded'),
            EROFS: gettext('The file system is read-only'),
            ELOCKED: gettext('The guest screen is locked'),
            ERIGHTS: gettext('Your PVE role does not allow this (VM.GuestAgent.FileWrite)'),
            ENOTREG: gettext('It is not a regular file'),
            ECANCELED: gettext('Cancelled'),
            EBUSY: gettext('The guest is busy with too many requests'),
            ENAMETOOLONG: gettext('The name is too long'),
            ELINK: gettext('The connection to the guest ended'),
            ELOCAL: gettext('This browser could not read or write the local file'),
            ECHANGED: gettext('The file changed during the transfer, so this copy was not kept'),
            ESTALLED: gettext('The transfer stopped making progress'),
            ETRUNCATED: gettext('This folder is too large to copy completely'),
            EPROTO: gettext('The guest sent something invalid'),
            ENOHELPER: gettext('The file service is not running in the guest'),
        }[code];
        return text || (error && error.message) || gettext('The operation failed');
    };

    // A frame is u16be header length, ASCII JSON and a payload. JSON.stringify
    // writes a lone surrogate as \udcXX, which keeps a non-UTF-8 guest name intact.
    const qfmEncode = (header, payload = null) => {
        const json = JSON.stringify(header).replace(/[\u0080-￿]/g,
            (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
        const size = payload ? payload.byteLength : 0;
        const frame = new Uint8Array(2 + json.length + size);
        new DataView(frame.buffer).setUint16(0, json.length);
        for (let index = 0; index < json.length; index += 1) { frame[2 + index] = json.charCodeAt(index); }
        if (size) { frame.set(payload instanceof Uint8Array ? payload : new Uint8Array(payload), 2 + json.length); }
        return frame.buffer;
    };

    const qfmDecode = (buffer) => {
        if (Object.prototype.toString.call(buffer) !== '[object ArrayBuffer]' || buffer.byteLength < 2) { throw new QfmError('EPROTO'); }
        const size = new DataView(buffer).getUint16(0);
        if (2 + size > buffer.byteLength) { throw new QfmError('EPROTO'); }
        const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 2, size)));
        if (!header || typeof header !== 'object' || Array.isArray(header)) { throw new QfmError('EPROTO'); }
        return { header, payload: buffer.byteLength > 2 + size ? new Uint8Array(buffer, 2 + size) : null };
    };

    // A guest chooses these names. One that could leave the chosen folder never
    // reaches the browser's file APIs or a tar header.
    const qfmSafeName = (name) => typeof name === 'string' && name.length > 0 && name.length <= 4096 &&
        name !== '.' && name !== '..' && !/[/\\\u0000-\u001f]/.test(name);

    const qfmValidEntries = (part) => {
        if (!Array.isArray(part)) { throw new QfmError('EPROTO'); }
        const entries = [];
        for (const row of part) {
            if (!Array.isArray(row) || row.length !== 6) { continue; }
            const [name, type, size, mtime, mode, target] = row;
            if (typeof name !== 'string' || name.length === 0 || name.length > 4096 || name.includes('/') ||
                name.includes('\u0000') || !['f', 'd', 'l', 'o'].includes(type)) { continue; }
            entries.push({ name, type, size: Number.isSafeInteger(size) && size >= 0 ? size : 0,
                mtime: Number.isFinite(mtime) ? mtime : 0, mode: Number.isInteger(mode) ? mode : 0,
                target: ['f', 'd', 'o', 'x'].includes(target) ? target : null });
        }
        return entries;
    };

    const qfmJoin = (directory, name) => (directory === '/' ? `/${name}` : `${directory}/${name}`);
    const qfmParent = (path) => {
        const cut = path.lastIndexOf('/');
        return cut <= 0 ? '/' : path.slice(0, cut);
    };
    const qfmSize = (bytes) => {
        if (!Number.isFinite(bytes)) { return ''; }
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let value = bytes;
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
        return `${unit && value < 100 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
    };

    // Keeps the extra round-trip time near a target, as LEDBAT does, so that a
    // transfer leaves the queue to keys, tiles and video. See the design note.
    class QfmRate {
        static MIN = 16 * 1024;
        // Below this a transfer is not the cause of a queue on any path it would share with
        // a console, and giving way frees nothing. On TURN over TCP a slow flow also reads tens
        // of ms higher (Nagle and delayed ACKs), so that a low rate looks like its own queue.
        static HARMLESS = 64 * 1024;
        static START = 256 * 1024;
        static MAX = 256 * 1024 * 1024;
        static TARGET_BACKGROUND = 25;
        static TARGET_FOREGROUND = 100;
        // An echo every so many ms while a transfer runs, the lowest of how many counts,
        // and the time a cut takes to show in the echoes.
        static SAMPLE_MS = 100;
        static FILTER = 5;
        static HOLD_MS = 400;
        // The path's own delay is only seen with the queue empty, and it can change. So the
        // transfer steps aside now and then, which costs about 2.5% of what it carries.
        static DRAIN_EVERY_MS = 15000;
        static DRAIN_MS = 500;
        static EARLIEST_DRAIN_MS = 5000;
        // Bytes on their way. The node holds about twice this for a session.
        static MAX_WINDOW = 8 * 1024 * 1024;

        constructor() {
            this.rate = QfmRate.START;
            this.target = QfmRate.TARGET_BACKGROUND;
            this.bases = [];
            this.recent = [];
            this.unanswered = null;
            this.lastSample = null;
            this.arrived = 0;
            this.history = [];
            this.holdUntil = 0;
            this.capacity = null;
            this.probing = 0;
            this.drainUntil = 0;
            this.nextDrain = null;
            this.draining = false;
            this.drainLow = Infinity;
            this.lastDrain = -Infinity;
            this.cuts = 0;
            this.floorDelay = Infinity;
            this.standingAside = false;
            this.offered = 0;
        }

        // What to send at now. this.rate stays what the path is thought to take.
        current() {
            if (this.standingAside) { return QfmRate.MIN; }
            return this.draining ? Math.max(QfmRate.MIN, this.rate * .25) : this.rate;
        }

        startDrain(now) {
            this.draining = true;
            this.drainUntil = now + QfmRate.DRAIN_MS;
            this.lastDrain = now;
            this.nextDrain = now + QfmRate.DRAIN_EVERY_MS;
            this.drainLow = Infinity;
            this.cuts = 0;
        }

        // Bytes the far end has really taken. On a full path this is what the path carries
        // beside the console, which is the one number that says how fast to go.
        deliver(bytes) { this.arrived += bytes; }

        delivered(now) {
            this.history.push([now, this.arrived, this.current()]);
            this.arrived = 0;
            // Long enough to hold many frames: at a low rate a short window is all chance.
            const span = Math.min(2000, Math.max(500, 16 * this.chunk() / this.rate * 1000));
            while (now - this.history[0][0] > 2000) { this.history.shift(); }
            const recent = this.history.filter((item) => now - item[0] <= span);
            const bytes = recent.reduce((sum, item) => sum + item[1], 0);
            // What was asked over the same time, since the rate may just have changed.
            this.offered = recent.reduce((sum, item) => sum + item[2], 0) / recent.length;
            return bytes * 1000 / Math.max(QfmRate.SAMPLE_MS, now - recent[0][0] + QfmRate.SAMPLE_MS);
        }

        sample(rtt, now) {
            this.floorDelay = Math.min(this.floorDelay, rtt);
            // An echo waits for this page's main thread, so one sample says little: a busy
            // page delays many of them, while a queue on the path delays all of them.
            const previous = this.recent.length ? this.recent[this.recent.length - 1] : rtt;
            this.recent.push(rtt);
            if (this.recent.length > QfmRate.FILTER) { this.recent.shift(); }
            const base = this.floorDelay;
            const excess = Math.min(...this.recent) - base;
            // Far too much on two echoes in a row does not wait for the filter: the path has
            // just become much narrower, and the queue is growing fast.
            const sudden = Math.min(rtt, previous) - base;
            const pressure = sudden >= 4 * this.target ? Math.max(sudden, excess) : excess;
            this.unanswered = null;
            // A hidden tab samples once a second at best: growth follows time, not samples.
            const steps = this.lastSample === null ? 1 : Math.min(10, (now - this.lastSample) / QfmRate.SAMPLE_MS);
            this.lastSample = now;
            const delivered = this.delivered(now);
            // What arrives while standing aside says nothing about the path either.
            if (this.standingAside) { return excess; }
            if (this.draining) {
                // The queue is gone by the end of a drain, so what is left is the path itself.
                if (now > this.drainUntil - QfmRate.DRAIN_MS / 2) { this.drainLow = Math.min(this.drainLow, rtt); }
                if (now < this.drainUntil) { return excess; }
                this.draining = false;
                if (Number.isFinite(this.drainLow)) { this.floorDelay = this.drainLow; }
                this.recent = [this.floorDelay];
                // What arrived during the drain says nothing about the path.
                this.holdUntil = now + 500;
            }
            if (now < this.holdUntil) { return excess; }
            if (delivered > 0 && this.nextDrain !== null && now >= this.nextDrain) {
                this.startDrain(now);
                return excess;
            }
            if (this.nextDrain === null) { this.nextDrain = now + QfmRate.DRAIN_EVERY_MS; }
            // A pace that has just changed and whole frames keep arrivals about 10% under what
            // was asked even on an open path, so this has to be a clear shortfall.
            const full = delivered > 0 && delivered < this.offered * .75;
            const harmless = this.rate < QfmRate.HARMLESS && !full;
            if (pressure >= this.target && !harmless) {
                const factor = Math.max(.6, .95 - .05 * (pressure / this.target - 1));
                if (full || this.capacity === null) {
                    // This is the path's width, as far as can be seen beside the console.
                    const width = delivered > 0 ? Math.min(delivered, this.rate) : this.rate;
                    this.capacity = width >= QfmRate.HARMLESS ? width : null;
                    this.rate = width * factor;
                } else {
                    // It nearly all arrives: a slightly full path, a burst from the console, or
                    // a busy page. Give way a little and learn nothing about the path's width.
                    this.rate *= factor;
                }
                this.probing = 0;
                this.holdUntil = now + QfmRate.HOLD_MS;
                // Cut after cut with no effect: the queue is deep, or the delay is the path's
                // own and has just grown. A drain settles which, and now rather than later.
                this.cuts += 1;
                if (this.cuts >= 3 && now - this.lastDrain >= QfmRate.EARLIEST_DRAIN_MS) { this.startDrain(now); }
            } else {
                if (pressure < this.target) { this.cuts = 0; }
                this.grow(delivered, steps, harmless ? 1 : 1 - Math.max(0, excess) / this.target);
            }
            this.clamp();
            return excess;
        }

        grow(delivered, steps, room) {
            // A rate far above what is really sent would only store up a burst for later.
            if (this.rate >= Math.max(QfmRate.START, delivered * 2)) {
                if (delivered * 4 < this.rate) { this.rate *= .98 ** steps; }
                return;
            }
            if (this.capacity === null || this.rate < QfmRate.HARMLESS) {
                // A narrow path is found while the rate is low. Past that the same care would
                // spend many seconds of a fast link on getting up to speed.
                // A path of under 5 ms is a LAN, where a queue shows at once and costs little.
                const step = this.floorDelay < 5 ? .4 : this.rate < 1024 * 1024 ? .05 : this.rate < 8 * 1024 * 1024 ? .1 : .2;
                this.rate *= (1 + step * room) ** steps;
            } else if (this.rate < this.capacity * .95) {
                // Back to what the path was seen to carry, quickly: that much is known to fit.
                this.rate = Math.min(this.capacity * .95, this.rate * (1 + .1 * room) ** steps);
            } else {
                // Past it nothing is known: the console may have stopped using its share, so
                // the probe starts small and doubles every two seconds while the path holds.
                this.probing += steps;
                this.rate *= (1 + room * Math.min(.05, .002 * 2 ** (this.probing / 20))) ** steps;
            }
        }

        pinged(now) { if (this.unanswered === null) { this.unanswered = now; } }

        // An echo that does not come back is the narrow path's other signal: half a second
        // of queue, or a loss. Time since the last echo would not do, because in a hidden tab
        // pings themselves are a second apart.
        tick(now) {
            const patience = Number.isFinite(this.floorDelay) ? Math.max(300, 3 * this.floorDelay) : 1000;
            if (this.unanswered !== null && now - this.unanswered > patience) {
                const delivered = this.delivered(now);
                // On a path that has just narrowed what still arrives tells its new width.
                this.rate = delivered > 0 ? Math.min(this.rate * .7, delivered * .9) : this.rate * .7;
                this.unanswered = now;
                this.clamp();
            }
        }

        clamp() { this.rate = Math.min(QfmRate.MAX, Math.max(QfmRate.MIN, this.rate)); }
        window() { return Math.min(QfmRate.MAX_WINDOW, Math.max(64 * 1024, Math.round(this.rate * .5))); }
        // What waits in this browser delays every echo behind it. A hidden tab fills it once a second.
        queueLimit(hidden) { return hidden ? Math.max(16 * 1024, Math.round(this.current())) : Math.max(16 * 1024, Math.min(2 * 1024 * 1024, Math.round(this.current() * .05))); }
        burst(hidden, size) { return this.current() * (hidden ? 1 : .05) + size; }
        // A frame is a burst on the wire, and whatever comes next waits behind it.
        chunk() { return Math.min(QFM_CHUNK, Math.max(2048, Math.round(this.rate * .02))); }
    }

    // A strict priority, where it can be had: video the path is holding back goes first. A
    // desktop that is idle after a narrow spell sends next to nothing and holds nothing up.
    class QfmConsoleDemand {
        constructor({ videoRate, stats, now }) {
            this.videoRate = videoRate;
            this.stats = stats;
            this.now = now;
            this.last = null;
            this.seconds = 0;
        }

        async wants() {
            const rate = this.videoRate();
            const report = await this.stats();
            let bytes = 0;
            report.forEach((item) => { if (item.type === 'inbound-rtp' && item.kind === 'video') { bytes += item.bytesReceived || 0; } });
            const now = this.now();
            const last = this.last;
            this.last = { bytes, now };
            if (!rate || !last || now <= last.now || rate.bitrate >= rate.maximum * .9) {
                this.seconds = 0;
                return false;
            }
            // An idle desktop still sends a few hundred kbit/s, and a key frame or a repaint is a
            // brief burst on top. Only video that keeps going is waiting for the path.
            // A clip that a transfer has pushed down sends little and is still waiting, so
            // what it did recently counts: a long-running clip keeps its claim for seconds.
            const sending = (bytes - last.bytes) * 8 / (now - last.now) > Math.max(rate.bitrate * .4, 600);
            this.seconds = sending ? Math.min(15, this.seconds + 1) : Math.max(0, this.seconds - 1);
            return this.seconds >= 3;
        }
    }

    // The file session's own connection, its requests and its pacing. A text message is
    // an echo or a status, a binary one is a frame. See the design note.
    class QfmLink {
        static STALL_MS = 20000;
        static OPEN = 1;

        constructor({ createSocket, offer, timers, isHidden, onEnd, demand = null }) {
            this.createSocket = createSocket;
            this.offer = offer;
            this.timers = timers;
            this.isHidden = isHidden;
            this.onEnd = onEnd;
            this.demand = demand;
            this.downloads = 0;
            this.lastProgress = 0;
            this.rate = new QfmRate();
            this.pending = new Map();
            this.nextRequest = 1;
            this.waiters = [];
            this.tokens = 0;
            this.tokensAt = 0;
            this.inflight = 0;
            this.pumpTimer = null;
            this.pingTimer = null;
            this.closed = false;
            this.info = null;
            this.sentRate = 0;
        }

        now() { return this.timers.now(); }

        async open() {
            // A POST has no time limit of its own, and a dead path would hold this for good.
            let offerTimer;
            const answer = await Promise.race([this.offer(), new Promise((_resolve, reject) => {
                offerTimer = this.timers.setTimeout(() => reject(new QfmError('ELINK')), 20000);
            })]).finally(() => this.timers.clearTimeout(offerTimer));
            if (this.closed) { throw new QfmError(this.endReason || 'ELINK'); }
            const socket = this.createSocket(answer.socket);
            this.socket = socket;
            socket.binaryType = 'arraybuffer';
            const ready = new Promise((resolve, reject) => { this.readyWaiter = { resolve, reject }; });
            ready.catch(() => {}); // It can fail before anything waits on this.
            socket.addEventListener('message', (event) => this.receive(event.data));
            socket.addEventListener('close', () => this.end('ELINK'));
            this.timeoutTimer = this.timers.setTimeout(() => {
                if (this.readyWaiter) { this.readyWaiter.reject(new QfmError('ELINK')); }
            }, 20000);
            await ready;
            this.timers.clearTimeout(this.timeoutTimer);
            this.pingTimer = this.timers.setInterval(() => this.ping(), QfmRate.SAMPLE_MS);
            let helloTimer;
            try {
                this.info = await Promise.race([this.call('hello'), new Promise((_resolve, reject) => {
                    helloTimer = this.timers.setTimeout(() => reject(new QfmError('ELINK')), 20000);
                })]);
            } finally { this.timers.clearTimeout(helloTimer); }
            return this.info;
        }

        ping() {
            const now = this.now();
            const busy = this.pending.size > 0 || this.waiters.length > 0 || this.inflight > 0;
            this.pingCount = (this.pingCount || 0) + 1;
            if (this.pingCount % 10 === 0) { this.checkDemand(); }
            if (!busy && this.pingCount % 10) { return; }
            // After a stall of this page its echoes are still waiting to be read: not a loss.
            const punctual = now - (this.lastPing || now) < 250;
            this.lastPing = now;
            if (busy && punctual) { this.rate.tick(now); } else if (!busy) { this.rate.unanswered = null; }
            // Data on its way and nothing back for this long: whatever holds it (a dead path,
            // a stopped process), a new connection is the way on.
            if ((this.inflight > 0 || this.downloads > 0) && now - this.lastProgress > QfmLink.STALL_MS) {
                this.end('ESTALLED');
                return;
            }
            if (this.socket.readyState === QfmLink.OPEN) {
                this.rate.pinged(now);
                this.socket.send(JSON.stringify({ t: now }));
                const rate = Math.round(this.rate.current());
                if (Math.abs(rate - this.sentRate) > this.sentRate * .05) {
                    this.sentRate = rate;
                    this.socket.send(JSON.stringify({ rate }));
                }
            }
            this.pump();
        }

        // Only a download shares its direction with video, and with the file manager
        // covering the console nobody is watching that video.
        checkDemand() {
            const asked = this.demand && this.downloads > 0 && this.rate.target <= QfmRate.TARGET_BACKGROUND;
            if (!asked) {
                this.rate.standingAside = false;
                return;
            }
            this.demand.wants().then((wants) => { this.rate.standingAside = wants && this.downloads > 0; }, () => {});
        }

        receive(data) {
            if (typeof data === 'string') {
                let value;
                try { value = JSON.parse(data); } catch (_error) { return; }
                if (value && Number.isFinite(value.t)) {
                    const now = this.now();
                    if (value.t <= now) { this.rate.sample(now - value.t, now); }
                } else if (value && value.op === 'status') {
                    if (value.state === 'ready' && this.readyWaiter) {
                        this.readyWaiter.resolve();
                    } else if (value.state === 'closed') {
                        const reason = value.reason === 'nohelper' ? 'ENOHELPER' : 'ELINK';
                        if (this.readyWaiter) { this.readyWaiter.reject(new QfmError(reason)); }
                        this.end(reason);
                    }
                }
                return;
            }
            let frame;
            try { frame = qfmDecode(data); } catch (_error) { this.end('ELINK'); return; }
            const { header, payload } = frame;
            if (payload) { this.rate.deliver(payload.byteLength); }
            // Any frame: a long listing goes ahead of file data, and the link is alive meanwhile.
            this.lastProgress = this.now();
            const request = this.pending.get(header.id);
            if (!request) { return; }
            if (header.ok === true) {
                this.pending.delete(header.id);
                request.resolve(header);
            } else if (header.ok === false) {
                this.pending.delete(header.id);
                request.reject(new QfmError(typeof header.err === 'string' ? header.err : 'EIO',
                    typeof header.msg === 'string' ? header.msg : ''));
            } else if (request.onFrame) {
                try { request.onFrame(header, payload); } catch (error) { request.reject(error); this.pending.delete(header.id); }
            }
        }

        id() {
            const value = this.nextRequest;
            this.nextRequest = value >= 0x7fffffff ? 1 : value + 1;
            return value;
        }

        track(id, onFrame = null) {
            return new Promise((resolve, reject) => {
                if (this.closed) { reject(new QfmError(this.endReason || 'ELINK')); return; }
                this.pending.set(id, { resolve, reject, onFrame });
            });
        }

        // Tells the guest and stops waiting for it: a hung mount or a hostile guest never answers.
        abandon(id, error = new QfmError('ECANCELED')) {
            const request = this.pending.get(id);
            if (!request) { return; }
            this.pending.delete(id);
            try { this.send({ op: 'cancel', id }); } catch (_error) { /* Link closed. */ }
            request.reject(error);
        }

        send(header, payload = null) {
            if (this.closed || this.socket.readyState !== QfmLink.OPEN) { throw new QfmError('ELINK'); }
            this.socket.send(qfmEncode(header, payload));
        }

        // With a job, its cancel reaches this request too, even if the guest never answers.
        call(op, fields = {}, payload = null, onFrame = null, job = null) {
            const id = this.id();
            const result = this.track(id, onFrame);
            try { this.send({ op, id, ...fields }, payload); } catch (error) {
                this.pending.delete(id);
                result.catch(() => {});
                return Promise.reject(error);
            }
            if (!job) { return result; }
            const abort = () => this.abandon(id);
            job.aborts.add(abort);
            if (job.cancelled) { abort(); }
            return result.finally(() => job.aborts.delete(abort));
        }

        async list(path, job = null) {
            const entries = [];
            let truncated = false;
            const result = await this.call('list', { path }, null, (header) => {
                if (!header.part) { return; }
                const rows = qfmValidEntries(header.part);
                const room = QFM_MAX_LIST - entries.length;
                if (rows.length > room) { truncated = true; }
                for (const row of rows.slice(0, room)) { entries.push(row); }
            }, job);
            return { entries, path: typeof result.path === 'string' ? result.path : path,
                truncated: truncated || result.truncated === true };
        }

        // Resolves once `size` bytes may be sent: window, queue and rate all agree.
        acquire(size) {
            return new Promise((resolve, reject) => {
                if (this.closed) { reject(new QfmError(this.endReason || 'ELINK')); return; }
                this.waiters.push({ size, resolve, reject });
                this.pump();
            });
        }

        release(size) {
            this.inflight = Math.max(0, this.inflight - size);
            this.pump();
        }

        pump() {
            if (this.pumpTimer !== null) { this.timers.clearTimeout(this.pumpTimer); this.pumpTimer = null; }
            while (this.waiters.length && !this.closed) {
                const head = this.waiters[0];
                const hidden = this.isHidden();
                const now = this.now();
                this.tokens = Math.min(this.rate.burst(hidden, head.size),
                    this.tokens + Math.max(0, now - this.tokensAt) / 1000 * this.rate.current());
                this.tokensAt = now;
                if (this.inflight > 0 && this.inflight + head.size > this.rate.window()) { return; }
                // A WebSocket does not say when its queue has gone down.
                if (this.socket.bufferedAmount > this.rate.queueLimit(hidden)) {
                    this.pumpTimer = this.timers.setTimeout(() => { this.pumpTimer = null; this.pump(); }, 2);
                    return;
                }
                if (this.tokens < head.size) {
                    const wait = (head.size - this.tokens) / this.rate.current() * 1000;
                    this.pumpTimer = this.timers.setTimeout(() => { this.pumpTimer = null; this.pump(); }, Math.max(1, Math.min(50, wait)));
                    return;
                }
                this.tokens -= head.size;
                if (this.inflight === 0) { this.lastProgress = now; }
                this.inflight += head.size;
                this.waiters.shift();
                head.resolve();
            }
        }

        end(reason) {
            if (this.closed) { return; }
            this.closed = true;
            this.endReason = reason;
            this.timers.clearInterval(this.pingTimer);
            this.timers.clearTimeout(this.pumpTimer);
            this.timers.clearTimeout(this.timeoutTimer);
            const error = new QfmError(reason);
            if (this.readyWaiter) { this.readyWaiter.reject(error); }
            for (const waiter of this.waiters.splice(0)) { waiter.reject(error); }
            for (const request of this.pending.values()) { request.reject(error); }
            this.pending.clear();
            try { if (this.socket) { this.socket.close(); } } catch (_error) { /* Already closing. */ }
            if (this.onEnd) { this.onEnd(reason); }
        }

        close() { this.end('ELINK'); }
    }

    // One tar stream, for a browser that can hand over only one file per folder.
    const qfmTarHeader = (name, size, mtimeMs, type) => {
        const encoder = new TextEncoder();
        const bytes = (text) => encoder.encode(text);
        const block = (path, length, flag) => {
            const header = new Uint8Array(512);
            const put = (text, offset, max) => { header.set(bytes(text).subarray(0, max), offset); };
            const octal = (value, width) => value.toString(8).padStart(width - 1, '0');
            put(path, 0, 100);
            put(flag === '5' ? '0000755' : '0000644', 100, 7);
            put('0000000', 108, 7);
            put('0000000', 116, 7);
            put(octal(length, 12), 124, 11);
            put(octal(Math.max(0, Math.floor(mtimeMs / 1000)), 12), 136, 11);
            put('        ', 148, 8);
            put(flag, 156, 1);
            put('ustar', 257, 6);
            put('00', 263, 2);
            let sum = 0;
            for (const value of header) { sum += value; }
            put(`${sum.toString(8).padStart(6, '0')}\0 `, 148, 8);
            return header;
        };
        const encoded = bytes(name);
        // Long or non-ASCII names, and a size past ustar's 8 GiB, take a PAX record.
        const pax = [];
        if (encoded.length > 100 || /[^ -~]/.test(name)) { pax.push(['path', name]); }
        if (size >= 8589934592) { pax.push(['size', String(size)]); }
        const parts = [];
        if (pax.length) {
            let text = '';
            for (const [key, value] of pax) {
                const body = ` ${key}=${value}\n`;
                let length = bytes(body).length;
                length += String(length + String(length).length).length;
                text += `${length}${body}`;
            }
            const record = bytes(text);
            parts.push(block('qsm-pax', record.length, 'x'));
            const padded = new Uint8Array(Math.ceil(record.length / 512) * 512);
            padded.set(record);
            parts.push(padded);
        }
        parts.push(block(encoded.length > 100 || /[^ -~]/.test(name) ? 'qsm-long-name' : name,
            size >= 8589934592 ? 0 : size, type));
        const total = parts.reduce((sum, part) => sum + part.length, 0);
        const result = new Uint8Array(total);
        let offset = 0;
        for (const part of parts) { result.set(part, offset); offset += part.length; }
        return result;
    };

    // One read of a local file costs far more than a frame of it, so a fast transfer reads
    // ahead. A slow one reads as little as it did, and notices a file that changes as soon.
    class QfmBlockReader {
        static MAX = 8 * 1024 * 1024;

        constructor(file, rate) {
            this.file = file;
            this.rate = rate;
            this.block = null;
            this.start = 0;
        }

        async read(offset, length) {
            if (!this.block || offset < this.start || offset + length > this.start + this.block.byteLength) {
                const wanted = Math.min(QfmBlockReader.MAX, Math.max(length, Math.round(this.rate.current() * .05)));
                this.block = new Uint8Array(await this.file.slice(offset, Math.min(this.file.size, offset + wanted)).arrayBuffer());
                this.start = offset;
                if (this.block.byteLength < length) { return null; }
            }
            return this.block.subarray(offset - this.start, offset - this.start + length);
        }
    }

    // Downloads for a browser without a folder picker: spooled, then handed to
    // its Downloads folder. A memory fallback covers browsers without OPFS writes.
    class QfmSpool {
        static MEMORY_LIMIT = 1024 * 1024 * 1024;

        constructor(win, name) {
            this.win = win;
            this.name = name;
            this.parts = [];
            this.size = 0;
            this.position = 0;
        }

        async open() {
            const storage = this.win.navigator.storage;
            if (storage && typeof storage.getDirectory === 'function') {
                try {
                    this.directory = await storage.getDirectory();
                    // A tab closed before its download was cleaned up leaves its spool behind.
                    for await (const name of this.directory.keys()) {
                        const match = /^qsm-spool-(\d+)-/.exec(name);
                        if (match && Date.now() - Number(match[1]) > 86400000) {
                            await this.directory.removeEntry(name).catch(() => {});
                        }
                    }
                    this.spoolName = `qsm-spool-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                    this.handle = await this.directory.getFileHandle(this.spoolName, { create: true });
                    if (typeof this.handle.createWritable === 'function') {
                        this.writer = await this.handle.createWritable();
                        return this;
                    }
                    await this.directory.removeEntry(this.spoolName);
                } catch (_error) { /* Fall back to memory below. */ }
                this.handle = null;
            }
            return this;
        }

        async append(data) {
            if (this.writer) {
                await this.writer.write({ type: 'write', position: this.position, data });
            } else {
                if (this.size + data.byteLength > QfmSpool.MEMORY_LIMIT) { throw new QfmError('ELOCAL', gettext('Too large for this browser without a folder picker')); }
                this.parts.push(new Uint8Array(data));
            }
            this.position += data.byteLength;
            this.size += data.byteLength;
        }

        async pad(count) { if (count) { await this.append(new Uint8Array(count)); } }

        async finish() {
            let file;
            if (this.writer) {
                await this.writer.close();
                this.writer = null;
                file = await this.handle.getFile();
            } else {
                file = new this.win.Blob(this.parts);
            }
            const url = this.win.URL.createObjectURL(file);
            const link = this.win.document.createElement('a');
            link.href = url;
            link.download = this.name;
            link.style.display = 'none';
            this.win.document.body.append(link);
            link.click();
            link.remove();
            // The pane lists it, and for these ten minutes it can go back to the guest from there.
            // After that only its name and size are left: the copy is in the Downloads folder.
            this.win.setTimeout(() => {
                this.win.URL.revokeObjectURL(url);
                if (this.handle) { this.directory.removeEntry(this.spoolName).catch(() => {}); }
            }, 600000);
            return { name: this.name, file };
        }

        async abort() {
            try {
                if (this.writer) { await this.writer.abort(); }
                if (this.handle) { await this.directory.removeEntry(this.spoolName); }
            } catch (_error) { /* Nothing left to remove. */ }
            this.writer = null;
            this.parts = [];
        }
    }

    // Local panes share readdir/file. Folder handles (Chromium) browse real
    // folders; the staged tree holds what a user dropped or picked elsewhere.
    class QfmLocalFolders {
        constructor(root) {
            this.stack = [{ name: root.name, handle: root }];
            this.writable = true;
            this.kind = 'folder';
        }

        get location() { return this.stack.map((item) => item.name).join('/'); }
        get atRoot() { return this.stack.length === 1; }
        get handle() { return this.stack[this.stack.length - 1].handle; }

        async readdir(directory = this.handle) {
            const entries = [];
            for await (const [name, handle] of directory.entries()) {
                if (handle.kind === 'directory') {
                    entries.push({ name, type: 'd', size: 0, mtime: 0, handle });
                } else {
                    let file = null;
                    try { file = await handle.getFile(); } catch (_error) { /* Shown without details. */ }
                    entries.push({ name, type: 'f', size: file ? file.size : 0, mtime: file ? file.lastModified : 0, handle });
                }
            }
            return entries;
        }

        async file(entry) { return entry.handle.getFile(); }
        directoryOf(entry) { return entry.handle; }
        parentDirectory() { return this.stack[this.stack.length - 2].handle; }
        enter(entry) { this.stack.push({ name: entry.name, handle: entry.handle }); }
        up() { if (!this.atRoot) { this.stack.pop(); } }
        async mkdir(name) { await this.handle.getDirectoryHandle(name, { create: true }); }
        async remove(entry) { await this.handle.removeEntry(entry.name, { recursive: true }); }
        async rename(entry, name) {
            if (entry.type !== 'f' || typeof entry.handle.move !== 'function') { throw new QfmError('ELOCAL', gettext('This browser cannot rename a local folder')); }
            await entry.handle.move(name);
        }
    }

    class QfmStagedFiles {
        constructor() {
            this.root = { name: '', type: 'd', children: new Map() };
            this.stack = [this.root];
            this.writable = false;
            this.kind = 'staged';
        }

        get location() {
            return this.stack.map((item) => item.name).join('/') ||
                gettext('Added here, and downloaded from the guest into your Downloads folder');
        }
        get atRoot() { return this.stack.length === 1; }

        add(path, file) {
            const parts = path.split('/').filter((part) => part && part !== '.' && part !== '..');
            let node = this.root;
            for (const part of parts.slice(0, -1)) {
                if (!node.children.has(part)) { node.children.set(part, { name: part, type: 'd', children: new Map(), size: 0, mtime: 0 }); }
                node = node.children.get(part);
            }
            const name = parts[parts.length - 1];
            if (name && file) { node.children.set(name, { name, type: 'f', file, size: file.size, mtime: file.lastModified }); }
            if (name && !file && !node.children.has(name)) { node.children.set(name, { name, type: 'd', children: new Map(), size: 0, mtime: 0 }); }
        }

        async readdir(directory = this.stack[this.stack.length - 1]) { return [...directory.children.values()]; }
        async file(entry) { return entry.file; }
        directoryOf(entry) { return entry; }
        parentDirectory() { return this.stack[this.stack.length - 2]; }
        enter(entry) { this.stack.push(entry); }
        up() { if (!this.atRoot) { this.stack.pop(); } }
        async remove(entry) { this.stack[this.stack.length - 1].children.delete(entry.name); }

        // A file saved to the Downloads folder: listed at the top, as a page cannot see that folder.
        delivered({ name, file }) {
            this.add(name, file);
            const entry = this.root.children.get(name);
            if (entry && entry.file === file) { entry.mtime = Date.now(); }
        }
    }

    const qfmReadDroppedEntry = async (entry, path, staged) => {
        const target = path ? `${path}/${entry.name}` : entry.name;
        if (entry.isFile) {
            const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
            staged.add(target, file);
            return;
        }
        staged.add(target, null);
        const reader = entry.createReader();
        for (;;) {
            const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
            if (!batch.length) { return; }
            for (const child of batch) { await qfmReadDroppedEntry(child, target, staged); }
        }
    };

    // One user action. Its worker pool shares the link's window with other jobs.
    class QfmJob {
        constructor(label, direction) {
            this.label = label;
            this.direction = direction;
            this.state = 'queued';
            this.total = 0;
            this.done = 0;
            this.files = 0;
            this.filesDone = 0;
            this.skipped = 0;
            this.errors = [];
            this.cancelled = false;
            this.policy = null;
            this.started = 0;
            this.samples = [];
            this.aborts = new Set();
            this.halted = false;
        }

        cancel() {
            this.cancelled = true;
            for (const abort of this.aborts) { abort(); }
        }

        add(bytes, now) {
            this.done += bytes;
            this.samples.push([now, this.done]);
            while (this.samples.length > 2 && now - this.samples[0][0] > 5000) { this.samples.shift(); }
        }

        speed() {
            if (this.samples.length < 2) { return 0; }
            const [startAt, start] = this.samples[0];
            const [endAt, end] = this.samples[this.samples.length - 1];
            return endAt > startAt ? (end - start) * 1000 / (endAt - startAt) : 0;
        }
    }

    // Copies one job. It knows the link and two sources, and nothing of the page.
    class QfmTransfers {
        constructor({ link, now, sleep, ask, notify, spool, relink = null, delivered = null }) {
            this.link = link;
            this.relink = relink;
            this.delivered = delivered;
            this.now = now;
            this.sleep = sleep;
            this.ask = ask;
            this.notify = notify;
            this.spool = spool;
            this.activeGets = 0;
        }

        guestStep(job, action) {
            const attempt = async () => {
                let stalls = 0;
                for (;;) {
                    try { return await action(); } catch (error) {
                        if (error && (error.code === 'ESTALLED' || error.code === 'ELINK') && this.relink && !job.cancelled && stalls < 3) {
                            // A connection that stopped or went away, and the path may well be
                            // back. The file starts over on a new connection; its temporary file
                            // went with the old session.
                            stalls += 1;
                            // Nothing else listens for a cancel while a new link comes up.
                            let stop;
                            const stopped = new Promise((_resolve, reject) => { stop = () => reject(new QfmError('ECANCELED')); });
                            job.aborts.add(stop);
                            try { await Promise.race([this.relink(), stopped]); } finally { job.aborts.delete(stop); }
                            continue;
                        }
                        if (!error || error.code !== 'ELOCKED' || job.cancelled) { throw error; }
                        job.state = 'locked';
                        this.notify();
                        await this.sleep(2000);
                        if (job.cancelled) { throw new QfmError('ECANCELED'); }
                        const hello = await this.link().call('hello');
                        if (!hello.locked) { job.state = 'running'; this.notify(); }
                    }
                }
            };
            return attempt();
        }

        async pool(job, tasks, workers, run) {
            let next = 0;
            const worker = async () => {
                while (next < tasks.length && !job.cancelled && !job.halted) {
                    const task = tasks[next];
                    next += 1;
                    try {
                        await run(task);
                    } catch (error) {
                        if (error && error.code === 'ECANCELED') { continue; }
                        job.errors.push({ path: task.path || task.name, error });
                        this.notify();
                        // The rest of the job cannot arrive over a closed link.
                        if (error && (error.code === 'ELINK' || error.code === 'ENOHELPER' || error.code === 'ESTALLED')) { job.halted = true; }
                    }
                }
            };
            await Promise.all(Array.from({ length: workers }, worker));
        }

        async conflict(job, path) {
            if (job.policy) { return job.policy; }
            if (!this.conflictLock) { this.conflictLock = Promise.resolve(); }
            const previous = this.conflictLock;
            let release;
            this.conflictLock = new Promise((resolve) => { release = resolve; });
            await previous;
            try {
                if (job.policy) { return job.policy; }
                const { choice, all } = await this.ask(path);
                if (choice === 'cancel') { job.cancel(); throw new QfmError('ECANCELED'); }
                if (all) { job.policy = choice; }
                return choice;
            } finally { release(); }
        }

        static alternative(path, attempt) {
            const cut = path.lastIndexOf('/');
            const name = path.slice(cut + 1);
            const dot = name.lastIndexOf('.');
            const stem = dot > 0 ? name.slice(0, dot) : name;
            const extension = dot > 0 ? name.slice(dot) : '';
            return `${path.slice(0, cut + 1)}${stem} (${attempt})${extension}`;
        }

        // ---- browser to guest
        async planUpload(job, source, entries, destination) {
            const directories = [];
            const files = [];
            const visit = async (entry, target) => {
                if (job.cancelled) { return; }
                if (entry.type === 'd') {
                    directories.push(target);
                    for (const child of await source.readdir(entry.children ? entry : entry.handle)) {
                        if (qfmSafeName(child.name)) { await visit(child, qfmJoin(target, child.name)); } else { job.skipped += 1; }
                    }
                } else if (entry.type === 'f') {
                    files.push({ entry, path: target });
                    job.total += entry.size;
                    job.files += 1;
                    this.notify();
                } else { job.skipped += 1; }
            };
            for (const entry of entries) { await visit(entry, qfmJoin(destination, entry.name)); }
            return { directories, files };
        }

        async upload(job, source, entries, destination) {
            const plan = await this.planUpload(job, source, entries, destination);
            job.state = 'running';
            this.notify();
            for (const directory of plan.directories) {
                if (job.cancelled) { return; }
                try {
                    await this.guestStep(job, () => this.link().call('mkdir', { path: directory, parents: true }, null, null, job));
                } catch (error) {
                    if (!error || error.code !== 'ECANCELED') { job.errors.push({ path: directory, error }); }
                }
            }
            await this.pool(job, plan.files, QFM_WORKERS, async (task) => {
                const file = await source.file(task.entry).catch(() => { throw new QfmError('ELOCAL'); });
                let path = task.path;
                let overwrite = job.policy === 'replace' ? 'replace' : 'fail';
                for (let attempt = 1; ; attempt += 1) {
                    try {
                        await this.guestStep(job, () => this.putFile(job, file, path, overwrite));
                        job.filesDone += 1;
                        this.notify();
                        return;
                    } catch (error) {
                        if (!error || error.code !== 'EEXIST' || overwrite === 'replace') { throw error; }
                        const choice = await this.conflict(job, path);
                        if (choice === 'skip') { job.skipped += 1; job.total -= file.size; this.notify(); return; }
                        if (choice === 'replace') { overwrite = 'replace'; } else { path = QfmTransfers.alternative(task.path, attempt); }
                    }
                }
            });
        }

        async putFile(job, file, path, overwrite) {
            if (job.cancelled) { throw new QfmError('ECANCELED'); }
            const link = this.link();
            const size = file.size;
            const fields = { path, size, overwrite };
            if (Number.isFinite(file.lastModified) && file.lastModified > 0) { fields.mtime = Math.round(file.lastModified); }
            // The name goes in the same frame, and a long one in \\uXXXX takes real room.
            const headerRoom = 65536 - qfmEncode({ op: 'put', id: 0x7fffffff, ...fields, last: true }).byteLength;
            if (size <= Math.min(link.rate.chunk(), headerRoom)) {
                const data = await file.arrayBuffer().catch(() => { throw new QfmError('ELOCAL'); });
                await link.acquire(data.byteLength + 256);
                try {
                    await link.call('put', { ...fields, last: true }, data, null, job);
                } finally { link.release(data.byteLength + 256); }
                link.rate.deliver(data.byteLength);
                job.add(data.byteLength, this.now());
                this.notify();
                return;
            }
            const id = link.id();
            let acked = 0;
            let sent = 0;
            let failure = null;
            const abort = () => link.abandon(id);
            job.aborts.add(abort);
            const final = link.track(id, (header) => {
                if (Number.isSafeInteger(header.ack) && header.ack > acked && header.ack <= sent) {
                    const delta = header.ack - acked;
                    acked = header.ack;
                    link.release(delta);
                    link.rate.deliver(delta);
                    job.add(delta, this.now());
                    this.notify();
                }
            }).catch((error) => {
                failure = error;
                // A stalled window must not outlive the upload it belonged to.
                link.release(sent - acked);
                job.done = Math.max(0, job.done - acked);
                sent = 0;
                acked = 0;
                throw error;
            });
            final.catch(() => {});
            try {
                link.send({ op: 'put', id, ...fields });
                const reader = new QfmBlockReader(file, link.rate);
                let offset = 0;
                while (offset < size && !failure && !job.cancelled) {
                    const length = Math.min(link.rate.chunk(), size - offset);
                    await link.acquire(length);
                    if (failure) { link.release(length); break; }
                    let data = null;
                    let local = null;
                    try {
                        data = await reader.read(offset, length);
                        if (!data) { local = 'ECHANGED'; }
                    } catch (_error) { local = 'ELOCAL'; }
                    if (failure || local) {
                        link.release(length);
                        if (local && !failure) { abort(); await final.catch(() => {}); throw new QfmError(local); }
                        break;
                    }
                    sent += length;
                    link.send({ op: 'data', id, o: offset }, data);
                    offset += length;
                }
                if (!failure) {
                    // A cancel can come by the dialog of another file, with this one idle.
                    if (job.cancelled) { abort(); } else { link.send({ op: 'end', id, size }); }
                }
                await final;
                if (acked < size) { job.add(size - acked, this.now()); }
            } finally {
                job.aborts.delete(abort);
                link.release(sent - acked);
                sent = acked;
            }
        }

        // ---- guest to browser
        async planDownload(job, entries, destinationName) {
            const directories = [];
            const files = [];
            const visit = async (entry, path, relative, top) => {
                if (job.cancelled) { return; }
                const type = entry.type === 'l' && top ? entry.target : entry.type;
                if (type === 'd') {
                    directories.push(relative);
                    const listing = await this.guestStep(job, () => this.link().list(path, job));
                    if (listing.truncated) { job.errors.push({ path, error: new QfmError('ETRUNCATED') }); }
                    for (const child of listing.entries) {
                        if (!qfmSafeName(child.name)) { job.skipped += 1; continue; }
                        await visit(child, qfmJoin(path, child.name), [...relative, child.name], false);
                    }
                } else if (type === 'f') {
                    files.push({ entry, path, relative });
                    job.total += entry.size;
                    job.files += 1;
                    this.notify();
                } else { job.skipped += 1; }
            };
            for (const item of entries) {
                if (!qfmSafeName(item.entry.name)) { job.skipped += 1; continue; }
                await visit(item.entry, item.path, destinationName ? [destinationName, item.entry.name] : [item.entry.name], true);
            }
            return { directories, files };
        }

        async getFile(job, path, sink) {
            const link = this.link();
            const id = link.id();
            let written = 0;
            let granted = Math.min(link.rate.window(), 128 * 1024);
            let declared = null;
            let writer = null;
            let chain = Promise.resolve();
            let failure = null;
            let held = [];
            const fail = (error) => {
                if (!failure) {
                    failure = error;
                    link.abandon(id, error);
                }
            };
            const queue = (step) => { chain = chain.then(() => (failure ? undefined : step())).catch(fail); };
            const abort = () => fail(new QfmError('ECANCELED'));
            job.aborts.add(abort);
            this.activeGets += 1;
            if (link.downloads === 0) { link.lastProgress = link.now(); }
            link.downloads += 1;
            const final = link.track(id, (header, payload) => {
                if (header.meta === true) {
                    if (!Number.isSafeInteger(header.size) || header.size < 0) { fail(new QfmError('EPROTO')); return; }
                    declared = header.size;
                    queue(async () => { writer = await sink(header); });
                } else if (Number.isSafeInteger(header.o) && payload) {
                    if (job.cancelled) { fail(new QfmError('ECANCELED')); return; }
                    // More than was granted is a guest filling this disk, not a transfer.
                    if (declared === null || header.o + payload.byteLength > granted) { fail(new QfmError('EPROTO')); return; }
                    // A write costs far more than a frame, and a queue here means the disk is
                    // behind: whatever has piled up goes in one write.
                    held.push(header.o, payload);
                    if (held.length > 2) { return; }
                    queue(async () => {
                        const pieces = held;
                        held = [];
                        const position = pieces[0];
                        let size = 0;
                        for (let index = 0; index < pieces.length; index += 2) {
                            if (pieces[index] !== position + size) { throw new QfmError('EIO'); }
                            size += pieces[index + 1].byteLength;
                        }
                        if (!writer || position !== written) { throw new QfmError('EIO'); }
                        if (written + size > declared) { throw new QfmError('ECHANGED'); }
                        let block = pieces[1];
                        if (pieces.length > 2) {
                            block = new Uint8Array(size);
                            let offset = 0;
                            for (let index = 1; index < pieces.length; index += 2) {
                                block.set(pieces[index], offset);
                                offset += pieces[index].byteLength;
                            }
                        }
                        await writer.write(position, block);
                        written += size;
                        job.add(size, this.now());
                        this.notify();
                        const wanted = Math.max(64 * 1024, Math.floor(link.rate.window() / Math.max(1, this.activeGets)));
                        const outstanding = granted - written;
                        if (outstanding < wanted / 2) {
                            const grant = wanted - outstanding;
                            granted += grant;
                            link.send({ op: 'credit', id, n: grant, chunk: link.rate.chunk() });
                        }
                    });
                }
            });
            final.catch(() => {});
            try {
                link.send({ op: 'get', id, path, credit: granted, chunk: link.rate.chunk() });
                const result = await final;
                await chain;
                if (failure) { throw failure; }
                if (!writer) { writer = await sink({ size: 0, mtime: 0 }); }
                // A torn copy must not replace a good file or pass for one.
                if (result.changed === true) { throw new QfmError('ECHANGED'); }
                await writer.close(written);
            } catch (error) {
                await chain;
                job.done = Math.max(0, job.done - written);
                if (writer) { await writer.abort().catch(() => {}); }
                throw failure && error.code === 'ECANCELED' ? failure : error;
            } finally {
                this.activeGets -= 1;
                link.downloads -= 1;
                job.aborts.delete(abort);
            }
        }

        // Into a folder chosen in Chromium.
        async downloadToFolders(job, entries, target) {
            const plan = await this.planDownload(job, entries, null);
            job.state = 'running';
            this.notify();
            const folders = new Map();
            // Two guest names can be one name here (case, or bytes that are not UTF-8).
            const reserved = new Set();
            const reserve = (place, name) => {
                const key = `${place}/${(name.toWellFormed ? name.toWellFormed() : name).toLowerCase()}`;
                if (reserved.has(key)) { return false; }
                reserved.add(key);
                return true;
            };
            const folder = async (parts) => {
                let handle = target.handle;
                let key = '';
                for (const part of parts) {
                    key += `/${part}`;
                    if (!folders.has(key)) { folders.set(key, await handle.getDirectoryHandle(part, { create: true })); }
                    handle = folders.get(key);
                }
                return handle;
            };
            for (const directory of plan.directories) {
                try { await folder(directory); } catch (error) { job.errors.push({ path: directory.join('/'), error: new QfmError('ELOCAL', error.message) }); }
            }
            await this.pool(job, plan.files, QFM_WORKERS, async (task) => {
                const directory = await folder(task.relative.slice(0, -1)).catch((error) => { throw new QfmError('ELOCAL', error.message); });
                const place = task.relative.slice(0, -1).join('/');
                const original = task.relative[task.relative.length - 1];
                let name = original;
                let existed = false;
                for (let attempt = 1; ; attempt += 1) {
                    if (!reserve(place, name)) {
                        name = QfmTransfers.alternative(original, attempt);
                        continue;
                    }
                    try { await directory.getFileHandle(name); existed = true; } catch (_error) { existed = false; }
                    if (!existed) { break; }
                    const choice = await this.conflict(job, task.relative.join('/'));
                    if (choice === 'skip') { job.skipped += 1; job.total -= task.entry.size; this.notify(); return; }
                    if (choice === 'replace') { break; }
                    name = QfmTransfers.alternative(original, attempt);
                }
                await this.guestStep(job, () => this.getFile(job, task.path, async () => {
                    const handle = await directory.getFileHandle(name, { create: true });
                    const stream = await handle.createWritable();
                    return {
                        write: (position, data) => stream.write({ type: 'write', position, data }),
                        close: () => stream.close(),
                        // Creating the handle already made an empty file under the final name.
                        abort: async () => {
                            await stream.abort();
                            if (!existed) { await directory.removeEntry(name).catch(() => {}); }
                        },
                    };
                }));
                job.filesDone += 1;
                this.notify();
            });
        }

        // Through the browser's Downloads folder: one file as it is, or one tar.
        async downloadToSpool(job, entries, archiveName) {
            const plan = await this.planDownload(job, entries, null);
            job.state = 'running';
            this.notify();
            if (job.cancelled || (!plan.files.length && !plan.directories.length)) { return; }
            const single = plan.files.length === 1 && !plan.directories.length;
            if (single) {
                const task = plan.files[0];
                let spool = null;
                await this.guestStep(job, () => this.getFile(job, task.path, async () => {
                    // Each attempt has its own: an aborted one has ended its file.
                    spool = await this.spool(task.relative[task.relative.length - 1]);
                    return {
                        write: async (position, data) => { await spool.append(data); },
                        close: async () => { this.deliver(await spool.finish()); },
                        abort: () => spool.abort(),
                    };
                })).catch(async (error) => {
                    if (!error || error.code !== 'ECANCELED') { job.errors.push({ path: task.path, error }); }
                    if (spool) { await spool.abort(); }
                });
                if (!job.errors.length) { job.filesDone += 1; }
                return;
            }
            const spool = await this.spool(archiveName);
            for (const directory of plan.directories) {
                await spool.append(qfmTarHeader(`${directory.join('/')}/`, 0, 0, '5'));
            }
            let failed = false;
            await this.pool(job, plan.files, 1, async (task) => {
                let declared = 0;
                let length = 0;
                let headed = false;
                await this.guestStep(job, () => this.getFile(job, task.path, async (meta) => {
                    if (headed) { throw new QfmError('EIO'); }
                    headed = true;
                    declared = meta.size;
                    await spool.append(qfmTarHeader(task.relative.join('/'), declared, meta.mtime || 0, '0'));
                    return {
                        write: async (position, data) => {
                            if (length + data.byteLength > declared) { throw new QfmError('ECHANGED'); }
                            length += data.byteLength;
                            await spool.append(data);
                        },
                        close: async () => {
                            if (length !== declared) { throw new QfmError('ECHANGED'); }
                            await spool.pad((512 - (declared % 512)) % 512);
                        },
                        abort: async () => { failed = true; },
                    };
                }));
                job.filesDone += 1;
                this.notify();
            });
            if (failed || job.cancelled) {
                await spool.abort();
                if (failed) { job.errors.push({ path: archiveName, error: new QfmError('ELOCAL', gettext('The archive was not saved because a file in it failed')) }); }
                return;
            }
            await spool.pad(1024);
            this.deliver(await spool.finish());
        }

        deliver(saved) {
            if (saved && saved.file && this.delivered) { this.delivered(saved); }
        }
    }

    class QfmPane {
        constructor(manager, side, title) {
            this.manager = manager;
            this.side = side;
            this.entries = [];
            this.visible = [];
            this.selected = new Set();
            this.cursor = 0;
            this.showHidden = false;
            this.sort = { key: 'name', ascending: true };
            const doc = manager.doc;
            const create = (tag, className, text) => {
                const element = doc.createElement(tag);
                if (className) { element.className = className; }
                if (text) { element.textContent = text; }
                return element;
            };
            this.create = create;
            this.root = create('section', `qfm-pane qfm-${side}`);
            this.root.setAttribute('aria-label', title);
            const heading = create('div', 'qfm-heading');
            this.titleLabel = create('strong', '', title);
            this.location = create('span', 'qfm-location');
            heading.append(this.titleLabel, this.location);
            this.tools = create('div', 'qfm-tools');
            const button = (text, label, action) => {
                const item = create('button', '', text);
                item.type = 'button';
                item.title = label;
                item.setAttribute('aria-label', label);
                item.addEventListener('click', () => { this.focus(); action(); });
                this.tools.append(item);
                return item;
            };
            this.upButton = button('↑', gettext('Up one folder (Backspace)'), () => this.up());
            this.refreshButton = button('⟳', gettext('Refresh'), () => this.refresh());
            this.mkdirButton = button('+', gettext('New folder (F7)'), () => this.mkdir());
            this.renameButton = button('✎', gettext('Rename (F2)'), () => this.rename());
            this.deleteButton = button('🗑', gettext('Delete (F8)'), () => this.remove());
            this.hiddenButton = button('.*', gettext('Show hidden files'), () => {
                this.showHidden = !this.showHidden;
                this.hiddenButton.setAttribute('aria-pressed', String(this.showHidden));
                this.apply();
            });
            this.hiddenButton.setAttribute('aria-pressed', 'false');
            this.extra = create('span', 'qfm-extra');
            this.copyButton = create('button', 'qfm-copy', side === 'local' ? gettext('Copy to guest →') : gettext('← Copy here'));
            this.copyButton.type = 'button';
            this.copyButton.title = gettext('Copy the selection to the other side (F5)');
            this.copyButton.addEventListener('click', () => { this.focus(); manager.copyFrom(this); });
            this.tools.append(this.extra, this.copyButton);
            this.columns = create('div', 'qfm-columns');
            for (const [key, text] of [['name', gettext('Name')], ['size', gettext('Size')], ['mtime', gettext('Modified')]]) {
                const column = create('button', `qfm-col qfm-col-${key}`, text);
                column.type = 'button';
                column.addEventListener('click', () => {
                    this.sort = { key, ascending: this.sort.key === key ? !this.sort.ascending : true };
                    this.apply();
                });
                this.columns.append(column);
            }
            this.scroller = create('div', 'qfm-list');
            this.scroller.tabIndex = 0;
            this.scroller.setAttribute('role', 'listbox');
            this.scroller.setAttribute('aria-multiselectable', 'true');
            this.scroller.setAttribute('aria-label', title);
            this.spacer = create('div', 'qfm-spacer');
            this.rows = create('div', 'qfm-rows');
            this.spacer.append(this.rows);
            this.message = create('div', 'qfm-message');
            this.scroller.append(this.spacer, this.message);
            this.root.append(heading, this.tools, this.columns, this.scroller);
            this.scroller.addEventListener('scroll', () => this.render());
            this.scroller.addEventListener('keydown', (event) => this.keydown(event));
            this.scroller.addEventListener('focus', () => { manager.active = this; manager.syncActive(); });
            this.rows.addEventListener('click', (event) => this.click(event));
            this.rows.addEventListener('dblclick', (event) => {
                const index = this.rowIndex(event);
                if (index !== null) { this.cursor = index; this.open(); }
            });
            this.rows.addEventListener('dragstart', (event) => {
                const index = this.rowIndex(event);
                if (index === null) { return; }
                if (!this.selected.has(this.visible[index])) { this.selected = new Set([this.visible[index]]); this.render(); }
                manager.dragging = this;
                event.dataTransfer.effectAllowed = 'copy';
                event.dataTransfer.setData('text/plain', this.visible[index].name);
            });
            this.rows.addEventListener('dragend', () => { manager.dragging = null; });
            this.root.addEventListener('dragover', (event) => {
                if (manager.acceptsDrop(this, event)) { event.preventDefault(); this.root.classList.add('qfm-drop'); }
            });
            this.root.addEventListener('dragleave', (event) => {
                if (!this.root.contains(event.relatedTarget)) { this.root.classList.remove('qfm-drop'); }
            });
            this.root.addEventListener('drop', (event) => {
                this.root.classList.remove('qfm-drop');
                if (manager.acceptsDrop(this, event)) { event.preventDefault(); manager.drop(this, event); }
            });
        }

        focus() { this.scroller.focus({ preventScroll: true }); }

        // A new folder starts afresh. The same folder read again keeps what the user chose, by
        // name: a refresh finishing in the background must not turn Copy or Delete on another row.
        setEntries(entries, sameFolder = false) {
            const chosen = sameFolder ? new Set([...this.selected].map((entry) => entry.name)) : new Set();
            const under = sameFolder && this.visible[this.cursor] ? this.visible[this.cursor].name : null;
            const top = this.scroller.scrollTop;
            this.entries = entries;
            this.selected = new Set(entries.filter((entry) => chosen.has(entry.name)));
            this.cursor = 0;
            this.scroller.scrollTop = sameFolder ? top : 0;
            this.apply();
            const index = under === null ? -1 : this.visible.findIndex((entry) => entry.name === under);
            if (index >= 0) { this.cursor = index; this.render(); }
        }

        apply() {
            const previous = this.visible[this.cursor];
            const direction = this.sort.ascending ? 1 : -1;
            const key = this.sort.key;
            const collator = this.manager.collator;
            const rank = (entry) => ((entry.type === 'l' ? entry.target : entry.type) === 'd' ? 0 : 1);
            this.visible = this.entries.filter((entry) => this.showHidden || !entry.name.startsWith('.'))
                .sort((a, b) => rank(a) - rank(b) || (key === 'name' ? 0 : direction * ((a[key] || 0) - (b[key] || 0))) ||
                    direction * collator.compare(a.name, b.name));
            const index = previous ? this.visible.indexOf(previous) : 0;
            this.cursor = Math.max(0, index);
            this.spacer.style.height = `${this.visible.length * QFM_ROW}px`;
            this.render();
        }

        showMessage(text) { this.message.textContent = text || ''; this.message.style.display = text ? 'block' : 'none'; }

        render() {
            const top = this.scroller.scrollTop;
            const first = Math.max(0, Math.floor(top / QFM_ROW) - 4);
            const last = Math.min(this.visible.length, first + Math.ceil(this.scroller.clientHeight / QFM_ROW) + 8);
            this.rows.style.transform = `translateY(${first * QFM_ROW}px)`;
            // Rows stay the same elements: a double click or a drag starting on one
            // needs the row it pressed to still be there after the selection repaints.
            const count = Math.max(0, last - first);
            while (this.rows.children.length > count) { this.rows.lastChild.remove(); }
            while (this.rows.children.length < count) {
                const row = this.create('div', 'qfm-row');
                row.draggable = true;
                row.setAttribute('role', 'option');
                row.append(this.create('span', 'qfm-icon'), this.create('span', 'qfm-name'),
                    this.create('span', 'qfm-col-size'), this.create('span', 'qfm-col-mtime'));
                this.rows.append(row);
            }
            for (let offset = 0; offset < count; offset += 1) {
                const index = first + offset;
                const entry = this.visible[index];
                const row = this.rows.children[offset];
                row.dataset.index = String(index);
                const chosen = this.selected.has(entry);
                row.setAttribute('aria-selected', String(chosen));
                row.classList.toggle('qfm-selected', chosen);
                row.classList.toggle('qfm-cursor', index === this.cursor);
                const kind = entry.type === 'l' ? entry.target : entry.type;
                const [icon, name, size, mtime] = row.children;
                icon.textContent = kind === 'd' ? '📁' : entry.type === 'l' ? '↪' : entry.type === 'o' ? '◇' : '📄';
                name.textContent = entry.name;
                name.title = entry.name;
                size.textContent = kind === 'd' ? '' : qfmSize(entry.size);
                mtime.textContent = entry.mtime ? new Date(entry.mtime).toLocaleString() : '';
            }
        }

        rowIndex(event) {
            const row = event.target.closest && event.target.closest('.qfm-row');
            if (!row) { return null; }
            const index = Number(row.dataset.index);
            return Number.isInteger(index) && index < this.visible.length ? index : null;
        }

        click(event) {
            const index = this.rowIndex(event);
            if (index === null) { return; }
            const entry = this.visible[index];
            if (event.shiftKey) {
                const [start, end] = this.cursor < index ? [this.cursor, index] : [index, this.cursor];
                for (let item = start; item <= end; item += 1) { this.selected.add(this.visible[item]); }
            } else if (event.ctrlKey || event.metaKey) {
                if (this.selected.has(entry)) { this.selected.delete(entry); } else { this.selected.add(entry); }
            } else {
                this.selected = new Set([entry]);
            }
            this.cursor = index;
            this.render();
        }

        move(index) {
            this.cursor = Math.max(0, Math.min(this.visible.length - 1, index));
            const top = this.cursor * QFM_ROW;
            if (top < this.scroller.scrollTop) { this.scroller.scrollTop = top; } else if (top + QFM_ROW > this.scroller.scrollTop + this.scroller.clientHeight) {
                this.scroller.scrollTop = top + QFM_ROW - this.scroller.clientHeight;
            }
            this.render();
        }

        keydown(event) {
            const page = Math.max(1, Math.floor(this.scroller.clientHeight / QFM_ROW) - 1);
            const actions = {
                ArrowUp: () => this.move(this.cursor - 1), ArrowDown: () => this.move(this.cursor + 1),
                PageUp: () => this.move(this.cursor - page), PageDown: () => this.move(this.cursor + page),
                Home: () => this.move(0), End: () => this.move(this.visible.length - 1),
                Enter: () => this.open(), Backspace: () => this.up(),
                Insert: () => {
                    const entry = this.visible[this.cursor];
                    if (entry) { if (this.selected.has(entry)) { this.selected.delete(entry); } else { this.selected.add(entry); } }
                    this.move(this.cursor + 1);
                },
                Tab: () => this.manager.other(this).focus(),
                F2: () => this.rename(), F5: () => this.manager.copyFrom(this), F6: () => this.rename(),
                F7: () => this.mkdir(), F8: () => this.remove(), Delete: () => this.remove(),
                Escape: () => this.manager.hide(),
            };
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
                event.preventDefault();
                this.selected = new Set(this.visible);
                this.render();
                return;
            }
            const action = actions[event.key];
            if (action && !event.altKey) { event.preventDefault(); event.stopPropagation(); action(); }
        }

        selection() {
            if (this.selected.size) { return this.visible.filter((entry) => this.selected.has(entry)); }
            const entry = this.visible[this.cursor];
            return entry ? [entry] : [];
        }

        async up() { await this.manager.guarded(this, () => this.navigateUp()); }
        async refresh() { await this.manager.guarded(this, () => this.load()); }
        async open() {
            const entry = this.visible[this.cursor];
            if (!entry) { return; }
            const kind = entry.type === 'l' ? entry.target : entry.type;
            if (kind === 'd') { await this.manager.guarded(this, () => this.enter(entry)); }
        }

        async mkdir() {
            if (!this.canWrite()) { return; }
            const name = await this.manager.askName(gettext('New folder'), '');
            if (!name) { return; }
            await this.manager.guarded(this, async () => { await this.createFolder(name); await this.load(); });
        }

        async rename() {
            const entry = this.visible[this.cursor];
            if (!entry || !this.canWrite()) { return; }
            const name = await this.manager.askName(gettext('Rename'), entry.name);
            if (!name || name === entry.name) { return; }
            await this.manager.guarded(this, async () => { await this.renameEntry(entry, name); await this.load(); });
        }

        async remove() {
            const entries = this.selection();
            if (!entries.length || !this.canWrite()) { return; }
            const text = entries.length === 1 ? entries[0].name : qfmFormat(gettext('{0} items'), entries.length);
            const answer = await this.manager.dialog({ title: gettext('Delete'), text: qfmFormat(gettext('Delete {0}? This cannot be undone.'), text),
                buttons: [['delete', gettext('Delete')], ['cancel', gettext('Cancel')]] });
            if (answer.choice !== 'delete') { return; }
            await this.manager.guarded(this, async () => {
                let failure = null;
                for (const entry of entries) {
                    try { await this.removeEntry(entry); } catch (error) { failure = error; }
                }
                await this.load();
                if (failure) { throw failure; }
            });
        }
    }

    class QfmGuestPane extends QfmPane {
        constructor(manager) {
            super(manager, 'guest', gettext('Guest'));
            this.path = null;
            this.ticket = 0;
        }

        canWrite() {
            if (this.manager.rights === 'rw') { return true; }
            this.manager.report(qfmErrorText(new QfmError('ERIGHTS')));
            return false;
        }

        // Every action names a file by this.path and a row, so those two change together
        // and only on success. The newest request wins over one still on its way.
        async load(target = this.path) {
            const ticket = this.ticket += 1;
            const link = await this.manager.ensureLink();
            const listing = await link.list(target === null ? (link.info.home || '/') : target);
            if (ticket !== this.ticket) { return; }
            const sameFolder = listing.path === this.path;
            this.path = listing.path;
            this.location.textContent = this.path;
            this.location.title = this.path;
            this.setEntries(listing.entries, sameFolder);
            this.showMessage(listing.truncated ? gettext('This folder is too large. Only part of it is shown.') : (listing.entries.length ? '' : gettext('This folder is empty.')));
        }

        async navigateUp() { await this.load(qfmParent(this.path || '/')); }
        async enter(entry) { await this.load(qfmJoin(this.path, entry.name)); }
        async createFolder(name) { await this.manager.link.call('mkdir', { path: qfmJoin(this.path, name), parents: false }); }
        async renameEntry(entry, name) { await this.manager.link.call('rename', { from: qfmJoin(this.path, entry.name), to: qfmJoin(this.path, name) }); }
        async removeEntry(entry) { await this.manager.link.call('delete', { path: qfmJoin(this.path, entry.name), recursive: entry.type === 'd' }); }
    }

    class QfmLocalPane extends QfmPane {
        constructor(manager) {
            super(manager, 'local', gettext('This computer'));
            const win = manager.win;
            this.source = null;
            this.ticket = 0;
            this.pickerAvailable = typeof win.showDirectoryPicker === 'function';
            if (this.pickerAvailable) {
                this.pickButton = this.toolButton(gettext('Choose folder…'), () => this.pick());
            } else {
                this.useStaged();
            }
        }

        useStaged() {
            this.pickerAvailable = false;
            this.extra.replaceChildren();
            this.source = new QfmStagedFiles();
            this.filesInput = this.input(false);
            this.folderInput = this.input(true);
            this.pickButton = this.toolButton(gettext('Add files…'), () => this.filesInput.click());
            this.folderButton = this.toolButton(gettext('Add folder…'), () => this.folderInput.click());
        }

        toolButton(text, action) {
            const item = this.create('button', 'qfm-text', text);
            item.type = 'button';
            item.addEventListener('click', action);
            this.extra.append(item);
            return item;
        }

        input(folder) {
            const input = this.create('input');
            input.type = 'file';
            input.multiple = true;
            input.style.display = 'none';
            if (folder) { input.webkitdirectory = true; }
            input.addEventListener('change', () => {
                for (const file of input.files) { this.source.add(file.webkitRelativePath || file.name, file); }
                input.value = '';
                this.manager.guarded(this, () => this.load());
            });
            this.root.append(input);
            return input;
        }

        canWrite() {
            if (this.source) { return true; }
            this.manager.report(gettext('Choose a folder first.'));
            return false;
        }

        async pick() {
            try {
                // Chrome refuses Downloads itself but not a folder in it, which this dialog can make.
                const root = await this.manager.win.showDirectoryPicker({ mode: 'readwrite', id: 'qsm-files', startIn: 'downloads' });
                this.source = new QfmLocalFolders(root);
                await this.manager.guarded(this, () => this.load());
                this.manager.setPaneMode();
            } catch (error) {
                if (error && error.name === 'AbortError') { return; }
                // A frame or policy can forbid the picker: files can still go through Downloads.
                this.useStaged();
                this.manager.setPaneMode();
                await this.manager.guarded(this, () => this.load());
                this.manager.report(gettext('This browser refused access to a local folder here, so files from the guest go to the Downloads folder.'));
            }
        }

        async load() {
            if (!this.source) {
                this.location.textContent = '';
                this.setEntries([]);
                this.showMessage(gettext([
                    'Choose a folder on this computer to browse it and to receive files.',
                    'Chrome and Edge do not let a web page open your home folder, the Desktop, Documents or Downloads folder itself, or a system folder, and say that it contains system files. A folder inside Desktop, Documents or Downloads is allowed.',
                    'The dialog opens in Downloads. Make a new folder there, for example QSM, and choose it.',
                ].join('\n\n')));
                return;
            }
            await this.move(undefined, () => {});
        }

        // As in the guest pane, the folder moves only once it has been read.
        async move(directory, commit) {
            const ticket = this.ticket += 1;
            const source = this.source;
            const entries = await (directory === undefined ? source.readdir() : source.readdir(directory));
            if (ticket !== this.ticket || source !== this.source) { return; }
            commit();
            this.location.textContent = source.location;
            this.setEntries(entries, directory === undefined);
            if (entries.length) { this.showMessage(''); } else if (source.kind === 'staged') {
                this.showMessage(gettext([
                    'Firefox does not let a web page see the folders of this computer. This side lists only what you add here and what you download, and only while this page is open.',
                    'Add files… opens the file dialog of the browser, and the page may then read the files you choose. Add folder… does the same for a whole folder with its subfolders. Empty subfolders are left out, but not when you drag the folder here instead.',
                    'Nothing is copied until you press Copy to guest →. Each file is read at that moment, and one that has changed in the meantime is not copied.',
                    'Removing an item here only takes it off this list. Nothing is deleted from the disk.',
                    'Files from the guest are saved to your Downloads folder and appear here. For ten minutes they can go back to the guest from here.',
                    'Chrome and Edge show a real folder on this side.',
                ].join('\n\n')));
            } else { this.showMessage(gettext('This folder is empty.')); }
        }

        async navigateUp() {
            if (this.source && !this.source.atRoot) {
                const source = this.source;
                await this.move(source.parentDirectory(), () => source.up());
            }
        }

        async enter(entry) {
            const source = this.source;
            await this.move(source.directoryOf(entry), () => source.enter(entry));
        }
        async createFolder(name) {
            if (!qfmSafeName(name)) { throw new QfmError('EINVAL', gettext('This is not a valid name')); }
            if (this.source.kind !== 'folder') { throw new QfmError('ELOCAL', gettext('Choose a folder first.')); }
            await this.source.mkdir(name);
        }

        async renameEntry(entry, name) {
            if (this.source.kind !== 'folder') { throw new QfmError('ELOCAL', gettext('Choose a folder first.')); }
            await this.source.rename(entry, name);
        }

        async removeEntry(entry) { await this.source.remove(entry); }

        delivered(saved) {
            if (!this.source || this.source.kind !== 'staged') { return; }
            this.source.delivered(saved);
            if (this.source.atRoot) { this.manager.guarded(this, () => this.load()); }
        }
    }

    const QFM_STYLE = `
        .qfm{position:fixed;inset:0;z-index:25;display:none;flex-direction:column;background:#0f172a;color:#e2e8f0;font:13px/1.35 system-ui,sans-serif}
        .qfm *{box-sizing:border-box}
        .qfm button{cursor:pointer;background:#1e293b;color:#e2e8f0;border:1px solid #475569;border-radius:4px;padding:3px 8px;font:inherit}
        .qfm button:hover{background:#334155}
        .qfm button:focus-visible,.qfm-list:focus-visible{outline:2px solid #38bdf8;outline-offset:-2px}
        .qfm button[disabled]{opacity:.45;cursor:default}
        .qfm-bar{display:flex;align-items:center;gap:10px;padding:6px 10px;background:#1e293b;border-bottom:1px solid #334155}
        .qfm-bar strong{font-size:14px}
        .qfm-state{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#cbd5e1}
        .qfm-panes{flex:1;min-height:0;display:flex;gap:6px;padding:6px}
        .qfm-pane{flex:1 1 0;min-width:0;display:flex;flex-direction:column;border:1px solid #334155;border-radius:6px;background:#111c30}
        .qfm-pane.qfm-active{border-color:#38bdf8}
        .qfm-pane.qfm-drop{background:#172a45;border-color:#7dd3fc}
        .qfm-heading{display:flex;gap:8px;align-items:baseline;padding:5px 8px;min-width:0}
        .qfm-location{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;text-align:left;color:#94a3b8;unicode-bidi:plaintext}
        .qfm-tools{display:flex;flex-wrap:wrap;align-items:center;gap:4px;padding:0 8px 6px}
        .qfm-extra{display:inline-flex;gap:4px}
        .qfm-copy{margin-left:auto;background:#0369a1!important;border-color:#0284c7!important;color:#fff!important;font-weight:600}
        .qfm-columns,.qfm-row{display:grid;grid-template-columns:22px minmax(0,1fr) 84px 160px;align-items:center;gap:4px;padding:0 8px}
        .qfm-columns{border-top:1px solid #334155;border-bottom:1px solid #334155;background:#16213a}
        .qfm-col{background:none!important;border:0!important;padding:3px 0!important;text-align:left;color:#94a3b8!important}
        .qfm-col-name{grid-column:1/3}
        .qfm-list{flex:1;min-height:0;overflow:auto;position:relative}
        .qfm-spacer{position:relative;min-height:1px}
        .qfm-rows{position:absolute;top:0;left:0;right:0;will-change:transform}
        .qfm-row{height:${QFM_ROW}px;white-space:nowrap;cursor:default;user-select:none}
        .qfm-row:hover{background:#1a2740}
        .qfm-selected{background:#164e63!important}
        .qfm-list:focus .qfm-cursor{box-shadow:inset 0 0 0 1px #38bdf8}
        .qfm-name{overflow:hidden;text-overflow:ellipsis}
        .qfm-col-size,.qfm-col-mtime{color:#94a3b8;overflow:hidden;text-overflow:ellipsis}
        .qfm-message{display:none;padding:16px;color:#94a3b8;line-height:1.5;white-space:pre-line;max-width:680px}
        .qfm-jobs{max-height:30%;overflow:auto;border-top:1px solid #334155;background:#0b1324}
        .qfm-job{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 10px;padding:6px 10px;border-bottom:1px solid #1e293b}
        .qfm-job-text{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        .qfm-job progress{grid-column:1;width:100%;height:6px}
        .qfm-job-errors{grid-column:1/3;color:#fca5a5;white-space:pre-wrap;word-break:break-word;max-height:9em;overflow:auto}
        .qfm-dialog-back{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.55);z-index:2}
        .qfm-dialog{width:min(460px,calc(100% - 32px));padding:16px;background:#1e293b;border:1px solid #475569;border-radius:8px}
        .qfm-dialog h3{margin:0 0 8px;font-size:15px}
        .qfm-dialog p{margin:0 0 10px;word-break:break-word}
        .qfm-dialog input[type=text]{width:100%;padding:5px;background:#0f172a;color:#e2e8f0;border:1px solid #475569;border-radius:4px;font:inherit;margin-bottom:10px}
        .qfm-dialog label{display:block;margin-bottom:10px}
        .qfm-dialog-buttons{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end}
        .qfm-report{padding:5px 10px;background:#7f1d1d;color:#fee2e2;display:none;align-items:center;gap:10px}
        .qfm-report span{flex:1;word-break:break-word}
        @media (max-width:760px){
            .qfm-panes{flex-direction:column}
            .qfm-pane{min-height:0}
            .qfm-columns,.qfm-row{grid-template-columns:22px minmax(0,1fr) 70px}
            .qfm-col-mtime{display:none}
        }`;

    class QsmFileManager {
        constructor({ win, doc, getToken, offer, createSocket, onVisible, onProgress, demand = null }) {
            this.win = win;
            this.doc = doc;
            this.getToken = getToken;
            this.offerRequest = offer;
            this.createSocket = createSocket;
            this.onVisible = onVisible;
            this.onProgress = onProgress;
            this.demand = demand;
            this.collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
            this.link = null;
            this.linking = null;
            this.rights = 'r';
            this.jobs = [];
            this.running = false;
            this.visible = false;
            this.dragging = null;
            this.attention = false;
            this.idleTimer = null;
            this.drawPending = false;
            this.lastDraw = 0;
            this.build();
        }

        build() {
            const doc = this.doc;
            const style = doc.createElement('style');
            style.textContent = QFM_STYLE;
            doc.head.append(style);
            this.root = doc.createElement('div');
            this.root.className = 'qfm';
            this.root.setAttribute('role', 'dialog');
            this.root.setAttribute('aria-label', gettext('Files'));
            const bar = doc.createElement('div');
            bar.className = 'qfm-bar';
            const title = doc.createElement('strong');
            title.textContent = gettext('Files');
            this.state = doc.createElement('span');
            this.state.className = 'qfm-state';
            const close = doc.createElement('button');
            close.type = 'button';
            close.textContent = gettext('Back to console');
            close.title = gettext('Transfers continue in the background and give way to the console.');
            close.addEventListener('click', () => this.hide());
            bar.append(title, this.state, close);
            this.reportBar = doc.createElement('div');
            this.reportBar.className = 'qfm-report';
            this.reportText = doc.createElement('span');
            const dismiss = doc.createElement('button');
            dismiss.type = 'button';
            dismiss.textContent = gettext('Dismiss');
            dismiss.addEventListener('click', () => { this.reportBar.style.display = 'none'; });
            this.reportBar.append(this.reportText, dismiss);
            const panes = doc.createElement('div');
            panes.className = 'qfm-panes';
            this.local = new QfmLocalPane(this);
            this.guest = new QfmGuestPane(this);
            panes.append(this.local.root, this.guest.root);
            this.jobList = doc.createElement('div');
            this.jobList.className = 'qfm-jobs';
            this.root.append(bar, this.reportBar, panes, this.jobList);
            doc.body.append(this.root);
            this.active = this.local;
            this.setPaneMode();
        }

        other(pane) { return pane === this.local ? this.guest : this.local; }

        syncActive() {
            this.local.root.classList.toggle('qfm-active', this.active === this.local);
            this.guest.root.classList.toggle('qfm-active', this.active === this.guest);
        }

        setPaneMode() {
            const source = this.local.source;
            const browsing = source && source.kind === 'folder';
            this.local.mkdirButton.disabled = !browsing;
            this.local.renameButton.disabled = !browsing;
            this.guest.copyButton.textContent = browsing || this.local.pickerAvailable ? gettext('← Copy here') : gettext('← Download');
            const write = this.rights === 'rw';
            this.guest.mkdirButton.disabled = !write;
            this.guest.renameButton.disabled = !write;
            this.guest.deleteButton.disabled = !write;
            this.local.copyButton.disabled = !write;
        }

        isVisible() { return this.visible; }

        show() {
            if (this.visible) { return; }
            this.visible = true;
            this.root.style.display = 'flex';
            this.onVisible(true);
            if (this.idleTimer !== null) { this.win.clearTimeout(this.idleTimer); this.idleTimer = null; }
            if (this.link) { this.link.rate.target = QfmRate.TARGET_FOREGROUND; }
            this.local.load().catch((error) => this.report(qfmErrorText(error)));
            this.guarded(this.guest, () => this.guest.load());
            this.active.focus();
            this.syncActive();
        }

        hide() {
            if (!this.visible) { return; }
            this.visible = false;
            this.root.style.display = 'none';
            if (this.link) { this.link.rate.target = QfmRate.TARGET_BACKGROUND; }
            this.onVisible(false);
            this.scheduleIdle();
        }

        toggle() { if (this.visible) { this.hide(); } else { this.show(); } }

        // With the manager closed and nothing queued, its peer process has no reason to live.
        scheduleIdle() {
            if (this.idleTimer !== null) { this.win.clearTimeout(this.idleTimer); }
            this.idleTimer = this.win.setTimeout(() => {
                this.idleTimer = null;
                if (!this.visible && !this.running && this.link) { this.link.close(); }
            }, 60000);
        }

        async ensureLink() {
            if (this.link && !this.link.closed) { return this.link; }
            if (this.linking) { return this.linking; }
            const token = this.getToken();
            if (!token) { throw new QfmError('ELINK', gettext('This server does not offer files yet. Reopen the console after updating QSM.')); }
            const link = new QfmLink({
                createSocket: this.createSocket,
                offer: async () => {
                    const answer = await this.offerRequest(token);
                    if (answer.rights === 'r' || answer.rights === 'rw') { this.rights = answer.rights; }
                    return answer;
                },
                timers: {
                    now: () => this.win.performance.now(),
                    setTimeout: (callback, delay) => this.win.setTimeout(callback, delay),
                    clearTimeout: (timer) => this.win.clearTimeout(timer),
                    setInterval: (callback, delay) => this.win.setInterval(callback, delay),
                    clearInterval: (timer) => this.win.clearInterval(timer),
                },
                isHidden: () => this.doc.hidden,
                onEnd: () => { this.draw(); },
                demand: this.demand,
            });
            link.rate.target = this.visible ? QfmRate.TARGET_FOREGROUND : QfmRate.TARGET_BACKGROUND;
            this.linking = link.open().then((info) => {
                this.link = link;
                if (info.rights === 'r' || info.rights === 'rw') { this.rights = info.rights; }
                this.setPaneMode();
                return link;
            }).catch((error) => {
                link.end('ELINK');
                throw error;
            }).finally(() => { this.linking = null; });
            return this.linking;
        }

        openFailure(error) {
            const code = error && error.code;
            const status = error && error.status;
            if (code === 'ENOHELPER') {
                return gettext('The file service is not running in the guest. Log in to the guest\'s desktop. In a VM, the QSM Desktop Agent must be up to date and enabled with qsm-desktop-agent-setup USER.');
            }
            if (status === 403) {
                return gettext('Your PVE role lacks VM.GuestAgent.FileRead on this guest, which browsing its files needs.');
            }
            if (status === 404 || status === 405) { return gettext('The QSM signalling service on this node does not offer files. Update QSM on the node.'); }
            if (status) { return gettext('Files could not start. Log in to the guest first. If you are logged in, the guest needs an up-to-date QSM agent: QSM Desktop Agent in a VM, or qsm-console-guest in a container.'); }
            return qfmErrorText(error);
        }

        async guarded(pane, action) {
            try {
                pane.showMessage('');
                await action();
            } catch (error) {
                const text = this.openFailure(error);
                // Over a list that is still shown, the message would sit out of sight.
                if (pane.entries.length) { this.report(text); } else { pane.showMessage(text); }
            }
        }

        report(text) {
            this.reportText.textContent = text;
            this.reportBar.style.display = 'flex';
        }

        dialog({ title, text, input = null, buttons, checkbox = null }) {
            const doc = this.doc;
            return new Promise((resolve) => {
                const back = doc.createElement('div');
                back.className = 'qfm-dialog-back';
                const box = doc.createElement('div');
                box.className = 'qfm-dialog';
                box.setAttribute('role', 'alertdialog');
                const heading = doc.createElement('h3');
                heading.textContent = title;
                box.append(heading);
                if (text) {
                    const paragraph = doc.createElement('p');
                    paragraph.textContent = text;
                    box.append(paragraph);
                }
                let field = null;
                if (input !== null) {
                    field = doc.createElement('input');
                    field.type = 'text';
                    field.value = input;
                    box.append(field);
                }
                let tick = null;
                if (checkbox) {
                    const label = doc.createElement('label');
                    tick = doc.createElement('input');
                    tick.type = 'checkbox';
                    label.append(tick, ` ${checkbox}`);
                    box.append(label);
                }
                const row = doc.createElement('div');
                row.className = 'qfm-dialog-buttons';
                const finish = (choice) => {
                    back.remove();
                    (this.active || this.local).focus();
                    resolve({ choice, value: field ? field.value : null, all: tick ? tick.checked : false });
                };
                for (const [choice, label] of buttons) {
                    const item = doc.createElement('button');
                    item.type = 'button';
                    item.textContent = label;
                    item.addEventListener('click', () => finish(choice));
                    row.append(item);
                }
                box.append(row);
                box.addEventListener('keydown', (event) => {
                    event.stopPropagation();
                    if (event.key === 'Escape') { finish('cancel'); }
                    if (event.key === 'Enter' && field) { finish(buttons[0][0]); }
                });
                back.append(box);
                this.root.append(back);
                if (field) { field.focus(); field.select(); } else { row.firstChild.focus(); }
            });
        }

        async askName(title, initial) {
            const answer = await this.dialog({ title, input: initial,
                buttons: [['ok', gettext('OK')], ['cancel', gettext('Cancel')]] });
            if (answer.choice !== 'ok') { return null; }
            const name = answer.value;
            if (!qfmSafeName(name)) {
                this.report(gettext('This is not a valid name.'));
                return null;
            }
            return name;
        }

        transfers() {
            return new QfmTransfers({
                link: () => this.link,
                delivered: (saved) => this.local.delivered(saved),
                now: () => this.win.performance.now(),
                sleep: (ms) => new Promise((resolve) => this.win.setTimeout(resolve, ms)),
                ask: async (path) => {
                    // The user may be typing in the console: wait to be opened, do not jump.
                    this.attention = true;
                    this.drawNow();
                    while (!this.visible) { await new Promise((resolve) => this.win.setTimeout(resolve, 300)); }
                    this.attention = false;
                    const answer = await this.dialog({
                        title: gettext('It already exists'),
                        text: path,
                        checkbox: gettext('Do the same for the rest of this copy'),
                        buttons: [['replace', gettext('Replace')], ['rename', gettext('Keep both')],
                            ['skip', gettext('Skip')], ['cancel', gettext('Cancel the copy')]],
                    });
                    return { choice: answer.choice, all: answer.all };
                },
                notify: () => this.draw(),
                spool: (name) => new QfmSpool(this.win, name).open(),
                relink: () => this.ensureLink(),
            });
        }

        copyFrom(pane) {
            const entries = pane.selection();
            if (!entries.length) { return; }
            if (pane === this.local) {
                if (!this.local.source) { this.report(gettext('Choose a folder first.')); return; }
                if (this.rights !== 'rw') { this.report(qfmErrorText(new QfmError('ERIGHTS'))); return; }
                if (this.guest.path === null) { this.report(qfmErrorText(new QfmError('ELINK'))); return; }
                this.enqueueUpload(this.local.source, entries, this.guest.path);
            } else {
                this.enqueueDownload(entries.map((entry) => ({ entry, path: qfmJoin(this.guest.path, entry.name) })));
            }
        }

        enqueueUpload(source, entries, destination) {
            const label = entries.length === 1 ? entries[0].name : qfmFormat(gettext('{0} items'), entries.length);
            const job = new QfmJob(qfmFormat(gettext('{0} to the guest'), label), 'up');
            job.run = (transfers) => transfers.upload(job, source, entries, destination);
            job.after = () => this.guarded(this.guest, () => this.guest.load());
            this.enqueue(job);
        }

        enqueueDownload(items) {
            const label = items.length === 1 ? items[0].entry.name : qfmFormat(gettext('{0} items'), items.length);
            const job = new QfmJob(qfmFormat(gettext('{0} from the guest'), label), 'down');
            const local = this.local.source;
            if (local && local.kind === 'folder') {
                // The folder of this moment: the pane may have moved on by the time it runs.
                const target = { handle: local.handle };
                job.run = (transfers) => transfers.downloadToFolders(job, items, target);
                job.after = () => this.guarded(this.local, () => this.local.load());
            } else if (this.local.pickerAvailable) {
                this.report(gettext('Choose a folder first.'));
                return;
            } else {
                const name = items.length === 1 ? `${items[0].entry.name}.tar` : 'qsm-files.tar';
                job.run = (transfers) => transfers.downloadToSpool(job, items, name);
            }
            this.enqueue(job);
        }

        acceptsDrop(pane, event) {
            if (this.dragging) { return this.dragging !== pane; }
            const types = event.dataTransfer && event.dataTransfer.types;
            const files = types && Array.from(types).includes('Files');
            return Boolean(files) && (pane === this.guest || (this.local.source && this.local.source.kind === 'staged'));
        }

        async drop(pane, event) {
            if (this.dragging) {
                const source = this.dragging;
                this.dragging = null;
                this.copyFrom(source);
                return;
            }
            const items = Array.from(event.dataTransfer.items || []);
            const roots = items.map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null)).filter(Boolean);
            const staged = pane === this.local ? this.local.source : new QfmStagedFiles();
            try {
                for (const root of roots) { await qfmReadDroppedEntry(root, '', staged); }
            } catch (_error) {
                this.report(qfmErrorText(new QfmError('ELOCAL')));
                return;
            }
            if (pane === this.local) {
                await this.guarded(this.local, () => this.local.load());
            } else if (this.rights === 'rw' && this.guest.path !== null) {
                this.enqueueUpload(staged, await staged.readdir(), this.guest.path);
            } else {
                this.report(qfmErrorText(new QfmError('ERIGHTS')));
            }
        }

        enqueue(job) {
            this.jobs.push(job);
            this.draw();
            this.runJobs();
        }

        async runJobs() {
            if (this.running) { return; }
            this.running = true;
            if (this.idleTimer !== null) { this.win.clearTimeout(this.idleTimer); this.idleTimer = null; }
            for (;;) {
                const job = this.jobs.find((item) => item.state === 'queued');
                if (!job) { break; }
                job.state = 'preparing';
                job.started = this.win.performance.now();
                this.draw();
                try {
                    if (job.cancelled) { throw new QfmError('ECANCELED'); }
                    await this.ensureLink();
                    await job.run(this.transfers());
                    job.state = job.cancelled ? 'cancelled' : job.errors.length ? 'failed' : 'done';
                } catch (error) {
                    if (error && error.code === 'ECANCELED') { job.state = 'cancelled'; } else {
                        job.errors.push({ path: '', error });
                        job.state = 'failed';
                    }
                }
                this.draw();
                if (job.after) { job.after(); }
            }
            this.running = false;
            if (!this.visible) { this.scheduleIdle(); }
            this.draw();
        }

        draw() {
            if (this.drawPending) { return; }
            const wait = Math.max(0, 200 - (this.win.performance.now() - this.lastDraw));
            this.drawPending = true;
            this.win.setTimeout(() => {
                this.drawPending = false;
                this.lastDraw = this.win.performance.now();
                this.drawNow();
            }, wait);
        }

        jobText(job) {
            const counts = job.files ? qfmFormat(gettext('{0} of {1} files'), job.filesDone, job.files) : '';
            const bytes = `${qfmSize(job.done)} / ${qfmSize(job.total)}`;
            if (job.state === 'queued') { return `${job.label} — ${gettext('Waiting')}`; }
            if (job.state === 'preparing') { return `${job.label} — ${gettext('Preparing…')} ${counts}`; }
            if (job.state === 'locked') { return `${job.label} — ${gettext('Paused: the guest screen is locked. It continues after you unlock it.')}`; }
            if (job.state === 'running') {
                const speed = job.speed();
                const rest = speed > 0 ? Math.ceil((job.total - job.done) / speed) : null;
                const eta = rest === null ? '' : ` · ${Math.floor(rest / 60)}:${String(rest % 60).padStart(2, '0')}`;
                return `${job.label} — ${counts} · ${bytes} · ${qfmSize(speed)}/s${eta}`;
            }
            const skipped = job.skipped ? ` · ${qfmFormat(gettext('{0} skipped'), job.skipped)}` : '';
            const end = { done: gettext('Done'), cancelled: gettext('Cancelled'),
                failed: qfmFormat(gettext('{0} failed'), job.errors.length) }[job.state];
            return `${job.label} — ${end} · ${counts} · ${qfmSize(job.done)}${skipped}`;
        }

        jobView(job) {
            if (job.view) { return job.view; }
            const doc = this.doc;
            const view = { row: doc.createElement('div'), text: doc.createElement('div'), action: doc.createElement('button'),
                bar: doc.createElement('progress'), errors: doc.createElement('div'), errorCount: 0 };
            view.row.className = 'qfm-job';
            view.text.className = 'qfm-job-text';
            view.errors.className = 'qfm-job-errors';
            view.action.type = 'button';
            // Rows are updated in place: a click needs the same button at press and release.
            view.action.addEventListener('click', () => {
                if (this.jobActive(job)) {
                    job.cancel();
                    if (job.state === 'queued') { job.state = 'cancelled'; }
                } else {
                    this.jobs.splice(this.jobs.indexOf(job), 1);
                }
                this.drawNow();
            });
            view.row.append(view.text, view.action, view.bar, view.errors);
            job.view = view;
            return view;
        }

        jobActive(job) { return ['queued', 'preparing', 'running', 'locked'].includes(job.state); }

        drawNow() {
            const rows = [];
            for (const job of this.jobs.slice(-20)) {
                const view = this.jobView(job);
                const active = this.jobActive(job);
                view.text.textContent = this.jobText(job);
                view.text.title = view.text.textContent;
                view.action.textContent = active ? gettext('Cancel') : gettext('Remove');
                view.bar.style.display = active && job.total ? 'block' : 'none';
                if (active && job.total) {
                    view.bar.max = job.total;
                    view.bar.value = Math.min(job.done, job.total);
                }
                if (view.errorCount !== job.errors.length) {
                    view.errorCount = job.errors.length;
                    view.errors.textContent = job.errors.slice(0, 50).map(({ path, error }) => (path ? `${path}: ${qfmErrorText(error)}` : this.openFailure(error))).join('\n');
                }
                view.errors.style.display = job.errors.length ? 'block' : 'none';
                rows.push(view.row);
            }
            if (rows.length !== this.jobList.children.length || rows.some((row, index) => this.jobList.children[index] !== row)) {
                this.jobList.replaceChildren(...rows);
            }
            const running = this.jobs.filter((job) => ['preparing', 'running', 'locked'].includes(job.state));
            if (running.length) {
                const total = running.reduce((sum, job) => sum + job.total, 0);
                const done = running.reduce((sum, job) => sum + job.done, 0);
                this.state.textContent = this.jobText(running[0]);
                this.onProgress(this.attention ? '!' : (total ? Math.min(99, Math.floor(done * 100 / total)) : 0));
            } else {
                this.state.textContent = this.link && !this.link.closed && this.rights === 'r' ? gettext('Read only: your PVE role lacks VM.GuestAgent.FileWrite on this guest.') : '';
                this.onProgress(null);
            }
        }

        destroy() {
            for (const job of this.jobs) { job.cancelled = true; }
            this.visible = true; // Ends a question that was waiting to be seen.
            if (this.idleTimer !== null) { this.win.clearTimeout(this.idleTimer); }
            if (this.link) { this.link.close(); }
            this.root.remove();
        }
    }
    // QSM_FILES_END

    const defaultConsoleSettings = () => ({
        ...Object.fromEntries(Object.entries(CONSOLE_SETTING_SCHEMA).map(
            ([name, definition]) => [name, definition.defaultValue],
        )),
        // The direct Console is a remote-desktop window, so the guest gets
        // received keys by default. The explicit client-first mode remains
        // available for users who want browser/OS shortcuts to win.
        keyboardPriority: KEYBOARD_PRIORITY.guest,
        keyInputMode: KEY_INPUT_MODE.typing,
        mouseInputMode: MOUSE_INPUT_MODE.exact,
        hostShortcuts: defaultShortcutRules(),
        clientShortcuts: defaultShortcutRules(true),
        modifierShortcutDefaultsVersion: 1,
        exactUi: false,
        exactUiPreferenceSet: false,
    });
    // Disposable, bounded uplink samples identify frames the browser painted.
    const createVideoPresentationFeedback = channelForFeedback => {
        let previous = null, sentAt = -Infinity;
        return (rtp, now) => {
            if (!Number.isInteger(rtp) || rtp < 0 || rtp > 0xffffffff ||
                !Number.isFinite(now) || rtp === previous || now - sentAt < 100) { return; }
            const channel = channelForFeedback();
            if (!channel || channel.readyState !== 'open' || channel.bufferedAmount > 1024) { return; }
            try {
                channel.send(JSON.stringify({ op: 'qsm_video_presented', rtp }));
                previous = rtp; sentAt = now;
            } catch (_error) { /* A newer painted frame replaces this sample. */ }
        };
    };

    // Optional encoded-frame arrival feedback. Frames pass through unchanged;
    // this does not acknowledge presentation or authorize an exact-UI paint.
    const createVideoReceiveFeedback = (scope, channelForFeedback, onDirect = null) => {
        let showing = false;
        let permitted = false, closed = false, failed = false;
        let receiver = null, worker = null, transform = null, timer = null, url = null;
        let previous = null, reports = 0;
        const end = () => {
            if (timer !== null) { scope.clearTimeout(timer); timer = null; }
            if (url !== null) { scope.URL.revokeObjectURL(url); url = null; }
            if (receiver && transform && receiver.transform === transform) {
                try { receiver.transform = null; } catch (_error) { /* Closing receiver. */ }
            }
            if (worker) { worker.terminate(); worker = null; }
            transform = null; previous = null;
        };
        const disable = () => { failed = true; end(); };
        const start = () => {
            if (closed || failed || worker || !receiver ||
                typeof scope.Worker !== 'function' || typeof scope.RTCRtpScriptTransform !== 'function' ||
                !('transform' in receiver) || receiver.transform !== null) { return; }
            const source = `
                let pending = false, unsupported = false, permitted = false;
                let decoder = null, keyed = false, directOff = typeof VideoDecoder !== 'function', showing = false, sps = '';
                const started = new Map();
                self.onmessage = event => {
                    if (event.data === 'next') pending = false;
                    else if (event.data === 'allow') permitted = true;
                    else if (event.data === 'deny') permitted = false;
                    else if (event.data === 'show') showing = true;
                    else if (event.data === 'hide') showing = false;
                };
                // An optional presentation path: a lasting failure ends it and leaves
                // the unchanged RTP stream decoding in the browser.
                const directFail = reason => {
                    if (directOff) return;
                    directOff = true;
                    try { decoder?.close(); } catch (_error) { /* Already closed. */ }
                    decoder = null; self.postMessage({ op: 'direct-failed', reason: String(reason?.message ?? reason).slice(0, 120) });
                };
                // A picture lost ahead of this decoder is a decode error at one stream
                // position, not a lasting fault: resume at the next key frame. Errors that
                // keep returning show the decoder cannot use this stream at all.
                let restarts = [];
                const directDecodeError = error => {
                    const now = performance.now();
                    restarts = restarts.filter(at => now - at < 60000);
                    if ((error?.name !== 'EncodingError' && error?.name !== 'OperationError') || restarts.length >= 3) { directFail(error); return; }
                    restarts.push(now);
                    try { decoder?.close(); } catch (_error) { /* A decode error already closed it. */ }
                    decoder = null; keyed = false; sps = ''; started.clear();
                    self.postMessage({ op: 'direct-restart', reason: String(error?.message ?? error).slice(0, 120) });
                };
                const hex = n => n.toString(16).padStart(2, '0');
                // The HEVC codec string from an SPS payload, per ISO/IEC 14496-15 annex E.
                const hevcCodec = payload => {
                    const raw = [];
                    for (let i = 0; i < payload.length && raw.length < 13; i += 1) {
                        // Zero runs are common in these fields, so emulation prevention must go first.
                        if (i >= 2 && payload[i] === 3 && payload[i - 1] === 0 && payload[i - 2] === 0) continue;
                        raw.push(payload[i]);
                    }
                    if (raw.length < 13) return null;
                    let compatibility = 0;
                    for (let bit = 0; bit < 32; bit += 1) {
                        if (raw[2 + (bit >> 3)] & (0x80 >> (bit & 7))) compatibility += 2 ** bit;
                    }
                    const constraints = raw.slice(6, 12);
                    while (constraints.length && constraints.at(-1) === 0) constraints.pop();
                    return ['hvc1', ['', 'A', 'B', 'C'][raw[1] >> 6] + (raw[1] & 31), compatibility.toString(16),
                        ((raw[1] & 32) ? 'H' : 'L') + raw[12], ...constraints.map(n => hex(n).toUpperCase())].join('.');
                };
                // A key frame's codec string and SPS identity, in Annex B for both codecs.
                // A base-layer HEVC NAL header is even, so it cannot match the H.264 test.
                const findSps = bytes => {
                    for (let i = 0; i + 7 < bytes.length; i += 1) {
                        if (bytes[i] !== 0 || bytes[i + 1] !== 0 || bytes[i + 2] !== 1) continue;
                        const hevc = bytes[i + 3] === 0x42 && bytes[i + 4] === 1;
                        if (!hevc && (bytes[i + 3] & 31) !== 7) continue;
                        const from = i + (hevc ? 5 : 4);
                        let end = from;
                        while (end + 2 < bytes.length && !(bytes[end] === 0 && bytes[end + 1] === 0 && bytes[end + 2] <= 1)) { end += 1; }
                        const payload = bytes.subarray(from, end);
                        const codec = hevc ? hevcCodec(payload) : (payload.length < 3 ? null : 'avc1.' + hex(payload[0]) + hex(payload[1]) + hex(payload[2]));
                        return codec && { codec, text: String.fromCharCode(...payload) };
                    }
                    return null;
                };
                const decodeDirect = frame => {
                    const key = frame.type === 'key';
                    if (directOff || (!keyed && !key)) return;
                    const bytes = new Uint8Array(frame.data);
                    if (key) {
                        const found = findSps(bytes);
                        if (!found) { directFail('no SPS in key frame'); return; }
                        // A cropped plane's size or profile arrives in-band with its IDR.
                        if (found.text !== sps) {
                            if (!decoder) {
                                decoder = new VideoDecoder({
                                    output(picture) {
                                        const queued = started.get(picture.timestamp);
                                        started.delete(picture.timestamp);
                                        // A decoder still catching up must not show old pictures:
                                        // the browser's parallel path stays live meanwhile.
                                        if (!showing || (queued !== undefined && performance.now() - queued > 200)) { picture.close(); return; }
                                        try { self.postMessage({ op: 'frame', rtp: picture.timestamp, frame: picture }, [picture]); }
                                        catch (error) { picture.close(); directFail(error); }
                                    },
                                    error: directDecodeError,
                                });
                            }
                            decoder.configure({ codec: found.codec, optimizeForLatency: true, hardwareAcceleration: 'no-preference' });
                            sps = found.text;
                        }
                    }
                    // A hardware decoder can take a few hundred ms to start: only a backlog
                    // this long shows it cannot keep up, and old pictures would be useless anyway.
                    if (decoder.decodeQueueSize > 60) { directFail('decode backlog'); return; }
                    const rtp = frame.getMetadata().rtpTimestamp;
                    started.set(rtp, performance.now());
                    if (started.size > 256) { started.delete(started.keys().next().value); }
                    decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: rtp, data: bytes }));
                    keyed = true;
                };
                self.onrtctransform = event => {
                    const t = event.transformer; let last = -Infinity;
                    t.readable.pipeThrough(new TransformStream({ transform(frame, controller) {
                        const now = performance.now();
                        if (permitted && !unsupported && !pending && now - last >= 16) {
                            try {
                                const rtp = frame.getMetadata().rtpTimestamp;
                                if (Number.isInteger(rtp) && rtp >= 0 && rtp <= 0xffffffff) {
                                    pending = true; last = now;
                                    self.postMessage({ op: 'received', rtp });
                                } else if (!unsupported) {
                                    unsupported = true; self.postMessage({ op: 'unsupported' });
                                }
                            } catch (_error) {
                                if (!unsupported) { unsupported = true; self.postMessage({ op: 'unsupported' }); }
                            }
                        }
                        try { decodeDirect(frame); } catch (error) { directFail(error); }
                        controller.enqueue(frame);
                    }})).pipeTo(t.writable).catch(() => self.postMessage({ op: 'failed' }));
                };
                self.postMessage({ op: 'ready' });
            `;
            try {
                url = scope.URL.createObjectURL(new scope.Blob([source], { type: 'application/javascript' }));
                const target = receiver, candidate = new scope.Worker(url);
                worker = candidate;
                timer = scope.setTimeout(() => { if (worker === candidate) { disable(); } }, 1500);
                candidate.onerror = () => { if (worker === candidate) { disable(); } };
                candidate.onmessage = event => {
                    if (closed || worker !== candidate || receiver !== target) { return; }
                    const value = event.data;
                    if (!value || typeof value !== 'object') { return; }
                    if (value.op === 'frame') { onDirect?.(value.frame, value.rtp); return; }
                    if (value.op === 'direct-failed') { onDirect?.(null, value.reason); return; }
                    if (value.op === 'direct-restart') { onDirect?.(null, value.reason, true); return; }
                    if (value.op === 'ready') {
                        if (timer !== null) { scope.clearTimeout(timer); timer = null; }
                        if (url !== null) { scope.URL.revokeObjectURL(url); url = null; }
                    } else if (value.op === 'unsupported') {
                        // Keep forwarding media when optional metadata is
                        // unavailable. Detaching can strand Chromium's pipeline.
                        failed = true; candidate.postMessage('deny');
                    } else if (value.op === 'failed') {
                        disable();
                    } else if (value.op === 'received') {
                        try {
                            const rtp = value.rtp;
                            const channel = channelForFeedback();
                            if (permitted && !failed && transform && Number.isInteger(rtp) && rtp >= 0 && rtp <= 0xffffffff &&
                                rtp !== previous && channel &&
                                channel.readyState === 'open' && channel.bufferedAmount <= 1024) {
                                channel.send(JSON.stringify({ op: 'qsm_video_received', rtp }));
                                previous = rtp; reports += 1;
                            }
                        } catch (_error) { /* Newer receipt replaces a dropped sample. */ }
                        // The worker already bounds frequency and keeps one
                        // outstanding report. IPC delivery can bunch after a
                        // main-thread stall; resampling its clock here would
                        // discard a newer valid receipt. Media never waits here.
                        if (worker === candidate) { candidate.postMessage('next'); }
                    }
                };
                // Chromium bypasses transforms permanently after the receiver's
                // creation task. Attach synchronously, before worker readiness
                // or the server's later permission message; bytes pass unchanged.
                transform = new scope.RTCRtpScriptTransform(candidate);
                receiver.transform = transform;
                candidate.postMessage(permitted ? 'allow' : 'deny');
                candidate.postMessage(showing ? 'show' : 'hide');
            } catch (_error) { disable(); }
        };
        return {
            allow(value) {
                permitted = value === true;
                if (worker && !failed) { worker.postMessage(permitted ? 'allow' : 'deny'); }
            },
            attach(value) { if (receiver !== value) { end(); receiver = value; } start(); },
            show(value) {
                showing = value === true;
                if (worker && !failed) { worker.postMessage(showing ? 'show' : 'hide'); }
            },
            close() { closed = true; end(); },
            get active() { return transform !== null; },
            get reports() { return reports; },
        };
    };

    // Standard receiver target is in milliseconds; the older Chromium hint
    // uses seconds. This is a target: the browser can enforce a larger floor.
    const applyReceiverPlayoutDelay = (receiver, milliseconds) => {
        if (!receiver) { return; }
        if ('jitterBufferTarget' in receiver) {
            try { receiver.jitterBufferTarget = milliseconds; return; }
            catch (_error) { /* Try the older hint if this setter rejects it. */ }
        }
        if ('playoutDelayHint' in receiver) {
            try { receiver.playoutDelayHint = milliseconds / 1000; }
            catch (_error) { /* A receiver can reject an unsupported target. */ }
        }
    };
    const readConsoleSettings = (storage) => {
        const settings = defaultConsoleSettings();
        try {
            if (!storage) { return settings; }
            const stored = JSON.parse(storage.getItem(CONSOLE_SETTINGS_STORAGE_KEY) || '{}');
            if (!stored || typeof stored !== 'object' || Array.isArray(stored)) { return settings; }
            for (const [name, definition] of Object.entries(CONSOLE_SETTING_SCHEMA)) {
                const value = stored[name];
                if (Number.isInteger(value) && value >= definition.minimum && value <= definition.maximum) {
                    settings[name] = value;
                }
            }
            if (typeof stored.exactUi === 'boolean') { settings.exactUi = stored.exactUi; }
            // Earlier versions persisted their false default with unrelated
            // settings. A deliberate selection now has an explicit marker.
            settings.exactUiPreferenceSet = stored.exactUiPreferenceSet === true ||
                (!Object.hasOwn(stored, 'exactUiPreferenceSet') && stored.exactUi === true);
            if (Object.values(KEYBOARD_PRIORITY).includes(stored.keyboardPriority)) {
                settings.keyboardPriority = stored.keyboardPriority;
            }
            if (Object.values(KEY_INPUT_MODE).includes(stored.keyInputMode)) {
                settings.keyInputMode = stored.keyInputMode;
            }
            if (Object.values(MOUSE_INPUT_MODE).includes(stored.mouseInputMode)) {
                settings.mouseInputMode = stored.mouseInputMode;
            }
            for (const name of ['hostShortcuts', 'clientShortcuts']) {
                const rules = normalizeShortcutRules(stored[name]);
                if (rules !== null) {
                    settings[name] = rules;
                    if (stored.modifierShortcutDefaultsVersion !== 1) {
                        const seen = new Set(rules.map(rule => rule.shortcut));
                        settings[name] = [...rules, ...defaultModifierShortcutRules()
                            .filter(rule => !seen.has(rule.shortcut))].slice(0, 128);
                    }
                }
            }
        } catch (_error) { /* A disabled/private localStorage uses defaults. */ }
        return settings;
    };
    const writeConsoleSettings = (storage, settings) => {
        try {
            if (storage) { storage.setItem(CONSOLE_SETTINGS_STORAGE_KEY, JSON.stringify(settings)); }
        }
        catch (_error) { /* The active popup remains usable without persistence. */ }
    };
    // Display1 injects into QEMU's *active* input devices.  q35 normally
    // supplies PS/2 plus VMware's vmmouse; a guest can select either one and
    // make Display1 pointer events disappear even though the WebRTC channel
    // is healthy.  A direct console owns one explicit USB keyboard and the
    // existing PVE USB tablet, and removes both legacy alternatives.  Keep
    // these exact args managed with the Display1 lifecycle: changing back to
    // a normal PVE display restores PVE's normal input topology.
    const DIRECT_INPUT_ARGUMENTS = [
        '-machine vmport=off',
        '-machine i8042=off',
        '-device usb-kbd,id=qsm-direct-keyboard',
    ];

    const validNode = (value) => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/.test(value);
    const validVmid = (value) => Number.isInteger(value) && value >= MIN_VMID && value <= MAX_VMID;
    const validRenderNode = (value) => typeof value === 'string' && /^\/dev\/dri\/renderD[0-9]{1,4}$/.test(value);
    const enabled = (value) => value === true || value === 1 || value === '1';
    const displayProfile = (value) => Object.values(DISPLAY_PROFILE).includes(value)
        ? value : DISPLAY_PROFILE.virgl;
    const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const windowVmid = (window) => {
        const selected = window && window.pveSelNode && window.pveSelNode.data;
        const vmid = selected ? Number(selected.vmid) : NaN;
        return validVmid(vmid) ? vmid : null;
    };
    // Audio is optional: Display1 carries it only when -display names a dbus
    // audiodev.  48 kHz stereo is what the console encodes (Opus), so QEMU
    // does no resampling the worker would otherwise have to undo.
    const audioSuffix = (audio) => audio ? `,audiodev=${DIRECT_AUDIO_ID}` : '';
    const displayArgument = (vmid, profile, rendernode, audio = false) => {
        if (!validVmid(vmid)) {
            throw new Error('invalid direct Display1 settings');
        }
        if (profile === DISPLAY_PROFILE.cpu) {
            return `-display dbus,addr=unix:path=${RUNTIME_PREFIX}/${vmid}/qemu-display1.bus,gl=off${audioSuffix(audio)}`;
        }
        if (profile !== DISPLAY_PROFILE.virgl || !validRenderNode(rendernode)) {
            throw new Error('invalid direct Display1 settings');
        }
        return `-display dbus,addr=unix:path=${RUNTIME_PREFIX}/${vmid}/qemu-display1.bus,gl=on,rendernode=${rendernode}${audioSuffix(audio)}`;
    };
    // hda-micro: speakers plus a microphone input, which the console feeds
    // with the browser's microphone.
    const AUDIO_ARGUMENTS = [
        `-audiodev dbus,id=${DIRECT_AUDIO_ID},in.frequency=48000,in.channels=2,out.frequency=48000,out.channels=2`,
        '-device ich9-intel-hda,id=qsm-direct-hda',
        `-device hda-micro,id=qsm-direct-hda-codec,bus=qsm-direct-hda.0,audiodev=${DIRECT_AUDIO_ID}`,
    ];
    // Output only, as written by 0.4.0+git171; recognised and upgraded on save.
    const LEGACY_AUDIO_ARGUMENTS = [
        `-audiodev dbus,id=${DIRECT_AUDIO_ID},out.frequency=48000,out.channels=2`,
        '-device ich9-intel-hda,id=qsm-direct-hda',
        `-device hda-output,id=qsm-direct-hda-codec,bus=qsm-direct-hda.0,audiodev=${DIRECT_AUDIO_ID}`,
    ];
    const gpuArgument = () => `-device virtio-vga-gl,id=${DIRECT_GPU_ID}`;
    const guestArguments = (vmid) => [
        `-chardev socket,id=${DIRECT_AGENT_ID},path=${RUNTIME_PREFIX}/${vmid}/qsm-agent.sock,server=on,wait=off`,
        '-device virtio-serial-pci,id=qsm-direct-serial',
        `-device virtserialport,chardev=${DIRECT_AGENT_ID},name=org.qsm.direct.agent`,
    ];
    const virglDisplayPattern = (vmid) => new RegExp(
        `(?:^|\\s)-display\\s+dbus,addr=unix:path=${escapeRegExp(RUNTIME_PREFIX)}/${vmid}/qemu-display1\\.bus,gl=on,rendernode=(/dev/dri/renderD[0-9]{1,4})(,audiodev=${DIRECT_AUDIO_ID})?(?=\\s|$)`,
    );
    const cpuDisplayPattern = (vmid) => new RegExp(
        `(?:^|\\s)-display\\s+dbus,addr=unix:path=${escapeRegExp(RUNTIME_PREFIX)}/${vmid}/qemu-display1\\.bus,gl=off(,audiodev=${DIRECT_AUDIO_ID})?(?=\\s|$)`,
    );
    const displayCount = (args) => (args.match(/(?:^|\s)-display(?:\s|$)/g) || []).length;
    const virtioVgaGlArguments = (args) => args.match(
        /(?:^|\s)-device\s+virtio-vga-gl(?:,[^\s]+)?(?=\s|$)/g,
    ) || [];
    // A VirGL GPU that qdev-host runs out of process (QEMU vhost-user-gpu, its
    // virgl-gpu plugin) and writes into args itself. Under the VirGL display it
    // takes the place of QSM's own GPU; the display needs gl=on to show it.
    const externalGpuArguments = (args) => args.match(
        /(?:^|\s)-device\s+vhost-user-(?:vga|gpu-pci),id=[A-Za-z][A-Za-z0-9_-]{0,31},chardev=qdev-[^\s]+(?=\s|$)/g,
    ) || [];
    const EXTERNAL_GPU_MESSAGE = 'this VM\'s GPU comes from qdev-host (vhost-user-gpu) and needs the VirGL display: '
        + 'remove the qdev-host GPU in the Hardware panel first';
    const normaliseArgument = (value) => value.trim();
    const containsArgument = (args, value) => new RegExp(
        `(?:^|\\s)${escapeRegExp(value)}(?=\\s|$)`,
    ).test(args);
    const managedGuestChannel = (args, vmid) => guestArguments(vmid).every((value) => containsArgument(args, value));
    const managedDirectInput = (args) => DIRECT_INPUT_ARGUMENTS.every((value) => containsArgument(args, value));
    const removeManagedGuestChannel = (args, vmid) => guestArguments(vmid).reduce((current, value) =>
        current.replace(new RegExp(`(?:^|\\s)${escapeRegExp(value)}(?=\\s|$)`), ''), args,
    ).trim().replace(/\s{2,}/g, ' ');
    const addManagedGuestChannel = (args, vmid) => {
        if (args.includes(DIRECT_AGENT_ID) && !managedGuestChannel(args, vmid)) {
            throw new Error('unsafe QSM guest channel arguments');
        }
        return managedGuestChannel(args, vmid) ? args : `${args} ${guestArguments(vmid).join(' ')}`.trim();
    };
    const hasArguments = (args, list) => list.every((value) => containsArgument(args, value));
    const managedAudio = (args) => hasArguments(args, AUDIO_ARGUMENTS) || hasArguments(args, LEGACY_AUDIO_ARGUMENTS);
    // The id may appear only in the managed arguments and as Display1's
    // audiodev; anything else using it belongs to an administrator.
    const foreignAudio = (args) => args.split(`,audiodev=${DIRECT_AUDIO_ID}`).join('').includes(DIRECT_AUDIO_ID);
    const addManagedAudio = (args) => {
        if (foreignAudio(args) && !managedAudio(args)) {
            throw new Error('unsafe QSM audio arguments');
        }
        return hasArguments(args, AUDIO_ARGUMENTS) ? args : `${removeManagedAudio(args)} ${AUDIO_ARGUMENTS.join(' ')}`.trim();
    };
    const removeManagedAudio = (args) => [...AUDIO_ARGUMENTS, ...LEGACY_AUDIO_ARGUMENTS].reduce((current, value) => current.replace(
        new RegExp(`(?:^|\\s)${escapeRegExp(value)}(?=\\s|$)`), '',
    ), args).trim().replace(/\s{2,}/g, ' ');
    const withAudio = (args, audio) => audio ? addManagedAudio(args) : removeManagedAudio(args);
    const addManagedDirectInput = (args) => managedDirectInput(args)
        ? args : `${args} ${DIRECT_INPUT_ARGUMENTS.filter((value) => !containsArgument(args, value)).join(' ')}`.trim();
    const removeManagedDirectInput = (args) => DIRECT_INPUT_ARGUMENTS.reduce((current, value) => current.replace(
        new RegExp(`(?:^|\\s)${escapeRegExp(value)}(?=\\s|$)`), '',
    ), args).trim().replace(/\s{2,}/g, ' ');
    const displayState = (args, vmid) => {
        if (typeof args !== 'string' || !validVmid(vmid)) {
            return { managed: false, legacy: false, legacyGpu: false, profile: DISPLAY_PROFILE.virgl,
                rendernode: DEFAULT_RENDER_NODE };
        }
        const virglMatch = args.match(virglDisplayPattern(vmid));
        const cpuMatch = args.match(cpuDisplayPattern(vmid));
        const gpu = virtioVgaGlArguments(args).map(normaliseArgument);
        if (virglMatch && displayCount(args) === 1) {
            if (gpu.length === 1 && gpu[0] === gpuArgument()) {
                return { managed: true, legacy: false, legacyGpu: false, guest: managedGuestChannel(args, vmid),
                    input: managedDirectInput(args), audio: Boolean(virglMatch[2]), profile: DISPLAY_PROFILE.virgl,
                    rendernode: virglMatch[1] };
            }
            // git20 emitted the unlabelled VirtIO-GPU argument. It is safe to
            // migrate only that exact historical form; any device options or
            // a second adapter belong to an administrator and must not be
            // silently claimed or duplicated by this UI overlay.
            if (gpu.length === 1 && gpu[0] === '-device virtio-vga-gl') {
                return { managed: false, legacy: true, legacyGpu: true, guest: managedGuestChannel(args, vmid),
                    input: managedDirectInput(args), audio: Boolean(virglMatch[2]), profile: DISPLAY_PROFILE.virgl,
                    rendernode: virglMatch[1] };
            }
            if (gpu.length === 0 && externalGpuArguments(args).length === 1) {
                return { managed: true, legacy: false, legacyGpu: false, externalGpu: true,
                    guest: managedGuestChannel(args, vmid), input: managedDirectInput(args),
                    audio: Boolean(virglMatch[2]), profile: DISPLAY_PROFILE.virgl, rendernode: virglMatch[1] };
            }
            if (gpu.length === 0) {
                return { managed: false, legacy: true, legacyGpu: false, guest: managedGuestChannel(args, vmid),
                    input: managedDirectInput(args), audio: Boolean(virglMatch[2]), profile: DISPLAY_PROFILE.virgl,
                    rendernode: virglMatch[1] };
            }
        }
        // The stock Standard VGA/non-GL VirtIO adapter is owned by PVE and is
        // not present in `args`. Reject any GL GPU here instead of claiming a
        // foreign adapter while enabling CPU Display1.
        if (cpuMatch && displayCount(args) === 1 && gpu.length === 0) {
            return { managed: true, legacy: false, legacyGpu: false, guest: managedGuestChannel(args, vmid),
                input: managedDirectInput(args), audio: Boolean(cpuMatch[1]), profile: DISPLAY_PROFILE.cpu,
                rendernode: DEFAULT_RENDER_NODE };
        }
        return { managed: false, legacy: false, legacyGpu: false, profile: DISPLAY_PROFILE.virgl,
            rendernode: DEFAULT_RENDER_NODE };
    };
    const removeArgument = (args, argument) => args.replace(
        new RegExp(`(?:^|\\s)${escapeRegExp(argument)}(?=\\s|$)`), '',
    ).trim().replace(/\s{2,}/g, ' ');
    const replaceArgument = (args, previous, wanted) => args.replace(
        new RegExp(`(?:^|\\s)${escapeRegExp(previous)}(?=\\s|$)`), (value) =>
            value.startsWith(' ') ? ` ${wanted}` : wanted);
    // `audio` undefined keeps what is configured (off for a new console).
    const updateDisplayArgument = (args, vmid, profile, rendernode, want, audio) => {
        args = args === undefined || args === null ? '' : args;
        if (typeof args !== 'string' || args.length > MAX_QEMU_ARGS_BYTES || /[\x00-\x1f\x7f]/.test(args)) {
            throw new Error('unsafe QEMU display arguments');
        }
        profile = displayProfile(profile);
        const existing = displayState(args, vmid);
        const count = displayCount(args);
        const gpu = virtioVgaGlArguments(args).map(normaliseArgument);
        audio = audio === undefined ? Boolean(existing.audio) : Boolean(audio);
        const wanted = displayArgument(vmid, profile, rendernode, audio);
        const external = externalGpuArguments(args).length > 0;
        if (external && (!want || profile !== DISPLAY_PROFILE.virgl)) { throw new Error(EXTERNAL_GPU_MESSAGE); }
        if (want) {
            if (count === 0) {
                if (gpu.length !== 0) { throw new Error('unsafe QEMU Display1 device arguments'); }
                if (foreignAudio(args)) { throw new Error('unsafe QSM audio arguments'); }
                const ownedGpu = profile === DISPLAY_PROFILE.virgl && !external ? `${gpuArgument()} ` : '';
                // A VM moved off the native shared display (dbus-display) can
                // already carry the managed guest channel and input arguments.
                // Add only what is missing: a second qsm-direct-agent chardev
                // stops QEMU with a duplicate id.
                const added = addManagedDirectInput(addManagedGuestChannel(
                    `${args} ${ownedGpu}${wanted}`.trim(), vmid));
                return audio ? `${added} ${AUDIO_ARGUMENTS.join(' ')}` : added;
            }
            if (!existing.managed && !existing.legacy) {
                throw new Error('another QEMU display is configured');
            }
            const previous = displayArgument(vmid, existing.profile, existing.rendernode, existing.audio);
            let updated = withAudio(replaceArgument(args, previous, wanted), audio);
            if (profile === DISPLAY_PROFILE.cpu) {
                if (existing.profile === DISPLAY_PROFILE.virgl) {
                    updated = existing.legacyGpu
                        ? updated.replace(/(?:^|\s)-device\s+virtio-vga-gl(?=\s|$)/, '')
                        : removeArgument(updated, gpuArgument());
                }
                return addManagedDirectInput(addManagedGuestChannel(updated, vmid));
            }
            if (existing.profile === DISPLAY_PROFILE.cpu) {
                return addManagedDirectInput(addManagedGuestChannel(`${updated} ${gpuArgument()}`, vmid));
            }
            if (existing.managed) { return addManagedDirectInput(addManagedGuestChannel(updated, vmid)); }
            if (existing.legacyGpu) {
                return addManagedDirectInput(addManagedGuestChannel(updated.replace(
                    /(?:^|\s)-device\s+virtio-vga-gl(?=\s|$)/,
                    (value) => value.startsWith(' ') ? ` ${gpuArgument()}` : gpuArgument(),
                ), vmid));
            }
            return addManagedDirectInput(addManagedGuestChannel(`${updated} ${gpuArgument()}`, vmid));
        }
        if (!existing.managed && !existing.legacy) {
            if (gpu.length !== 0) { throw new Error('unsafe QEMU Display1 device arguments'); }
            return args;
        }
        const previous = displayArgument(vmid, existing.profile, existing.rendernode, existing.audio);
        const withoutDisplay = removeManagedAudio(removeArgument(args, previous));
        if (existing.profile === DISPLAY_PROFILE.cpu) {
            return removeManagedDirectInput(removeManagedGuestChannel(withoutDisplay, vmid));
        }
        const withoutGpu = existing.legacyGpu
            ? withoutDisplay.replace(/(?:^|\s)-device\s+virtio-vga-gl(?=\s|$)/, '')
            : removeArgument(withoutDisplay, gpuArgument());
        return removeManagedDirectInput(removeManagedGuestChannel(withoutGpu, vmid));
    };

    const stockVgaType = (value) => {
        if (typeof value !== 'string' || !value) { return 'none'; }
        const typed = /(?:^|,)type=([a-z0-9-]+)/.exec(value);
        return typed ? typed[1] : value.split(',', 1)[0];
    };
    // A node can switch its web UI to standalone while retaining the native
    // qemu-server backend for already running shared-display VMs. The terminal
    // service supports both buses. Never convert such a VM to private args as
    // a side effect of opening/saving its ordinary Display or Audio dialog.
    const sharedDisplayConfigured = (config) => Boolean(config &&
        [1, '1', true].includes(config['dbus-display']) &&
        ['virtio-gl', 'std', 'virtio', 'vmware', 'cirrus', 'qxl', 'qxl2', 'qxl3', 'qxl4']
            .includes(stockVgaType(config.vga || 'std')));
    const consoleConfigured = (config, vmid) => sharedDisplayConfigured(config) ||
        displayState(config && config.args, vmid).managed;
    const effectiveDisplayAdapter = (active, profile, vga) => {
        if (!active) { return gettext('PVE Graphic card: ') + stockVgaType(vga || 'none'); }
        if (profile === DISPLAY_PROFILE.virgl) {
            return gettext('QSM VirtIO-GPU (VirGL, GL) — PVE Graphic card is None');
        }
        const type = stockVgaType(vga || 'none');
        if (type === 'virtio') { return gettext('PVE VirtIO-GPU + QSM Display1 (CPU, no GL)'); }
        if (type === 'vmware') { return gettext('PVE VMware SVGA + QSM Display1 (CPU, no GL)'); }
        return gettext('PVE Standard VGA + QSM Display1 (CPU, no GL)');
    };
    const updateDisplayFields = (panel, active, profile, vga) => {
        const rendernode = panel && panel.down('[name=qsm_direct_rendernode]');
        if (rendernode) { rendernode.setDisabled(!active || profile !== DISPLAY_PROFILE.virgl); }
        const adapter = panel && panel.down('[name=qsm_direct_effective_adapter]');
        if (adapter) {
            const type = vga || (panel.down('[name=type]') && panel.down('[name=type]').getValue());
            adapter.setValue(effectiveDisplayAdapter(active, profile, type));
        }
    };

    const displayFields = () => [
        {
            xtype: 'proxmoxcheckbox', name: 'qsm_direct_display1', uncheckedValue: 0,
            defaultValue: 0, deleteDefaultValue: true, fieldLabel: gettext('QSM Display1'),
            boxLabel: gettext('Enable the private D-Bus Display1 browser console'),
            listeners: { change: function (_field, value) {
                const panel = this.up('inputpanel');
                const profileField = panel && panel.down('[name=qsm_direct_profile]');
                updateDisplayFields(panel, enabled(value), displayProfile(profileField && profileField.getValue()));
            }},
        },
        {
            xtype: 'combo', name: 'qsm_direct_profile', value: DISPLAY_PROFILE.virgl,
            fieldLabel: gettext('Display1 profile'), queryMode: 'local', editable: false, forceSelection: true,
            displayField: 'label', valueField: 'value',
            store: { fields: ['value', 'label'], data: [
                { value: DISPLAY_PROFILE.virgl, label: gettext('VirGL GPU (GL)') },
                { value: DISPLAY_PROFILE.cpu, label: gettext('CPU — Standard VGA, VirtIO or VMware (no GL)') },
            ] },
            listeners: { change: function (_field, value) {
                const panel = this.up('inputpanel');
                const enabledField = panel && panel.down('[name=qsm_direct_display1]');
                updateDisplayFields(panel, enabled(enabledField && enabledField.getValue()), displayProfile(value));
            }},
        },
        {
            xtype: 'textfield', name: 'qsm_direct_rendernode', value: DEFAULT_RENDER_NODE, disabled: true,
            fieldLabel: gettext('Render node'), allowBlank: false,
            validator: (value) => validRenderNode(value) || gettext('Use a DRM render node, for example /dev/dri/renderD128.'),
        },
        {
            xtype: 'displayfield', name: 'qsm_direct_effective_adapter',
            fieldLabel: gettext('Effective display adapter'),
            value: effectiveDisplayAdapter(false, DISPLAY_PROFILE.virgl, 'none'),
        },
        { xtype: 'displayfield', userCls: 'pmx-hint', value: gettext(
            'VirGL owns a private VirtIO-GPU and saves PVE Graphic card as None, because VNC and GL Display1 are incompatible. CPU Display1 uses gl=off and keeps the selected Standard VGA or non-GL VirtIO adapter, so no render node or host GPU is needed. If an adapter does not implement Display1 resize, QSM keeps its fixed guest scanout connected instead of failing the console. For sound and microphone in the console, add an Audio Device with the backend QSM Console. The optional QSM Desktop Agent serial channel provides clipboard integration. Restart the VM after changing this setting.'),
        },
    ];

    defineOverride('PVE.qsmDirect.DisplayInputPanelOverlay', {
        override: 'PVE.qemu.DisplayInputPanel',
        initComponent: function () {
            this.advancedItems = (this.advancedItems || []).concat(displayFields());
            this.callParent();
        },
        onGetValues: function (values) {
            const edit = this.up('proxmoxWindowEdit');
            if (sharedDisplayConfigured(edit && edit.vmconfig)) {
                return this.callParent([values]);
            }
            const vmid = windowVmid(edit);
            const active = enabled(values.qsm_direct_display1);
            const profile = displayProfile(values.qsm_direct_profile);
            const rendernode = values.qsm_direct_rendernode || DEFAULT_RENDER_NODE;
            if (!edit || !edit.vmconfig || (active && (!validVmid(vmid) ||
                (profile === DISPLAY_PROFILE.virgl && !validRenderNode(rendernode))))) {
                throw new Error('direct Display1 configuration is invalid');
            }
            if (active && profile === DISPLAY_PROFILE.cpu && !CPU_DISPLAY_VGA_TYPES.has(values.type)) {
                throw new Error('CPU Display1 requires PVE Graphic card Standard VGA, VirtIO or VMware');
            }
            // PVE 9 does not populate `memory` for every VGA type. Passing an
            // explicit `undefined` becomes `vga.memory=undefined` at the API
            // boundary, which fails its integer schema validation. Preserve a
            // supplied integer (the ExtJS field may serialize it as a string),
            // but leave the property out when the field is absent.
            // PVE normally appends both egl-headless and VNC for virtio-gl.
            // QEMU rejects VNC next to a GL Display1 backend. VirGL therefore
            // owns its GPU and uses vga=none. The CPU DBus backend is gl=off,
            // works alongside PVE VNC, and preserves std/virtio unchanged.
            const result = { type: active && profile === DISPLAY_PROFILE.virgl ? 'none' : values.type };
            const rawMemory = values.memory;
            if ((!active || profile === DISPLAY_PROFILE.cpu) &&
                rawMemory !== undefined && rawMemory !== null && rawMemory !== '') {
                const memory = Number(rawMemory);
                if (Number.isInteger(memory)) { result.memory = memory; }
            }
            const printed = PVE.Parser.printPropertyString(result, 'type');
            const response = printed ? { vga: printed } : { delete: 'vga' };
            const audio = values.qsm_direct_audio === undefined ? undefined : enabled(values.qsm_direct_audio);
            const changed = updateDisplayArgument(edit.vmconfig.args, vmid, profile, rendernode, active, audio);
            if (changed !== (edit.vmconfig.args || '')) { response.args = changed; }
            return response;
        },
    });

    // The custom controls are represented by the exact `args` value rather
    // than an unsupported PVE config key. Populate them after DisplayEdit's
    // asynchronous load so reopening Hardware -> Display reflects what was
    // actually saved for this VM.
    // Sound: PVE's own Audio Device dialog and Hardware row.  A stock audio0
    // backend (spice, none) cannot feed a D-Bus display, so the backend
    // "QSM Console" is this overlay's sound card in `args` (AUDIO_ARGUMENTS)
    // instead of audio0, shown and removed like an ordinary Audio Device.
    const QSM_AUDIO_DRIVER = 'qsm';
    const QSM_AUDIO_LABEL = 'ich9-intel-hda, QSM Console (speakers + microphone)';
    const qsmAudioActive = (args, vmid) => {
        const state = displayState(args, vmid);
        return (state.managed || state.legacy) && Boolean(state.audio);
    };
    // The args for switching the QSM sound card on or off; null when the VM
    // has no managed Display1 to carry it.
    const qsmAudioArgs = (args, vmid, on) => {
        const state = displayState(args, vmid);
        if (!state.managed && !state.legacy) { return null; }
        return updateDisplayArgument(args, vmid, state.profile, state.rendernode, true, on);
    };

    defineOverride('PVE.qsmDirect.AudioInputPanelOverlay', {
        override: 'PVE.qemu.AudioInputPanel',
        onGetValues: function (values) {
            const edit = this.up('proxmoxWindowEdit');
            const config = (edit && edit.vmconfig) || {};
            const vmid = windowVmid(edit);
            const args = config.args || '';
            if (values.driver === QSM_AUDIO_DRIVER) {
                const changed = qsmAudioArgs(args, vmid, true);
                if (changed === null) { throw new Error('QSM Console audio needs QSM Display1'); }
                const response = {};
                if (changed !== args) { response.args = changed; }
                if (config.audio0) { response.delete = 'audio0'; }
                return response;
            }
            const response = this.callParent([values]);
            if (qsmAudioActive(args, vmid)) {
                response.args = qsmAudioArgs(args, vmid, false);
            }
            return response;
        },
    });

    defineOverride('PVE.qsmDirect.AudioEditOverlay', {
        override: 'PVE.qemu.AudioEdit',
        initComponent: function () {
            const me = this;
            const stockLoad = me.load;
            me.load = function (options) {
                const chained = Ext.apply({}, options);
                const stockSuccess = chained.success;
                chained.success = function (response) {
                    const loaded = (response && response.result && response.result.data) || {};
                    if (sharedDisplayConfigured(loaded)) {
                        const driver = me.down('[name=driver]');
                        if (driver && driver.getStore && !driver.getStore().getById('dbus')) {
                            driver.getStore().add({ key: 'dbus', value: 'D-Bus (shared display)' });
                        }
                        if (stockSuccess) { stockSuccess.apply(this, arguments); }
                        return;
                    }
                    if (stockSuccess) { stockSuccess.apply(this, arguments); }
                    const data = (response && response.result && response.result.data) || {};
                    const vmid = windowVmid(me);
                    const state = displayState(data.args, vmid);
                    if (!state.managed && !state.legacy) { return; }
                    const driver = me.down('[name=driver]');
                    if (driver && driver.getStore && !driver.getStore().getById(QSM_AUDIO_DRIVER)) {
                        driver.getStore().add({ key: QSM_AUDIO_DRIVER, value: 'QSM Console' });
                    }
                    // The QSM sound card is an ich9-intel-hda with hda-micro.
                    const device = me.down('[name=device]');
                    const follow = (value) => {
                        if (!device) { return; }
                        if (value === QSM_AUDIO_DRIVER) { device.setValue('ich9-intel-hda'); }
                        device.setDisabled(value === QSM_AUDIO_DRIVER);
                    };
                    if (driver) { driver.on('change', (_field, value) => follow(value)); }
                    if (!data.audio0) {
                        // Configured or new on a QSM console: the console's own.
                        me.setValues({ device: 'ich9-intel-hda', driver: QSM_AUDIO_DRIVER });
                    }
                    follow(driver ? driver.getValue() : null);
                };
                return stockLoad.call(me, chained);
            };
            me.callParent();
            me.load = stockLoad;
        },
    });

    defineOverride('PVE.qsmDirect.HardwareViewOverlay', {
        override: 'PVE.qemu.HardwareView',
        initComponent: function () {
            const me = this;
            me.callParent();
            const vmid = Number(me.pveSelNode && me.pveSelNode.data && me.pveSelNode.data.vmid);
            // The store loads only the keys of its reader's `rows`; load args
            // too, and keep it out of the grid (its filter uses `me.rows`).
            const reader = me.rstore.getProxy().getReader();
            for (const rows of [me.rows, reader && reader.rows]) {
                if (rows && !rows.args) { rows.args = { visible: false }; }
            }
            const currentArgs = () => {
                const record = me.rstore.getById('args');
                if (!record) { return ''; }
                const pending = record.data.pending;
                return typeof pending === 'string' ? pending : (record.data.value || '');
            };
            // The grid shows a copy (DiffStore) with model fields only, so the
            // marker is looked up in the loaded store itself.
            const isVirtual = (record) => {
                const loaded = record && record.data && record.data.key === 'audio0' ? me.rstore.getById('audio0') : null;
                return Boolean(loaded && loaded.data.qsmAudio);
            };
            // Show the QSM sound card as the VM's Audio Device row; ahead of the
            // grid's copy of the store, which listens to the same event.
            me.rstore.on('load', (store, _records, success) => {
                if (success === false || store.getById('audio0')) { return; }
                if (qsmAudioActive(currentArgs(), vmid)) {
                    store.add({ key: 'audio0', value: QSM_AUDIO_LABEL, qsmAudio: true });
                }
            }, null, { priority: 1000 });
            // Remove on that row takes the sound card out of `args`.
            const remove = me.query('button').find((button) => button.RESTMethod && button.dangerous);
            if (remove) {
                const stockHandler = remove.handler;
                remove.handler = function (button, event) {
                    const record = me.getSelectionModel().getSelection()[0];
                    if (!isVirtual(record)) { return stockHandler.apply(this, arguments); }
                    Ext.Msg.confirm(gettext('Confirm'), gettext('Remove the QSM Console audio device?'), (answer) => {
                        if (answer !== 'yes') { return; }
                        Proxmox.Utils.API2Request({
                            url: `/api2/extjs/nodes/${me.pveSelNode.data.node}/qemu/${vmid}/config`,
                            method: 'PUT',
                            waitMsgTarget: me,
                            params: { args: qsmAudioArgs(currentArgs(), vmid, false) },
                            callback: () => me.reload(),
                            failure: (response) => Ext.Msg.alert(gettext('Error'), response.htmlStatus),
                        });
                    });
                    return undefined;
                };
            }
        },
    });

    defineOverride('PVE.qsmDirect.DisplayEditOverlay', {
        override: 'PVE.qemu.DisplayEdit',
        initComponent: function () {
            const me = this;
            const stockLoad = me.load;
            me.load = function (options) {
                const chained = Ext.apply({}, options);
                const stockSuccess = chained.success;
                chained.success = function (response) {
                    if (stockSuccess) { stockSuccess.apply(this, arguments); }
                    const data = response && response.result && response.result.data;
                    if (sharedDisplayConfigured(data)) {
                        for (const name of ['qsm_direct_display1', 'qsm_direct_profile',
                            'qsm_direct_rendernode', 'qsm_direct_effective_adapter']) {
                            const field = me.down(`[name=${name}]`);
                            if (field) { field.setDisabled(true); field.hide(); }
                        }
                        return;
                    }
                    const state = displayState(data && data.args, windowVmid(me));
                    me.setValues({
                        qsm_direct_display1: state.managed || state.legacy ? 1 : 0,
                        qsm_direct_profile: state.profile,
                        qsm_direct_rendernode: state.rendernode,
                        qsm_direct_effective_adapter: effectiveDisplayAdapter(
                            state.managed || state.legacy, state.profile, data && data.vga),
                    });
                    updateDisplayFields(me, state.managed || state.legacy, state.profile, data && data.vga);
                };
                return stockLoad.call(me, chained);
            };
            me.callParent();
            me.load = stockLoad;
        },
    });

    // QEMU key numbers used by the Display1 Keyboard interface. Browser
    // `code` is physical-key based, so keyboard layout does not alter remote
    // shortcut behavior. Unknown keys remain local and are never guessed.
    const scanCodes = {
        Escape: 1, Digit1: 2, Digit2: 3, Digit3: 4, Digit4: 5, Digit5: 6, Digit6: 7,
        Digit7: 8, Digit8: 9, Digit9: 10, Digit0: 11, Minus: 12, Equal: 13, Backspace: 14,
        Tab: 15, KeyQ: 16, KeyW: 17, KeyE: 18, KeyR: 19, KeyT: 20, KeyY: 21, KeyU: 22,
        KeyI: 23, KeyO: 24, KeyP: 25, BracketLeft: 26, BracketRight: 27, Enter: 28,
        ControlLeft: 29, KeyA: 30, KeyS: 31, KeyD: 32, KeyF: 33, KeyG: 34, KeyH: 35,
        KeyJ: 36, KeyK: 37, KeyL: 38, Semicolon: 39, Quote: 40, Backquote: 41,
        ShiftLeft: 42, Backslash: 43, KeyZ: 44, KeyX: 45, KeyC: 46, KeyV: 47, KeyB: 48,
        KeyN: 49, KeyM: 50, Comma: 51, Period: 52, Slash: 53, ShiftRight: 54,
        NumpadMultiply: 55, AltLeft: 56, Space: 57, CapsLock: 58, F1: 59, F2: 60, F3: 61,
        F4: 62, F5: 63, F6: 64, F7: 65, F8: 66, F9: 67, F10: 68, NumLock: 69, ScrollLock: 70,
        Numpad7: 71, Numpad8: 72, Numpad9: 73, NumpadSubtract: 74, Numpad4: 75, Numpad5: 76,
        Numpad6: 77, NumpadAdd: 78, Numpad1: 79, Numpad2: 80, Numpad3: 81, Numpad0: 82,
        NumpadDecimal: 83, IntlBackslash: 86, F11: 87, F12: 88,
    };
    const extendedScanCodes = {
        // Display1 takes QEMU's key number, not a raw set-1 byte stream.
        // The E0 prefix is therefore folded into QEMU's extended number
        // (for example Insert is 0xd2, not the VNC-style 0x152).
        ControlRight: 0x9d, AltRight: 0xb8, MetaLeft: 0xdb, MetaRight: 0xdc,
        NumpadEnter: 0x9c, NumpadDivide: 0xb5, PrintScreen: 0xb7, Pause: 0xc6, ContextMenu: 0xdd,
        Insert: 0xd2, Delete: 0xd3, Home: 0xc7, End: 0xcf, PageUp: 0xc9, PageDown: 0xd1,
        ArrowUp: 0xc8, ArrowLeft: 0xcb, ArrowRight: 0xcd, ArrowDown: 0xd0,
    };
    const qemuKey = (event, raw = false) => {
        // Keep printable keys physical for the guest layout, but modifiers
        // must follow the host's logical remapping (e.g. macOS Option/Command
        // swap). Raw input deliberately retains physical key positions.
        if (!raw && /^(Control|Alt|Meta|Shift)(Left|Right)$/.test(event.code || '')) {
            const modifiers = { Control: [29, 0x9d], Alt: [56, 0xb8],
                Meta: [0xdb, 0xdc], OS: [0xdb, 0xdc], Shift: [42, 54] };
            const pair = modifiers[event.key];
            if (pair) { return pair[event.location === 2 || /Right$/.test(event.code) ? 1 : 0]; }
            if (event.key === 'AltGraph') { return 0xb8; }
        }
        return scanCodes[event.code] || extendedScanCodes[event.code] || null;
    };
    const SHORTCUT_ACTIONS = Object.freeze(['host', 'guest', 'release', 'fullscreen', 'toolbar']);
    const SHORTCUT_MODIFIERS = Object.freeze(['Ctrl', 'Alt', 'Shift', 'Meta']);
    const shortcutModifierName = (key) => ({ 29: 'Ctrl', 157: 'Ctrl', 42: 'Shift', 54: 'Shift',
        56: 'Alt', 184: 'Alt', 219: 'Meta', 220: 'Meta' })[key] || null;
    const modifierShortcutFromKeys = (keys) => {
        const names = keys.map(shortcutModifierName);
        return names.length && names.every(Boolean)
            ? SHORTCUT_MODIFIERS.filter(name => names.includes(name)).join('+') : null;
    };
    const parseShortcut = (value) => {
        if (typeof value !== 'string' || value.length > 96) { return null; }
        const parts = value.trim().split('+').map(part => part.trim());
        const modifiers = new Set();
        const aliases = { ctrl: 'Ctrl', control: 'Ctrl', alt: 'Alt', option: 'Alt',
            shift: 'Shift', meta: 'Meta', cmd: 'Meta', command: 'Meta', win: 'Meta', super: 'Meta' };
        let code = null;
        for (const part of parts) {
            const modifier = Object.hasOwn(aliases, part.toLowerCase()) ? aliases[part.toLowerCase()] : null;
            if (modifier) { if (modifiers.has(modifier)) { return null; } modifiers.add(modifier); continue; }
            if (code || !part) { return null; }
            const names = { esc: 'Escape', del: 'Delete', return: 'Enter', space: 'Space',
                left: 'ArrowLeft', right: 'ArrowRight', up: 'ArrowUp', down: 'ArrowDown' };
            code = (Object.hasOwn(names, part.toLowerCase()) ? names[part.toLowerCase()] : null) || (/^[a-z]$/i.test(part) ? 'Key' + part.toUpperCase() :
                /^\d$/.test(part) ? 'Digit' + part : [...Object.keys(scanCodes), ...Object.keys(extendedScanCodes)]
                    .find(name => name.toLowerCase() === part.toLowerCase()));
            if (!code || /^(Control|Alt|Meta|Shift)(Left|Right)$/.test(code)) { return null; }
        }
        if (!code && !modifiers.size) { return null; }
        return [...SHORTCUT_MODIFIERS.filter(name => modifiers.has(name)), ...(code ? [code] : [])].join('+');
    };
    const shortcutFromEvent = (event) => {
        if (typeof event.getModifierState === 'function' && event.getModifierState('AltGraph')) { return null; }
        const modifier = shortcutModifierName(qemuKey(event));
        return parseShortcut([...new Set([event.ctrlKey && 'Ctrl', event.altKey && 'Alt', event.shiftKey && 'Shift',
            event.metaKey && 'Meta', modifier].filter(Boolean)), ...(!modifier ? [event.code] : [])].filter(Boolean).join('+'));
    };
    const normalizeShortcutRules = (value) => {
        if (!Array.isArray(value) || value.length > 128) { return null; }
        const seen = new Set(), rules = [];
        for (const rule of value) {
            if (!rule || typeof rule !== 'object' || !SHORTCUT_ACTIONS.includes(rule.action)) { return null; }
            const shortcut = parseShortcut(rule.shortcut);
            if (!shortcut || seen.has(shortcut)) { return null; }
            seen.add(shortcut); rules.push({ shortcut, action: rule.action });
        }
        return rules;
    };
    const defaultModifierShortcutRules = () => {
        const rules = [];
        for (let mask = 1; mask < (1 << SHORTCUT_MODIFIERS.length); mask += 1) {
            const names = SHORTCUT_MODIFIERS.filter((_name, index) => mask & (1 << index));
            if (names.length > 1) { rules.push({ shortcut: names.join('+'), action: 'host' }); }
        }
        return rules;
    };
    const defaultShortcutRules = (browser = false) => {
        const rules = ['Alt+Tab', 'Alt+Shift+Tab', 'Meta+Tab', 'Shift+Meta+Tab',
            'Meta+Space', 'Meta+L', 'Ctrl+Alt+Delete'].map(shortcut => ({ shortcut, action: 'host' }));
        rules.push({ shortcut: 'Ctrl+Alt+Shift+Escape', action: 'release' });
        rules.push(...defaultModifierShortcutRules());
        if (browser) {
            for (const modifier of ['Ctrl', 'Meta']) {
                for (const key of ['C', 'V', 'L', 'T', 'W', 'R', 'N', 'P', 'F', 'G', 'H', 'J', 'K', 'D',
                    'Equal', 'Minus', '0', 'BracketLeft', 'BracketRight']) {
                    rules.push({ shortcut: `${modifier}+${key}`, action: 'host' });
                    rules.push({ shortcut: `${modifier}+Shift+${key}`, action: 'host' });
                }
            }
            for (const shortcut of ['Escape', 'F5', 'F6', 'F11', 'F12', 'Alt+ArrowLeft', 'Alt+ArrowRight',
                'Ctrl+Tab', 'Ctrl+Shift+Tab', 'Ctrl+PageUp', 'Ctrl+PageDown']) {
                rules.push({ shortcut, action: 'host' });
            }
        }
        // Defaults are canonical, deduplicated independently of UI labels.
        return [...new Map(rules.map(rule => [parseShortcut(rule.shortcut),
            { shortcut: parseShortcut(rule.shortcut), action: rule.action }])).values()];
    };
    const shortcutRulesForSettings = (settings) => settings.keyboardPriority === KEYBOARD_PRIORITY.client
        ? settings.clientShortcuts || defaultShortcutRules(true) : settings.hostShortcuts || defaultShortcutRules();
    const matchShortcutRule = (event, rules) => {
        const shortcut = shortcutFromEvent(event);
        return shortcut ? rules.find(rule => rule.shortcut === shortcut) || null : null;
    };
    const isHostShortcutPrefix = (keys, rules) => {
        const names = keys.map(shortcutModifierName);
        return names.length > 0 && names.every(Boolean) && rules.some(rule => {
            const modifiers = rule.shortcut.split('+').filter(name => SHORTCUT_MODIFIERS.includes(name));
            return rule.action !== 'guest' && names.every(name => modifiers.includes(name));
        });
    };
    const shortcutGuestChord = (shortcut) => {
        const canonical = parseShortcut(shortcut);
        if (!canonical) { return null; }
        const parts = canonical.split('+');
        // Explicit modifier-only sends use the usual Shift-last gesture.
        // Storage canonicalization is not a keyboard press-order policy.
        if (parts.every(name => SHORTCUT_MODIFIERS.includes(name)) && parts.includes('Shift')) {
            parts.splice(parts.indexOf('Shift'), 1); parts.push('Shift');
        }
        const code = parts.pop();
        const modifierKeys = { Ctrl: 29, Alt: 56, Shift: 42, Meta: 0xdb };
        return { key: modifierKeys[code] || qemuKey({ code }), modifiers: parts.map(name => modifierKeys[name]) };
    };

    // A guest-first console forwards received keys except its explicit host rules.
    const waitForIce = (peer) => new Promise((resolve, reject) => {
        if (peer.iceGatheringState === 'complete') { resolve(); return; }
        const done = () => {
            if (peer.iceGatheringState !== 'complete') { return; }
            window.clearTimeout(timer);
            peer.removeEventListener('icegatheringstatechange', done);
            resolve();
        };
        const timer = window.setTimeout(() => {
            peer.removeEventListener('icegatheringstatechange', done);
            if (/^a=candidate:/m.test(peer.localDescription?.sdp || '')) { resolve(); }
            else { reject(new Error(gettext('The browser could not prepare a WebRTC network connection. Check browser network permissions and VPN settings.'))); }
        }, 10000);
        // The first transition can be 'gathering', before any candidates.
        // Keep listening until completion: the offer is sent only once.
        peer.addEventListener('icegatheringstatechange', done);
    });
    // The SDP exchange and the per-VM codec policy go to the node-local QSM
    // signalling service, not to a PVE API route: PVE has no supported route
    // plug-in ABI, so nothing is registered inside pveproxy/pvedaemon.  The
    // service runs on the same node with the node's own TLS certificate and
    // authorises each request by relaying this browser's PVE ticket to the
    // node's own /access/ticket endpoint.  The ticket travels in an
    // Authorization header (never the URL) and the endpoints take no ambient
    // cookie, so they are CSRF-safe.  waitMsgTarget spinners are handled by
    // the callers; these helpers only perform the cross-origin request.
    const signalEndpoint = (typeof window !== 'undefined' && window.QSM_DIRECT_ENDPOINT) ||
        { port: 8007, public_url: '' };
    const signalOrigin = () => {
        if (signalEndpoint.gateway) { return location.origin; }
        if (signalEndpoint.public_url) { return new URL(signalEndpoint.public_url).origin; }
        const address = new URL(location.origin || `https://${location.hostname}`);
        address.port = String(signalEndpoint.port);
        return address.origin;
    };
    const pveConsoleRequest = async (method, path, params) => {
        const options = { method, credentials: 'same-origin', cache: 'no-store' };
        if (method === 'GET') {
            options.signal = AbortSignal.timeout(10000);
        } else {
            options.headers = { CSRFPreventionToken: Proxmox.CSRFPreventionToken };
            options.body = new URLSearchParams(params || {});
        }
        const response = await fetch(`/api2/json${path}`, options);
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(`WebRTC ${response.status}: ${body.message || ''}`.trim());
            error.status = response.status;
            throw error;
        }
        return body && body.data;
    };
    const signalRequest = async (method, path, params) => {
        // On the shared platform containers use the same PVE origin and
        // session/CSRF checks as VM consoles. Port 8007 is legacy-only.
        if (NATIVE_QEMU && /^\/nodes\/[^/]+\/lxc\/\d+\/qsm-direct(?:-settings)?$/.test(path)) {
            return pveConsoleRequest(method, path.replace(/qsm-direct-settings$/, 'webrtcsettings')
                .replace(/qsm-direct$/, 'webrtcproxy'), params);
        }
        const cookieName = (window.Proxmox && Proxmox.Setup && Proxmox.Setup.auth_cookie_name)
            || 'PVEAuthCookie';
        const ticket = Ext.util.Cookies.get(cookieName);
        const user = window.Proxmox && Proxmox.UserName;
        if (!ticket || !user) { throw new Error('not authenticated'); }
        const headers = { 'Authorization': `Bearer ${ticket}`, 'X-QSM-User': user };
        const options = { method, mode: 'cors', cache: 'no-store', credentials: 'omit', headers };
        if (method === 'GET') { options.signal = AbortSignal.timeout(10000); }
        if (method !== 'GET') {
            headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(params || {});
        }
        const response = await fetch(`${signalOrigin()}${signalEndpoint.gateway ? '/qsm-direct' : ''}${path}`, options);
        if (!response.ok) {
            const error = new Error(`signal ${response.status}`);
            error.status = response.status;
            throw error;
        }
        const body = await response.json();
        return body && body.data;
    };
    // The offer of a viewer page goes through the PVE API itself, as the
    // logged-in user (cookie + CSRF token of the page): qemu-server checks
    // VM.Console and attaches the node's WebRTC console service.
    const pveWebrtcOffer = async (params) => {
        const body = new URLSearchParams();
        for (const [key, value] of Object.entries(params || {})) {
            if (value !== undefined && value !== null) { body.append(key, String(value)); }
        }
        const response = await fetch(
            `/api2/json/nodes/${encodeURIComponent(VIEWER.node)}/qemu/${encodeURIComponent(VIEWER.vmid)}/webrtcproxy`,
            { method: 'POST', credentials: 'same-origin', cache: 'no-store', body,
              headers: { 'CSRFPreventionToken': VIEWER.csrf } });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) { throw new Error(`webrtcproxy ${response.status}: ${result.message || ''}`.trim()); }
        return result.data;
    };
    const api = async (url, params) => {
        const answer = VIEWER ? await pveWebrtcOffer(params) : await signalRequest('POST', url, params);
        if (!answer || answer.type !== 'answer' || typeof answer.sdp !== 'string' || answer.sdp.length < 1) {
            throw new Error('invalid direct WebRTC answer');
        }
        return answer;
    };
    // Ordinary PVE reads (VM run state) still use the stock protected API; only
    // the QSM SDP exchange and codec policy use the signalling service above.
    const apiValue = (url, method, params, target) => new Promise((resolve, reject) => {
        Proxmox.Utils.API2Request({ url, method, params, waitMsgTarget: target,
            success: ({ result }) => resolve(result && result.data),
            failure: () => reject(new Error('PVE request failed')),
        });
    });
    const dimensions = (video, fps) => {
        const box = video.getBoundingClientRect();
        const width = Math.max(64, Math.min(16384, Math.floor(box.width / 2) * 2));
        const height = Math.max(64, Math.min(16384, Math.floor(box.height / 2) * 2));
        return { width, height, fps };
    };

    // kind: 'qemu' (a VM's Display1) or 'lxc' (a container's headless
    // display).  It only selects the PVE/signalling URL segment; the terminal
    // service tells the two apart from the node's own guest configs.
    const openConsole = function (button, node, vmid, embeddedFrame = null, kind = 'qemu') {
        const guestKind = kind === 'lxc' ? 'lxc' : 'qemu';
        const nativeConsole = Boolean(VIEWER) || (NATIVE_QEMU && guestKind === 'lxc');
        const embedded = Boolean(embeddedFrame) && !VIEWER;
        let popup;
        if (VIEWER) {
            // pveproxy's viewer page: its own window (or the VM's Console
            // panel frame) is the console surface.
            popup = window;
        } else if (embedded) {
            // The ordinary VM Console card is an in-page PVE area, just as
            // noVNC is. An about:blank same-origin frame gives the existing
            // self-contained console DOM a private document and viewport
            // without opening a system popup or expanding PVE's page scope.
            popup = embeddedFrame.contentWindow;
            if (!popup) { return false; }
        } else {
            const windowId = `qsm-direct-${node}-${vmid}-${Date.now()}`;
            // The top Console split-menu remains an optional separate-window
            // action. `window.open` must execute synchronously in that menu
            // click, otherwise browsers treat it as an unsolicited popup.
            popup = window.open('', windowId,
                'popup=yes,width=1280,height=800,resizable=yes,scrollbars=no');
            if (!popup) {
                Ext.Msg.alert(gettext('QSM Direct'), gettext(
                    'The browser blocked the separate console window. Allow popups for this Proxmox site and try again.'));
                return false;
            }
        }
        const closeSurface = () => {
            if (VIEWER) {
                // Keep the page and its status line: in the Console panel a
                // closed frame would only leave an empty area behind.
                return;
            }
            if (embedded) {
                if (embeddedFrame.parentNode) { embeddedFrame.parentNode.removeChild(embeddedFrame); }
            } else if (!popup.closed) {
                popup.close();
            }
        };
        const document = popup.document;
        document.title = gettext('QSM Direct Console');
        document.documentElement.style.cssText = 'width:100%;height:100%;background:#000';
        // A viewport meta is added only for the separate WINDOW console: there
        // the document IS the top-level page, so width=device-width makes it map
        // 1:1 on a phone.  It must NOT be added for the embedded card iframe —
        // an iframe honours its own viewport meta, so width=device-width there
        // makes the iframe's internal layout viewport differ from the iframe
        // element's size, and getBoundingClientRect() then no longer matches the
        // touch clientX, sending the guest the wrong coordinates.
        if (!embedded) {
            try {
                let viewportMeta = document.querySelector('meta[name="viewport"]');
                if (!viewportMeta) {
                    viewportMeta = document.createElement('meta');
                    viewportMeta.setAttribute('name', 'viewport');
                    (document.head || document.documentElement).appendChild(viewportMeta);
                }
                viewportMeta.setAttribute('content',
                    'width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover');
            } catch (_error) { /* A missing head is non-fatal; desktop is unaffected. */ }
        }
        let browserStorage = null;
        try { browserStorage = popup.localStorage; } catch (_error) { /* Defaults remain available. */ }
        const settings = readConsoleSettings(browserStorage);
        // ---- Touch gesture -> action mapping -----------------------------
        // The single touch behaviour is "direct" (a tap acts where you touch,
        // the guest cursor is shown as an overlay).  Which finger gesture
        // triggers which guest action is configurable (Settings), with working
        // defaults: 1-finger motion=move, hold then move=drag/select,
        // tap=left click, hold then lift=right click, 2-finger motion=scroll.
        const GESTURE_ACTIONS = ['move', 'leftClick', 'rightClick', 'middleClick', 'drag', 'scroll', 'zoom'];
        const GESTURES = ['tap', 'longPress', 'drag1', 'holdDrag', 'tap2', 'drag2', 'pinch', 'tap3', 'none'];
        const DEFAULT_GESTURE_MAP = { move: 'drag1', leftClick: 'tap', rightClick: 'longPress', middleClick: 'tap3', drag: 'holdDrag', scroll: 'drag2', zoom: 'pinch' };
        const gestureMap = { ...DEFAULT_GESTURE_MAP };
        try {
            const raw = browserStorage && browserStorage.getItem('qsm-gestures');
            if (raw) {
                const p = JSON.parse(raw);
                for (const a of GESTURE_ACTIONS) { if (GESTURES.includes(p[a])) { gestureMap[a] = p[a]; } }
                if (p.move === undefined) {
                    // Upgrade the old one-finger selection binding as well as
                    // fresh defaults; retain other deliberate custom bindings.
                    if (p.drag === 'drag1') { gestureMap.drag = 'holdDrag'; }
                    else if (GESTURE_ACTIONS.some(a => a !== 'move' && gestureMap[a] === 'drag1')) {
                        gestureMap.move = 'none';
                    }
                }
            }
        } catch (_error) { /* defaults remain */ }
        let gestureToAction = {};
        const rebuildGestureMap = () => {
            gestureToAction = {};
            for (const a of GESTURE_ACTIONS) { if (gestureMap[a] && gestureMap[a] !== 'none') { gestureToAction[gestureMap[a]] = a; } }
        };
        rebuildGestureMap();
        const saveGestureMap = () => {
            rebuildGestureMap();
            try { if (browserStorage) { browserStorage.setItem('qsm-gestures', JSON.stringify(gestureMap)); } } catch (_error) { /* ignore */ }
        };
        // A media worker is shared by every Console watching the same VM, so
        // its RTP clock cannot be mutated under an existing viewer. Freeze
        // the selected FPS for this session; a changed preference applies
        // when the next fresh worker is launched, as stated in the panel.
        const sessionFps = settings.targetFps;
        // Keep the video viewport equal to the entire popup.  The controls
        // deliberately float above it: a desktop console must not silently
        // lose a row of guest pixels merely because its window has controls.
        document.body.style.cssText = 'width:100vw;height:100vh;min-width:100vw;min-height:100vh;margin:0;position:relative;overflow:hidden;background:#000;color:#fff;font:13px sans-serif';
        const toolbar = document.createElement('div');
        toolbar.id = 'qsm-direct-toolbar';
        toolbar.setAttribute('aria-label', gettext('Console controls'));
        // The control strip floats over the guest video.  Its container is
        // click-through (pointer-events:none) so moving the mouse across the
        // strip still reaches the guest; only the actual buttons below opt
        // back in (pointer-events:auto).  Otherwise the revealed toolbar left
        // a dead band down the left edge where guest input was swallowed.
        toolbar.style.cssText = 'position:absolute;z-index:10;top:12px;left:32px;display:flex;flex-direction:column;max-width:calc(100vw - 44px);box-sizing:border-box;align-items:stretch;gap:5px;padding:6px;background:rgba(17,24,39,.88);box-shadow:1px 0 6px rgba(0,0,0,.55);border-radius:0 7px 7px 0;opacity:1;transform:translateX(0);transition:opacity .16s ease,transform .16s ease;pointer-events:none';
        // Always-visible reveal handle: the toolbar reveals on mouse hover, but
        // touchscreens have no hover, so on a tablet/phone the controls (⌨
        // keyboard, settings, clipboard) would be unreachable once the toolbar
        // auto-hid.  This small handle toggles it and works with a finger.
        const revealHandle = document.createElement('button');
        revealHandle.type = 'button';
        revealHandle.textContent = '☰';
        revealHandle.setAttribute('aria-controls', toolbar.id);
        revealHandle.style.cssText = 'position:fixed;z-index:11;left:0;top:50%;transform:translateY(-50%);width:26px;height:52px;padding:0;border:0;border-radius:0 8px 8px 0;cursor:pointer;font-size:16px;line-height:1;color:#e5e7eb;background:rgba(17,24,39,.55);pointer-events:auto';
        const status = document.createElement('span');
        status.textContent = gettext('Connecting…');
        status.style.cssText = 'min-width:0;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
        // Diagnostics toggle: shows the connection/input panel on demand so a
        // client with no picture or no input can be inspected without remote
        // dev tools (mostly phones/tablets/TVs).
        const diagButton = document.createElement('button');
        diagButton.type = 'button';
        diagButton.textContent = '🛈';
        diagButton.setAttribute('aria-label', gettext('Toggle diagnostics'));
        diagButton.title = gettext('Toggle diagnostics');
        diagButton.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:16px;line-height:1;pointer-events:auto';
        diagButton.addEventListener('click', () => { toggleDiag(); });
        // On-screen keyboard for touch devices with no physical keyboard: a
        // focusable off-screen input raises the device keyboard, and its
        // keydown/beforeinput events are translated to guest key scancodes
        // (mobile keyboards mostly emit beforeinput, not keydown with a code).
        const kbButton = document.createElement('button');
        kbButton.type = 'button';
        kbButton.textContent = '⌨';
        kbButton.setAttribute('aria-label', gettext('Toggle on-screen keyboard'));
        kbButton.title = gettext('Toggle on-screen keyboard');
        kbButton.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:16px;line-height:1;pointer-events:auto';
        const kbInput = document.createElement('input');
        kbInput.type = 'text';
        kbInput.setAttribute('autocomplete', 'off');
        kbInput.setAttribute('autocapitalize', 'off');
        kbInput.setAttribute('autocorrect', 'off');
        kbInput.setAttribute('spellcheck', 'false');
        kbInput.setAttribute('aria-hidden', 'true');
        // Kept in the layout and focusable (not display:none, no negative
        // z-index, opacity>0 but ~invisible) so mobile browsers reliably raise
        // their keyboard when it is focused from the ⌨ button's tap gesture.
        kbInput.style.cssText = 'position:fixed;left:2px;bottom:2px;width:16px;height:16px;opacity:0.01;border:0;padding:0;margin:0;background:transparent;color:transparent;caret-color:transparent';
        const fullscreen = document.createElement('button');
        fullscreen.type = 'button';
        fullscreen.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:18px;line-height:1;pointer-events:auto';
        const captureKeyboard = document.createElement('button');
        captureKeyboard.type = 'button';
        // An ordinary button like the others: its name is in the tooltip and in the settings.
        captureKeyboard.textContent = '🔒';
        captureKeyboard.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:16px;line-height:1;pointer-events:auto';
        captureKeyboard.setAttribute('aria-label', gettext('Capture keyboard (full screen)'));
        captureKeyboard.title = gettext('Capture keyboard (full screen)');
        captureKeyboard.setAttribute('aria-pressed', 'false');
        const settingsButton = document.createElement('button');
        settingsButton.type = 'button';
        settingsButton.textContent = '⚙';
        settingsButton.setAttribute('aria-label', gettext('Console settings'));
        settingsButton.title = gettext('Console settings');
        settingsButton.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:18px;line-height:1;pointer-events:auto';
        // Assigned once the WebRTC control channel is created.  A full-screen
        // transition is not consistently reported by ResizeObserver across
        // Chromium/Safari, so this explicit hook is part of the resize
        // contract rather than merely a toolbar-label update.
        let resizeConsole = () => undefined;
        // Browser full-screen transitions can cancel a DOM mouse/key
        // sequence before its corresponding `up` event.  This hook is
        // assigned once the authenticated control channel exists; keeping a
        // harmless early default makes an Escape pressed during connection
        // setup safe too.
        let releaseHeldInput = () => undefined;
        const video = document.createElement('video');
        let videoFeedbackChannel = null;
        const reportVideoPresented = createVideoPresentationFeedback(() => videoFeedbackChannel);
        let decodedPresentationAllowed = false;
        const receiveFeedback = createVideoReceiveFeedback(popup, () => videoFeedbackChannel, (frame, rtp, restarting = false) => {
            if (frame) { decodedPresenter.pushDirect(frame, rtp, decodedPresentationAllowed); }
            else if (restarting) { decodedPresenter.directInterrupted(rtp); }
            else { decodedPresenter.directFailed(rtp); }
        });
        video.autoplay = true;
        video.playsInline = true;
        // Mobile engines (Android Chrome, Samsung Internet) honour the HTML
        // attributes, not only the properties, and require inline playback to
        // be declared or they refuse to start a muted stream — which showed as
        // a connected console that "receives no guest data" on tablets/phones.
        video.setAttribute('playsinline', '');
        video.setAttribute('webkit-playsinline', '');
        video.setAttribute('autoplay', '');
        video.setAttribute('muted', '');
        // The SDP has an Opus m-line as well as video. Chromium and Safari
        // are allowed to reject an asynchronous, unmuted `play()` in a popup
        // even when that popup itself was opened by a Console-menu gesture.
        // Muting before the first track makes the visual console autoplay
        // deterministically; audio can be enabled explicitly afterwards.
        video.muted = true;
        video.tabIndex = 0;
        // Mobile browsers commonly block even a muted autoplay until a user
        // gesture.  Any press on the console counts as that gesture, so retry
        // play() from the input handlers below; this makes a tap start the
        // video instead of leaving it paused with "no guest data".
        const ensurePlaying = () => { const p = video.play(); if (p && typeof p.catch === 'function') { p.catch(() => undefined); } };
        // Preserve the guest's pixel aspect ratio.  Resize retry/acknowledge
        // below converges Display1 to this exact box; after convergence
        // `contain` occupies it completely without stretching an image just
        // because a user dragged one window edge.
        // `touch-action:none` is essential on touch devices: without it the
        // browser claims a finger drag for panning/zooming the page and never
        // delivers pointer motion to the guest, so nothing can be selected on
        // an Android tablet or phone.  user-select/touch-callout off keeps a
        // long press from starting a browser text selection over the video.
        video.style.cssText = 'position:fixed;inset:0;display:block;width:100vw;height:100vh;max-width:none;max-height:none;background:#000;object-fit:contain;outline:none;touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none';
        const exactCanvas = document.createElement('canvas');
        exactCanvas.setAttribute('aria-hidden', 'true');
        exactCanvas.style.cssText = 'display:none;position:fixed;inset:0;width:100vw;height:100vh;object-fit:contain;pointer-events:none;z-index:2';
        const inflateExactPixels = async (compressed, expected) => {
            const reader = new Blob([compressed]).stream()
                .pipeThrough(new DecompressionStream('deflate')).getReader();
            const pixels = new Uint8Array(expected);
            let offset = 0;
            try {
                while (true) {
                    const { value, done } = await reader.read();
                    if (done) { break; }
                    if (offset + value.length > expected) { throw Error('Oversized exact UI tile'); }
                    pixels.set(value, offset); offset += value.length;
                }
                if (offset !== expected) { throw Error('Incomplete exact UI tile'); }
                return pixels;
            } finally { await reader.cancel().catch(() => {}); }
        };
        const videoCanvas = document.createElement('canvas');
        videoCanvas.setAttribute('aria-hidden', 'true');
        videoCanvas.style.cssText = 'display:none;position:fixed;inset:0;width:100vw;height:100vh;object-fit:contain;pointer-events:none;z-index:1';
        const videoRegions = new VideoRegionRenderer(videoCanvas, video, rtp => {
            if (control && control.readyState === 'open') { control.send(JSON.stringify({ op: 'qsm_video_ack', rtp })); }
        });
        const composedCanvas = document.createElement('canvas');
        composedCanvas.setAttribute('aria-hidden', 'true');
        composedCanvas.style.cssText = 'display:none;position:fixed;inset:0;width:100vw;height:100vh;object-fit:contain;pointer-events:none;z-index:3';
        const exactCompositor = new ExactUiCompositor(composedCanvas, video, exactCanvas, videoRegions);
        let compositionQueued = false;
        const queueComposition = () => {
            exactCompositor.dirty = true;
            if (compositionQueued || popup.closed) { return; }
            compositionQueued = true;
            popup.requestAnimationFrame(() => { compositionQueued = false; syncExactGeometry(); });
        };
        const remoteVideoWidth = () => videoRegions.width || video.videoWidth;
        const remoteVideoHeight = () => videoRegions.height || video.videoHeight;
        let exactUiPolicy = { available: false, enabled: false };
        let exactUiRequested = null, exactUiVersionRequested = null;
        let exactFrameSupport = null;
        // An internal failure ends the layer for this console only. It is not the
        // user's choice, so it is not saved and the next console tries again.
        let exactUiFailed = false;
        const exactRenderer = new ExactUiRenderer(exactCanvas, inflateExactPixels, () => {
            exactUiFailed = true;
            updateExactPreference();
        }, ack => {
            if (control && control.readyState === 'open' && exactUiPolicy.max_version >= 2) {
                control.send(JSON.stringify(ack));
            }
        }, (rtp, region) => !region || videoRegions.painted === rtp, queueComposition);
        let presentationRtp = null, reportedPresentationRtp = null;
        const finishVideoPresentation = (rtp, now) => {
            if (rtp !== presentationRtp) { return; }
            if (videoRegions.width && videoRegions.painted !== rtp) { return; }
            if (exactRenderer.enabled) { exactCompositor.capture(); }
            exactRenderer.videoFrame(rtp);
            syncExactGeometry();
            if (reportedPresentationRtp !== rtp && !document.hidden) {
                reportedPresentationRtp = rtp; reportVideoPresented(rtp, now);
            }
        };
        const presentVideoFrame = (rtp, now, image = null) => {
            if (presentationRtp !== null) {
                const distance = (rtp - presentationRtp) >>> 0;
                // HTML video can still be behind when the decoded reader
                // retires. Resume only with a newer picture, including wrap.
                if (!distance || distance >= 0x80000000) { return false; }
            }
            presentationRtp = rtp;
            videoRegions.frame(rtp, image);
            finishVideoPresentation(rtp, now);
            return true;
        };
        const decodedPresenter = new DecodedVideoPresenter(popup,
            (image, rtp, now) => presentVideoFrame(rtp, now, image),
            () => videoRegions.retireDecoded());
        const updateDecodedPresentation = () => {
            const track = video.srcObject?.getVideoTracks()[0];
            decodedPresentationAllowed = !closed && !document.hidden && exactRenderer.enabled &&
                exactFrameSupport === true && videoRegions.width > 0;
            receiveFeedback.show(decodedPresentationAllowed && popup.QSM_DIRECT_DECODE !== false && decodedPresenter.directUsable);
            decodedPresenter.configure(track, decodedPresentationAllowed);
        };
        decodedPresenter.onDirectFailed = updateDecodedPresentation;
        document.addEventListener('visibilitychange', updateDecodedPresentation);
        if (typeof video.requestVideoFrameCallback === 'function') {
            const presented = (_now, metadata) => {
                if (closed) { return; }
                const frameSupport = Number.isInteger(metadata.rtpTimestamp);
                if (exactFrameSupport !== frameSupport) {
                    exactFrameSupport = frameSupport; updateExactPreference();
                }
                if (!decodedPresenter.active) {
                    if (frameSupport) { presentVideoFrame(metadata.rtpTimestamp, _now); }
                    else {
                        if (exactRenderer.enabled) { exactCompositor.capture(); }
                        syncExactGeometry();
                    }
                }
                video.requestVideoFrameCallback(presented);
            };
            video.requestVideoFrameCallback(presented);
        }
        let legacyCompositionFrame = null;
        const syncExactGeometry = () => {
            const enabled = exactRenderer.enabled && exactRenderer.width === remoteVideoWidth() &&
                exactRenderer.height === remoteVideoHeight() && exactRenderer.width > 0;
            if (enabled && typeof video.requestVideoFrameCallback !== 'function') {
                exactCompositor.capture();
                if (legacyCompositionFrame === null && !popup.closed) {
                    legacyCompositionFrame = popup.requestAnimationFrame(() => {
                        legacyCompositionFrame = null; syncExactGeometry();
                    });
                }
            } else if (legacyCompositionFrame !== null) {
                popup.cancelAnimationFrame(legacyCompositionFrame); legacyCompositionFrame = null;
            }
            exactCompositor.present(enabled);
            updateDecodedPresentation();
        };
        const updateExactPreference = () => {
            applyExactUiDefault(settings, exactUiPolicy);
            const supported = typeof DecompressionStream === 'function';
            const requested = settings.exactUi && !exactUiFailed;
            exactUiInput.checked = requested;
            exactUiInput.disabled = !supported || !exactUiPolicy.available;
            const wanted = supported && exactUiPolicy.available && requested;
            const version = exactFrameSupport ? Math.min(exactUiPolicy.features & 64 ? 7 : exactUiPolicy.features & 32 ? 6 : exactUiPolicy.features & 8 ? 5 : 4, exactUiPolicy.max_version || 1) : 1;
            exactRenderer.configure(wanted);
            syncExactGeometry();
            if (control && control.readyState === 'open' && (exactUiRequested !== wanted || exactUiVersionRequested !== version) &&
                (exactUiPolicy.available || exactUiRequested === true)) {
                exactUiRequested = wanted; exactUiVersionRequested = version;
                videoRegions.acknowledged = false;
                control.send(JSON.stringify({ op: 'qsm_ui_mode', enabled: wanted,
                    ...(exactUiPolicy.max_version >= 2 ? { version } : {}) }));
            }
            exactUiStatus.textContent = !supported ? gettext('This browser cannot use exact UI.') :
                !exactUiPolicy.available ? gettext('Waiting for exact UI support…') :
                !wanted ? `${gettext('Exact UI')}: ${exactUiFailed ? gettext('Off after an error') : gettext('Off')}` :
                !exactUiPolicy.enabled ? `${gettext('Exact UI')}: ${gettext('Enabling…')}` :
                `${gettext('Exact UI')}: ${gettext('Active')} · ${exactRenderer.patches} ${gettext('updates')}`;
        };
        // QEMU Display1 supplies the cursor shape and position separately
        // from scanout damage. This canvas is deliberately visual-only: it
        // never participates in hit testing, resize, or input forwarding.
        // The operating system still owns the cursor on the actual browser
        // window frame, while the guest owns it inside this video rectangle.
        const guestCursor = document.createElement('canvas');
        guestCursor.setAttribute('aria-hidden', 'true');
        guestCursor.style.cssText = 'display:none;position:fixed;z-index:5;pointer-events:none;image-rendering:auto';
        // On touch there is no OS cursor to style and VirGL does not draw the
        // cursor into the video, so the guest's own cursor bitmap is shown as
        // this overlay at the guest-reported position (scaled with the content).
        const guestPointer = document.createElement('div');
        guestPointer.setAttribute('aria-hidden', 'true');
        guestPointer.style.cssText = 'display:none;position:fixed;z-index:6;pointer-events:none;background-repeat:no-repeat;background-size:100% 100%;image-rendering:auto;filter:drop-shadow(0 0 1px rgba(0,0,0,.6))';
        const audio = document.createElement('button');
        audio.type = 'button';
        audio.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:16px;line-height:1;pointer-events:auto';
        const setAudioLabel = () => {
            const label = video.muted ? gettext('Enable Audio') : gettext('Mute Audio');
            audio.textContent = video.muted ? '🔇' : '🔊';
            audio.setAttribute('aria-label', label);
            audio.title = label;
        };
        setAudioLabel();
        audio.addEventListener('click', () => {
            video.muted = !video.muted;
            setAudioLabel();
            // A user gesture on this explicit control satisfies the browser
            // audio-autoplay policy without making video startup depend on it.
            video.play().catch(() => undefined);
        });
        // Microphone: the audio m-line is negotiated sendrecv up front, so
        // switching the microphone on or off only swaps the sender's track --
        // no renegotiation.  The browser asks for permission on first use.
        let micTransceiver = null;
        let micStream = null;
        const mic = document.createElement('button');
        mic.type = 'button';
        mic.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:16px;line-height:1;pointer-events:auto';
        const setMicLabel = () => {
            const label = micStream ? gettext('Turn Microphone Off') : gettext('Turn Microphone On');
            mic.textContent = micStream ? '🎙️' : '🎤';
            mic.style.opacity = micStream ? '1' : '0.55';
            mic.setAttribute('aria-pressed', String(Boolean(micStream)));
            mic.setAttribute('aria-label', label);
            mic.title = label;
        };
        const stopMicrophone = () => {
            if (micStream) { micStream.getTracks().forEach((track) => track.stop()); }
            micStream = null;
            if (micTransceiver) { micTransceiver.sender.replaceTrack(null).catch(() => undefined); }
            setMicLabel();
        };
        setMicLabel();
        mic.addEventListener('click', async () => {
            if (micStream) { stopMicrophone(); return; }
            if (!micTransceiver) { status.textContent = gettext('The console is not connected yet.'); return; }
            const media = navigator.mediaDevices;
            if (!media || !media.getUserMedia) {
                status.textContent = gettext('This browser does not allow the microphone here (it needs HTTPS).');
                return;
            }
            try {
                const stream = await media.getUserMedia({
                    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
                });
                if (closed) { stream.getTracks().forEach((track) => track.stop()); return; }
                await micTransceiver.sender.replaceTrack(stream.getAudioTracks()[0] || null);
                micStream = stream;
                setMicLabel();
            } catch (error) {
                status.textContent = error && error.name === 'NotAllowedError'
                    ? gettext('Microphone access was denied by the browser.')
                    : gettext('No microphone is available.');
            }
        });
        // The embedded card console maximises within the page instead of going
        // to exclusive OS full screen: it fills the viewport below and right of
        // its current position, leaving the PVE navigation and header visible.
        let embeddedMaximized = false;
        let embeddedFrameSavedStyle = null;
        const setFullscreenToggle = () => {
            const active = Boolean(document.fullscreenElement) || embeddedMaximized;
            // Keep the compact control's name stable while exposing a real
            // pressed/unpressed state to both assistive technology and CSS.
            fullscreen.textContent = active ? '⤢' : '⛶';
            fullscreen.setAttribute('aria-label', active ? gettext('Exit Full Screen') : gettext('Enter Full Screen'));
            fullscreen.setAttribute('aria-pressed', String(active));
            fullscreen.title = active ? gettext('Exit Full Screen') : gettext('Enter Full Screen');
            fullscreen.style.background = active ? '#1d4ed8' : '';
        };
        setFullscreenToggle();
        const toggleEmbeddedMaximize = () => {
            const frameEl = embeddedFrame;
            if (!frameEl) { return; }
            if (!embeddedMaximized) {
                embeddedFrameSavedStyle = frameEl.getAttribute('style') || '';
                const rect = frameEl.getBoundingClientRect();
                const left = Math.max(0, Math.round(rect.left));
                const top = Math.max(0, Math.round(rect.top));
                // Page-level maximise, not OS full screen: fill the viewport
                // from the console's current top-left to the bottom-right edge,
                // so the PVE chrome above and to the left stays on screen.  The
                // larger iframe drives the guest resize (this console becomes
                // the reference size) without taking over the whole monitor.
                frameEl.style.cssText = `position:fixed;left:${left}px;top:${top}px;` +
                    `width:calc(100vw - ${left}px);height:calc(100vh - ${top}px);` +
                    `border:0;background:#000;z-index:2147483000`;
                embeddedMaximized = true;
            } else {
                frameEl.setAttribute('style', embeddedFrameSavedStyle ||
                    'display:block;width:100%;height:100%;border:0;background:#000');
                embeddedMaximized = false;
            }
            setFullscreenToggle();
            resizeConsole(true);
            placeGuestCursor();
            video.focus({ preventScroll: true });
        };
        fullscreen.addEventListener('click', () => {
            // Native fullscreen has an explicit exit in both contexts. The
            // embedded card retains its in-page maximize outside that mode.
            if (document.fullscreenElement) { exitConsoleFullscreen(); }
            else if (embedded) { toggleEmbeddedMaximize(); }
            else { enterConsoleFullscreen(settings.keyboardPriority !== KEYBOARD_PRIORITY.client); }
        });
        let restoreClientFocusAfterFullscreen = false;
        document.addEventListener('fullscreenchange', () => {
            releaseHeldInput();
            updateGuestKeyboardLock();
            setFullscreenToggle();
            resizeConsole(true);
            // Safari and Chromium can leave the video without focus after
            // Escape restores the native popup.  Re-focus after the browser
            // has completed its layout transition, otherwise the pointer can
            // still move while clicks and keys go to the parent page.
            popup.setTimeout(() => {
                if (restoreClientFocusAfterFullscreen) {
                    restoreClientFocusAfterFullscreen = false;
                    settingsButton.focus({ preventScroll: true });
                } else if (!consolePanelOpen()) {
                    video.focus({ preventScroll: true });
                }
            }, 0);
        });
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.textContent = '⧉';
        copy.setAttribute('aria-label', gettext('Copy guest clipboard to this browser'));
        copy.title = gettext('Copy guest clipboard to this browser');
        copy.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:18px;line-height:1;pointer-events:auto';
        const paste = document.createElement('button');
        paste.type = 'button';
        paste.textContent = '⇩';
        paste.setAttribute('aria-label', gettext('Paste this browser clipboard into the guest'));
        paste.title = gettext('Paste this browser clipboard into the guest');
        paste.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:18px;line-height:1;pointer-events:auto';
        const filesButton = document.createElement('button');
        filesButton.type = 'button';
        filesButton.textContent = '📁';
        filesButton.hidden = true;
        filesButton.setAttribute('aria-label', gettext('Files: copy between this computer and the guest'));
        filesButton.title = gettext('Files: copy between this computer and the guest');
        // As wide as its content: one icon, or the icon and a transfer's percentage.
        filesButton.style.cssText = 'align-self:flex-start;min-width:32px;height:30px;padding:0 4px;cursor:pointer;font-size:15px;line-height:1;pointer-events:auto';
        const settingsPanel = document.createElement('aside');
        settingsPanel.setAttribute('aria-label', gettext('Console settings'));
        settingsPanel.style.cssText = 'display:none;position:absolute;z-index:21;left:12px;top:52px;width:min(440px,calc(100% - 24px));max-height:calc(100% - 64px);overflow:auto;box-sizing:border-box;padding:12px;border:1px solid rgba(148,163,184,.55);border-radius:8px;background:rgba(15,23,42,.97);box-shadow:0 8px 28px rgba(0,0,0,.65);color:#f8fafc';
        const settingsTitle = document.createElement('div');
        settingsTitle.textContent = gettext('Console settings');
        settingsTitle.style.cssText = 'font-weight:600;font-size:15px;margin:0 0 8px';
        const settingsHint = document.createElement('p');
        settingsHint.textContent = gettext('Display controls are saved only in this browser. The guest media policy is saved on this Proxmox node. Changes apply after all consoles for this guest are closed and a new connection is opened.');
        settingsHint.style.cssText = 'margin:0 0 10px;color:#cbd5e1;line-height:1.35';
        const mediaPolicyTitle = document.createElement('div');
        mediaPolicyTitle.textContent = gettext('This guest — media policy');
        mediaPolicyTitle.style.cssText = 'font-weight:600;margin:0 0 8px';
        const mediaPolicyHint = document.createElement('p');
        mediaPolicyHint.textContent = gettext('Automatic prefers HEVC when this browser offers WebRTC H.265 and this node has a tested hardware encoder, otherwise H.264. Verified with Chrome on Apple silicon and hevc_nvenc. Hardware mode never silently falls back to CPU.');
        mediaPolicyHint.style.cssText = 'margin:0 0 8px;color:#cbd5e1;line-height:1.35';
        const mediaPolicyForm = document.createElement('div');
        mediaPolicyForm.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) 170px;gap:8px;align-items:center';
        const codecLabel = document.createElement('label');
        codecLabel.htmlFor = 'qsm-direct-vm-codec';
        codecLabel.textContent = gettext('Video codec');
        const codecInput = document.createElement('select');
        codecInput.id = 'qsm-direct-vm-codec';
        codecInput.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
        const autoCodecOption = document.createElement('option');
        autoCodecOption.value = 'auto';
        autoCodecOption.textContent = gettext('Automatic (HEVC when supported)');
        const h264Option = document.createElement('option');
        h264Option.value = 'h264';
        h264Option.textContent = gettext('H.264 (browser WebRTC)');
        const hevcOption = document.createElement('option');
        hevcOption.value = 'hevc';
        hevcOption.textContent = gettext('HEVC (hardware encoder required)');
        codecInput.append(autoCodecOption, h264Option, hevcOption);
        const encoderLabel = document.createElement('label');
        encoderLabel.htmlFor = 'qsm-direct-vm-encoder';
        encoderLabel.textContent = gettext('Encoder');
        const encoderInput = document.createElement('select');
        encoderInput.id = 'qsm-direct-vm-encoder';
        encoderInput.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
        for (const [value, label] of [
            ['auto', gettext('Automatic (tested backend)')],
            ['hardware', gettext('Hardware only')],
            ['software', gettext('Software (H.264 libx264)')],
        ]) {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = label;
            encoderInput.append(option);
        }
        const bitrateLabel = document.createElement('label');
        bitrateLabel.htmlFor = 'qsm-direct-video-bitrate';
        bitrateLabel.textContent = gettext('Maximum video bitrate (Mbit/s)');
        const bitrateInput = document.createElement('input');
        bitrateInput.id = 'qsm-direct-video-bitrate';
        bitrateInput.type = 'number';
        bitrateInput.min = '0.5';
        bitrateInput.max = '100';
        bitrateInput.step = '0.5';
        bitrateInput.value = '8';
        bitrateInput.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
        bitrateInput.title = gettext('Adaptive video ceiling, 0.5–100 Mbit/s. The encoder follows network feedback up to this limit. Allow additional bandwidth for audio and packet overhead.');
        mediaPolicyForm.append(codecLabel, codecInput, encoderLabel, encoderInput, bitrateLabel, bitrateInput);
        const adaptiveRateStatus = document.createElement('p');
        adaptiveRateStatus.id = 'qsm-direct-adaptive-rate';
        adaptiveRateStatus.style.gridColumn = '1 / -1';
        adaptiveRateStatus.textContent = gettext('Video bitrate adapts automatically to network conditions.');
        mediaPolicyForm.append(adaptiveRateStatus);
        let videoRateSequence = -1;
        let videoRateNow = null;
        const acceptVideoRate = (message) => {
            if (!Number.isSafeInteger(message.sequence) || message.sequence <= videoRateSequence ||
                !Number.isInteger(message.bitrate) || !Number.isInteger(message.maximum) ||
                message.bitrate < 500 || message.maximum > 100000 || message.bitrate > message.maximum) { return; }
            videoRateSequence = message.sequence;
            videoRateNow = { bitrate: message.bitrate, maximum: message.maximum };
            receiveFeedback.allow(message.receive_feedback === 1);
            adaptiveRateStatus.textContent = (message.adaptive === true ? gettext('Adaptive video') :
                gettext('Fixed video — adaptive control is unavailable for this encoder')) + ': ' +
                (message.bitrate / 1000).toFixed(2) + ' / ' +
                (message.maximum / 1000).toFixed(2) + ' Mbit/s';
        };
        const saveMediaPolicy = document.createElement('button');
        saveMediaPolicy.type = 'button';
        saveMediaPolicy.textContent = gettext('Save guest media policy');
        saveMediaPolicy.style.cssText = 'margin:0 0 14px;padding:4px 9px;cursor:pointer';
        const keyboardPolicyTitle = document.createElement('div');
        keyboardPolicyTitle.textContent = gettext('Keyboard shortcut priority');
        keyboardPolicyTitle.style.cssText = 'font-weight:600;margin:0 0 8px';
        const keyboardPolicyForm = document.createElement('div');
        keyboardPolicyForm.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) 170px;gap:8px;align-items:center';
        const keyboardPriorityLabel = document.createElement('label');
        keyboardPriorityLabel.htmlFor = 'qsm-direct-keyboard-priority';
        keyboardPriorityLabel.textContent = gettext('When guest picture has focus');
        const keyboardPriorityInput = document.createElement('select');
        keyboardPriorityInput.id = 'qsm-direct-keyboard-priority';
        keyboardPriorityInput.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
        for (const [value, label] of [
            [KEYBOARD_PRIORITY.guest, gettext('Guest first — forward received keys')],
            [KEYBOARD_PRIORITY.raw, gettext('Raw input — send each key and mouse press/release')],
            [KEYBOARD_PRIORITY.client, gettext('Client first — browser shortcuts win')],
        ]) {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = label;
            keyboardPriorityInput.append(option);
        }
        keyboardPriorityInput.value = settings.keyboardPriority;
        keyboardPolicyForm.append(keyboardPriorityLabel, keyboardPriorityInput);
        const shortcutEditor = document.createElement('section');
        shortcutEditor.setAttribute('aria-label', gettext('Host shortcuts'));
        shortcutEditor.style.cssText = 'margin:12px 0;padding:10px;border:1px solid #475569;border-radius:6px';
        const shortcutTitle = document.createElement('div');
        shortcutTitle.textContent = gettext('Host shortcuts');
        shortcutTitle.style.cssText = 'font-weight:600;margin-bottom:6px';
        const shortcutHint = document.createElement('p');
        shortcutHint.textContent = gettext('Rules select Host/browser, Guest or a console action. All combinations of two or more modifiers default to Host/browser. Remove a rule or select Guest to send that completed combination to the guest on release. Exact matching keeps Alt+Shift separate from Alt+Shift+Tab. Send to guest bypasses a rule once. Modifier names follow host remapping; Meta means Command on macOS. Raw bypasses this list.');
        shortcutHint.style.cssText = 'margin:0 0 8px;color:#cbd5e1;line-height:1.35';
        const shortcutList = document.createElement('ul');
        shortcutList.setAttribute('aria-label', gettext('Configured host shortcuts'));
        shortcutList.style.cssText = 'list-style:none;margin:0 0 8px;padding:0;max-height:240px;overflow:auto';
        const shortcutForm = document.createElement('div');
        shortcutForm.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px';
        const shortcutInput = document.createElement('input');
        shortcutInput.type = 'text'; shortcutInput.maxLength = 96; shortcutInput.autocomplete = 'off';
        shortcutInput.spellcheck = false; shortcutInput.placeholder = 'Alt+Tab';
        shortcutInput.setAttribute('aria-label', gettext('Shortcut combination'));
        shortcutInput.style.cssText = 'min-width:100px;width:100%;box-sizing:border-box;padding:5px 7px';
        const shortcutAction = document.createElement('select');
        shortcutAction.setAttribute('aria-label', gettext('Shortcut action'));
        const shortcutLabels = { host: gettext('Host / browser'), guest: gettext('Guest'), release: gettext('Release keyboard'),
            fullscreen: gettext('Toggle full screen'), toolbar: gettext('Show / hide controls') };
        for (const action of SHORTCUT_ACTIONS) {
            const option = document.createElement('option'); option.value = action;
            option.textContent = shortcutLabels[action]; shortcutAction.append(option);
        }
        const recordShortcut = document.createElement('button');
        recordShortcut.type = 'button'; recordShortcut.textContent = gettext('Record shortcut');
        const saveShortcut = document.createElement('button');
        saveShortcut.type = 'button'; saveShortcut.textContent = gettext('Add shortcut');
        const cancelShortcut = document.createElement('button');
        cancelShortcut.type = 'button'; cancelShortcut.textContent = gettext('Cancel edit'); cancelShortcut.hidden = true;
        const resetShortcuts = document.createElement('button');
        resetShortcuts.type = 'button'; resetShortcuts.textContent = gettext('Reset shortcut defaults');
        const shortcutError = document.createElement('p');
        shortcutError.setAttribute('role', 'alert'); shortcutError.style.cssText = 'margin:6px 0;color:#fbbf24';
        const shortcutRecordHint = document.createElement('p');
        shortcutRecordHint.textContent = gettext('Enter e.g. Alt+Tab, Alt+Shift, Ctrl+Shift or Win+Shift. Record completes a modifier-only combination on release. No extra waiting is added. A bare modifier that could start a host shortcut stays local unless an exact Guest rule permits it. Record captures keys received by the browser; enter OS-reserved combinations manually.');
        shortcutRecordHint.style.cssText = 'margin:6px 0;color:#cbd5e1;line-height:1.35';
        shortcutForm.append(shortcutInput, shortcutAction, recordShortcut, saveShortcut, cancelShortcut, resetShortcuts);
        shortcutEditor.append(shortcutTitle, shortcutHint, shortcutList, shortcutForm, shortcutError, shortcutRecordHint);
        let editingShortcut = null, recordingShortcut = false, recordedModifierShortcut = null;
        const clearShortcutEdit = () => {
            editingShortcut = null; recordingShortcut = false; recordedModifierShortcut = null; shortcutInput.value = '';
            recordShortcut.textContent = gettext('Record shortcut'); saveShortcut.textContent = gettext('Add shortcut');
            cancelShortcut.hidden = true; shortcutError.textContent = '';
        };
        const shortcutSettingsKey = () => settings.keyboardPriority === KEYBOARD_PRIORITY.client ? 'clientShortcuts' : 'hostShortcuts';
        const persistShortcutRules = (rules) => {
            const normalized = normalizeShortcutRules(rules);
            if (!normalized) { shortcutError.textContent = gettext('Invalid shortcut rule.'); return; }
            releaseHeldInput(); settings[shortcutSettingsKey()] = normalized;
            clearShortcutEdit(); applyConsoleSettings();
        };
        const refreshShortcutEditor = () => {
            const raw = settings.keyboardPriority === KEYBOARD_PRIORITY.raw;
            for (const element of [shortcutInput, shortcutAction, recordShortcut, saveShortcut, cancelShortcut, resetShortcuts]) { element.disabled = raw; }
            shortcutList.replaceChildren();
            for (const [index, rule] of shortcutRulesForSettings(settings).entries()) {
                const row = document.createElement('li');
                row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px;padding:5px 0;border-bottom:1px solid #334155';
                const description = document.createElement('span');
                description.textContent = rule.shortcut.replace(/Key([A-Z])/g, '$1').replace(/Digit(\d)/g, '$1') + ' — ' + shortcutLabels[rule.action];
                const actions = document.createElement('span'); actions.style.cssText = 'display:flex;gap:4px';
                for (const [label, invoke] of [
                    [gettext('Send to guest'), () => {
                        if (!control || control.readyState !== 'open') { status.textContent = gettext('The console is not connected yet.'); return; }
                        const chord = shortcutGuestChord(rule.shortcut);
                        releaseHeldInput(); setSettingsPanelOpen(false); video.focus({ preventScroll: true });
                        sendGuestChord(chord.modifiers, chord.key);
                    }],
                    [gettext('Edit'), () => {
                        editingShortcut = index; recordingShortcut = false;
                        recordShortcut.textContent = gettext('Record shortcut');
                        shortcutInput.value = rule.shortcut; shortcutAction.value = rule.action;
                        saveShortcut.textContent = gettext('Save shortcut'); cancelShortcut.hidden = false;
                        shortcutError.textContent = ''; shortcutInput.focus({ preventScroll: true });
                    }],
                    [gettext('Remove'), () => persistShortcutRules(shortcutRulesForSettings(settings).filter((_rule, n) => n !== index))],
                ]) {
                    const action = document.createElement('button'); action.type = 'button'; action.textContent = label;
                    action.disabled = raw; action.style.cssText = 'padding:3px 5px;cursor:pointer';
                    action.addEventListener('click', invoke); actions.append(action);
                }
                row.append(description, actions); shortcutList.append(row);
            }
        };
        recordShortcut.addEventListener('click', () => {
            recordingShortcut = !recordingShortcut;
            recordedModifierShortcut = null;
            recordShortcut.textContent = recordingShortcut ? gettext('Press a shortcut…') : gettext('Record shortcut');
            shortcutError.textContent = ''; shortcutInput.focus({ preventScroll: true });
        });
        shortcutInput.addEventListener('keydown', (event) => {
            if (!recordingShortcut) { return; }
            event.preventDefault(); event.stopPropagation();
            const shortcut = shortcutFromEvent(event);
            if (!shortcut) { return; }
            if (shortcutModifierName(qemuKey(event))) { recordedModifierShortcut = shortcut; return; }
            shortcutInput.value = shortcut; recordingShortcut = false;
            recordShortcut.textContent = gettext('Record shortcut');
        });
        shortcutInput.addEventListener('keyup', (event) => {
            if (!recordingShortcut || !recordedModifierShortcut) { return; }
            event.preventDefault(); event.stopPropagation();
            shortcutInput.value = recordedModifierShortcut;
            recordingShortcut = false; recordedModifierShortcut = null;
            recordShortcut.textContent = gettext('Record shortcut');
        });
        saveShortcut.addEventListener('click', () => {
            const shortcut = parseShortcut(shortcutInput.value), rules = [...shortcutRulesForSettings(settings)];
            if (!shortcut) { shortcutError.textContent = gettext('Use a key combination, e.g. Alt+Tab, Alt+Shift or Ctrl+Shift+F.'); return; }
            if (rules.some((rule, index) => index !== editingShortcut && rule.shortcut === shortcut)) {
                shortcutError.textContent = gettext('This shortcut already has a rule. Edit or remove that rule.'); return;
            }
            const rule = { shortcut, action: shortcutAction.value };
            if (editingShortcut === null) { if (rules.length >= 128) { shortcutError.textContent = gettext('A maximum of 128 shortcuts is supported.'); return; } rules.push(rule); }
            else { rules[editingShortcut] = rule; }
            persistShortcutRules(rules);
        });
        cancelShortcut.addEventListener('click', clearShortcutEdit);
        resetShortcuts.addEventListener('click', () => persistShortcutRules(defaultShortcutRules(settings.keyboardPriority === KEYBOARD_PRIORITY.client)));
        refreshShortcutEditor();
        const keyInputModeLabel = document.createElement('label');
        keyInputModeLabel.htmlFor = 'qsm-direct-key-input-mode';
        keyInputModeLabel.textContent = gettext('Key repeat and holds');
        const keyInputModeInput = document.createElement('select');
        keyInputModeInput.id = 'qsm-direct-key-input-mode';
        keyInputModeInput.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
        for (const [value, label] of [
            [KEY_INPUT_MODE.typing, gettext('Exact typing — client repeats')],
            [KEY_INPUT_MODE.physical, gettext('Physical holds — guest repeats')],
        ]) {
            const option = document.createElement('option'); option.value = value; option.textContent = label;
            keyInputModeInput.append(option);
        }
        keyInputModeInput.value = settings.keyInputMode;
        keyInputModeInput.disabled = settings.keyboardPriority === KEYBOARD_PRIORITY.raw;
        if (keyInputModeInput.disabled) { keyInputModeInput.value = KEY_INPUT_MODE.physical; }
        keyboardPolicyForm.append(keyInputModeLabel, keyInputModeInput);
        const keyInputModeHint = document.createElement('p');
        keyInputModeHint.style.cssText = 'margin:8px 0;color:#cbd5e1;line-height:1.35';
        keyInputModeHint.textContent = gettext('Exact typing sends each character/repeat as one complete action, so a delayed release cannot generate extra characters. Modifier chords still work. Use Physical holds or Raw input for games and shortcuts that require holding an ordinary key. Physical mode uses guest autorepeat, which can repeat during input delays. Raw input always uses physical holds.');
        const mouseInputModeInput = document.createElement('select');
        mouseInputModeInput.id = 'qsm-direct-mouse-input-mode';
        mouseInputModeInput.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
        const mouseInputModeLabel = document.createElement('label');
        mouseInputModeLabel.htmlFor = mouseInputModeInput.id;
        mouseInputModeLabel.textContent = gettext('Mouse and trackpad buttons');
        for (const [value, label] of [
            [MOUSE_INPUT_MODE.exact, gettext('Exact clicks — mouse and trackpad')],
            [MOUSE_INPUT_MODE.physical, gettext('Physical button holds')],
        ]) {
            const option = document.createElement('option'); option.value = value; option.textContent = label;
            mouseInputModeInput.append(option);
        }
        mouseInputModeInput.value = settings.mouseInputMode;
        keyboardPolicyForm.append(mouseInputModeLabel, mouseInputModeInput);
        const mouseInputModeHint = document.createElement('p');
        mouseInputModeHint.style.cssText = 'margin:8px 0;color:#cbd5e1;line-height:1.35';
        mouseInputModeHint.textContent = gettext('Exact clicks send a complete click with its position on release, including trackpad taps. Moving 4 pixels or holding for 250 ms starts a drag/hold. Drag movement and release stay ordered. Raw input uses immediate physical button edges.');
        const keyboardPolicyHint = document.createElement('p');
        keyboardPolicyHint.textContent = gettext('Guest first forwards received keys with clipboard integration except the configured host shortcut list. Click Capture keyboard for full-screen capture, including in the embedded console. Hold Esc or click Release keyboard to exit; Ctrl+Alt+Shift+Esc releases capture by default and can be changed in the list. Raw input bypasses the list. In client-first mode, browser shortcuts stay local.');
        keyboardPolicyHint.style.cssText = 'margin:8px 0;color:#cbd5e1;line-height:1.35';
        const keyboardUnavailableHint = document.createElement('p');
        keyboardUnavailableHint.textContent = gettext('Keys reserved by the operating system can stay local, including macOS Cmd+Tab / Cmd+Space and Windows Ctrl+Alt+Del / Win+L. Capture keyboard does not require F11 or changes to macOS shortcuts. Use Send to guest buttons for keys the browser cannot receive.');
        keyboardUnavailableHint.textContent += ' ' + gettext('Host shortcut rules prevent guest delivery; the OS/browser determines local execution, including during capture. Alt+Tab and Cmd+Tab stay on the host by default. Use Send to guest beside a rule for a one-off guest action. Host modifier remapping is respected; Raw keeps physical positions.');
        keyboardUnavailableHint.style.cssText = 'margin:0 0 14px;color:#fbbf24;line-height:1.35';
        const keyboardCaptureStatus = document.createElement('p');
        keyboardCaptureStatus.style.cssText = 'margin:8px 0;color:#cbd5e1;line-height:1.35';
        const captureKeyboardSettings = document.createElement('button');
        captureKeyboardSettings.type = 'button';
        captureKeyboardSettings.textContent = gettext('Capture keyboard (full screen)');
        captureKeyboardSettings.style.cssText = 'margin:0 0 8px;padding:5px 9px;cursor:pointer';
        const releaseKeyboardSettings = document.createElement('button');
        releaseKeyboardSettings.type = 'button';
        releaseKeyboardSettings.textContent = gettext('Release keyboard');
        releaseKeyboardSettings.setAttribute('aria-label', gettext('Release keyboard and exit full screen'));
        releaseKeyboardSettings.title = gettext('Release keyboard and exit full screen');
        releaseKeyboardSettings.style.cssText = 'margin:0 0 8px;padding:5px 9px;cursor:pointer';
        const keyboardCaptureActions = document.createElement('div');
        keyboardCaptureActions.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px';
        keyboardCaptureActions.append(captureKeyboardSettings, releaseKeyboardSettings);
        const specialKeys = document.createElement('div');
        specialKeys.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 14px';
        for (const [label, modifiers, key] of [
            ['Esc', [], 1], ['Alt', [], 56], ['Alt+Shift', [56], 42],
            ['Alt+Tab', [56], 15], ['Ctrl+Alt+Del', [29, 56], 0xd3],
            ['Super / Windows', [], 0xdb],
        ]) {
            const action = document.createElement('button');
            action.type = 'button';
            action.textContent = label;
            action.title = gettext('Send to guest') + ': ' + label;
            action.style.cssText = 'padding:4px 8px;cursor:pointer';
            action.addEventListener('click', () => {
                if (!control || control.readyState !== 'open') { return; }
                releaseHeldInput();
                setSettingsPanelOpen(false);
                video.focus({ preventScroll: true });
                sendGuestChord(modifiers, key);
            });
            specialKeys.append(action);
        }
        const settingsForm = document.createElement('div');
        const exactUiLabel = document.createElement('label');
        exactUiLabel.style.cssText = 'display:flex;align-items:center;gap:8px;margin:14px 0 4px';
        const exactUiInput = document.createElement('input');
        exactUiInput.type = 'checkbox'; exactUiInput.id = 'qsm-direct-exact-ui';
        exactUiInput.checked = settings.exactUi; exactUiInput.disabled = true;
        const exactUiText = document.createElement('span');
        exactUiText.textContent = gettext('Exact UI');
        exactUiLabel.append(exactUiInput, exactUiText);
        const exactUiStatus = document.createElement('div');
        exactUiStatus.style.cssText = 'color:#cbd5e1;font-size:12px;margin:0 0 12px';
        exactUiInput.addEventListener('change', () => {
            settings.exactUi = exactUiInput.checked;
            settings.exactUiPreferenceSet = true;
            exactUiFailed = false;
            applyConsoleSettings();
        });
        settingsForm.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) 92px;gap:8px;align-items:center';
        const settingLabels = {
            toolbarHotZonePx: gettext('Left activation zone (px)'),
            toolbarNearbyPx: gettext('Controls nearby area (px)'),
            toolbarRevealDelayMs: gettext('Left hold delay (ms)'),
            toolbarHideDelayMs: gettext('Controls hide delay (ms)'),
            targetFps: gettext('Target frame rate (FPS)'),
            playoutDelayMs: gettext('Decoder playout delay (ms)'),
            resizeDebounceMs: gettext('Resize end delay (ms)'),
            resizeSettleMs: gettext('Guest resize settle time (ms)'),
        };
        const settingInputs = new Map();
        for (const [name, definition] of Object.entries(CONSOLE_SETTING_SCHEMA)) {
            const label = document.createElement('label');
            const input = document.createElement('input');
            const inputId = `qsm-direct-setting-${name}`;
            label.htmlFor = inputId;
            label.textContent = settingLabels[name];
            label.style.cssText = 'min-width:0';
            input.id = inputId;
            input.type = 'number';
            input.min = String(definition.minimum);
            input.max = String(definition.maximum);
            input.step = '1';
            input.value = String(settings[name]);
            input.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
            settingsForm.append(label, input);
            settingInputs.set(name, input);
        }
        // Touch gesture assignment: for each guest action, choose which finger
        // gesture triggers it.  Working defaults are pre-selected; a gesture can
        // be used by only one action (picking it elsewhere frees it here).
        const gesturesTitle = document.createElement('div');
        gesturesTitle.textContent = gettext('Touch gestures');
        gesturesTitle.style.cssText = 'font-weight:600;margin:16px 0 8px';
        const gesturesHint = document.createElement('p');
        gesturesHint.textContent = gettext('Slide one finger to move the cursor. Hold for half a second, then move to drag or select. Hold and lift without moving for a right click. Each gesture can drive only one action.');
        gesturesHint.style.cssText = 'margin:0 0 8px;color:#cbd5e1;line-height:1.35';
        const gesturesForm = document.createElement('div');
        gesturesForm.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) 160px;gap:8px;align-items:center';
        const GESTURE_ACTION_LABELS = {
            move: gettext('Move cursor'),
            leftClick: gettext('Left click'),
            rightClick: gettext('Right click'),
            middleClick: gettext('Middle click'),
            drag: gettext('Drag / select'),
            scroll: gettext('Scroll'),
            zoom: gettext('Zoom (magnify)'),
        };
        const GESTURE_LABELS = {
            tap: gettext('1-finger tap'),
            longPress: gettext('Long press'),
            drag1: gettext('1-finger drag'),
            holdDrag: gettext('Hold, then drag'),
            tap2: gettext('2-finger tap'),
            drag2: gettext('2-finger drag'),
            pinch: gettext('Pinch (spread)'),
            tap3: gettext('3-finger tap'),
            none: gettext('(off)'),
        };
        const gestureSelects = new Map();
        const refreshGestureSelects = () => {
            for (const [action, select] of gestureSelects) { select.value = gestureMap[action] || 'none'; }
        };
        for (const action of GESTURE_ACTIONS) {
            const label = document.createElement('label');
            const select = document.createElement('select');
            const inputId = `qsm-direct-gesture-${action}`;
            label.htmlFor = inputId;
            label.textContent = GESTURE_ACTION_LABELS[action] || action;
            label.style.cssText = 'min-width:0';
            select.id = inputId;
            select.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
            for (const gesture of GESTURES) {
                const option = document.createElement('option');
                option.value = gesture;
                option.textContent = GESTURE_LABELS[gesture] || gesture;
                select.append(option);
            }
            select.value = gestureMap[action] || 'none';
            select.addEventListener('change', () => {
                const chosen = select.value;
                if (chosen !== 'none') {
                    // A gesture drives one action: free it from any other action.
                    for (const other of GESTURE_ACTIONS) {
                        if (other !== action && gestureMap[other] === chosen) { gestureMap[other] = 'none'; }
                    }
                }
                gestureMap[action] = chosen;
                saveGestureMap();
                refreshGestureSelects();
            });
            gesturesForm.append(label, select);
            gestureSelects.set(action, select);
        }
        const resetGestures = document.createElement('button');
        resetGestures.type = 'button';
        resetGestures.textContent = gettext('Reset gesture defaults');
        resetGestures.style.cssText = 'margin:10px 0 4px;padding:4px 9px;cursor:pointer';
        resetGestures.addEventListener('click', () => {
            for (const action of GESTURE_ACTIONS) { gestureMap[action] = DEFAULT_GESTURE_MAP[action] || 'none'; }
            saveGestureMap();
            refreshGestureSelects();
        });
        const settingsActions = document.createElement('div');
        settingsActions.style.cssText = 'display:flex;gap:8px;margin-top:12px;justify-content:flex-end';
        const resetSettings = document.createElement('button');
        resetSettings.type = 'button';
        resetSettings.textContent = gettext('Reset defaults');
        resetSettings.style.cssText = 'padding:4px 9px;cursor:pointer';
        const closeSettings = document.createElement('button');
        closeSettings.type = 'button';
        closeSettings.textContent = gettext('Close');
        closeSettings.style.cssText = 'padding:4px 9px;cursor:pointer';
        const settingsHeader = document.createElement('div');
        settingsHeader.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px';
        settingsHeader.append(settingsTitle, closeSettings);
        settingsActions.append(resetSettings);
        // Capture is the first Settings action, visible without scrolling past
        // encoder policy and gesture preferences. Guest first alone cannot
        // acquire the browser's fullscreen keyboard permission.
        settingsPanel.append(settingsHeader, keyboardCaptureActions, keyboardCaptureStatus,
            settingsHint, keyboardPolicyTitle, keyboardPolicyForm, shortcutEditor, keyboardPolicyHint, keyInputModeHint,
            mouseInputModeHint, specialKeys, keyboardUnavailableHint,
            mediaPolicyTitle, mediaPolicyHint, mediaPolicyForm, saveMediaPolicy, gesturesTitle, gesturesHint,
            gesturesForm, resetGestures, exactUiLabel, exactUiStatus, settingsForm, settingsActions);
        toolbar.append(status, captureKeyboard, fullscreen, copy, paste, filesButton, audio, mic, kbButton, diagButton, settingsButton);
        // On-screen diagnostics for the case a client shows no picture (mostly
        // phones/tablets/TVs where remote logs are unreachable). Hidden until a
        // few seconds pass with no decoded frame, then it prints the video and
        // WebRTC receive state right on the black console so it can be read or
        // photographed. It removes itself as soon as a frame decodes.
        const diag = document.createElement('pre');
        diag.setAttribute('aria-hidden', 'true');
        diag.hidden = true;
        // Top-RIGHT so it never covers the left-edge toolbar/handle buttons
        // (the diagnostics toggle among them), which made it impossible to turn
        // off on a phone.  max-width keeps it off the controls entirely.
        diag.style.cssText = 'position:fixed;right:8px;top:8px;z-index:30;margin:0;max-width:min(70vw,520px);white-space:pre-wrap;word-break:break-word;padding:10px 12px;background:rgba(0,0,0,.82);color:#e5e7eb;font:13px/1.45 monospace;border:1px solid #334155;border-radius:8px;pointer-events:none';
        document.body.append(video, exactCanvas, videoCanvas, composedCanvas, guestCursor, guestPointer, toolbar, settingsPanel, diag, kbInput, revealHandle);
        popup.focus();

        let peer = null;
        let control = null;
        let pointer = null;
        let videoReceiver = null;
        let observer = null;
        let closed = false;
        let closeWatcher = null;
        // Send an isolated input immediately; retain one newest sample while
        // pacing continuous motion to at most 125 updates/second.
        let nextPointerSendAt = 0;
        let pointerTimer = null;
        let pendingPointer = null;
        let latestPointer = null;
        const heldMouseButtons = new Set();
        const heldKeys = new Set();
        let inputStateTimer = null;
        let inputStateEnabled = false;
        let inputTapsEnabled = false;
        let inputMouseActionsEnabled = false;
        let mouseFenceSequence = null;
        const pendingMousePresses = new Map();
        const cancelPendingMouse = () => {
            for (const press of pendingMousePresses.values()) { popup.clearTimeout(press.timer); }
            pendingMousePresses.clear();
        };
        const physicalHeldKeys = new Set();
        // A host switch can consume Tab before the browser receives it.
        // Defer shortcut modifiers until a guest action, so losing focus
        // cannot leave a stray Alt/Super/menu press in the guest.
        const deferredModifiers = new Map();
        const hostShortcutKeys = new Map();
        const routedShortcutEvents = new WeakSet();
        let suppressHostPaste = false;
        const hostPlatform = popup.navigator.platform || popup.navigator.userAgent || '';
        const markModifiersUsed = () => {
            for (const key of deferredModifiers.keys()) { deferredModifiers.set(key, true); }
        };
        const flushDeferredModifiers = () => {
            for (const key of [...deferredModifiers.keys()]) {
                if (send({ op: 'keyboard', key, down: true, modifiers: 0 })) {
                    deferredModifiers.delete(key); physicalHeldKeys.add(key);
                }
            }
        };
        let fullscreenEscapePending = false;
        // `qsm-pointer` is deliberately unordered and non-retransmitted.
        // Preserve an explicit sequence number with every latest-state sample
        // so a packet which took a longer SCTP path cannot move a held window
        // backwards after a newer coordinate already reached the guest.
        let pointerSequence = 0;
        let resizeTimer = null;
        let resizeRetryTimer = null;
        let resizeRetryIdentity = '';
        let resizeRetryAttempts = 0;
        // Set once the fast retry budget is spent without the guest adopting
        // the requested mode; the next wake attempt (input) re-issues it.
        let resizeStalled = false;
        let stalledRetryCount = 0;
        let lastStalledRetryAt = 0;
        let retryStalledResize = () => undefined;
        // True while the status line carries the resize text, so convergence
        // restores "Connected" without overwriting an unrelated message.
        let resizeStatusShown = false;
        // Last moment this Console forwarded pointer or key input.  A black
        // picture right after input is a booting or genuinely black guest,
        // not a sleeping one, so the wake hint is withheld for a few seconds.
        let lastInputSentAt = 0;
        let noteGuestInput = () => undefined;
        let lastResizeSentAt = 0;
        let firstFrameTimer = null;
        let diagTimer = null;
        let diagPinned = false;
        let diagPointerSent = 0;
        let diagButtonSent = 0;
        let diagKeySent = 0;
        let diagLastInput = '';
        let diagLastPtr = '';
        let diagZoom = '1.00';
        let negotiatedVideoCodec = '';
        // Fill the on-screen diagnostics panel from the live video + WebRTC
        // receive state.  Used to debug "connected but no picture" on clients
        // whose console cannot be inspected remotely.
        const updateDiag = async () => {
            if (closed) { return; }
            const lines = [];
            lines.push('QSM console diagnostics');
            lines.push(`conn=${peer ? peer.connectionState : '-'} ice=${peer ? peer.iceConnectionState : '-'} gather=${peer ? peer.iceGatheringState : '-'}`);
            lines.push(`video paused=${video.paused} readyState=${video.readyState} size=${remoteVideoWidth()}x${remoteVideoHeight()}`);
            lines.push(`answer codec=${negotiatedVideoCodec || '?'}`);
            lines.push(`chan control=${control ? control.readyState : '-'} pointer=${pointer ? pointer.readyState : '-'}`);
            lines.push(`input focus=${popup.document.activeElement === video} sent ptr=${diagPointerSent} btn=${diagButtonSent} key=${diagKeySent} last=${diagLastInput || '-'}`);
            lines.push(`touch zoom=${diagZoom} cursor=${usingTouchInput ? 'overlay' : 'os'}`);
            lines.push('ptr ' + (diagLastPtr || '-'));
            // Render the basic lines NOW, before awaiting getStats(): if getStats
            // hangs (it can on some engines) the panel must still update, or it
            // looks frozen and the pointer line can never be read.
            diag.textContent = lines.join('\n');
            try {
                if (peer) {
                    const stats = await peer.getStats();
                    let inbound = null; const codecs = new Map();
                    const cands = new Map(); const pairs = [];
                    stats.forEach((report) => {
                        if (report.type === 'codec') { codecs.set(report.id, report.mimeType); }
                        if (report.type === 'inbound-rtp' && report.kind === 'video') { inbound = report; }
                        if (report.type === 'local-candidate' || report.type === 'remote-candidate') { cands.set(report.id, report); }
                        if (report.type === 'candidate-pair') { pairs.push(report); }
                    });
                    // ICE detail: a "connecting/checking" stall is almost always
                    // a candidate-reachability problem (different subnet/Wi-Fi
                    // isolation), so surface the candidates and pair states.
                    const fmt = (c) => c ? `${c.candidateType||'?'} ${c.address||c.ip||'?'}:${c.port||'?'}/${c.protocol||'?'}` : '?';
                    const locals = [...cands.values()].filter((c) => c.type === 'local-candidate');
                    const remotes = [...cands.values()].filter((c) => c.type === 'remote-candidate');
                    lines.push('local: ' + (locals.map(fmt).join(' | ') || 'none'));
                    lines.push('remote:' + (remotes.map(fmt).join(' | ') || 'none'));
                    if (pairs.length) {
                        lines.push('pairs: ' + pairs.map((p) => `${p.state}${p.nominated ? '*' : ''}`).join(','));
                    }
                    if (inbound) {
                        lines.push(`recv=${inbound.framesReceived || 0} decoded=${inbound.framesDecoded || 0} keyDec=${inbound.keyFramesDecoded || 0} dropped=${inbound.framesDropped || 0}`);
                        lines.push(`asm=${inbound.framesAssembledFromMultiplePackets || 0} pkts=${inbound.packetsReceived || 0} lost=${inbound.packetsLost || 0} bytes=${inbound.bytesReceived || 0}`);
                        lines.push(`pli=${inbound.pliCount || 0} nack=${inbound.nackCount || 0} fir=${inbound.firCount || 0} frame=${inbound.frameWidth || 0}x${inbound.frameHeight || 0}`);
                        lines.push(`decoder=${inbound.decoderImplementation || '?'} power=${inbound.powerEfficientDecoder}`);
                        lines.push(`codec=${codecs.get(inbound.codecId) || inbound.codecId || '?'}`);
                    } else {
                        lines.push('no inbound-rtp video report');
                    }
                }
            } catch (error) { lines.push('getStats: ' + String(error).slice(0, 80)); }
            diag.textContent = lines.join('\n');
        };
        const startDiag = () => {
            if (diagTimer !== null || closed) { return; }
            diag.hidden = false;
            updateDiag();
            diagTimer = popup.setInterval(updateDiag, 1000);
        };
        const stopDiag = () => {
            if (diagPinned) { return; }
            if (diagTimer !== null) { popup.clearInterval(diagTimer); diagTimer = null; }
            diag.hidden = true;
        };
        const toggleDiag = () => {
            diagPinned = !diagPinned;
            if (diagPinned) { startDiag(); }
            else { diagPinned = false; if (diagTimer !== null) { popup.clearInterval(diagTimer); diagTimer = null; } diag.hidden = true; }
        };
        // Guest sleep detection: an all-black decoded picture for longer than
        // GUEST_SLEEP_HINT_MS means the guest output is off, not that the
        // transport failed.  The monitor owns the wake hint and clears it
        // as soon as a non-black frame is decoded again.
        let livenessTimer = null;
        let livenessCanvas = null;
        let livenessContext = null;
        let darkFrameSince = null;
        let guestAsleepHint = false;
        let toolbarTimer = null;
        let toolbarRevealTimer = null;
        let toolbarVisible = true;
        // Pinned by the touch reveal handle: the hover-based auto-hide must not
        // apply on touchscreens, where there is no hover to bring it back.
        let toolbarPinned = false;
        let pointerInToolbarZone = false;
        let pointerInToolbar = false;
        let pointerNearToolbar = false;
        let settingsPanelOpen = false;
        let filesPanelOpen = false;
        const consolePanelOpen = () => settingsPanelOpen || filesPanelOpen;
        let guestRequestNumber = 0;
        let lastGuestClipboardText = null;
        let guestClipboardEventSequence = 0;
        const guestRequests = new Map();
        const guestClipboardWaiters = new Set();
        const guestCursorShapes = new Map();
        let latestGuestCursor = null;
        let appliedGuestCursor = 'default';
        // ``qsm-guest-cursor`` is intentionally unordered.  A delayed first
        // MouseSet is commonly (0,0); without a sequence guard it could land
        // after the actual pointer position and pin a resize/edge cursor in
        // the top-left corner of the guest image.
        let latestGuestCursorSequence = -1;
        let lastResize = '';
        // The last guest scanout size this console actually saw applied.  It
        // distinguishes "the guest is ignoring my resize (still my previous
        // size)" — which should keep retrying — from "another console resized
        // the shared guest" (a new size that is neither my request nor my
        // previous one) — which this console must FOLLOW rather than fight, so
        // the console that is being stretched stays the reference and does not
        // end up showing a scaled picture because a background console kept
        // re-asserting its own smaller size.
        let lastAppliedSize = '';
        let mediaPolicyLoading = false;
        const cancelToolbarReveal = () => {
            if (toolbarRevealTimer !== null) {
                popup.clearTimeout(toolbarRevealTimer);
                toolbarRevealTimer = null;
            }
        };
        const setToolbarHandleLabel = () => {
            // The handle stays at the screen's left edge. The strip is inset
            // by 32px, so controls never overlap this fixed hit target.
            const label = toolbarVisible ? gettext('Hide console controls') : gettext('Show console controls');
            revealHandle.setAttribute('aria-label', label);
            revealHandle.setAttribute('aria-expanded', String(toolbarVisible));
            revealHandle.title = label;
        };
        setToolbarHandleLabel();
        const revealToolbar = () => {
            if (closed) { return; }
            cancelToolbarReveal();
            if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); toolbarTimer = null; }
            toolbarVisible = true;
            toolbar.style.opacity = '1';
            toolbar.style.transform = 'translateX(0)';
            setToolbarHandleLabel();
        };
        const hideToolbarNow = () => {
            toolbarVisible = false;
            toolbar.style.opacity = '0';
            toolbar.style.transform = 'translateX(calc(-100% - 32px))';
            setToolbarHandleLabel();
        };
        // The handle pins the toolbar open (touch has no hover to re-reveal it);
        // a second tap unpins and hides it, giving the guest the full screen.
        revealHandle.addEventListener('click', () => {
            toolbarPinned = !toolbarVisible;
            if (toolbarPinned) {
                revealToolbar();
                revealHandle.style.background = 'rgba(29,78,216,.9)';
            } else {
                revealHandle.style.background = 'rgba(17,24,39,.55)';
                hideToolbarNow();
            }
        });
        const hideToolbarSoon = () => {
            if (closed || toolbarPinned || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA ||
                pointerNearToolbar || pointerInToolbar || consolePanelOpen() || toolbarTimer !== null) { return; }
            toolbarTimer = popup.setTimeout(() => {
                toolbarTimer = null;
                if (toolbarPinned || pointerNearToolbar || pointerInToolbar || consolePanelOpen()) { return; }
                hideToolbarNow();
            }, settings.toolbarHideDelayMs);
        };
        const observeToolbarZone = (event) => {
            const box = video.getBoundingClientRect();
            const inside = event.clientX >= box.left && event.clientX < box.left + settings.toolbarHotZonePx;
            pointerInToolbarZone = inside;
            // The timeout measures continuous absence from the controls and
            // their immediate vicinity, not absence of mouse events. Moving
            // the pointer around the remote desktop therefore cannot keep a
            // visible toolbar alive indefinitely, while an intentional move
            // toward its left edge never hides it just before a click.
            const near = event.clientX >= box.left &&
                event.clientX <= box.left + 32 + toolbar.offsetWidth + settings.toolbarNearbyPx &&
                event.clientY >= box.top + 12 - settings.toolbarNearbyPx &&
                event.clientY <= box.top + 12 + toolbar.offsetHeight + settings.toolbarNearbyPx;
            pointerNearToolbar = near;
            if (near) {
                if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); toolbarTimer = null; }
            }
            if (!inside) {
                cancelToolbarReveal();
                hideToolbarSoon();
                return;
            }
            if (toolbarVisible) {
                if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); toolbarTimer = null; }
                return;
            }
            if (toolbarRevealTimer === null) {
                toolbarRevealTimer = popup.setTimeout(() => {
                    toolbarRevealTimer = null;
                    if (pointerInToolbarZone && !closed) { revealToolbar(); }
                }, settings.toolbarRevealDelayMs);
            }
        };
        toolbar.addEventListener('pointerenter', () => {
            pointerInToolbar = true;
            pointerNearToolbar = true;
            revealToolbar();
        });
        toolbar.addEventListener('pointerleave', () => {
            pointerInToolbar = false;
            pointerNearToolbar = false;
            hideToolbarSoon();
        });
        const send = (value) => {
            if (!control || control.readyState !== 'open') { return false; }
            try { control.send(JSON.stringify(value)); return true; }
            catch (_error) { return false; }
        };
        let keyboardLockGeneration = 0;
        let fullscreenCaptureRequested = false;
        let fullscreenKeyboardLock = false;
        let fullscreenRequestPending = false;
        let fullscreenRequestGeneration = 0;
        let keyboardCaptureSuspended = false;
        const keyboardController = () => {
            let keyboard;
            try { keyboard = popup.navigator && popup.navigator.keyboard; }
            catch (_error) { return null; }
            try { return popup.top.navigator.keyboard || keyboard; }
            catch (_error) { return keyboard; }
        };
        const setKeyboardCaptureLabel = () => {
            const active = Boolean(document.fullscreenElement && fullscreenCaptureRequested);
            const label = gettext('Capture keyboard (full screen)');
            captureKeyboard.textContent = '🔒';
            captureKeyboard.title = label;
            captureKeyboard.setAttribute('aria-label', label);
            captureKeyboard.setAttribute('aria-pressed', String(active));
            captureKeyboard.style.background = active ? '#1d4ed8' : '';
            captureKeyboard.disabled = fullscreenRequestPending;
            captureKeyboardSettings.textContent = label;
            captureKeyboardSettings.disabled = fullscreenRequestPending;
            releaseKeyboardSettings.disabled = !active && !fullscreenRequestPending;
        };
        const exitConsoleFullscreen = () => {
            releaseHeldInput();
            if (!document.fullscreenElement) { return; }
            const action = document.exitFullscreen();
            if (action && typeof action.catch === 'function') {
                action.catch(() => {
                    keyboardCaptureStatus.textContent = gettext('Could not exit full screen. Hold Esc to leave keyboard capture.');
                });
            }
        };
        const releaseGuestKeyboardLock = () => {
            keyboardCaptureSuspended = false;
            fullscreenRequestGeneration += 1;
            keyboardLockGeneration += 1;
            const keyboard = keyboardController();
            if (keyboard && typeof keyboard.unlock === 'function') {
                try { keyboard.unlock(); } catch (_error) { /* A closed document has already lost its lock. */ }
            }
            // Firefox/Safari tie their lock to the Fullscreen API. Unlike
            // Chromium's keyboard.unlock(), releasing it requires leaving
            // the full-screen session that this console requested.
            if (fullscreenCaptureRequested && document.fullscreenElement) {
                restoreClientFocusAfterFullscreen = true;
                exitConsoleFullscreen();
            }
            fullscreenCaptureRequested = false;
            fullscreenKeyboardLock = false;
            setKeyboardCaptureLabel();
        };
        const suspendGuestKeyboardCapture = () => {
            // Host app/tab switching is a temporary loss of input authority,
            // not an explicit request to leave the console's fullscreen.
            // In particular, Firefox/Safari's fullscreen-bound lock must not
            // be released by exitFullscreen() on Alt/Cmd+Tab.
            releaseHeldInput();
            keyboardCaptureSuspended = true;
            keyboardLockGeneration += 1;
            const keyboard = keyboardController();
            if (keyboard && typeof keyboard.unlock === 'function') {
                try { keyboard.unlock(); } catch (_error) { /* Window may be closing. */ }
            }
            keyboardCaptureStatus.textContent = gettext('Keyboard input paused while the console is unfocused. Full screen is preserved; return to resume.');
        };
        const resumeGuestKeyboardCapture = () => {
            if (closed || document.hidden || (typeof document.hasFocus === 'function' && !document.hasFocus())) { return; }
            keyboardCaptureSuspended = false;
            // Never manufacture held keys/buttons or request fullscreen on
            // return. Only restore capture inside an existing full screen.
            updateGuestKeyboardLock();
        };
        const updateGuestKeyboardLock = () => {
            if (!document.fullscreenElement && !fullscreenRequestPending) {
                fullscreenCaptureRequested = false;
                fullscreenKeyboardLock = false;
            }
            setKeyboardCaptureLabel();
            if (closed) { releaseGuestKeyboardLock(); return; }
            const generation = ++keyboardLockGeneration;
            const keyboard = keyboardController();
            const guestFocus = document.activeElement === video || document.activeElement === kbInput;
            if (settings.keyboardPriority === KEYBOARD_PRIORITY.client) {
                releaseGuestKeyboardLock();
                keyboardCaptureStatus.textContent = gettext('Client keyboard shortcuts active. Click Capture keyboard to return to the guest.');
                return;
            }
            if (consolePanelOpen()) {
                // Settings takes DOM focus and releases held guest input.
                // Keep Firefox/Safari's fullscreen-bound capture alive; only
                // explicit Release / Client first / close should exit it.
                releaseHeldInput();
                if (keyboard && typeof keyboard.unlock === 'function') {
                    try { keyboard.unlock(); } catch (_error) { /* Document may be closing. */ }
                }
                keyboardCaptureStatus.textContent = document.fullscreenElement && fullscreenCaptureRequested
                    ? gettext('Guest keyboard input paused while Settings is open. Full screen is preserved; close Settings to resume.')
                    : gettext('Guest first forwards received keys. Click Capture keyboard to enter full screen and capture browser shortcuts.');
                return;
            }
            if (keyboardCaptureSuspended || document.hidden ||
                (typeof document.hasFocus === 'function' && !document.hasFocus())) {
                suspendGuestKeyboardCapture();
                return;
            }
            if (document.fullscreenElement && fullscreenKeyboardLock) {
                keyboardCaptureStatus.textContent = guestFocus
                    ? gettext('Keyboard capture active. Hold Esc or click Release keyboard to exit full screen.')
                    : gettext('Keyboard capture paused — click the guest picture to resume, or Release keyboard to exit.');
                return;
            }
            if (keyboard && typeof keyboard.lock === 'function') {
                if (document.fullscreenElement && guestFocus) {
                    try {
                        // Chromium's separate API still needs the top-level
                        // owner for a same-origin embedded console.
                        Promise.resolve(keyboard.lock()).then(() => {
                            if (generation === keyboardLockGeneration) {
                                keyboardCaptureStatus.textContent = gettext('Keyboard capture active for all available keys. Hold Esc or click Release keyboard to exit full screen.');
                            }
                        }).catch(() => {
                            if (generation === keyboardLockGeneration) {
                                keyboardCaptureStatus.textContent = gettext('Keyboard capture was denied. Allow keyboard capture in the browser or use Send to guest buttons below.');
                            }
                        });
                    } catch (_error) {
                        keyboardCaptureStatus.textContent = gettext('Keyboard capture is unavailable. Use Send to guest buttons below.');
                    }
                } else {
                    if (typeof keyboard.unlock === 'function') { keyboard.unlock(); }
                    keyboardCaptureStatus.textContent = gettext('Click Capture keyboard and focus the guest picture to capture available browser keys.');
                }
            } else {
                keyboardCaptureStatus.textContent = document.fullscreenElement && fullscreenCaptureRequested
                    ? gettext('This browser did not enable keyboard capture. Use Send to guest buttons or update the browser.')
                    : gettext('Click Capture keyboard to enter full screen with guest keys. F11 is not required.');
            }
        };
        const enterConsoleFullscreen = (capture = true) => {
            if (fullscreenRequestPending || closed) { return; }
            keyboardCaptureSuspended = false;
            releaseHeldInput();
            if (capture && settings.keyboardPriority === KEYBOARD_PRIORITY.client) {
                settings.keyboardPriority = KEYBOARD_PRIORITY.guest;
                keyboardPriorityInput.value = settings.keyboardPriority;
                applyConsoleSettings();
            }
            setSettingsPanelOpen(false);
            video.focus({ preventScroll: true });
            fullscreenRequestPending = true;
            const requestGeneration = ++fullscreenRequestGeneration;
            fullscreenCaptureRequested = capture;
            let optionRead = false;
            // WebIDL only reads known dictionary members. This detects the
            // Firefox/Safari API without browser sniffing or assuming that
            // a resolved ordinary fullscreen request has locked keys.
            const options = capture ? {
                get keyboardLock() { optionRead = true; return 'browser'; },
            } : { keyboardLock: 'none' };
            try {
                const action = document.documentElement.requestFullscreen(options);
                Promise.resolve(action).then(() => {
                    fullscreenRequestPending = false;
                    fullscreenKeyboardLock = capture && optionRead && Boolean(document.fullscreenElement);
                    if (requestGeneration !== fullscreenRequestGeneration || closed ||
                        settings.keyboardPriority === KEYBOARD_PRIORITY.client) {
                        // An explicitly cancelled request can complete late. It
                        // still owns this newly entered fullscreen session.
                        fullscreenCaptureRequested = capture;
                        releaseGuestKeyboardLock(); return;
                    }
                    if (!consolePanelOpen() && !keyboardCaptureSuspended && !document.hidden &&
                        (typeof document.hasFocus !== 'function' || document.hasFocus())) {
                        video.focus({ preventScroll: true });
                    }
                    updateGuestKeyboardLock();
                }).catch(() => {
                    fullscreenRequestPending = false;
                    fullscreenCaptureRequested = false;
                    fullscreenKeyboardLock = false;
                    setKeyboardCaptureLabel();
                    keyboardCaptureStatus.textContent = gettext('Full-screen keyboard capture was denied or is unsupported. Allow full screen in the browser or use Send to guest buttons.');
                });
            } catch (_error) {
                fullscreenRequestPending = false;
                fullscreenCaptureRequested = false;
                fullscreenKeyboardLock = false;
                keyboardCaptureStatus.textContent = gettext('This browser cannot enter full-screen keyboard capture. Use Send to guest buttons.');
            }
            setKeyboardCaptureLabel();
        };
        const captureGuestKeyboard = () => {
            if (fullscreenRequestPending || closed) { return; }
            if (document.fullscreenElement && fullscreenCaptureRequested) {
                // Capture is idempotent. Focus changes before a click must
                // never turn an enable/resume action into fullscreen exit.
                keyboardCaptureSuspended = false;
                releaseHeldInput();
                setSettingsPanelOpen(false);
                video.focus({ preventScroll: true });
                updateGuestKeyboardLock();
            } else {
                enterConsoleFullscreen(true);
            }
        };
        captureKeyboard.addEventListener('click', captureGuestKeyboard);
        captureKeyboardSettings.addEventListener('click', captureGuestKeyboard);
        releaseKeyboardSettings.addEventListener('click', releaseGuestKeyboardLock);
        for (const element of [video, kbInput]) {
            element.addEventListener('focus', updateGuestKeyboardLock);
            element.addEventListener('blur', updateGuestKeyboardLock);
        }
        // Fullscreen capture can be requested before media connects. Its
        // lifecycle must therefore be independent of the WebRTC listeners.
        popup.addEventListener('blur', suspendGuestKeyboardCapture);
        popup.addEventListener('focus', resumeGuestKeyboardCapture);
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) { suspendGuestKeyboardCapture(); }
            else { resumeGuestKeyboardCapture(); }
        });
        updateGuestKeyboardLock();
        const bytesToB64 = (bytes) => {
            let binary = '';
            const view = new Uint8Array(bytes);
            for (let index = 0; index < view.length; index += 0x8000) {
                binary += String.fromCharCode(...view.subarray(index, index + 0x8000));
            }
            return btoa(binary);
        };
        const b64ToBytes = (encoded) => {
            const binary = atob(encoded);
            const result = new Uint8Array(binary.length);
            for (let index = 0; index < binary.length; index += 1) { result[index] = binary.charCodeAt(index); }
            return result;
        };
        const hideGuestCursor = () => {
            guestCursor.style.display = 'none';
            guestPointer.style.display = 'none';
            if (appliedGuestCursor !== 'default') {
                appliedGuestCursor = 'default';
                video.style.cursor = appliedGuestCursor;
            }
        };
        const guestContentBox = () => {
            const box = video.getBoundingClientRect();
            const sourceWidth = Math.max(1, remoteVideoWidth() || Math.floor(box.width));
            const sourceHeight = Math.max(1, remoteVideoHeight() || Math.floor(box.height));
            const scale = Math.min(box.width / sourceWidth, box.height / sourceHeight);
            return {
                box, sourceWidth, sourceHeight, scale,
                left: box.left + (box.width - sourceWidth * scale) / 2,
                top: box.top + (box.height - sourceHeight * scale) / 2,
            };
        };
        // ---- Client-side zoom / pan (touch) ------------------------------
        // A view transform on the video magnifies part of the guest for
        // precise touch targeting.  transform-origin is 0,0 so the
        // screen<->guest mapping stays analytic (screen = t + scale*local,
        // where local is the object-fit:contain viewport space); the
        // pointer math undoes it.  Off (scale 1) leaves behaviour identical
        // to the un-zoomed console.
        let zoomScale = 1;
        let zoomTx = 0;
        let zoomTy = 0;
        const MIN_ZOOM = 1;
        const MAX_ZOOM = 6;
        // True while the last input came from touch: the guest cursor is then
        // drawn as an on-screen overlay (there is no OS cursor to style, and
        // VirGL does not composite the cursor into the video).
        let usingTouchInput = false;
        // The last guest coordinate we drove the pointer to.  On touch the
        // overlay falls back to a synthetic arrow here whenever the guest has
        // not (yet) streamed its own cursor bitmap, so a cursor is ALWAYS
        // visible where the finger acted, not only once the guest reports.
        let lastSentGuestX = null;
        let lastSentGuestY = null;
        const SYNTHETIC_CURSOR = 'data:image/svg+xml,' + encodeURIComponent(
            "<svg xmlns='http://www.w3.org/2000/svg' width='12' height='19' viewBox='0 0 12 19'>" +
            "<path d='M0 0 L0 16 L4 12 L6.5 17.5 L8.7 16.5 L6.1 11 L11 11 Z' fill='white' stroke='black' stroke-width='1' stroke-linejoin='round'/></svg>");
        const SYNTHETIC_CURSOR_W = 20; // on-screen px (fixed; fallback arrow)
        const SYNTHETIC_CURSOR_H = 31;
        // object-fit:contain letterbox metrics (screen px per guest px = contain).
        const viewMetrics = () => {
            const viewW = popup.innerWidth || video.clientWidth || 1;
            const viewH = popup.innerHeight || video.clientHeight || 1;
            const sw = Math.max(1, remoteVideoWidth() || viewW);
            const sh = Math.max(1, remoteVideoHeight() || viewH);
            const raw = Math.min(viewW / sw, viewH / sh);
            const contain = Number.isFinite(raw) && raw > 0 ? raw : 1;
            return { viewW, viewH, sw, sh, contain, contentLeft: (viewW - sw * contain) / 2, contentTop: (viewH - sh * contain) / 2 };
        };
        // guest px -> on-screen px (inverse of pointerForMouseEvent).
        const guestToScreen = (gx, gy) => {
            const m = viewMetrics();
            const localX = gx * m.contain + m.contentLeft;
            const localY = gy * m.contain + m.contentTop;
            return { x: zoomTx + zoomScale * localX, y: zoomTy + zoomScale * localY };
        };
        const clampZoom = () => {
            const viewW = popup.innerWidth || video.clientWidth || 1;
            const viewH = popup.innerHeight || video.clientHeight || 1;
            zoomScale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoomScale));
            // Keep the (letterboxed) video covering the viewport: no gap.
            zoomTx = Math.max(viewW * (1 - zoomScale), Math.min(0, zoomTx));
            zoomTy = Math.max(viewH * (1 - zoomScale), Math.min(0, zoomTy));
            if (zoomScale <= 1.0001) { zoomScale = 1; zoomTx = 0; zoomTy = 0; }
        };
        const applyViewTransform = () => {
            if (zoomScale === 1 && zoomTx === 0 && zoomTy === 0) {
                video.style.transform = '';
                video.style.transformOrigin = '';
            } else {
                video.style.transformOrigin = '0 0';
                video.style.transform = `translate(${zoomTx}px, ${zoomTy}px) scale(${zoomScale})`;
            }
            diagZoom = zoomScale.toFixed(2);
            exactCanvas.style.transform = video.style.transform;
            exactCanvas.style.transformOrigin = video.style.transformOrigin;
            videoCanvas.style.transform = video.style.transform;
            videoCanvas.style.transformOrigin = video.style.transformOrigin;
            composedCanvas.style.transform = video.style.transform;
            composedCanvas.style.transformOrigin = video.style.transformOrigin;
            syncExactGeometry();
            placeGuestCursor();
        };
        const zoomAroundPoint = (mx, my, newScale) => {
            const s0 = zoomScale || 1;
            const lx = (mx - zoomTx) / s0;
            const ly = (my - zoomTy) / s0;
            zoomScale = newScale;
            zoomTx = mx - newScale * lx;
            zoomTy = my - newScale * ly;
            clampZoom();
            applyViewTransform();
        };
        // Re-clamp the pan and re-place the guest cursor after the viewport or
        // the guest scanout changes size (full screen, window resize, guest
        // mode switch), so a magnified view never leaves a gap.
        const reflowView = () => { clampZoom(); applyViewTransform(); };
        const placeGuestCursor = () => {
            const state = latestGuestCursor;
            const shape = state && guestCursorShapes.get(state.shape_id);
            const guestReady = !!(state && state.visible && shape && remoteVideoWidth() && remoteVideoHeight());
            if (state && !state.visible) {
                guestCursor.style.display = 'none';
                guestPointer.style.display = 'none';
                appliedGuestCursor = 'none';
                video.style.cursor = 'none';
                return;
            }
            if (usingTouchInput) {
                // Touch: there is no OS cursor to style and VirGL does not draw
                // the cursor into the video, so render an overlay locally.
                // Prefer the guest's bitmap at the newest local input position;
                // fall back to a synthetic arrow at the last
                // position we drove the pointer to, so a cursor is never missing
                // while the guest has not streamed its shape yet.
                guestCursor.style.display = 'none';
                const scale = viewMetrics().contain * zoomScale;
                let img;
                let boxW;
                let boxH;
                let offX;
                let offY;
                let gxp;
                let gyp;
                if (guestReady) {
                    img = shape.cursor_url;
                    boxW = Math.max(1, shape.width * scale);
                    boxH = Math.max(1, shape.height * scale);
                    offX = shape.hotspot_x * scale;
                    offY = shape.hotspot_y * scale;
                    gxp = lastSentGuestX ?? state.x; gyp = lastSentGuestY ?? state.y;
                } else if (lastSentGuestX !== null && remoteVideoWidth()) {
                    img = SYNTHETIC_CURSOR;
                    boxW = SYNTHETIC_CURSOR_W;      // fixed on-screen size: the
                    boxH = SYNTHETIC_CURSOR_H;      // fallback is an approximation.
                    offX = 0; offY = 0;             // tip at top-left.
                    gxp = lastSentGuestX; gyp = lastSentGuestY;
                } else {
                    guestPointer.style.display = 'none';
                    return;
                }
                const hot = guestToScreen(gxp, gyp);
                guestPointer.style.backgroundImage = `url("${img}")`;
                guestPointer.style.width = `${boxW}px`;
                guestPointer.style.height = `${boxH}px`;
                guestPointer.style.left = `${hot.x - offX}px`;
                guestPointer.style.top = `${hot.y - offY}px`;
                guestPointer.style.display = 'block';
                if (appliedGuestCursor !== 'default') { appliedGuestCursor = 'default'; video.style.cursor = 'default'; }
                return;
            }
            // Desktop: the browser/OS draws the cursor from CSS.  A canvas
            // overlay was avoided here because it visibly jumps at a native
            // window edge when only one guest pixel moves; the OS cursor does not.
            guestPointer.style.display = 'none';
            if (!guestReady) { hideGuestCursor(); return; }
            guestCursor.style.display = 'none';
            const cursor = `url("${shape.cursor_url}") ${shape.hotspot_x} ${shape.hotspot_y}, default`;
            if (cursor !== appliedGuestCursor) {
                appliedGuestCursor = cursor;
                video.style.cursor = cursor;
            }
        };
        const acceptGuestCursorShape = (message) => {
            if (!Number.isSafeInteger(message.shape_id) || message.shape_id <= 0 ||
                !Number.isInteger(message.width) || !Number.isInteger(message.height) ||
                !Number.isInteger(message.hotspot_x) || !Number.isInteger(message.hotspot_y) ||
                message.width < 1 || message.width > 64 || message.height < 1 || message.height > 64 ||
                message.hotspot_x < 0 || message.hotspot_x >= message.width ||
                message.hotspot_y < 0 || message.hotspot_y >= message.height ||
                typeof message.bgra_b64 !== 'string') { return; }
            let bgra;
            try { bgra = b64ToBytes(message.bgra_b64); } catch (_error) { return; }
            if (bgra.length !== message.width * message.height * 4) { return; }
            guestCursor.width = message.width;
            guestCursor.height = message.height;
            const context = guestCursor.getContext('2d', { alpha: true });
            if (!context) { return; }
            const rgba = context.createImageData(message.width, message.height);
            // Display1's pixman ARGB word is stored as BGRA bytes on the
            // little-endian PVE hosts we support. Canvas expects RGBA.
            for (let index = 0; index < bgra.length; index += 4) {
                rgba.data[index] = bgra[index + 2];
                rgba.data[index + 1] = bgra[index + 1];
                rgba.data[index + 2] = bgra[index];
                rgba.data[index + 3] = bgra[index + 3];
            }
            context.putImageData(rgba, 0, 0);
            guestCursorShapes.set(message.shape_id, {
                width: message.width, height: message.height,
                hotspot_x: message.hotspot_x, hotspot_y: message.hotspot_y,
                cursor_url: guestCursor.toDataURL('image/png'),
            });
            // A bridge sends each image once and reuses its id, so the cache is
            // what makes a change of cursor free. It mirrors this eviction order.
            while (guestCursorShapes.size > 32) {
                guestCursorShapes.delete(guestCursorShapes.keys().next().value);
            }
            placeGuestCursor();
        };
        const acceptGuestCursorPosition = (message) => {
            if (!Number.isSafeInteger(message.sequence) || !Number.isSafeInteger(message.shape_id) ||
                !Number.isInteger(message.x) || !Number.isInteger(message.y) ||
                typeof message.visible !== 'boolean') { return; }
            if (message.sequence < latestGuestCursorSequence) { return; }
            latestGuestCursorSequence = message.sequence;
            latestGuestCursor = {
                sequence: message.sequence, shape_id: message.shape_id,
                visible: message.visible, x: message.x, y: message.y,
            };
            placeGuestCursor();
        };
        const guestRequest = (op, fields = {}) => new Promise((resolve, reject) => {
            if (!control || control.readyState !== 'open') {
                reject(new Error('guest tools are not connected')); return;
            }
            const requestId = `qsm-${Date.now()}-${guestRequestNumber += 1}`;
            const timer = popup.setTimeout(() => {
                guestRequests.delete(requestId);
                reject(new Error('guest tools did not respond'));
            }, 40000);
            guestRequests.set(requestId, { resolve, reject, timer });
            control.send(JSON.stringify({ op, request_id: requestId, ...fields }));
        });
        const applyConsoleSettings = () => {
            writeConsoleSettings(browserStorage, settings);
            updateExactPreference();
            refreshShortcutEditor();
            keyInputModeInput.disabled = settings.keyboardPriority === KEYBOARD_PRIORITY.raw;
            keyInputModeInput.value = keyInputModeInput.disabled ? KEY_INPUT_MODE.physical : settings.keyInputMode;
            mouseInputModeInput.disabled = settings.keyboardPriority === KEYBOARD_PRIORITY.raw;
            mouseInputModeInput.value = mouseInputModeInput.disabled ? MOUSE_INPUT_MODE.physical : settings.mouseInputMode;
            updateGuestKeyboardLock();
            applyReceiverPlayoutDelay(videoReceiver, settings.playoutDelayMs);
            if (toolbarTimer !== null) {
                popup.clearTimeout(toolbarTimer);
                toolbarTimer = null;
                hideToolbarSoon();
            }
        };
        const setSettingsPanelOpen = (open) => {
            const wasOpen = settingsPanelOpen;
            settingsPanelOpen = open;
            settingsPanel.style.display = open ? 'block' : 'none';
            if (open) {
                settingsPanel.scrollTop = 0;
                captureKeyboardSettings.focus({ preventScroll: true });
                revealToolbar();
                loadVmMediaPolicy();
            } else {
                if (wasOpen) { video.focus({ preventScroll: true }); }
                hideToolbarSoon();
            }
            updateGuestKeyboardLock();
        };
        const commitSetting = (name) => {
            const input = settingInputs.get(name);
            const definition = CONSOLE_SETTING_SCHEMA[name];
            if (!input || !definition) { return; }
            const value = Number(input.value);
            if (!Number.isInteger(value) || value < definition.minimum || value > definition.maximum) {
                input.value = String(settings[name]);
                return;
            }
            settings[name] = value;
            applyConsoleSettings();
        };
        for (const [name, input] of settingInputs) {
            input.addEventListener('change', () => commitSetting(name));
        }
        keyboardPriorityInput.addEventListener('change', () => {
            const value = keyboardPriorityInput.value;
            if (!Object.values(KEYBOARD_PRIORITY).includes(value)) {
                keyboardPriorityInput.value = settings.keyboardPriority;
                return;
            }
            releaseHeldInput();
            settings.keyboardPriority = value;
            clearShortcutEdit();
            applyConsoleSettings();
        });
        keyInputModeInput.addEventListener('change', () => {
            if (!Object.values(KEY_INPUT_MODE).includes(keyInputModeInput.value)) {
                keyInputModeInput.value = settings.keyInputMode; return;
            }
            releaseHeldInput();
            settings.keyInputMode = keyInputModeInput.value;
            applyConsoleSettings();
        });
        mouseInputModeInput.addEventListener('change', () => {
            if (!Object.values(MOUSE_INPUT_MODE).includes(mouseInputModeInput.value)) { return; }
            releaseHeldInput();
            settings.mouseInputMode = mouseInputModeInput.value;
            applyConsoleSettings();
        });
        resetSettings.addEventListener('click', () => {
            releaseHeldInput();
            Object.assign(settings, defaultConsoleSettings());
            clearShortcutEdit();
            for (const [name, input] of settingInputs) { input.value = String(settings[name]); }
            keyboardPriorityInput.value = settings.keyboardPriority;
            keyInputModeInput.value = settings.keyInputMode;
            applyConsoleSettings();
        });
        settingsButton.addEventListener('click', () => setSettingsPanelOpen(!settingsPanelOpen));
        closeSettings.addEventListener('click', () => setSettingsPanelOpen(false));
        const vmMediaPolicyUrl = () => `/nodes/${encodeURIComponent(node)}/${guestKind}/${encodeURIComponent(vmid)}/qsm-direct-settings`;
        const mediaPolicyRequest = async (method, params) => {
            if (!VIEWER) { return signalRequest(method, vmMediaPolicyUrl(), params); }
            const body = new URLSearchParams(params || {});
            const url = `/api2/json/nodes/${encodeURIComponent(node)}/qemu/${encodeURIComponent(vmid)}/webrtcsettings`;
            const response = await fetch(url, {
                method, credentials: 'same-origin', cache: 'no-store',
                headers: { 'CSRFPreventionToken': VIEWER.csrf },
                ...(method === 'GET' ? {} : { body }),
            });
            if (!response.ok) { throw new Error(`webrtcsettings ${response.status}`); }
            return (await response.json()).data;
        };
        const validVmMediaPolicy = (value) => value && ['auto', 'h264', 'hevc'].includes(value.codec) &&
            ['auto', 'hardware', 'software'].includes(value.encoder) &&
            Number.isInteger(value.bitrate) && value.bitrate >= 500 && value.bitrate <= 100000;
        const loadVmMediaPolicy = async () => {
            if (mediaPolicyLoading || closed) { return; }
            mediaPolicyLoading = true;
            codecInput.disabled = true;
            encoderInput.disabled = true;
            bitrateInput.disabled = true;
            saveMediaPolicy.disabled = true;
            try {
                const value = await mediaPolicyRequest('GET', {});
                if (!validVmMediaPolicy(value)) { throw new Error('invalid VM media policy'); }
                codecInput.value = value.codec;
                encoderInput.value = value.encoder;
                bitrateInput.value = String(value.bitrate / 1000);
            } catch (_error) {
                // A Console-only user may read an older node during a rolling
                // upgrade. Keep the active console usable and make the policy
                // failure explicit instead of pretending a browser-local value
                // was saved to the VM.
                status.textContent = gettext('Guest media policy is unavailable on this node.');
            } finally {
                mediaPolicyLoading = false;
                codecInput.disabled = false;
                encoderInput.disabled = false;
                bitrateInput.disabled = false;
                saveMediaPolicy.disabled = false;
            }
        };
        saveMediaPolicy.addEventListener('click', async () => {
            const bitrateMbit = Number(bitrateInput.value);
            const bitrate = Math.round(bitrateMbit * 1000);
            if (!Number.isFinite(bitrateMbit) || bitrateMbit < 0.5 || bitrateMbit > 100 || !Number.isInteger(bitrate) || bitrate < 500 || bitrate > 100000) {
                status.textContent = gettext('Video bitrate must be between 0.5 and 100 Mbit/s.');
                return;
            }
            if (mediaPolicyLoading || !['auto', 'h264', 'hevc'].includes(codecInput.value) ||
                !['auto', 'hardware', 'software'].includes(encoderInput.value) ||
                (codecInput.value === 'hevc' && encoderInput.value === 'software')) { return; }
            mediaPolicyLoading = true;
            codecInput.disabled = true;
            encoderInput.disabled = true;
            bitrateInput.disabled = true;
            saveMediaPolicy.disabled = true;
            try {
                const value = await mediaPolicyRequest('PUT', {
                    codec: codecInput.value, encoder: encoderInput.value, bitrate,
                });
                if (!validVmMediaPolicy(value)) { throw new Error('invalid saved VM media policy'); }
                encoderInput.value = value.encoder;
                bitrateInput.value = String(value.bitrate / 1000);
                status.textContent = gettext('Guest media policy saved. Close all consoles for this guest and reconnect to apply it.');
            } catch (_error) {
                status.textContent = gettext('Could not save guest media policy. Guest configuration permission is required.');
            } finally {
                mediaPolicyLoading = false;
                codecInput.disabled = false;
                encoderInput.disabled = false;
                bitrateInput.disabled = false;
                saveMediaPolicy.disabled = false;
            }
        });
        const copyToBrowser = async (text) => {
            if (!popup.navigator.clipboard || !popup.navigator.clipboard.writeText) {
                throw new Error('browser clipboard access is unavailable');
            }
            await popup.navigator.clipboard.writeText(text);
        };
        const guestClipboardText = async () => {
            const result = await guestRequest('qsm_guest_clipboard_get');
            if (!result || typeof result.text_b64 !== 'string') {
                throw new Error('invalid guest clipboard');
            }
            return new TextDecoder('utf-8', { fatal: true }).decode(b64ToBytes(result.text_b64));
        };
        const waitForNextGuestClipboard = () => new Promise((resolve, reject) => {
            const baseline = guestClipboardEventSequence;
            const waiter = { baseline, resolve, reject, timer: null };
            // This is a fail-safe for an unavailable desktop session, not a
            // propagation delay. A normal Copy resolves from the guest's
            // concrete clipboard-change event as soon as it is emitted.
            waiter.timer = popup.setTimeout(() => {
                guestClipboardWaiters.delete(waiter);
                reject(new Error('guest clipboard did not change'));
            }, 5000);
            guestClipboardWaiters.add(waiter);
        });
        const resolveGuestClipboardWaiters = (text) => {
            guestClipboardEventSequence += 1;
            for (const waiter of [...guestClipboardWaiters]) {
                if (guestClipboardEventSequence <= waiter.baseline) { continue; }
                guestClipboardWaiters.delete(waiter);
                popup.clearTimeout(waiter.timer);
                waiter.resolve(text);
            }
        };
        const sendGuestChord = (modifiers, key) => {
            if (inputTapsEnabled) {
                send({ op: 'key_tap', key, modifiers });
                return;
            }
            // Send an explicit physical chord.  The reliable control channel
            // preserves these edges, while the worker serializes them to the
            // Display1 keyboard endpoint.
            for (const modifier of modifiers) {
                send({ op: 'keyboard', key: modifier, down: true, modifiers: 0 });
            }
            send({ op: 'keyboard', key, down: true, modifiers: 0 });
            send({ op: 'keyboard', key, down: false, modifiers: 0 });
            for (const modifier of [...modifiers].reverse()) {
                send({ op: 'keyboard', key: modifier, down: false, modifiers: 0 });
            }
        };
        // Ctrl+C, or Ctrl+Shift+C when the user held Shift: terminals copy
        // with Ctrl+Shift+C and read a plain Ctrl+C as an interrupt.
        const sendGuestCopyShortcut = (shift = false) => sendGuestChord(shift ? [29, 42] : [29], 46);
        // Shift+Insert is the standard Linux clipboard paste accelerator in
        // both graphical editors and terminal emulators.  Ctrl+V is a literal
        // control character in Konsole/xterm (shown as ^V), so it cannot be
        // used by a universal remote-desktop Paste action.
        const sendGuestPasteShortcut = () => sendGuestChord([42], 0xd2); // Shift+Insert
        const writePendingClipboard = (textPromise) => {
            if (!popup.navigator.clipboard) {
                return Promise.reject(new Error('browser clipboard access is unavailable'));
            }
            // Clipboard permission is tied to the *initial* click/keydown.
            // Calling writeText only after a WebRTC guest round-trip loses
            // that user activation in Chromium. ClipboardItem accepts a
            // promise, so authorize the write now and resolve its contents
            // only once the guest agent returns the UTF-8 text.
            if (popup.navigator.clipboard.write && typeof popup.ClipboardItem === 'function') {
                const item = new popup.ClipboardItem({
                    'text/plain': Promise.resolve(textPromise).then((text) => new Blob([text], {
                        type: 'text/plain;charset=utf-8',
                    })),
                });
                return popup.navigator.clipboard.write([item]);
            }
            // Older engines may allow writeText after an asynchronous click;
            // retain it as a portable fallback, but never claim that a denied
            // browser permission reached the local clipboard.
            return Promise.resolve(textPromise).then(copyToBrowser);
        };
        const pasteFromBrowser = async () => {
            if (!popup.navigator.clipboard || !popup.navigator.clipboard.readText) {
                throw new Error('browser clipboard access is unavailable');
            }
            const text = await popup.navigator.clipboard.readText();
            return pasteTextIntoGuest(text);
        };
        const pasteTextIntoGuest = async (text) => {
            if (typeof text !== 'string') { throw new Error('browser clipboard is unavailable'); }
            const result = await guestRequest('qsm_guest_clipboard_set', {
                text_b64: bytesToB64(new TextEncoder().encode(text)),
            });
            if (!result || result.applied !== true) {
                throw new Error('guest desktop clipboard bridge is not ready');
            }
            // The result is a concrete acknowledgement from the desktop
            // bridge, not a guessed compositor delay. It is now safe to send
            // the universal Linux Paste accelerator on the ordered channel.
            sendGuestPasteShortcut();
            status.textContent = gettext('Clipboard pasted into guest');
        };
        const guestClipboardToBrowser = async () => {
            await writePendingClipboard(guestClipboardText());
            status.textContent = gettext('Guest clipboard copied');
        };
        const guestSelectionToBrowser = async (shift = false) => {
            // Start the authorized browser write before asking the guest to
            // Register first, then emit Ctrl+C. The subsequent guest event
            // carries the selection after the desktop broker has actually
            // published it; no compositor-duration timer is involved.
            const text = waitForNextGuestClipboard();
            sendGuestCopyShortcut(shift);
            await writePendingClipboard(text);
            status.textContent = gettext('Guest selection copied');
        };
        copy.addEventListener('click', () => {
            // Toolbar Copy is the same operation as Cmd/Ctrl+C: request the
            // focused guest application's current selection, not an old
            // clipboard value captured before the user made that selection.
            video.focus({ preventScroll: true });
            guestSelectionToBrowser().catch(() => {
                status.textContent = gettext('Guest clipboard is unavailable. Install and start QSM Desktop Agent.');
            });
        });
        paste.addEventListener('click', () => {
            // Retain this click's Clipboard API activation while restoring
            // the guest surface before synthesizing the guest Paste action.
            video.focus({ preventScroll: true });
            pasteFromBrowser().catch(() => {
                status.textContent = gettext('Browser clipboard is unavailable.');
            });
        });
        const sendPointer = (value) => {
            if (!pointer || pointer.readyState !== 'open' || pointer.bufferedAmount > 1024) { return false; }
            try { pointer.send(JSON.stringify(value)); }
            catch (_error) { return false; }
            diagPointerSent += 1;
            diagLastInput = value.op || 'pointer';
            return true;
        };
        const exactMouseClicks = () => inputMouseActionsEnabled &&
            settings.mouseInputMode === MOUSE_INPUT_MODE.exact && settings.keyboardPriority !== KEYBOARD_PRIORITY.raw;
        const inputModifiers = (event) => {
            const result = [...physicalHeldKeys, ...deferredModifiers.keys()].filter(key => [29, 42, 54, 56, 0x9d, 0xb8, 0xdb, 0xdc].includes(key));
            if (event) {
                const altGraph = typeof event.getModifierState === 'function' && event.getModifierState('AltGraph');
                for (const [flag, group] of [['ctrlKey', [29, 0x9d]], ['shiftKey', [42, 54]],
                    ['altKey', [56, 0xb8]], ['metaKey', [0xdb, 0xdc]]]) {
                    const active = flag === 'altKey' && altGraph ? true : event[flag];
                    if (active === false) {
                        for (let i = result.length - 1; i >= 0; --i) {
                            if (group.includes(result[i])) { result.splice(i, 1); }
                        }
                    } else if (active === true && !result.some(key => group.includes(key))) { result.push(flag === 'altKey' && altGraph ? 0xb8 : group[0]); }
                }
            }
            return result;
        };
        const sendMouseAction = (button, action, point = latestPointer, event = null) => {
            if (!point || !Number.isInteger(button) || button < 1 || button > 5) { return false; }
            markModifiersUsed();
            const { x, y, width, height } = point;
            const sequence = pointerSequence;
            pointerSequence = (pointerSequence + 1) >>> 0;
            if (!send({ op: 'mouse_action', button, action, sequence, x, y, width, height,
                modifiers: inputModifiers(event) })) { return false; }
            // Until the bridge has queued this action, subsequent coordinates
            // must not overtake it through the unordered pointer channel.
            mouseFenceSequence = sequence;
            if (pointerTimer !== null) { popup.clearTimeout(pointerTimer); pointerTimer = null; }
            pendingPointer = null;
            latestPointer = { op: 'mouse_position', x, y, width, height, sequence };
            diagButtonSent += 1; diagLastInput = 'mouse' + button + ':' + action;
            return true;
        };
        const sendMouseClick = (button, point = latestPointer, event = null) => {
            if (exactMouseClicks()) { return sendMouseAction(button, 2, point, event); }
            sendMouseButton(button, true); sendMouseButton(button, false);
            return true;
        };
        const sendMouseButton = (button, down, point = latestPointer, event = null) => {
            if (!Number.isInteger(button) || button < 1 || button > 5) { return; }
            noteGuestInput();
            if (down === heldMouseButtons.has(button)) { return; }
            if (exactMouseClicks()) {
                if (!sendMouseAction(button, down ? 1 : 0, point, event)) { return; }
            } else {
                if (down) { markModifiersUsed(); flushDeferredModifiers(); }
                if (!send({ op: 'mouse_button', button, down })) { return; }
            }
            if (down) {
                if (heldMouseButtons.has(button)) { return; }
                heldMouseButtons.add(button);
            } else {
                if (!heldMouseButtons.has(button)) { return; }
                heldMouseButtons.delete(button);
            }
            diagButtonSent += 1; diagLastInput = 'button' + button + (down ? '↓' : '↑');
        };
        const sendKeyboard = (key, down, repeat = false, sourceTime = null, sourceEvent = null) => {
            if (!Number.isInteger(key) || key < 0 || key > 0xffff) { return; }
            if (down && sourceEvent && (keyboardCaptureSuspended || document.hidden ||
                (typeof document.hasFocus === 'function' && !document.hasFocus()))) { return; }
            if (!down && deferredModifiers.has(key)) {
                const used = deferredModifiers.get(key);
                const modifiers = inputModifiers(sourceEvent).filter(value => value !== key);
                const active = new Set([key, ...modifiers]);
                const pressOrder = [...deferredModifiers.keys()].filter(value => active.has(value));
                deferredModifiers.delete(key); heldKeys.delete(key);
                // Release completes a modifier-only combination. Match that
                // exact combination, not the prefix of a longer shortcut.
                // A lone ambiguous Alt/Meta still has no evidence of a guest
                // chord when the OS hides Tab; an explicit Guest rule opts in.
                if (sourceEvent && !used && !keyboardCaptureSuspended && !document.hidden &&
                    (typeof document.hasFocus !== 'function' || document.hasFocus())) {
                    markModifiersUsed();
                    const shortcut = modifierShortcutFromKeys([key, ...modifiers]);
                    const rules = shortcutRulesForSettings(settings);
                    const rule = rules.find(item => item.shortcut === shortcut);
                    if (rule && rule.action !== 'guest') {
                        releaseHeldInput();
                        if (rule.action !== 'host') { sourceEvent.preventDefault(); sourceEvent.stopPropagation(); }
                        if (rule.action === 'release') { leaveGuestKeyboardCapture(); }
                        else if (rule.action === 'fullscreen') { fullscreen.click(); }
                        else if (rule.action === 'toolbar') { revealHandle.click(); }
                    } else if (rule?.action === 'guest' || sourceEvent.key === 'AltGraph' ||
                        (shortcut && shortcut.includes('+')) || !isHostShortcutPrefix([key, ...modifiers], rules)) {
                        // The released key commits the action, but must not
                        // become its last pressed key. Keep the observed down
                        // order so Meta-then-Shift cannot turn into Shift-Meta.
                        const tapKey = pressOrder.at(-1) || key;
                        const tapModifiers = [...new Set([
                            ...modifiers.filter(value => !pressOrder.includes(value)), ...pressOrder,
                        ])].filter(value => value !== tapKey);
                        noteGuestInput(); send({ op: 'key_tap', key: tapKey, modifiers: tapModifiers });
                    }
                }
                return;
            }
            const modifier = [29, 42, 54, 56, 0x9d, 0xb8, 0xdb, 0xdc].includes(key);
            const deferModifier = inputTapsEnabled && modifier && settings.keyboardPriority !== KEYBOARD_PRIORITY.raw;
            if (down && deferModifier) {
                if (repeat) { return; }
                if (!heldKeys.has(key)) { heldKeys.add(key); deferredModifiers.set(key, false); }
                return;
            }
            if (down && !modifier) { suppressHostPaste = false; markModifiersUsed(); }
            // Only a repeat of a press started here is eligible. Exact typing
            // sends one complete action per client event; physical main-key
            // holds flush their pending modifiers immediately before the key.
            const atomic = inputTapsEnabled && settings.keyInputMode === KEY_INPUT_MODE.typing &&
                settings.keyboardPriority !== KEYBOARD_PRIORITY.raw && !modifier;
            if (down && repeat && (!atomic || !heldKeys.has(key))) { return; }
            const message = { op: 'keyboard', key, down, modifiers: 0 };
            if (inputStateEnabled && Number.isFinite(sourceTime) && sourceTime >= 0) {
                message.client_time_ms = Math.round(sourceTime);
                message.event_delay_ms = Math.max(0, Math.round(popup.performance.now() - sourceTime));
            }
            noteGuestInput();
            if (down) {
                if (heldKeys.has(key) && !repeat) { return; }
                if (atomic) {
                    delete message.down; delete message.modifiers; message.op = 'key_tap';
                    message.modifiers = inputModifiers(sourceEvent);
                    if (!send(message)) { return; }
                } else {
                    flushDeferredModifiers();
                    if (!send(message)) { return; }
                    physicalHeldKeys.add(key);
                }
                heldKeys.add(key);
            } else {
                if (!heldKeys.has(key)) { return; }
                if (physicalHeldKeys.has(key)) { send(message); physicalHeldKeys.delete(key); }
                heldKeys.delete(key);
            }
            diagKeySent += 1; diagLastInput = 'key' + key + (down ? '↓' : '↑');
        };
        const publishInputState = () => {
            if (!inputStateEnabled) { return; }
            send({ op: 'input_state', keys: [...physicalHeldKeys], buttons: [...heldMouseButtons] });
        };
        const acceptInputPolicy = (message) => {
            // Negotiate first: a new viewer must not send unknown operations
            // to an older bridge during a rolling upgrade.
            if (message.version !== 1 || message.lease_ms !== 200 || message.snapshot_ms !== 50) { return; }
            if (inputStateTimer !== null) { popup.clearInterval(inputStateTimer); }
            inputStateEnabled = true;
            inputTapsEnabled = message.key_taps === true;
            inputMouseActionsEnabled = message.mouse_actions === true;
            if (!inputMouseActionsEnabled) { mouseInputModeHint.textContent = gettext('Exact mouse clicks require an updated QSM host package; physical buttons are active.'); }
            if (!inputTapsEnabled && settings.keyInputMode === KEY_INPUT_MODE.typing) {
                keyInputModeHint.textContent = gettext('Exact typing is unavailable on this server; physical input is active. Update the QSM host package and reopen this console.');
            }
            publishInputState();
            inputStateTimer = popup.setInterval(publishInputState, message.snapshot_ms);
        };
        // Element focus can move to the toolbar/settings without window blur.
        // Release there, and catch keyups even if their new target is another
        // element or stops propagation. Never capture new presses globally.
        const releaseGuestKeys = () => {
            for (const key of [...heldKeys]) { sendKeyboard(key, false); }
        };
        video.addEventListener('blur', releaseGuestKeys);
        kbInput.addEventListener('blur', releaseGuestKeys);
        document.addEventListener('keyup', (event) => {
            if (routeHostShortcut(event)) { return; }
            const key = qemuKey(event, settings.keyboardPriority === KEYBOARD_PRIORITY.raw);
            if (key !== null) { sendKeyboard(key, false, false, event.timeStamp, event); }
        }, true);
        popup.addEventListener('blur', () => hostShortcutKeys.clear());
        const routeHostShortcut = (event) => {
            if (routedShortcutEvents.has(event)) { return true; }
            if (settings.keyboardPriority === KEYBOARD_PRIORITY.raw) { return false; }
            // Modifier-only rules commit on release inside sendKeyboard.
            // On keydown they could still be part of a longer guest chord.
            if (shortcutModifierName(qemuKey(event))) { return false; }
            const latched = hostShortcutKeys.get(event.code);
            const rule = latched || (event.type === 'keydown'
                ? matchShortcutRule(event, shortcutRulesForSettings(settings)) : null);
            if (!rule || rule.action === 'guest') { return false; }
            routedShortcutEvents.add(event);
            if (event.type === 'keyup') { hostShortcutKeys.delete(event.code); }
            else { hostShortcutKeys.set(event.code, rule); }
            // Reserve both main-key edges even if modifiers are released first,
            // and discard the deferred prefix before any guest action is sent.
            markModifiersUsed(); releaseHeldInput();
            if (rule.action === 'host') {
                if (event.type === 'keydown' && event.code === 'KeyV') { suppressHostPaste = true; }
                return true;
            }
            event.preventDefault(); event.stopPropagation();
            if (event.type === 'keydown' && !latched && !event.repeat && !keyboardCaptureSuspended && !document.hidden &&
                (typeof document.hasFocus !== 'function' || document.hasFocus())) {
                if (rule.action === 'release') { leaveGuestKeyboardCapture(); }
                else if (rule.action === 'fullscreen') { fullscreen.click(); }
                else if (rule.action === 'toolbar') { revealHandle.click(); }
            }
            return true;
        };
        // Map a printable character to a US-layout scancode + shift flag, so
        // the on-screen keyboard (which reports typed text, not key codes) can
        // drive the guest.  Built once from the shared scanCodes table.
        const charToKey = (() => {
            const map = new Map();
            const put = (ch, code, shift) => { if (scanCodes[code] !== undefined) { map.set(ch, { key: scanCodes[code], shift: !!shift }); } };
            for (let c = 97; c <= 122; c += 1) { const l = String.fromCharCode(c); put(l, 'Key' + l.toUpperCase(), false); put(l.toUpperCase(), 'Key' + l.toUpperCase(), true); }
            const digits = '0123456789'; for (let i = 0; i < 10; i += 1) { put(digits[i], 'Digit' + digits[i], false); }
            [[')', 'Digit0'], ['!', 'Digit1'], ['@', 'Digit2'], ['#', 'Digit3'], ['$', 'Digit4'], ['%', 'Digit5'], ['^', 'Digit6'], ['&', 'Digit7'], ['*', 'Digit8'], ['(', 'Digit9']].forEach(([ch, code]) => put(ch, code, true));
            [[' ', 'Space'], ['-', 'Minus'], ['=', 'Equal'], ['[', 'BracketLeft'], [']', 'BracketRight'], [';', 'Semicolon'], ["'", 'Quote'], ['`', 'Backquote'], ['\\', 'Backslash'], [',', 'Comma'], ['.', 'Period'], ['/', 'Slash']].forEach(([ch, code]) => put(ch, code, false));
            [['_', 'Minus'], ['+', 'Equal'], ['{', 'BracketLeft'], ['}', 'BracketRight'], [':', 'Semicolon'], ['"', 'Quote'], ['~', 'Backquote'], ['|', 'Backslash'], ['<', 'Comma'], ['>', 'Period'], ['?', 'Slash']].forEach(([ch, code]) => put(ch, code, true));
            return map;
        })();
        const shiftKey = scanCodes.ShiftLeft;
        // Type one character into the guest as a press/release, wrapping it in a
        // Shift press/release when the US layout needs Shift for that glyph.
        const sendChar = (ch) => {
            const mapped = charToKey.get(ch);
            if (!mapped) { return; }
            if (mapped.shift) { sendKeyboard(shiftKey, true); }
            sendKeyboard(mapped.key, true);
            sendKeyboard(mapped.key, false);
            if (mapped.shift) { sendKeyboard(shiftKey, false); }
        };
        // Named keys the on-screen keyboard emits by name rather than as text.
        const namedKeyCode = (name) => {
            const table = { Enter: scanCodes.Enter, Backspace: scanCodes.Backspace, Tab: scanCodes.Tab,
                Escape: scanCodes.Escape, ArrowUp: scanCodes.ArrowUp, ArrowDown: scanCodes.ArrowDown,
                ArrowLeft: scanCodes.ArrowLeft, ArrowRight: scanCodes.ArrowRight, ' ': scanCodes.Space,
                Spacebar: scanCodes.Space };
            return table[name];
        };
        const tapKey = (code) => { if (code === undefined) { return; } sendKeyboard(code, true); sendKeyboard(code, false); };
        // Raise/hide the device keyboard by focusing/blurring the hidden input.
        kbButton.addEventListener('click', () => {
            if (popup.document.activeElement === kbInput) { kbInput.blur(); return; }
            kbInput.value = '';
            try { kbInput.focus({ preventScroll: true }); } catch (_error) { kbInput.focus(); }
            kbButton.style.background = '#1d4ed8';
        });
        kbInput.addEventListener('blur', () => { kbButton.style.background = ''; });
        // Physical/BT keyboards and on-screen keys that report a code map
        // straight through; the rest arrive as text via beforeinput below.
        for (const name of ['keydown', 'keyup']) {
            kbInput.addEventListener(name, (event) => {
                const code = qemuKey(event, settings.keyboardPriority === KEYBOARD_PRIORITY.raw);
                if (routeHostShortcut(event)) { return; }
                if (code !== null) {
                    if (!shortcutModifierName(code) ||
                        matchShortcutRule(event, shortcutRulesForSettings(settings))?.action !== 'host') { event.preventDefault(); }
                    event.stopPropagation();
                    sendKeyboard(code, name === 'keydown', event.repeat, event.timeStamp, event);
                }
                // Keep the field empty so the OS keyboard never shows a caret
                // position that fights composition on the next character.
                kbInput.value = '';
            });
        }
        // beforeinput is the primary path (Gboard/Samsung emit it, cancelable).
        // A suppression flag stops the input fallback below from re-sending the
        // same edit on engines where preventDefault still lets input fire.
        let kbHandledEdit = false;
        const applyEdit = (type, data) => {
            if (type === 'insertText' || type === 'insertCompositionText' || type === 'insertFromComposition') {
                for (const ch of (data || '')) { sendChar(ch); }
            } else if (type === 'deleteContentBackward') { tapKey(scanCodes.Backspace); }
            else if (type === 'deleteContentForward') { tapKey(extendedScanCodes.Delete); }
            else if (type === 'insertLineBreak' || type === 'insertParagraph') { tapKey(scanCodes.Enter); }
            else { return false; }
            return true;
        };
        kbInput.addEventListener('paste', (event) => {
            if (suppressHostPaste) { suppressHostPaste = false; event.preventDefault(); kbInput.value = ''; }
        });
        kbInput.addEventListener('beforeinput', (event) => {
            if (event.inputType === 'insertFromPaste' && suppressHostPaste) { suppressHostPaste = false; event.preventDefault(); kbInput.value = ''; return; }
            kbHandledEdit = applyEdit(event.inputType || '', event.data);
            event.preventDefault();
            kbInput.value = '';
        });
        kbInput.addEventListener('input', (event) => {
            if (kbHandledEdit) { kbHandledEdit = false; kbInput.value = ''; return; }
            applyEdit(event.inputType || '', event.data);
            kbInput.value = '';
        });
        releaseHeldInput = () => {
            // Do not rely on the browser to deliver mouseup/keyup when a
            // native full-screen, focus, or close transition interrupts the
            // DOM sequence.  The ordered control channel preserves these
            // release edges ahead of a transport close.
            cancelPendingMouse();
            for (const button of [...heldMouseButtons]) { sendMouseButton(button, false); }
            for (const key of [...heldKeys]) { sendKeyboard(key, false); }
            publishInputState();
        };
        const leaveGuestKeyboardCapture = () => {
            // The escape chord is intentionally local only in guest-first
            // mode. Releasing modifiers before moving focus prevents a
            // half-held Ctrl/Alt/Shift from affecting the next client action.
            releaseHeldInput();
            if (document.fullscreenElement) {
                // Guest-first deliberately gives ordinary Escape to the VM.
                // The capture-exit chord is therefore also the reliable way
                // to return from a native full-screen guest to client UI.
                restoreClientFocusAfterFullscreen = true;
                const action = document.exitFullscreen();
                if (action && typeof action.catch === 'function') {
                    action.catch(() => {
                        restoreClientFocusAfterFullscreen = false;
                        settingsButton.focus({ preventScroll: true });
                    });
                }
            } else {
                settingsButton.focus({ preventScroll: true });
            }
            status.textContent = gettext('Client keyboard shortcuts active — click the guest picture to resume.');
        };
        const close = () => {
            decodedPresenter.stop();
            exactRenderer.configure(false);
            if (closed) { return; }
            receiveFeedback.close();
            releaseHeldInput();
            closed = true;
            releaseGuestKeyboardLock();
            inputStateEnabled = false;
            if (inputStateTimer !== null) { popup.clearInterval(inputStateTimer); inputStateTimer = null; }
            if (observer) { observer.disconnect(); }
            stopMicrophone();
            if (fileManager) { fileManager.destroy(); }
            if (peer) { peer.close(); }
            if (closeWatcher !== null) { window.clearInterval(closeWatcher); }
            if (pointerTimer !== null) { popup.clearTimeout(pointerTimer); }
            if (resizeTimer !== null) { popup.clearTimeout(resizeTimer); }
            if (resizeRetryTimer !== null) { popup.clearTimeout(resizeRetryTimer); }
            if (firstFrameTimer !== null) { popup.clearTimeout(firstFrameTimer); }
            if (livenessTimer !== null) { popup.clearInterval(livenessTimer); }
            if (diagTimer !== null) { popup.clearInterval(diagTimer); diagTimer = null; }
            if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); }
            if (toolbarRevealTimer !== null) { popup.clearTimeout(toolbarRevealTimer); }
            for (const request of guestRequests.values()) {
                popup.clearTimeout(request.timer);
                request.reject(new Error('console closed'));
            }
            guestRequests.clear();
            for (const waiter of guestClipboardWaiters) {
                popup.clearTimeout(waiter.timer);
                waiter.reject(new Error('console closed'));
            }
            guestClipboardWaiters.clear();
        };
        let transportEndPending = false;
        const closeForStoppedVm = async () => {
            if (closed || transportEndPending) { return; }
            transportEndPending = true;
            let state;
            try { state = await apiValue(vmStatusUrl(), 'GET', {}, null); }
            catch (_error) { /* A failed status read does not prove VM shutdown. */ }
            if (closed) { return; }
            close();
            if (state && state.status === 'stopped') {
                status.textContent = gettext('The virtual machine was stopped. Closing console…');
                closeSurface();
                return;
            }
            status.textContent = state && state.status === 'running'
                ? gettext('WebRTC connection lost. The virtual machine is still running.')
                : gettext('WebRTC connection lost. Could not confirm the virtual machine status.');
            status.style.maxWidth = 'none';
            toolbar.style.opacity = '1';
            toolbar.style.transform = 'none';
            if (VIEWER) {
                const retry = document.createElement('button');
                retry.type = 'button';
                retry.textContent = gettext('Reconnect');
                retry.style.cssText = 'pointer-events:auto;cursor:pointer;padding:6px 10px';
                retry.addEventListener('click', () => popup.location.reload());
                toolbar.append(retry);
            }
        };
        popup.addEventListener('beforeunload', close, { once: true });
        window.addEventListener('beforeunload', close, { once: true });
        if (!embedded) {
            closeWatcher = window.setInterval(() => {
                if (popup.closed) { close(); }
            }, 500);
        }

        const vmStatusUrl = () => `/nodes/${encodeURIComponent(node)}/${guestKind}/${encodeURIComponent(vmid)}/${nativeConsole ? 'webrtcstatus' : 'status/current'}`;
        const waitForVmStart = async () => {
            // Opening Console is a valid action before Power On.  Do not
            // create a single-use WebRTC offer until QEMU exists: PVE would
            // correctly reject that offer, but reporting it as a console
            // failure forces an operator to close and reopen the popup.
            // Keep this window alive and begin negotiation as soon as the
            // normal protected PVE status route reports the VM as running.
            while (!closed) {
                try {
                    const state = await apiValue(vmStatusUrl(), 'GET', {}, null);
                    if (state && state.status === 'running') { return true; }
                    status.textContent = gettext('Virtual machine is stopped. Waiting for it to start…');
                } catch (_error) {
                    // A node that is still finishing its own start-up has no
                    // useful display yet either. Keep the same Console
                    // popup rather than mislabelling a transient status read
                    // as a WebRTC or permission error.
                    status.textContent = gettext('Waiting for virtual machine status…');
                }
                await new Promise((resolve) => popup.setTimeout(resolve, 1000));
            }
            return false;
        };

        // Returns true when the current decoded frame is entirely black,
        // false when any sampled pixel carries light, and null when there is
        // no decodable frame yet.  A 32×18 downscale of the intrinsic video
        // frame is sampled, so CSS letterboxing never counts as darkness.
        // Only exact DPMS black (every channel ≤ 8) qualifies: an encoded
        // dark movie scene keeps small non-zero values and does not trigger.
        const guestFrameIsDark = () => {
            if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA ||
                !remoteVideoWidth() || !remoteVideoHeight()) { return null; }
            try {
                if (!livenessCanvas) {
                    livenessCanvas = document.createElement('canvas');
                    livenessCanvas.width = 32;
                    livenessCanvas.height = 18;
                    livenessContext = livenessCanvas.getContext('2d', { willReadFrequently: true });
                }
                if (!livenessContext) { return null; }
                livenessContext.drawImage(videoRegions.width ? videoCanvas : video, 0, 0, 32, 18);
                if (exactRenderer.width) { livenessContext.drawImage(exactCanvas, 0, 0, 32, 18); }
                const pixels = livenessContext.getImageData(0, 0, 32, 18).data;
                for (let index = 0; index < pixels.length; index += 4) {
                    if (pixels[index] > 8 || pixels[index + 1] > 8 || pixels[index + 2] > 8) { return false; }
                }
                return true;
            } catch (_error) {
                // A same-origin WebRTC stream is readable; if a browser ever
                // refuses the readback the monitor simply stays inert.
                return null;
            }
        };
        const monitorGuestLiveness = () => {
            if (closed) { return; }
            const dark = guestFrameIsDark();
            if (dark === true) {
                if (darkFrameSince === null) { darkFrameSince = Date.now(); }
                if (!guestAsleepHint && Date.now() - darkFrameSince >= GUEST_SLEEP_HINT_MS &&
                    Date.now() - lastInputSentAt > 5000) {
                    guestAsleepHint = true;
                    status.textContent = gettext('Guest display looks asleep. Move the mouse or press a key here to wake it.');
                    revealToolbar();
                }
            } else if (dark === false) {
                darkFrameSince = null;
                if (guestAsleepHint) {
                    guestAsleepHint = false;
                    updateMediaStatus();
                    // The guest just woke: give a stalled size request its
                    // one re-issue now rather than waiting for more input.
                    retryStalledResize();
                }
            }
        };
        const startLivenessMonitor = () => {
            if (livenessTimer !== null || closed) { return; }
            livenessTimer = popup.setInterval(monitorGuestLiveness, 1000);
        };
        const updateMediaStatus = () => {
            if (closed || !peer) { return; }
            if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
                remoteVideoWidth() > 0 && remoteVideoHeight() > 0) {
                // The sleep monitor owns the status line while the guest
                // output is off; a decoded frame alone is not proof of a
                // visible desktop.
                if (!guestAsleepHint) { status.textContent = gettext('Connected'); }
                if (firstFrameTimer !== null) {
                    popup.clearTimeout(firstFrameTimer);
                    firstFrameTimer = null;
                }
                stopDiag();
                startLivenessMonitor();
                hideToolbarSoon();
            } else if (peer.connectionState === 'connected') {
                status.textContent = gettext('Connected — waiting for guest video…');
            }
        };

        const iceConfiguration = async () => {
            if (!signalEndpoint.gateway) { return {}; }
            const credentials = await signalRequest('GET',
                vmMediaPolicyUrl().replace(/qsm-direct-settings$/, 'qsm-direct-ice'));
            const endpoint = new URL(signalOrigin());
            return { iceTransportPolicy: 'relay', iceServers: [{
                urls: [...(credentials.udp_port ? [`turn:${endpoint.hostname}:${credentials.udp_port}?transport=udp`] : []),
                    `turn:${endpoint.hostname}:${endpoint.port || '443'}?transport=tcp`],
                username: credentials.username, credential: credentials.credential,
            }] };
        };

        // Created at the first click: nothing of it exists in an ordinary console.
        let fileManager = null;
        let filesToken = null;
        const openFileManager = () => {
            if (!fileManager) {
                fileManager = new QsmFileManager({
                    win: popup, doc: document,
                    getToken: () => filesToken,
                    createSocket: (token) => new WebSocket(`${signalOrigin().replace(/^http/, 'ws')}${signalEndpoint.gateway ? '/qsm-direct' : ''}/qsm-direct-files/socket/${token}`),
                    demand: new QfmConsoleDemand({
                        videoRate: () => videoRateNow,
                        stats: () => peer.getStats(),
                        now: () => popup.performance.now(),
                    }),
                    offer: (token) => signalRequest('POST',
                        `/nodes/${encodeURIComponent(node)}/${guestKind}/${encodeURIComponent(vmid)}/qsm-direct-files`,
                        { token }),
                    onVisible: (visible) => {
                        filesPanelOpen = visible;
                        if (visible) {
                            setSettingsPanelOpen(false);
                            releaseHeldInput();
                        } else {
                            video.focus({ preventScroll: true });
                        }
                        updateGuestKeyboardLock();
                    },
                    onProgress: (value) => {
                        filesButton.textContent = value === null ? '📁' : value === '!' ? '📁!' : `📁${value}%`;
                    },
                });
            }
            fileManager.toggle();
        };
        filesButton.addEventListener('click', openFileManager);

        const connect = async () => {
            try {
            if (!await waitForVmStart()) { return; }
            peer = new RTCPeerConnection(await iceConfiguration());
            control = peer.createDataChannel('qsm-control', { ordered: true });
            // Cursor positions are latest-state samples. Sending them as a
            // reliable ordered stream lets one congested packet make every
            // later gesture visibly stale, while keys/clicks must remain
            // ordered and reliable on qsm-control.
            pointer = peer.createDataChannel('qsm-pointer', { ordered: false, maxRetransmits: 0 });
            peer.addEventListener('datachannel', (event) => {
                const channel = event.channel;
                if (channel && channel.label === 'qsm-exact-ui' && channel.ordered === true &&
                    channel.maxRetransmits === null && channel.maxPacketLifeTime === null) {
                    channel.binaryType = 'arraybuffer';
                    channel.addEventListener('message', (uiEvent) => {
                        void exactRenderer.receive(uiEvent.data).then(syncExactGeometry);
                    });
                    channel.addEventListener('close', () => exactRenderer.configure(false), { once: true });
                    return;
                }
                if (!channel || channel.label !== 'qsm-guest-cursor' ||
                    channel.ordered !== false || channel.maxRetransmits !== 0) { return; }
                videoFeedbackChannel = channel;
                channel.addEventListener('message', (cursorEvent) => {
                    if (typeof cursorEvent.data !== 'string') { return; }
                    try {
                        const message = JSON.parse(cursorEvent.data);
                        if (message && message.op === 'qsm_guest_cursor') {
                            acceptGuestCursorPosition(message);
                        } else if (message && message.op === 'qsm_video_rate') {
                            acceptVideoRate(message);
                        }
                    } catch (_error) { /* An unordered cursor sample is disposable. */ }
                });
                channel.addEventListener('close', hideGuestCursor, { once: true });
            });
            peer.addEventListener('connectionstatechange', () => {
                if (peer.connectionState === 'failed' || peer.connectionState === 'closed') {
                    closeForStoppedVm();
                } else {
                    updateMediaStatus();
                }
            });
            peer.addEventListener('iceconnectionstatechange', () => {
                if (peer.iceConnectionState === 'failed' || peer.iceConnectionState === 'closed') {
                    closeForStoppedVm();
                }
            });
            peer.ontrack = (event) => {
                // `RTCTrackEvent.streams` is permitted to be empty. aiortc
                // and browser versions disagree on when a remote stream ID
                // is emitted, so attaching only streams[0] produced a
                // connected but black `<video>` on affected Chrome builds.
                // A remote-desktop console must prefer the newest decodable
                // picture over WebRTC's conference-call playout cushion.
                // This is an optional Chromium API, hence feature-detect it
                // rather than making an older browser unable to connect.
                if (event.track.kind === 'video' && event.receiver) {
                    decodedPresenter.stop();
                    presentationRtp = reportedPresentationRtp = null;
                    videoRegions.presented = null; videoRegions.frames.clear();
                    videoReceiver = event.receiver;
                    receiveFeedback.attach(videoReceiver);
                    applyReceiverPlayoutDelay(videoReceiver, settings.playoutDelayMs);
                }
                let stream = event.streams && event.streams[0];
                if (!stream) {
                    stream = video.srcObject instanceof MediaStream
                        ? video.srcObject : new MediaStream();
                    if (!stream.getTracks().some((track) => track.id === event.track.id)) {
                        stream.addTrack(event.track);
                    }
                }
                video.srcObject = stream;
                updateDecodedPresentation();
                event.track.addEventListener('ended', closeForStoppedVm, { once: true });
                video.play().catch(() => undefined);
                updateMediaStatus();
            };
            video.addEventListener('loadeddata', updateMediaStatus);
            video.addEventListener('playing', updateMediaStatus);
            // KWin/SDDM applies a virtio-gpu hotplug event asynchronously.
            // QEMU exposes its new scanout almost immediately, but a second
            // distinct mode sent during that compositor transition can leave
            // the greeter painting its previous canvas into the new buffer.
            // Treat an operating-system resize as an end-of-drag action and
            // serialize mode changes.  The initial/full-screen request still
            // dispatches immediately when no transition is pending.
            const resize = (immediate = false) => {
                const dispatch = () => {
                    resizeTimer = null;
                    const value = dimensions(video, sessionFps);
                    const identity = `${value.width}x${value.height}@${value.fps}`;
                    // In addition to CSS layout, update the media element's
                    // intrinsic presentation box. Chromium allocates the
                    // accelerated video layer from these dimensions on some
                    // platforms; CSS alone can leave the decoded plane at a
                    // previous popup size although getBoundingClientRect()
                    // already reports the new one.
                    if (video.width !== value.width) { video.width = value.width; }
                    if (video.height !== value.height) { video.height = value.height; }
                    if (identity !== lastResize) {
                        const remainingSettle = Math.max(0,
                            lastResizeSentAt + settings.resizeSettleMs - Date.now());
                        if (remainingSettle > 0) {
                            // Read dimensions again when the compositor has
                            // settled: a user may have continued dragging.
                            resizeTimer = popup.setTimeout(dispatch, remainingSettle);
                            return;
                        }
                        if (identity !== resizeRetryIdentity) {
                            // A genuinely new window geometry restarts the
                            // fast budget and the stalled bookkeeping.
                            resizeRetryAttempts = 0;
                            resizeStalled = false;
                            stalledRetryCount = 0;
                            // This is a fresh stretch of THIS console: the guest
                            // size we are changing away from is the current one,
                            // so record it as the baseline.  The retry then
                            // distinguishes "guest has not applied my request
                            // yet" (still this baseline → keep pushing) from
                            // "another console changed it" (a different size →
                            // yield), instead of mistaking our own not-yet-
                            // applied request for someone else's change.
                            if (remoteVideoWidth() > 0 && remoteVideoHeight() > 0) {
                                lastAppliedSize = `${remoteVideoWidth()}x${remoteVideoHeight()}`;
                            }
                        }
                        resizeRetryIdentity = identity;
                        lastResize = identity;
                        lastResizeSentAt = Date.now();
                        send({ op: 'resize', ...value });
                    }
                    // Control SCTP is reliable, but a D-Bus/guest mode
                    // change can race a resize immediately after a window or
                    // full-screen transition. Do not permanently letterbox
                    // the Console after one early request: retry until the
                    // decoded frame itself confirms the target geometry.
                    if (resizeRetryTimer !== null) { popup.clearTimeout(resizeRetryTimer); }
                    resizeRetryTimer = popup.setTimeout(() => {
                        resizeRetryTimer = null;
                        if (closed || resizeRetryIdentity !== identity) { return; }
                        const current = dimensions(video, sessionFps);
                        const currentIdentity = `${current.width}x${current.height}@${current.fps}`;
                        if (currentIdentity !== identity) { resize(false); return; }
                        if (remoteVideoWidth() === current.width && remoteVideoHeight() === current.height) {
                            resizeRetryAttempts = 0;
                            resizeStalled = false;
                            lastAppliedSize = `${remoteVideoWidth()}x${remoteVideoHeight()}`;
                            // The geometry converged: clear a "kept its own
                            // size" line left behind by an earlier stall,
                            // and only that line — a keyboard-capture or
                            // clipboard message must survive a resize.
                            if (resizeStatusShown) {
                                resizeStatusShown = false;
                                updateMediaStatus();
                            }
                            return;
                        }
                        // The guest is a different size than we asked for.  If
                        // it moved to a size that is neither our request nor the
                        // size we previously saw, another Console resized the
                        // shared guest — it is the reference now.  Follow it:
                        // re-sending our size here would drag the stretched
                        // console back and leave it showing a scaled desktop.
                        const guestSize = `${remoteVideoWidth()}x${remoteVideoHeight()}`;
                        if (remoteVideoWidth() > 0 && remoteVideoHeight() > 0 &&
                            lastAppliedSize && guestSize !== lastAppliedSize) {
                            lastAppliedSize = guestSize;
                            resizeRetryAttempts = 0;
                            resizeStalled = false;
                            resizeRetryIdentity = '';
                            lastResize = '';
                            if (resizeStatusShown) { resizeStatusShown = false; updateMediaStatus(); }
                            return;
                        }
                        if (resizeRetryAttempts < 16) {
                            resizeRetryAttempts += 1;
                            lastResize = '';
                            resize(true);
                            return;
                        }
                        // Sixteen fast retries cover an ordinary compositor
                        // transition.  A guest that still keeps its own mode
                        // is almost always one whose session is locked or
                        // whose output is off: it applies the pending mode
                        // when it wakes, and re-sending every 250 ms until
                        // then only makes it re-evaluate a request it is
                        // ignoring.  Stop here; the input path re-issues the
                        // request once per wake attempt (retryStalledResize).
                        resizeStalled = true;
                        if (guestAsleepHint || guestFrameIsDark() === null) { return; }
                        status.textContent = gettext('Guest display kept its own size. If the guest is asleep or locked, move the mouse or press a key here.');
                        if (!resizeStatusShown) { revealToolbar(); }
                        resizeStatusShown = true;
                    }, 250);
                };
                if (resizeTimer !== null) { popup.clearTimeout(resizeTimer); resizeTimer = null; }
                if (immediate) { dispatch(); }
                else { resizeTimer = popup.setTimeout(dispatch, settings.resizeDebounceMs); }
            };
            resizeConsole = resize;
            // A locked or sleeping guest adopts the last requested mode when
            // it wakes.  Ordinary input is the wake signal, so every input
            // burst re-issues a stalled request exactly once (3 s throttle)
            // with a short verification burst instead of an endless loop.
            retryStalledResize = () => {
                if (!resizeStalled || closed) { return; }
                // If the guest is now at a size other than the one we last saw
                // applied, another Console is driving the shared resolution.
                // Yield: this console follows the reference instead of shoving
                // its own size back on every mouse move.
                if (remoteVideoWidth() > 0 && lastAppliedSize &&
                    `${remoteVideoWidth()}x${remoteVideoHeight()}` !== lastAppliedSize) {
                    resizeStalled = false;
                    lastAppliedSize = `${remoteVideoWidth()}x${remoteVideoHeight()}`;
                    return;
                }
                const now = Date.now();
                if (now - lastStalledRetryAt < 3000) { return; }
                // A guest that keeps its own mode after several wake
                // attempts is one that cannot follow the window at all
                // (Standard VGA rejects Display1 resize requests); leave it
                // letterboxed rather than asking again on every gesture.
                if (stalledRetryCount >= 6) { return; }
                stalledRetryCount += 1;
                lastStalledRetryAt = now;
                resizeRetryAttempts = 8;
                lastResize = '';
                resize(true);
            };
            noteGuestInput = () => {
                lastInputSentAt = Date.now();
                retryStalledResize();
            };
            const flushPointer = (reliable = false) => {
                if (pointerTimer !== null) {
                    popup.clearTimeout(pointerTimer);
                    pointerTimer = null;
                }
                const value = reliable ? (pendingPointer || latestPointer) : pendingPointer;
                pendingPointer = null;
                if (!value || closed) { return; }
                // Fence the position and button on the same ordered channel:
                // SCTP does not order messages across different data channels.
                if (reliable || mouseFenceSequence !== null || (exactMouseClicks() && heldMouseButtons.size)) {
                    send(value); nextPointerSendAt = popup.performance.now() + 8;
                }
                else if (sendPointer(value)) { nextPointerSendAt = popup.performance.now() + 8; }
                else if (pointer && pointer.readyState === 'open') {
                    // Backpressure retains only the latest coordinate; reliable
                    // keyboard/button events never enter this motion queue.
                    pendingPointer = value;
                    pointerTimer = popup.setTimeout(() => flushPointer(false), 8);
                }
            };
            const queuePointer = (value) => {
                noteGuestInput();
                const sample = { ...value, sequence: pointerSequence };
                pointerSequence = (pointerSequence + 1) >>> 0;
                latestPointer = sample;
                pendingPointer = sample;
                const delay = Math.max(0, nextPointerSendAt - popup.performance.now());
                // A timer throttled while backgrounded must not delay fresh
                // input after this console is focused again.
                if (delay === 0) { flushPointer(false); }
                else if (pointerTimer === null) { pointerTimer = popup.setTimeout(() => flushPointer(false), delay); }
            };
            const pointerForMouseEvent = (event) => {
                // The video is position:fixed;inset:0;width:100vw;height:100vh, so
                // its on-screen box is exactly the popup/iframe viewport
                // (popup.innerWidth x innerHeight) — the SAME coordinate space as
                // event.clientX/Y and the touch marker (which the user confirmed
                // lands under the finger).  Mapping through this viewport is
                // therefore guaranteed consistent with the marker, unlike
                // getBoundingClientRect() or offsetX, which are in a different
                // (scaled) space inside a mobile iframe and collapsed every touch
                // to one guest coordinate.  object-fit:contain letterboxes to the
                // source aspect, handled below.
                const viewW = popup.innerWidth || video.clientWidth || 1;
                const viewH = popup.innerHeight || video.clientHeight || 1;
                const sourceWidth = Math.max(1, remoteVideoWidth() || viewW);
                const sourceHeight = Math.max(1, remoteVideoHeight() || viewH);
                const rawScale = Math.min(viewW / sourceWidth, viewH / sourceHeight);
                const scale = Number.isFinite(rawScale) && rawScale > 0 ? rawScale : 1;
                const contentLeft = (viewW - sourceWidth * scale) / 2;
                const contentTop = (viewH - sourceHeight * scale) / 2;
                // Undo the client-side zoom/pan (transform-origin 0,0):
                // screen -> local viewport coordinates the letterbox math uses.
                const localX = (event.clientX - zoomTx) / zoomScale;
                const localY = (event.clientY - zoomTy) / zoomScale;
                const gx = Math.max(0, Math.min(sourceWidth - 1, Math.floor((localX - contentLeft) / scale)));
                const gy = Math.max(0, Math.min(sourceHeight - 1, Math.floor((localY - contentTop) / scale)));
                lastSentGuestX = gx; lastSentGuestY = gy;
                diagLastPtr = `client=${Math.round(event.clientX)},${Math.round(event.clientY)} view=${Math.round(viewW)}x${Math.round(viewH)} zoom=${zoomScale.toFixed(2)} src=${sourceWidth}x${sourceHeight} scale=${scale.toFixed(3)} -> guest=${gx},${gy}`;
                // Refresh the panel immediately (its synchronous part runs before
                // any await) so each touch's coordinates show at once instead of
                // waiting for the interval — essential for reading them on-device.
                if (diag && !diag.hidden) { updateDiag(); }
                // Keep the touch cursor overlay under the finger as it acts,
                // even before the guest streams its own cursor for this move.
                if (usingTouchInput) { placeGuestCursor(); }
                return { op: 'mouse_position', x: gx, y: gy, width: sourceWidth, height: sourceHeight };
            };
            control.addEventListener('open', () => { exactUiRequested = null; updateExactPreference(); resize(true); });
            control.addEventListener('message', (event) => {
                if (typeof event.data !== 'string') { return; }
                let message;
                try { message = JSON.parse(event.data); } catch (_error) { return; }
                if (!message || typeof message !== 'object') { return; }
                if (message.op === 'qsm_input_policy') {
                    acceptInputPolicy(message);
                } else if (message.op === 'qsm_files_token' && typeof message.token === 'string' &&
                    /^[0-9a-f]{32}$/.test(message.token)) {
                    filesToken = message.token;
                    filesButton.hidden = Boolean(VIEWER);
                } else if (message.op === 'qsm_ui_policy' && message.version === 1) {
                    exactUiPolicy = message;
                    updateExactPreference();
                } else if (message.op === 'qsm_video_frame') {
                    const mapped = videoRegions.map(message.rtp, message.region);
                    if (message.region !== undefined && !mapped) { return; }
                    const region = mapped ? message.region : null;
                    exactRenderer.mapVideoFrame(message.rtp, message.capture, region);
                    if (mapped && videoRegions.painted === message.rtp) {
                        finishVideoPresentation(message.rtp, popup.performance.now());
                    }
                } else if (message.op === 'qsm_mouse_ack') {
                    if (message.sequence === mouseFenceSequence) { mouseFenceSequence = null; }
                } else if (message.op === 'qsm_video_rate') {
                    acceptVideoRate(message);
                } else if (message.op === 'qsm_guest_result' && typeof message.request_id === 'string') {
                    const request = guestRequests.get(message.request_id);
                    if (!request) { return; }
                    guestRequests.delete(message.request_id);
                    popup.clearTimeout(request.timer);
                    if (message.ok === true && message.result && typeof message.result === 'object') {
                        request.resolve(message.result);
                    } else {
                        request.reject(new Error('guest operation failed'));
                    }
                } else if (message.op === 'qsm_guest_clipboard' && typeof message.text_b64 === 'string') {
                    try {
                        // A website is not permitted to replace the user's
                        // system clipboard without a click/key gesture. Keep
                        // the notification for the explicit Copy action
                        // rather than silently discarding a browser rejection.
                        const text = new TextDecoder('utf-8', { fatal: true }).decode(b64ToBytes(message.text_b64));
                        resolveGuestClipboardWaiters(text);
                        // Klipper can report a transient selection while a
                        // window is being dragged. It must not repeatedly
                        // repaint the console toolbar or look like a local
                        // clipboard operation. The explicit Copy control
                        // remains available for the final guest selection.
                        if (text !== lastGuestClipboardText) {
                            lastGuestClipboardText = text;
                            if (!heldMouseButtons.size) {
                                status.textContent = gettext('Guest clipboard changed — press Copy to copy it here');
                            }
                        }
                    } catch (_error) { /* Ignore malformed guest clipboard notifications. */ }
                } else if (message.op === 'qsm_guest_cursor_shape') {
                    acceptGuestCursorShape(message);
                }
            });
            // aiortc closes every server-side data channel while retiring a
            // VM session. Track-end is the normal media signal, but this is
            // an independent browser-visible lifecycle signal for codecs or
            // browsers that postpone a track's `ended` event.
            control.addEventListener('close', closeForStoppedVm, { once: true });
            pointer.addEventListener('close', closeForStoppedVm, { once: true });
            observer = new ResizeObserver(() => { placeGuestCursor(); resize(false); });
            observer.observe(video);
            popup.addEventListener('resize', () => { resize(false); reflowView(); });
            // A VM has one Display1 scanout, while several PVE Console
            // windows may observe it.  A decoded-frame resize means that
            // *some* Console already changed the guest mode; it is not a
            // resize of this popup.  Feeding that event back into `resize()`
            // made an older, differently sized viewer immediately restore
            // its stale dimensions and left the window that the user had
            // just resized letterboxed.  Only this popup's CSS box and its
            // explicit full-screen transition are authority to request a
            // mode.  The video event merely lets its outstanding retry yield
            // to the newer Console request.
            video.addEventListener('resize', () => {
                syncExactGeometry();
                placeGuestCursor();
                reflowView();
                // The retry identity carries the session FPS; a literal
                // "@60" here left the convergence branch unreachable for
                // any other target frame rate.
                const visible = `${remoteVideoWidth()}x${remoteVideoHeight()}@${sessionFps}`;
                if (visible === resizeRetryIdentity) {
                    resizeRetryAttempts = 0;
                    resizeStalled = false;
                    if (resizeStatusShown) {
                        // A guest that woke up adopted the stalled request
                        // on its own: drop the "kept its own size" line.
                        resizeStatusShown = false;
                        updateMediaStatus();
                    }
                    return;
                }
                if (resizeRetryTimer !== null) {
                    popup.clearTimeout(resizeRetryTimer);
                    resizeRetryTimer = null;
                }
                resizeRetryIdentity = '';
                resizeRetryAttempts = 0;
            });
            // Control follows the pointer into the console: entering the guest
            // rectangle takes keyboard and clipboard focus at once, without
            // waiting for a click.  Relying on a click to move focus made
            // switching between two consoles feel like it lagged by seconds
            // (the previous surface kept focus until the guest was clicked).
            // Focusing an element in a window that is not the active one is a
            // harmless no-op, so no window-state test is needed; the settings
            // panel is skipped so hovering the video cannot steal its inputs.
            video.addEventListener('pointerenter', () => {
                if (consolePanelOpen()) { return; }
                video.focus({ preventScroll: true });
            });
            // Desktop trackpad taps/clicks are MouseEvents too. Keep a short
            // click wholly local until release; only a confirmed hold/drag
            // creates guest held state, renewed by the existing snapshots.
            const buttonMask = button => [0, 1, 4, 2, 8, 16][button];
            const beginMouseHold = (button, press) => {
                popup.clearTimeout(press.timer); pendingMousePresses.delete(button);
                sendMouseButton(button, true, press.point, press.event);
            };
            const moveMouse = (event) => {
                for (const [button, press] of pendingMousePresses) {
                    if (typeof event.buttons === 'number' && !(event.buttons & buttonMask(button))) {
                        popup.clearTimeout(press.timer); pendingMousePresses.delete(button);
                    } else if (Math.hypot(event.clientX - press.x, event.clientY - press.y) >= 4) {
                        beginMouseHold(button, press);
                    }
                }
                for (const button of [...heldMouseButtons]) {
                    if (typeof event.buttons === 'number' && !(event.buttons & buttonMask(button))) { sendMouseButton(button, false); }
                }
                queuePointer(pointerForMouseEvent(event));
            };
            const endMouse = (event) => {
                const button = event.button + 1;
                const press = pendingMousePresses.get(button);
                if (press) {
                    popup.clearTimeout(press.timer); pendingMousePresses.delete(button);
                    sendMouseClick(button, pointerForMouseEvent(event), event);
                } else if (heldMouseButtons.has(button)) {
                    queuePointer(pointerForMouseEvent(event)); flushPointer(true);
                    sendMouseButton(button, false, latestPointer, event);
                }
            };
            video.addEventListener('mousemove', (event) => {
                observeToolbarZone(event);
                // A real mouse is present: hand the cursor back to the OS/CSS
                // path (hide the touch overlay) so a desktop user is unaffected.
                if (usingTouchInput) { usingTouchInput = false; placeGuestCursor(); }
                // A missed button-up from a previous native full-screen
                // transition becomes observable on the next ordinary move.
                // Clear it before it can make every later click a drag.
                moveMouse(event);
            });
            video.addEventListener('mouseleave', () => {
                pointerInToolbarZone = false;
                pointerNearToolbar = false;
                cancelToolbarReveal();
                hideToolbarSoon();
            });
            video.addEventListener('mousedown', (event) => {
                ensurePlaying(); video.focus();
                const button = event.button + 1;
                if (button < 1 || button > 5 || pendingMousePresses.has(button) || heldMouseButtons.has(button)) { return; }
                markModifiersUsed();
                const point = pointerForMouseEvent(event);
                queuePointer(point);
                if (exactMouseClicks()) {
                    const press = { x: event.clientX, y: event.clientY, point, event, timer: null };
                    pendingMousePresses.set(button, press);
                    press.timer = popup.setTimeout(() => beginMouseHold(button, press), 250);
                } else {
                    flushPointer(true); sendMouseButton(button, true, point, event);
                }
                event.preventDefault();
            });
            video.addEventListener('mouseup', (event) => { endMouse(event); event.preventDefault(); });
            document.addEventListener('mousemove', (event) => {
                if (event.target !== video && (pendingMousePresses.size || heldMouseButtons.size)) { moveMouse(event); }
            });
            video.addEventListener('contextmenu', (event) => {
                // Button 3 was already sent on mousedown/mouseup above.
                // Suppress Chrome's menu without stopping the genuine guest
                // context-menu click, so one right click maps to one guest
                // right click rather than a browser menu followed by QEMU.
                event.preventDefault();
                event.stopPropagation();
            });
            // Touch and pen input (Android tablets/phones and other
            // touchscreens).  The mouse handlers above stay the desktop path;
            // these fire only for non-mouse pointers. A slide moves without
            // a button. Holding before a slide selects; holding and lifting
            // without moving right-clicks. Defer that click until release so
            // it cannot open a context menu before a selection drag.
            // A brief marker shows exactly where each touch registered.
            const touchMarker = document.createElement('div');
            touchMarker.setAttribute('aria-hidden', 'true');
            touchMarker.style.cssText = 'position:fixed;z-index:6;width:26px;height:26px;margin:-13px 0 0 -13px;border:2px solid rgba(56,189,248,.95);border-radius:50%;background:rgba(56,189,248,.25);pointer-events:none;opacity:0;transition:opacity .25s ease';
            document.body.append(touchMarker);
            let touchMarkerTimer = null;
            const showTouchMarker = (x, y) => {
                touchMarker.style.left = `${x}px`;
                touchMarker.style.top = `${y}px`;
                touchMarker.style.opacity = '1';
                if (touchMarkerTimer !== null) { popup.clearTimeout(touchMarkerTimer); }
                touchMarkerTimer = popup.setTimeout(() => { touchMarker.style.opacity = '0'; touchMarkerTimer = null; }, 350);
            };
            // ---- Touch gesture recognizer ------------------------------------
            // One "direct" behaviour: the guest cursor is shown (overlay) and a
            // gesture acts where you touch.  Which finger gesture triggers which
            // guest action is configurable (gestureMap); this classifies the raw
            // gesture and dispatches to the bound action.
            const touchPoints = new Map(); // pointerId -> {startX,startY,x,y}
            let peakFingers = 0;
            let oneMoved = false;
            let oneLongReady = false;
            let oneStartX = 0;
            let oneStartY = 0;
            let oneLongTimer = null;
            let heldDragButton = 0;
            let contAct = null;      // active continuous action ('move'|'drag'|'scroll'|'zoom')
            let contLastX = 0;
            let contLastY = 0;
            let scrollAccumY = 0;
            const SCROLL_STEP_PX = 18;
            let twoLocked = null;    // null | 'pinch' | 'drag2'
            let twoMoved = false;
            let twoStartDist = 1;
            let twoStartMidX = 0;
            let twoStartMidY = 0;
            let twoStartScale = 1;
            let twoStartTx = 0;
            let twoStartTy = 0;
            const clearOneLong = () => { if (oneLongTimer !== null) { popup.clearTimeout(oneLongTimer); oneLongTimer = null; } };
            const twoPoints = () => { const it = touchPoints.values(); return [it.next().value, it.next().value]; };
            const pointDistance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
            // --- action executors ---
            const clickAction = (action, clientX, clientY) => {
                queuePointer(pointerForMouseEvent({ clientX, clientY }));
                flushPointer(true);
                if (action === 'move') { return; }
                const button = action === 'rightClick' ? 3 : action === 'middleClick' ? 2 : 1;
                sendMouseClick(button);
            };
            const scrollByDelta = (dy) => {
                // Worker emits one wheel click per op (sign only): accumulate
                // finger travel and send one op per notch, natural direction
                // (drag down = content down = wheel-up = vertical<0), capped/event.
                markModifiersUsed(); flushDeferredModifiers();
                scrollAccumY += dy;
                let notches = 0;
                while (Math.abs(scrollAccumY) >= SCROLL_STEP_PX && notches < 8) {
                    const dir = scrollAccumY > 0 ? 1 : -1;
                    scrollAccumY -= dir * SCROLL_STEP_PX;
                    send({ op: 'scroll', vertical: -dir, horizontal: 0 });
                    notches += 1;
                    diagLastInput = 'scroll' + (dir > 0 ? '↑' : '↓');
                }
            };
            const beginContinuous = (action, sx, sy) => {
                contAct = action || null;
                contLastX = sx; contLastY = sy; scrollAccumY = 0;
                if (contAct === 'drag' || contAct === 'move') {
                    queuePointer(pointerForMouseEvent({ clientX: sx, clientY: sy }));
                    flushPointer(true);
                    if (contAct === 'drag') {
                        sendMouseButton(1, true);
                        heldDragButton = 1;
                    }
                }
            };
            const stepContinuous = (x, y) => {
                const dx = x - contLastX; const dy = y - contLastY;
                contLastX = x; contLastY = y;
                if (contAct === 'drag' || contAct === 'move') {
                    queuePointer(pointerForMouseEvent({ clientX: x, clientY: y }));
                    flushPointer(true);
                } else if (contAct === 'scroll') {
                    scrollByDelta(dy);
                } else if (contAct === 'zoom') {
                    if (zoomScale > 1) { zoomTx += dx; zoomTy += dy; clampZoom(); applyViewTransform(); }
                }
            };
            const endContinuous = (x, y) => {
                if (contAct === 'drag' && heldDragButton) {
                    queuePointer(pointerForMouseEvent({ clientX: x, clientY: y }));
                    flushPointer(true);
                    sendMouseButton(heldDragButton, false);
                    heldDragButton = 0;
                }
                contAct = null;
            };
            const pinchZoom = (dist, mx, my) => {
                const act = gestureToAction.pinch;
                if (act !== 'zoom') { return; }
                const ratio = dist / twoStartDist;
                const newScale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, twoStartScale * ratio));
                const lx = (twoStartMidX - twoStartTx) / twoStartScale;
                const ly = (twoStartMidY - twoStartTy) / twoStartScale;
                zoomScale = newScale;
                zoomTx = mx - newScale * lx;
                zoomTy = my - newScale * ly;
                clampZoom();
                applyViewTransform();
            };
            const resetInteraction = () => {
                clearOneLong();
                if (heldDragButton) { sendMouseButton(heldDragButton, false); heldDragButton = 0; }
                peakFingers = 0; oneMoved = false; oneLongReady = false;
                contAct = null; twoLocked = null; twoMoved = false;
            };
            const finalizeInteraction = (px, py) => {
                clearOneLong();
                if (peakFingers === 1) {
                    if (contAct) { endContinuous(px, py); }
                    else if (!oneMoved) {
                        const act = gestureToAction[oneLongReady ? 'longPress' : 'tap'];
                        if (act) { clickAction(act, px, py); }
                    }
                } else if (peakFingers === 2) {
                    if (twoLocked) { endContinuous(px, py); }
                    else if (!twoMoved) {
                        const act = gestureToAction.tap2;
                        if (act) { clickAction(act, twoStartMidX, twoStartMidY); }
                    }
                } else if (peakFingers >= 3) {
                    const act = gestureToAction.tap3;
                    if (act) { clickAction(act, twoStartMidX || px, twoStartMidY || py); }
                }
                resetInteraction();
            };
            const cancelTouchInteraction = () => {
                touchPoints.clear();
                resetInteraction();
                releaseHeldInput();
            };
            popup.addEventListener('blur', cancelTouchInteraction);
            document.addEventListener('visibilitychange', () => {
                if (document.hidden) { cancelTouchInteraction(); }
            });
            video.addEventListener('lostpointercapture', (event) => {
                if (event.pointerType !== 'mouse' && touchPoints.has(event.pointerId)) {
                    cancelTouchInteraction();
                }
            });
            // --- pointer events (touch/pen only; mouse keeps the desktop path) ---
            video.addEventListener('pointerdown', (event) => {
                if (event.pointerType === 'mouse') { return; }
                usingTouchInput = true;
                touchPoints.set(event.pointerId, { startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY });
                const n = touchPoints.size;
                if (n > peakFingers) { peakFingers = n; }
                try { video.setPointerCapture(event.pointerId); } catch (_error) { /* older engines */ }
                if (n === 1) {
                    ensurePlaying();
                    video.focus({ preventScroll: true });
                    if (toolbarVisible && !toolbarPinned) { hideToolbarNow(); }
                    oneMoved = false; oneLongReady = false; contAct = null;
                    oneStartX = event.clientX; oneStartY = event.clientY;
                    showTouchMarker(event.clientX, event.clientY);
                    queuePointer(pointerForMouseEvent(event));
                    placeGuestCursor();
                    clearOneLong();
                    oneLongTimer = popup.setTimeout(() => {
                        oneLongTimer = null;
                        if (peakFingers !== 1 || oneMoved) { return; }
                        oneLongReady = true;
                    }, 500);
                } else if (n === 2) {
                    // A second finger cancels any one-finger action in progress.
                    clearOneLong();
                    if (heldDragButton) { sendMouseButton(heldDragButton, false); heldDragButton = 0; }
                    contAct = null; oneMoved = false;
                    const [a, b] = twoPoints();
                    twoStartDist = pointDistance(a, b) || 1;
                    twoStartMidX = (a.x + b.x) / 2; twoStartMidY = (a.y + b.y) / 2;
                    twoStartScale = zoomScale; twoStartTx = zoomTx; twoStartTy = zoomTy;
                    twoLocked = null; twoMoved = false;
                    contLastX = twoStartMidX; contLastY = twoStartMidY;
                }
                event.preventDefault();
            });
            video.addEventListener('pointermove', (event) => {
                if (event.pointerType === 'mouse') { return; }
                const p = touchPoints.get(event.pointerId);
                if (!p) { return; }
                p.x = event.clientX; p.y = event.clientY;
                const n = touchPoints.size;
                if (n === 1 && peakFingers === 1) {
                    const dx = event.clientX - oneStartX; const dy = event.clientY - oneStartY;
                    if (!oneMoved && Math.hypot(dx, dy) > 8) {
                        oneMoved = true;
                        clearOneLong();
                        beginContinuous(gestureToAction[oneLongReady ? 'holdDrag' : 'drag1'], oneStartX, oneStartY);
                    }
                    if (oneMoved && contAct) { stepContinuous(event.clientX, event.clientY); }
                    else { queuePointer(pointerForMouseEvent(event)); }
                    showTouchMarker(event.clientX, event.clientY);
                } else if (n === 2) {
                    const [a, b] = twoPoints();
                    if (!a || !b) { return; }
                    const d = pointDistance(a, b) || 1;
                    const mx = (a.x + b.x) / 2; const my = (a.y + b.y) / 2;
                    if (twoLocked === null) {
                        const dDist = Math.abs(d - twoStartDist);
                        const dMid = Math.hypot(mx - twoStartMidX, my - twoStartMidY);
                        if (dDist > 12 || dMid > 12) {
                            twoMoved = true;
                            twoLocked = dDist > dMid ? 'pinch' : 'drag2';
                            if (twoLocked === 'drag2') { beginContinuous(gestureToAction.drag2, twoStartMidX, twoStartMidY); }
                        }
                    }
                    if (twoLocked === 'pinch') { pinchZoom(d, mx, my); }
                    else if (twoLocked === 'drag2') { stepContinuous(mx, my); }
                }
                event.preventDefault();
            });
            const endTouchPointer = (event) => {
                if (event.pointerType === 'mouse') { return; }
                // The same handler is on video and document; a captured pointerup
                // reaches both, so process each event once.
                if (event.__qsmTouchHandled) { return; }
                event.__qsmTouchHandled = true;
                if (!touchPoints.has(event.pointerId)) { return; }
                touchPoints.delete(event.pointerId);
                const remaining = touchPoints.size;
                try { video.releasePointerCapture(event.pointerId); } catch (_error) { /* already released */ }
                if (remaining === 0) {
                    finalizeInteraction(event.clientX, event.clientY);
                } else if (twoLocked && remaining < 2) {
                    // A two-finger gesture dropped to one finger: end it and wait
                    // for all contacts to lift (do not start a stray one-finger act).
                    endContinuous(event.clientX, event.clientY);
                    twoLocked = null;
                }
                event.preventDefault();
            };
            video.addEventListener('pointerup', endTouchPointer);
            // A touch that lifts off the video (or whose pointerup the engine
            // delivers to the document) must still release, else a held button or
            // a stale contact blocks every later tap.
            document.addEventListener('pointerup', endTouchPointer);
            document.addEventListener('pointercancel', (event) => {
                if (!event || event.pointerType === 'mouse') { releaseHeldInput(); return; }
                if (!touchPoints.has(event.pointerId)) { return; }
                touchPoints.delete(event.pointerId);
                if (touchPoints.size === 0) {
                    clearOneLong();
                    if (heldDragButton) { sendMouseButton(heldDragButton, false); heldDragButton = 0; }
                    resetInteraction();
                    releaseHeldInput();
                }
            });
            // Mouseup often targets the document instead of the video after
            // Escape leaves native full screen.  Preserve a captured guest
            // button only until that document-level release arrives.
            document.addEventListener('mouseup', endMouse);
            video.addEventListener('pointercancel', (event) => {
                if (!event || event.pointerType === 'mouse') { return; }
                if (!touchPoints.has(event.pointerId)) { return; }
                touchPoints.delete(event.pointerId);
                if (touchPoints.size === 0) {
                    clearOneLong();
                    if (heldDragButton) { sendMouseButton(heldDragButton, false); heldDragButton = 0; }
                    resetInteraction();
                    releaseHeldInput();
                }
            });
            popup.addEventListener('blur', releaseHeldInput);
            document.addEventListener('visibilitychange', () => {
                if (document.hidden) { releaseHeldInput(); }
            });
            let wheelX = 0, wheelY = 0;
            video.addEventListener('wheel', (event) => {
                // Trackpads can emit fractional deltas. Retain their remainder
                // instead of losing a slow two-finger scroll to truncation.
                const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? popup.innerHeight : 1;
                wheelX += event.deltaX * scale; wheelY += event.deltaY * scale;
                const horizontal = Math.max(-32768, Math.min(32767, Math.trunc(wheelX)));
                const vertical = Math.max(-32768, Math.min(32767, Math.trunc(wheelY)));
                wheelX -= horizontal; wheelY -= vertical;
                if (vertical || horizontal) {
                    markModifiersUsed(); flushDeferredModifiers();
                    send({ op: 'scroll', vertical, horizontal });
                }
                event.preventDefault();
            }, { passive: false });
            video.addEventListener('mousedown', () => { suppressHostPaste = false; });
            video.addEventListener('paste', (event) => {
                // Raw input is intentionally a literal key/mouse edge mode
                // for configuring guest shortcuts. Its clipboard controls
                // remain the explicit toolbar buttons; a browser Paste event
                // must not turn Ctrl/Cmd+V into a synthetic guest Paste action.
                if (settings.keyboardPriority === KEYBOARD_PRIORITY.raw) { return; }
                if (suppressHostPaste) { suppressHostPaste = false; event.preventDefault(); return; }
                const text = event.clipboardData && event.clipboardData.getData('text/plain');
                if (typeof text !== 'string') { return; }
                event.preventDefault();
                pasteTextIntoGuest(text).catch(() => {
                    status.textContent = gettext('Guest clipboard is unavailable.');
                });
            });
            for (const name of ['keydown', 'keyup']) {
                video.addEventListener(name, (event) => {
                    const key = qemuKey(event, settings.keyboardPriority === KEYBOARD_PRIORITY.raw);
                    if (routeHostShortcut(event)) { return; }
                    const clientFirst = settings.keyboardPriority === KEYBOARD_PRIORITY.client;
                    const rawInput = settings.keyboardPriority === KEYBOARD_PRIORITY.raw;
                    const pasteShortcut = event.code === 'KeyV' && (event.ctrlKey || event.metaKey) && !event.altKey;
                    const copyShortcut = event.code === 'KeyC' && (event.ctrlKey || event.metaKey) && !event.altKey;
                    if (!rawInput && pasteShortcut) {
                        suppressHostPaste = false;
                        event.preventDefault();
                        if (name === 'keydown' && !event.repeat) {
                            // Release a forwarded Cmd/Ctrl before emitting
                            // the Linux Paste sequence. Otherwise macOS Cmd
                            // can remain held in the guest and turn a paste
                            // into an unrelated desktop shortcut.
                            releaseHeldInput();
                            pasteFromBrowser().catch(() => {
                                status.textContent = gettext('Browser clipboard is unavailable.');
                            });
                        }
                    } else if (!rawInput && copyShortcut) {
                        event.preventDefault();
                        if (name === 'keydown' && !event.repeat) {
                            releaseHeldInput();
                            guestSelectionToBrowser(event.shiftKey).catch(() => {
                                status.textContent = gettext('Guest clipboard is unavailable.');
                            });
                        }
                    } else if (key !== null) {
                        if (!shortcutModifierName(key) ||
                            matchShortcutRule(event, shortcutRulesForSettings(settings))?.action !== 'host') { event.preventDefault(); }
                        event.stopPropagation();
                        sendKeyboard(key, name === 'keydown', event.repeat, event.timeStamp, event);
                    }
                });
            }
            const incomingVideo = peer.addTransceiver('video', { direction: 'recvonly' });
            receiveFeedback.attach(incomingVideo.receiver);
            micTransceiver = peer.addTransceiver('audio', { direction: 'sendrecv' });
            if (micStream) {
                // A reconnect keeps a switched-on microphone.
                micTransceiver.sender.replaceTrack(micStream.getAudioTracks()[0] || null).catch(() => undefined);
            }
            const offer = await peer.createOffer();
            await peer.setLocalDescription(offer);
            await waitForIce(peer);
            if (!/^a=candidate:/m.test(peer.localDescription?.sdp || '')) {
                throw new Error(gettext('The browser did not provide a WebRTC network address. Check browser network permissions and VPN settings.'));
            }
            const requestSize = dimensions(video, sessionFps);
            const answer = await api(`/nodes/${encodeURIComponent(node)}/${guestKind}/${encodeURIComponent(vmid)}/qsm-direct`, {
                sdp: peer.localDescription.sdp, width: requestSize.width, height: requestSize.height, fps: requestSize.fps,
            }, button);
            await peer.setRemoteDescription(answer);
            try {
                const answerSdp = (answer && answer.sdp) || (peer.remoteDescription && peer.remoteDescription.sdp) || '';
                const rtpmap = answerSdp.match(/a=rtpmap:\d+\s+(H264|H265|VP8|VP9|AV1)\b[^\r\n]*/i);
                if (rtpmap) { negotiatedVideoCodec = rtpmap[0].replace(/^a=rtpmap:\d+\s+/i, ''); }
            } catch (_error) { /* diagnostics only */ }
            status.textContent = gettext('Negotiating guest media…');
            // A transport connection is not visual output. Keep the popup
            // usable for a slow guest, but make a Display1/GL capture stall
            // explicit instead of reporting a misleading plain "Connected".
            firstFrameTimer = popup.setTimeout(async () => {
                if (closed || video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) { return; }
                // Autoplay blocked (usual on mobile): the stream is arriving but
                // the element is paused.  Tell the user a press starts it, and
                // surface the toolbar so the affordance is visible.
                startDiag();
                if (video.paused) {
                    status.textContent = gettext('Connected — tap the console to start the video.');
                    revealToolbar();
                    ensurePlaying();
                    return;
                }
                // Playing but nothing decoded: separate "not receiving" from
                // "receiving but this browser cannot decode the codec" so a
                // mobile decode problem is diagnosable from the status line.
                let message = gettext('Connected, but the guest has not produced a video frame.');
                try {
                    if (peer) {
                        const stats = await peer.getStats();
                        let received = 0; let decoded = 0; let seen = false;
                        stats.forEach((report) => {
                            if (report.type === 'inbound-rtp' && report.kind === 'video') {
                                seen = true;
                                received = report.framesReceived || 0;
                                decoded = report.framesDecoded || 0;
                            }
                        });
                        if (seen && received > 0 && decoded === 0) {
                            message = gettext('Connected and receiving video, but this browser could not decode it.');
                        }
                    }
                } catch (_error) { /* getStats is optional. */ }
                if (!closed && video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
                    status.textContent = message;
                }
            }, 7000);
            video.focus();
            } catch (error) {
                // 409: every console of the container is in use -- like its
                // ttys in the terminal console, each Console gets its own.
                const busy = String(error && error.message).indexOf('signal 409') >= 0;
                const message = busy
                    ? gettext('All consoles of this container are in use. Close another console of it, or wait until a closed one is logged out, and try again.')
                    : (VIEWER && error && error.message ? `${gettext('Could not create a direct browser console.')} ${String(error.message).replace(/^webrtcproxy \d+: /, '')}`
                        : gettext('Could not create a direct browser console.'));
                status.textContent = message;
                close();
                closeSurface();
                Ext.Msg.alert(gettext('QSM Direct'), message);
            }
        };
        connect();
        return {
            close: () => {
                close();
                closeSurface();
            },
        };
    };

    // Keep PVE's one, familiar "Console" card in the left navigation.  Its
    // configuration is normally a lazy card held in `savedItems`, so the
    // QEMU Config overlay below can substitute this xtype after it reads the
    // protected VM config and before the user opens Console. This card does
    // not add a second navigation entry and deliberately renders in PVE's
    // existing Console area instead of opening a second browser window.
    Ext.define('PVE.qsmDirect.ConsolePanel', {
        extend: 'Ext.panel.Panel',
        alias: 'widget.pveQsmDirectConsole',
        layout: 'fit',
        border: false,

        initComponent: function () {
            const me = this;
            const showPreparing = () => {
                const status = me.down('#qsm-direct-preparing');
                if (status) { status.update(gettext('Preparing console…')); }
            };
            const startNoVnc = () => {
                if (me.destroyed || me.qsmConsoleMode) { return; }
                me.qsmConsoleMode = 'novnc';
                me.removeAll(true);
                const noVnc = me.add({
                    xtype: 'pveNoVncConsole',
                    itemId: 'qsm-direct-stock-novnc',
                    vmid: Number(me.vmid),
                    consoleType: 'kvm',
                    nodename: me.nodename,
                });
                // PVE.panel.Config activates only the outer lazy card. Its
                // freshly-created noVNC child needs the same activation edge
                // to load the stock RFB iframe.
                noVnc.fireEvent('activate');
            };
            const startEmbeddedConsole = () => {
                if (me.destroyed || me.qsmConsoleMode || !validNode(me.nodename) || !validVmid(Number(me.vmid))) {
                    return;
                }
                const host = me.body && me.body.dom;
                if (!host) { return; }
                // A stopped VM or failed negotiation retires its iframe.
                // Returning to the normal PVE Console card must create a
                // fresh peer after the VM has been started again.
                if (me.qsmDirectFrame && host.contains(me.qsmDirectFrame)) { return; }
                if (me.qsmDirectSession) { me.qsmDirectSession.close(); }
                me.qsmConsoleMode = 'direct';
                me.removeAll(true);
                // A raw DOM iframe is invisible to ExtJS's card/fit layout.
                // The Console card then keeps its empty-panel minimum height
                // (roughly 150px) and negotiates a tiny Display1 surface.
                // Use PVE's own fit-aware IFrame component, as noVNC does,
                // before handing its native frame to the self-contained
                // WebRTC console.  This keeps the embedded viewport equal to
                // the whole existing Console card from its first SDP offer.
                const surface = me.add(Ext.create('Ext.ux.IFrame', {
                    itemId: 'qsm-direct-embedded-surface',
                    flex: 1,
                }));
                me.updateLayout();
                const frame = surface.getFrame();
                if (!frame) {
                    me.qsmConsoleMode = null;
                    surface.destroy();
                    return;
                }
                frame.title = gettext('QSM Direct Console');
                frame.setAttribute('allow', 'autoplay; clipboard-read; clipboard-write; fullscreen');
                frame.setAttribute('allowfullscreen', '');
                // touch-action:none on the iframe ELEMENT (top-page side) stops
                // the browser from claiming a two-finger gesture over the console
                // as a page pinch-zoom, in addition to the same rule on the video
                // inside; without it a tablet zooms the whole PVE page.
                frame.style.cssText = 'display:block;width:100%;height:100%;border:0;background:#000;touch-action:none';
                me.qsmDirectFrame = frame;
                me.qsmDirectSession = openConsole(me, me.nodename, Number(me.vmid), frame);
            };
            const selectBackend = () => {
                if (me.destroyed || me.qsmConsoleLoading || me.qsmConsoleMode ||
                    !validNode(me.nodename) || !validVmid(Number(me.vmid))) { return; }
                me.qsmConsoleLoading = true;
                showPreparing();
                // The Display1 predicate is read through PVE's protected API.
                // Right after a page refresh that read can transiently fail
                // before the app's auth/CSRF state settles.  Committing to
                // noVNC on the first failure permanently letterboxed a
                // managed VM's console after F5, so retry a bounded number of
                // times and only fall back to the stock console once the read
                // has genuinely failed.  A successful read is authoritative:
                // an unmanaged VM still goes straight to noVNC.
                const readConfig = (attempt) => {
                    if (me.destroyed || me.qsmConsoleMode) { return; }
                    Proxmox.Utils.API2Request({
                        url: `/nodes/${encodeURIComponent(me.nodename)}/qemu/${encodeURIComponent(me.vmid)}/config`,
                        method: 'GET',
                        success: ({ result }) => {
                            me.qsmConsoleLoading = false;
                            const data = result && result.data;
                            if (consoleConfigured(data, Number(me.vmid))) {
                                startEmbeddedConsole();
                            } else {
                                startNoVnc();
                            }
                        },
                        failure: () => {
                            if (attempt < 4 && !me.destroyed && !me.qsmConsoleMode) {
                                setTimeout(() => readConfig(attempt + 1), 750);
                                return;
                            }
                            // A Console-only role must always retain its stock
                            // PVE console when the optional Display1 predicate
                            // cannot be read at all. Never expose a blank or
                            // unauthorised QSM view.
                            me.qsmConsoleLoading = false;
                            startNoVnc();
                        },
                    });
                };
                readConfig(0);
            };
            Ext.apply(me, {
                items: [{
                    xtype: 'component',
                    itemId: 'qsm-direct-preparing',
                    html: gettext('Preparing console…'),
                    style: 'display:flex;align-items:center;justify-content:center;background:#000;color:#fff',
                }],
                listeners: {
                    afterrender: selectBackend,
                    activate: selectBackend,
                    beforedestroy: () => {
                        if (me.qsmDirectSession) { me.qsmDirectSession.close(); }
                        me.qsmDirectSession = null;
                        me.qsmDirectFrame = null;
                    },
                },
            });
            me.callParent();
        },
    });

    defineOverride('PVE.qsmDirect.ConsoleButtonOverlay', {
        override: 'PVE.button.ConsoleButton',
        enableQsmDirect: false,
        setEnableQsmDirect: function (enable) {
            this.enableQsmDirect = !!enable;
            const item = this.down('#qsm-direct');
            if (item) { item.setDisabled(!this.enableQsmDirect); }
        },
        initComponent: function () {
            const me = this;
            const kind = me.consoleType === 'kvm' ? 'qemu' : (me.consoleType === 'lxc' ? 'lxc' : null);
            if (kind && !(NATIVE_QEMU && kind === 'qemu') &&
                validNode(me.nodename) && validVmid(Number(me.vmid))) {
                me.itemId = 'qsm-direct-console-button';
                me.menu = (me.menu || []).map((item) => Ext.apply({}, item));
                me.menu.push({ xtype: 'menuitem', itemId: 'qsm-direct', text: 'QSM Direct',
                    iconCls: 'fa fa-desktop', disabled: !me.enableQsmDirect,
                    handler: () => openConsole(me, me.nodename, Number(me.vmid), null, kind) });
                if (kind === 'lxc') {
                    me.menu.push({ xtype: 'menuitem', itemId: 'qsm-direct-connection',
                        text: gettext('Check QSM Direct connection…'), hidden: true,
                        iconCls: 'fa fa-exclamation-triangle',
                        handler: () => {
                            if (NATIVE_QEMU) { if (me.qsmDirectRefresh) { me.qsmDirectRefresh(); } }
                            else { window.open(`${signalOrigin()}/`, '_blank', 'noopener'); }
                        } });
                }
            }
            me.callParent();
        },
    });

    // The active VM sub-tab is remembered globally (PVE.qemu.Config uses the
    // shared `kvmtab` state), so switching from another VM whose Console tab
    // was open makes the new VM's PVE.panel.Config activate its `console` card
    // *inside* initComponent — insertNodes() -> menu.setSelection() ->
    // activateCard('console') — before PVE.qemu.Config's own initComponent has
    // returned. QemuConfigOverlay below swaps the card xtype only after that
    // callParent(), which is too late in this path: the card was already built
    // as stock noVNC, so arriving from another VM's console landed on noVNC
    // even for a managed VM. insertNodes() populates savedItems from the item
    // definitions and runs before any card is activated, so rewrite the QEMU
    // KVM console xtype here. The guard keeps this to the QEMU console only
    // (never LXC, whose console carries consoleType 'lxc'); the QSM selector
    // then still mounts stock noVNC for an unmanaged VM or a failed predicate.
    defineOverride('PVE.qsmDirect.PanelConfigConsoleOverlay', {
        override: 'PVE.panel.Config',
        insertNodes: function (items) {
            if (!NATIVE_QEMU && Array.isArray(items)) {
                items.forEach((item) => {
                    if (item && item.itemId === 'console' &&
                        item.xtype === 'pveNoVncConsole' && item.consoleType === 'kvm') {
                        item.xtype = 'pveQsmDirectConsole';
                    }
                });
            }
            this.callParent([items]);
        },
    });

    // Unlike stock Spice/serial capability bits, PVE's status endpoint does
    // not expose arbitrary QEMU `args`. Read the ordinary protected config
    // once when a VM view opens and only enable this menu item if the exact
    // transport-owned Display1 argument is present.
    defineOverride('PVE.qsmDirect.QemuConfigOverlay', {
        override: 'PVE.qemu.Config',
        initComponent: function () {
            const me = this;
            me.callParent();
            const vm = me.pveSelNode && me.pveSelNode.data;
            const vmid = vm ? Number(vm.vmid) : NaN;
            const setConsoleProvider = (managed) => {
                // insertNodes (PanelConfigConsoleOverlay above) has already
                // rewritten the QEMU console card to the QSM selector in
                // savedItems, even when the card was activated during this
                // callParent(). Reassert it defensively and record the managed
                // predicate; the card itself mounts QSM Direct only for a
                // managed Display1 and otherwise keeps stock noVNC.
                const console = me.savedItems && me.savedItems.console;
                if (console) {
                    console.xtype = 'pveQsmDirectConsole';
                    console.qsmDirectConfigured = Boolean(managed);
                }
            };
            // Install the selector synchronously. The protected config read
            // below is asynchronous, while the existing Console node is
            // immediately clickable after PVE renders the VM view.
            setConsoleProvider(false);
            const setQsmDirectAvailable = (managed) => {
                const button = me.down('#qsm-direct-console-button');
                if (button) { button.setEnableQsmDirect(managed); }
                setConsoleProvider(managed);
            };
            const refreshQsmDirect = () => {
                if (!validNode(vm && vm.node) || !validVmid(vmid)) { return false; }
                Proxmox.Utils.API2Request({
                    url: `/nodes/${encodeURIComponent(vm.node)}/qemu/${encodeURIComponent(vmid)}/config`,
                    method: 'GET',
                    success: ({ result }) => {
                        const data = result && result.data;
                        setQsmDirectAvailable(consoleConfigured(data, vmid));
                    },
                    failure: () => setQsmDirectAvailable(false),
                });
                // The top Console split button is created late on first
                // visits. The left-navigation Console definition is already
                // available in savedItems, so it can be switched on this
                // first request even if the toolbar needs a retry.
                return Boolean(me.down('#qsm-direct-console-button'));
            };
            // PVE constructs the Console button late on some first visits to
            // a VM view. A direct lookup then sees no component and leaves
            // the item disabled until a full page refresh. Retry once after
            // this view has rendered; no configuration state is inferred
            // client-side, it is still read through PVE's protected API.
            if (!refreshQsmDirect() && typeof me.on === 'function') {
                me.on('afterrender', refreshQsmDirect, me, { single: true });
            }
        },
    });

    // LXC containers: the Console split button gains "QSM Direct" (added by
    // ConsoleButtonOverlay for consoleType 'lxc'), enabled once the node's
    // signalling service confirms this container is set up for the QSM
    // console (its policy exists; the call is authorised with VM.Console).
    // The container's own Console tab keeps the stock xterm.js terminal.
    defineOverride('PVE.qsmDirect.LxcConfigOverlay', {
        override: 'PVE.lxc.Config',
        initComponent: function () {
            const me = this;
            me.callParent();
            const ct = me.pveSelNode && me.pveSelNode.data;
            const vmid = ct ? Number(ct.vmid) : NaN;
            if (!validNode(ct && ct.node) || !validVmid(vmid)) { return; }
            let enabled = null;
            let error = null;
            let pending = false;
            let observedMenu = null;
            const apply = () => {
                if (me.destroyed) { return false; }
                const button = me.down('#qsm-direct-console-button');
                if (button && enabled !== null) {
                    button.setEnableQsmDirect(enabled);
                    const hint = button.down('#qsm-direct-connection');
                    if (hint) {
                        hint.setHidden(!error);
                        const restricted = error && [401, 403, 404].includes(error.status);
                        hint.setDisabled(!!restricted);
                        hint.setText(error && error.status === 404
                            ? gettext('QSM Direct is not configured for this container')
                            : restricted ? gettext('QSM Direct access denied')
                            : NATIVE_QEMU ? gettext('Retry QSM Direct connection')
                            : gettext('Check QSM Direct connection…'));
                        hint.setTooltip(NATIVE_QEMU
                            ? gettext('Retry the console availability check through the Proxmox API.')
                            : gettext('Open the QSM service to check its certificate and network access, then reopen this menu.'));
                    }
                }
                const menu = button && button.getMenu();
                if (menu && observedMenu !== menu) {
                    button.qsmDirectRefresh = refresh;
                    observedMenu = menu;
                    menu.on('beforeshow', () => { if (!enabled) { refresh(); } });
                }
                return Boolean(button);
            };
            const refresh = () => {
                if (pending || me.destroyed) { return; }
                pending = true;
                signalRequest('GET', `/nodes/${encodeURIComponent(ct.node)}/lxc/${encodeURIComponent(vmid)}/qsm-direct-settings`)
                    .then(() => { enabled = true; error = null; }, reason => { enabled = false; error = reason; })
                    .then(() => { pending = false; apply(); });
            };
            // Register before the asynchronous probe: a failed fetch can
            // finish after afterrender, when adding that listener is too late.
            if (typeof me.on === 'function') {
                me.on('afterrender', apply, me, { single: true });
            }
            apply();
            refresh();
        },
    });

    if (VIEWER) {
        Ext.onReady(() => openConsole(null, VIEWER.node, VIEWER.vmid, null, 'qemu'));
    }
}());
