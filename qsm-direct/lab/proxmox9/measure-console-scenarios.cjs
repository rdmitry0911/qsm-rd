#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Scenario comparison of QSM Direct and stock noVNC through the visible PVE
 * UI, with the shipped console surfaces and one shared guest fixture.
 *
 * The guest runs the scenario fixture built by build-scenario-seed.py.  The
 * measuring browser switches the guest workload by pressing Alt+1..4 inside
 * the console under test — the same input path a person uses — so both
 * transports are driven identically and no side channel into the guest is
 * required.  For each scenario the script records what the person would
 * see: how many visibly different pictures per second reach the browser
 * surface, the longest interval without a visible change, and the network
 * bytes the browser received for it.  QSM Direct additionally reports the
 * WebRTC inbound-rtp decoder statistics; noVNC reports RFB WebSocket frames.
 *
 * Output is one JSON object with fixed keys.  It never prints the PVE
 * password, cookie, ticket, SDP, WebSocket URL, or guest pixels.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const fail = (code) => { const error = new Error(code); error.code = code; throw error; };
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const nodePattern = /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/;
const userPattern = /^[^\s@/:\\\x00-\x1f]{1,64}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
let phase = 'INITIALIZING';
let lastReport = null;

const SCENARIO_NAMES = { 1: 'idle-fixture', 2: 'video-clip', 3: 'document-scroll', 4: 'animated-scene',
    5: 'document-manual-wheel' };

function options(argv) {
    const result = {
        transport: '', timeout: 60_000, chrome: process.env.QSM_DIRECT_CHROME || '',
        scenarios: [1, 2, 3, 4], durationMs: 10_000, settleMs: 4_000, wheelIntervalMs: 60, wheelDelta: 100,
        width: 1280, height: 800, report: '', label: '', hoverRuns: 0,
    };
    const keys = new Set(['--transport', '--pve-url', '--user', '--password-file', '--vmid', '--timeout-ms', '--chrome',
        '--scenarios', '--duration-ms', '--settle-ms', '--wheel-interval-ms', '--wheel-delta', '--width', '--height',
        '--report', '--label', '--hover-runs']);
    for (let index = 0; index < argv.length; index += 1) {
        const key = argv[index];
        if (!keys.has(key) || index + 1 === argv.length) { fail('INVALID_ARGUMENTS'); }
        const value = argv[++index];
        switch (key) {
        case '--timeout-ms': result.timeout = Number(value); break;
        case '--duration-ms': result.durationMs = Number(value); break;
        case '--settle-ms': result.settleMs = Number(value); break;
        case '--wheel-interval-ms': result.wheelIntervalMs = Number(value); break;
        case '--wheel-delta': result.wheelDelta = Number(value); break;
        case '--width': result.width = Number(value); break;
        case '--height': result.height = Number(value); break;
        case '--hover-runs': result.hoverRuns = Number(value); break;
        case '--scenarios': result.scenarios = value.split(',').map(Number); break;
        default: result[key.slice(2)] = value;
        }
    }
    if (!['qsm', 'novnc'].includes(result.transport) || !result['pve-url'] || !result.user || !result['password-file'] ||
        !result.vmid || !/^https:\/\/[^/?#]+(?::[0-9]{1,5})?$/.test(result['pve-url']) || !userPattern.test(result.user) ||
        !/^[1-9][0-9]*$/.test(result.vmid)) { fail('INVALID_ARGUMENTS'); }
    result.vmid = Number(result.vmid);
    for (const name of ['timeout', 'durationMs', 'settleMs', 'wheelIntervalMs', 'width', 'height']) {
        if (!Number.isSafeInteger(result[name]) || result[name] <= 0) { fail('INVALID_ARGUMENTS'); }
    }
    // A negative wheel delta scrolls towards the top; it is accepted so the
    // guest's wheel direction can itself be verified with this tool.
    if (!Number.isSafeInteger(result.wheelDelta) || result.wheelDelta === 0) { fail('INVALID_ARGUMENTS'); }
    if (!Number.isSafeInteger(result.hoverRuns) || result.hoverRuns < 0 || result.hoverRuns > 20) { fail('INVALID_ARGUMENTS'); }
    if (!result.scenarios.length || result.scenarios.some((value) => !SCENARIO_NAMES[value])) { fail('INVALID_ARGUMENTS'); }
    return result;
}

function readPassword(file) {
    const fd = fs.openSync(path.resolve(file), fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
    try {
        const stat = fs.fstatSync(fd);
        if (!stat.isFile() || stat.size < 1 || stat.size > 4096 || (stat.mode & 0o077) !== 0) { fail('UNSAFE_PASSWORD_FILE'); }
        const bytes = Buffer.alloc(stat.size);
        if (fs.readSync(fd, bytes, 0, bytes.length, 0) !== bytes.length) { fail('PASSWORD_FILE_UNAVAILABLE'); }
        const text = bytes.toString('utf8').replace(/\r?\n$/, '');
        bytes.fill(0);
        if (!text || /[\r\n\0]/.test(text)) { fail('PASSWORD_FILE_UNAVAILABLE'); }
        return text;
    } finally { fs.closeSync(fd); }
}

async function selectRealm(page, realm, timeout) {
    const description = await page.waitForFunction((wanted) => {
        const combo = window.Ext?.getCmp?.('pveloginrealm');
        const record = combo?.getStore?.().findRecord?.('realm', wanted, 0, false, true, true);
        return record ? String(record.get('descr') || '') : null;
    }, realm, { timeout }).then((handle) => handle.jsonValue());
    if (!description) { fail('PVE_REALM_UNAVAILABLE'); }
    await page.locator('#pveloginrealm-inputEl:visible').click();
    await page.locator('.x-boundlist:visible .x-boundlist-item').filter({ hasText: description }).first().click();
}

async function selectVm(page, vmid, timeout) {
    const value = await page.waitForFunction((wanted) => {
        const tree = window.Ext?.ComponentQuery?.query('pveResourceTree')?.[0];
        const record = tree?.getStore?.().getRootNode()?.findChild?.('id', `qemu/${wanted}`, true);
        if (!record || record.data.type !== 'qemu' || Number(record.data.vmid) !== wanted || !record.data.node) { return null; }
        tree.selectById(record.data.id);
        return String(record.data.node);
    }, vmid, { timeout }).then((handle) => handle.jsonValue());
    if (!value || !nodePattern.test(value)) { fail('QEMU_VM_UNAVAILABLE'); }
    return value;
}

async function openConsoleMenuItem(page, vmid, label, timeout) {
    const selector = await page.waitForFunction((wanted) => {
        const button = window.Ext?.ComponentQuery?.query('pveConsoleButton')?.find((candidate) =>
            candidate?.consoleType === 'kvm' && Number(candidate.vmid) === wanted && candidate.rendered && !candidate.disabled);
        return button?.getEl?.().dom?.id || null;
    }, vmid, { timeout }).then((handle) => handle.jsonValue());
    if (!selector) { fail('CONSOLE_BUTTON_UNAVAILABLE'); }
    const button = page.locator(`#${selector}`);
    const box = await button.boundingBox();
    if (!box) { fail('CONSOLE_BUTTON_UNAVAILABLE'); }
    await page.mouse.click(box.x + box.width - 6, box.y + box.height / 2);
    const item = page.locator('.x-menu:visible .x-menu-item').filter({ hasText: label });
    // ExtJS renders the split menu asynchronously; a count taken in the same
    // task as the click races the menu layout on a slower browser host.
    await item.first().waitFor({ state: 'visible', timeout }).catch(() => fail('CONSOLE_MENU_ITEM_UNAVAILABLE'));
    if (await item.count() !== 1) { fail('CONSOLE_MENU_ITEM_UNAVAILABLE'); }
    const popupPromise = page.waitForEvent('popup');
    const openedAt = performance.now();
    await item.click();
    const popup = await popupPromise;
    popup.setDefaultTimeout(timeout);
    return { popup, openedAt };
}

function payloadBytes(frame) {
    const payload = frame?.payload;
    if (typeof payload === 'string') { return Buffer.byteLength(payload, 'utf8'); }
    if (Buffer.isBuffer(payload)) { return payload.length; }
    if (payload instanceof Uint8Array) { return payload.byteLength; }
    return 0;
}

// Surface helpers run inside the popup.  `surface()` returns the element
// that carries guest pixels: the QSM <video> or the noVNC RFB <canvas>.
const SURFACE_SCRIPT = `
    window.__qsmSurface = () => document.querySelector('video') ||
        document.querySelector('canvas') || document.querySelector('iframe')?.contentDocument?.querySelector('canvas');
`;

async function waitForSurface(popup, transport, timeout) {
    await popup.evaluate(SURFACE_SCRIPT);
    await popup.waitForFunction((kind) => {
        const element = window.__qsmSurface();
        if (!element) { return false; }
        if (kind === 'qsm') {
            return element.tagName === 'VIDEO' && element.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
                element.videoWidth > 0 && element.videoHeight > 0;
        }
        if (element.tagName !== 'CANVAS' || element.width < 64 || element.height < 64) { return false; }
        try {
            const context = element.getContext('2d', { willReadFrequently: true });
            const pixels = context.getImageData(0, 0, Math.min(element.width, 32), Math.min(element.height, 32)).data;
            return Array.from(pixels).some((value) => value !== 0);
        } catch (_error) { return false; }
    }, transport, { timeout });
}

// Install the visible-change sampler.  It signs each presented picture with
// a 160×90 downscale and counts a new picture only when at least 0.5 % of
// those cells changed their luma by more than 16, so an H.264 repeat frame
// (decoder noise only) and an unchanged RFB canvas both count as "nothing
// new for the eye", while a scrolled text page or a new video frame counts.
const SAMPLER_SCRIPT = `
    (() => {
        const element = window.__qsmSurface();
        const canvas = document.createElement('canvas');
        canvas.width = 160; canvas.height = 90;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        const state = { running: true, presented: 0, distinct: 0, lastSignature: null, lastDistinctAt: null,
            maxGapMs: 0, startedAt: performance.now(), gaps: [] };
        window.__qsmSampler = state;
        // Two complementary tests: a localized change (a scrolled line, a
        // moved window) trips the changed-cell count; smooth full-frame
        // motion (a film, an animation) moves every cell a little and trips
        // the mean luma difference.  Neither is reached by decoder noise on
        // a repeated H.264 frame or by an untouched RFB canvas.
        const differs = (previous, current) => {
            if (!previous) { return true; }
            let changed = 0;
            let total = 0;
            const cells = current.length / 4;
            for (let index = 0; index < current.length; index += 4) {
                const before = 0.299 * previous[index] + 0.587 * previous[index + 1] + 0.114 * previous[index + 2];
                const after = 0.299 * current[index] + 0.587 * current[index + 1] + 0.114 * current[index + 2];
                const delta = Math.abs(before - after);
                total += delta;
                if (delta > 16 && ++changed >= 72) { return true; }
            }
            return total / cells >= 0.25;
        };
        const observe = () => {
            if (!state.running) { return; }
            state.presented += 1;
            try {
                context.drawImage(element, 0, 0, 160, 90);
                const signature = context.getImageData(0, 0, 160, 90).data;
                if (differs(state.lastSignature, signature)) {
                    const now = performance.now();
                    if (state.lastDistinctAt !== null) {
                        const gap = now - state.lastDistinctAt;
                        state.gaps.push(gap);
                        if (gap > state.maxGapMs) { state.maxGapMs = gap; }
                    }
                    state.lastDistinctAt = now;
                    state.distinct += 1;
                    state.lastSignature = signature;
                }
            } catch (_error) { /* keep sampling */ }
            schedule();
        };
        const schedule = () => {
            if (element.tagName === 'VIDEO' && typeof element.requestVideoFrameCallback === 'function') {
                element.requestVideoFrameCallback(observe);
            } else {
                requestAnimationFrame(observe);
            }
        };
        schedule();
    })();
`;

const STOP_SAMPLER_SCRIPT = `
    (() => {
        const state = window.__qsmSampler;
        if (!state) { return null; }
        state.running = false;
        const elapsedMs = performance.now() - state.startedAt;
        const sorted = state.gaps.slice().sort((a, b) => a - b);
        const percentile = (fraction) => sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * fraction))] : null;
        return { elapsedMs, presented: state.presented, distinct: state.distinct, maxGapMs: state.maxGapMs,
            gapP50Ms: percentile(0.5), gapP95Ms: percentile(0.95) };
    })();
`;

async function readRtcStats(page) {
    return page.evaluate(async () => {
        const peers = window.__qsmPeers || [];
        const peer = peers[peers.length - 1];
        if (!peer) { return null; }
        const report = await peer.getStats();
        const result = { bytesReceived: 0, framesDecoded: 0, framesDropped: 0, framesReceived: 0,
            freezeCount: 0, totalFreezesDuration: 0, jitterBufferDelay: 0, jitterBufferEmittedCount: 0,
            totalInterFrameDelay: 0, audioBytesReceived: 0 };
        report.forEach((entry) => {
            if (entry.type !== 'inbound-rtp') { return; }
            if (entry.kind === 'video') {
                result.bytesReceived += entry.bytesReceived || 0;
                result.framesDecoded += entry.framesDecoded || 0;
                result.framesDropped += entry.framesDropped || 0;
                result.framesReceived += entry.framesReceived || 0;
                result.freezeCount += entry.freezeCount || 0;
                result.totalFreezesDuration += entry.totalFreezesDuration || 0;
                result.jitterBufferDelay += entry.jitterBufferDelay || 0;
                result.jitterBufferEmittedCount += entry.jitterBufferEmittedCount || 0;
                result.totalInterFrameDelay += entry.totalInterFrameDelay || 0;
            } else if (entry.kind === 'audio') {
                result.audioBytesReceived += entry.bytesReceived || 0;
            }
        });
        return result;
    });
}

// Coarse fingerprint of what the guest currently shows: mean luma of the
// surface and whether two samples 400 ms apart differ.  It is enough to tell
// the four fixture pages apart: the input fixture is dark and static, the
// document is bright and static, the clip and the animation are moving.
const FINGERPRINT_SCRIPT = `
    (async () => {
        const element = window.__qsmSurface();
        const canvas = document.createElement('canvas');
        canvas.width = 32; canvas.height = 18;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        const sample = () => {
            context.drawImage(element, 0, 0, 32, 18);
            const pixels = context.getImageData(0, 0, 32, 18).data;
            let sum = 0;
            for (let index = 0; index < pixels.length; index += 4) {
                sum += 0.299 * pixels[index] + 0.587 * pixels[index + 1] + 0.114 * pixels[index + 2];
            }
            return { luma: sum / (pixels.length / 4), pixels };
        };
        const first = sample();
        await new Promise((resolve) => setTimeout(resolve, 400));
        const second = sample();
        let difference = 0;
        for (let index = 0; index < first.pixels.length; index += 4) {
            difference += Math.abs(first.pixels[index] - second.pixels[index]);
        }
        return { luma: Math.round(second.luma), moving: difference > 96 };
    })();
`;

const scenarioMatches = (number, fingerprint) => {
    if (!fingerprint) { return false; }
    switch (number) {
    case 1: return !fingerprint.moving && fingerprint.luma < 90;
    case 3: case 5: return fingerprint.luma >= 90;
    default: return fingerprint.moving;
    }
};

// Give the guest keyboard focus once through the console's own focus path.
// noVNC stops forwarding Alt chords after a second click on an already
// focused canvas, so the surface is clicked exactly once per session and
// every later scenario switch relies on that focus.
async function focusSurface(popup, transport) {
    const surface = popup.locator(transport === 'qsm' ? 'video' : 'canvas').first();
    const box = await surface.boundingBox();
    if (!box) { fail('SURFACE_UNAVAILABLE'); }
    await popup.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.9);
    await popup.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.9);
    await wait(800);
    return box;
}

async function switchScenario(popup, transport, number) {
    const surface = popup.locator(transport === 'qsm' ? 'video' : 'canvas').first();
    const box = await surface.boundingBox();
    if (!box) { fail('SURFACE_UNAVAILABLE'); }
    // Send the chord as explicit physical edges with the digit's key code:
    // Alt down, Digit<n> press, Alt up is what both consoles forward to the
    // guest.  noVNC can drop the first chord shortly after a focus click,
    // so verify the guest picture and repeat; switch.js ignores a chord for
    // the page that is already shown.
    for (let attempt = 0; attempt < 5; attempt += 1) {
        await popup.keyboard.down('Alt');
        await popup.keyboard.press(`Digit${number}`);
        await popup.keyboard.up('Alt');
        await wait(1200);
        const fingerprint = await popup.evaluate(FINGERPRINT_SCRIPT).catch(() => null);
        if (process.env.QSM_SCENARIO_DEBUG) {
            process.stderr.write(`QSM_SCENARIO_SWITCH scenario=${number} attempt=${attempt} fingerprint=${JSON.stringify(fingerprint)}\n`);
        }
        if (scenarioMatches(number, fingerprint)) { return box; }
    }
    fail(`SCENARIO_${number}_NOT_SHOWN`);
    return box;
}

async function main() {
    const config = options(process.argv.slice(2));
    let password = readPassword(config['password-file']);
    let browser;
    let context;
    let peerWatcher = null;
    const websocket = { rfbFrames: 0, rfbBytes: 0 };
    const report = { transport: config.transport, label: config.label, vmid: config.vmid,
        viewport: `${config.width}x${config.height}`, durationMs: config.durationMs, scenarios: [], events: [] };
    lastReport = report;
    try {
        phase = 'OPENING_BROWSER';
        let chromium;
        try { ({ chromium } = require('playwright')); }
        catch (_error) { ({ chromium } = require('/opt/qsm-browser/node_modules/playwright-core')); }
        browser = await chromium.launch({ headless: true, executablePath: config.chrome || undefined, args: [
            '--autoplay-policy=no-user-gesture-required', '--no-proxy-server', '--no-sandbox',
            '--disable-setuid-sandbox', '--disable-seccomp-filter-sandbox', '--disable-dev-shm-usage',
            '--disable-features=NetworkServiceSandbox,WebRtcHideLocalIpsWithMdns',
            '--force-webrtc-ip-handling-policy=default',
        ] });
        context = await browser.newContext({ ignoreHTTPSErrors: true, locale: 'en-US',
            viewport: { width: config.width, height: config.height } });
        // The shipped console creates its RTCPeerConnection in the PVE page
        // (the popup's opener).  Keep a reference so inbound-rtp statistics
        // of the real product transport can be read without modifying it.
        await context.addInitScript(() => {
            const Original = window.RTCPeerConnection;
            if (!Original) { return; }
            const Wrapped = function (...args) {
                const peer = new Original(...args);
                (window.__qsmPeers = window.__qsmPeers || []).push(peer);
                return peer;
            };
            Wrapped.prototype = Original.prototype;
            Object.setPrototypeOf(Wrapped, Original);
            window.RTCPeerConnection = Wrapped;
        });
        const page = await context.newPage();
        page.setDefaultTimeout(config.timeout);
        const observeWebSocket = (socket) => {
            if (!/\/vncwebsocket(?:\?|$)/.test(socket.url())) { return; }
            socket.on('framereceived', (frame) => { websocket.rfbFrames += 1; websocket.rfbBytes += payloadBytes(frame); });
        };
        page.on('websocket', observeWebSocket);
        phase = 'OPENING_PVE_UI';
        await page.goto(`${config['pve-url']}/`, { waitUntil: 'domcontentloaded' });
        const [username, realm] = config.user.split('@');
        await page.locator('input[name="username"]:visible').fill(username);
        await selectRealm(page, realm, config.timeout);
        await page.locator('input[name="password"]:visible').fill(password);
        password = '';
        phase = 'AUTHENTICATING_PVE';
        await page.locator('.x-window:visible .x-btn').filter({ hasText: /^Login$/ }).last().click();
        await page.waitForFunction((expected) => window.Proxmox?.UserName === expected && !!window.PVE, config.user);
        const notice = page.locator('.x-message-box:visible');
        if (await notice.count()) { await notice.locator('.x-btn').filter({ hasText: /^OK$/ }).click(); }
        phase = 'SELECTING_VM';
        await selectVm(page, config.vmid, config.timeout);
        phase = 'OPENING_CONSOLE';
        const { popup, openedAt } = await openConsoleMenuItem(page, config.vmid,
            config.transport === 'qsm' ? /^QSM Direct$/ : /^noVNC$/, config.timeout);
        popup.on('websocket', observeWebSocket);
        // Diagnostic trail for a console that closes itself mid-run: the
        // console's own status line is not visible to this script, so keep
        // its close event and any page errors (never credentials or SDP).
        popup.on('close', () => report.events.push({ at: Math.round(performance.now() - openedAt), name: 'popup-closed' }));
        popup.on('pageerror', (error) => report.events.push({ at: Math.round(performance.now() - openedAt),
            name: 'pageerror', text: String(error.message || '').slice(0, 160) }));
        popup.on('console', (message) => {
            if (message.type() === 'error') {
                report.events.push({ at: Math.round(performance.now() - openedAt), name: 'console-error', text: message.text().slice(0, 160) });
            }
        });
        // Track the product transport's own state transitions from the
        // opener page: a console that closes itself is explained by the
        // last ICE/DTLS state rather than guessed at.
        let lastPeerState = '';
        peerWatcher = config.transport === 'qsm' ? setInterval(() => {
            page.evaluate(() => {
                const peers = window.__qsmPeers || [];
                const peer = peers[peers.length - 1];
                if (!peer) { return null; }
                return `${peer.connectionState}/${peer.iceConnectionState}/${peer.signalingState}`;
            }).then((state) => {
                if (state && state !== lastPeerState) {
                    lastPeerState = state;
                    report.events.push({ at: Math.round(performance.now() - openedAt), name: 'peer-state', state });
                }
            }).catch(() => undefined);
        }, 500) : null;
        phase = 'WAITING_FOR_SURFACE';
        await waitForSurface(popup, config.transport, config.timeout);
        report.firstPictureMs = Math.round(performance.now() - openedAt);
        report.surface = await popup.evaluate(() => {
            const element = window.__qsmSurface();
            return element.tagName === 'VIDEO' ? `${element.videoWidth}x${element.videoHeight}` : `${element.width}x${element.height}`;
        });
        phase = 'FOCUSING_SURFACE';
        await focusSurface(popup, config.transport);
        for (const number of config.scenarios) {
            phase = `SCENARIO_${number}`;
            const box = await switchScenario(popup, config.transport, number);
            await wait(config.settleMs);
            const statsBefore = config.transport === 'qsm' ? await readRtcStats(page) : null;
            const socketBefore = { ...websocket };
            const startedAt = performance.now();
            await popup.evaluate(SAMPLER_SCRIPT);
            if (number === 5) {
                // Scroll the still document through the console with an
                // ordinary wheel at a steady human-like cadence: this checks
                // the console's wheel path and direction, not the transport.
                const centre = { x: box.x + box.width * 0.5, y: box.y + box.height * 0.55 };
                await popup.mouse.move(centre.x, centre.y);
                while (performance.now() - startedAt < config.durationMs) {
                    await popup.mouse.wheel(0, config.wheelDelta);
                    await wait(config.wheelIntervalMs);
                }
            } else {
                await wait(config.durationMs);
            }
            const sampler = await popup.evaluate(STOP_SAMPLER_SCRIPT);
            const elapsedMs = performance.now() - startedAt;
            const statsAfter = config.transport === 'qsm' ? await readRtcStats(page) : null;
            const entry = {
                scenario: number, name: SCENARIO_NAMES[number], sampleMs: Math.round(elapsedMs),
                distinctPicturesPerSecond: sampler ? Number((sampler.distinct * 1000 / sampler.elapsedMs).toFixed(2)) : null,
                presentedPerSecond: sampler ? Number((sampler.presented * 1000 / sampler.elapsedMs).toFixed(2)) : null,
                maxPictureGapMs: sampler ? Math.round(sampler.maxGapMs) : null,
                pictureGapP50Ms: sampler && sampler.gapP50Ms !== null ? Math.round(sampler.gapP50Ms) : null,
                pictureGapP95Ms: sampler && sampler.gapP95Ms !== null ? Math.round(sampler.gapP95Ms) : null,
            };
            if (config.transport === 'qsm' && statsBefore && statsAfter) {
                const seconds = elapsedMs / 1000;
                const emitted = statsAfter.jitterBufferEmittedCount - statsBefore.jitterBufferEmittedCount;
                entry.receivedMbps = Number(((statsAfter.bytesReceived - statsBefore.bytesReceived) * 8 / (elapsedMs * 1000)).toFixed(3));
                entry.audioMbps = Number(((statsAfter.audioBytesReceived - statsBefore.audioBytesReceived) * 8 / (elapsedMs * 1000)).toFixed(3));
                entry.decodedFps = Number(((statsAfter.framesDecoded - statsBefore.framesDecoded) / seconds).toFixed(1));
                entry.droppedFrames = statsAfter.framesDropped - statsBefore.framesDropped;
                entry.freezeCount = statsAfter.freezeCount - statsBefore.freezeCount;
                entry.freezeDurationMs = Math.round((statsAfter.totalFreezesDuration - statsBefore.totalFreezesDuration) * 1000);
                entry.jitterBufferMeanMs = emitted > 0
                    ? Number(((statsAfter.jitterBufferDelay - statsBefore.jitterBufferDelay) * 1000 / emitted).toFixed(2)) : null;
            } else if (config.transport === 'novnc') {
                entry.receivedMbps = Number(((websocket.rfbBytes - socketBefore.rfbBytes) * 8 / (elapsedMs * 1000)).toFixed(3));
                entry.rfbFramesPerSecond = Number(((websocket.rfbFrames - socketBefore.rfbFrames) * 1000 / elapsedMs).toFixed(1));
            }
            report.scenarios.push(entry);
        }
        phase = 'RESTORING_FIXTURE';
        await switchScenario(popup, config.transport, 1);
        await wait(1000);
        if (config.hoverRuns > 0) {
            // Input-to-pixel latency on the fixture page: move the pointer
            // from a neutral spot onto the blue icon and wait until the
            // magenta hover popup is visible in the decoded/painted surface.
            // Both transports are read at their browser surface, so the
            // number includes the guest's own hover repaint in both cases.
            phase = 'MEASURING_HOVER';
            report.hover = [];
            const surface = popup.locator(config.transport === 'qsm' ? 'video' : 'canvas').first();
            const box = await surface.boundingBox();
            const geometry = await popup.evaluate(() => {
                const element = window.__qsmSurface();
                return element.tagName === 'VIDEO' ? { width: element.videoWidth, height: element.videoHeight }
                    : { width: element.width, height: element.height };
            });
            if (!box || geometry.width !== 1280 || geometry.height < 480) { fail('HOVER_FIXTURE_UNAVAILABLE'); }
            const point = (x, y) => ({ x: box.x + ((x + 0.5) * box.width / geometry.width), y: box.y + ((y + 0.5) * box.height / geometry.height) });
            for (let run = 0; run < config.hoverRuns; run += 1) {
                const reset = point(40, 40);
                await popup.mouse.move(reset.x, reset.y);
                await wait(700);
                const target = point(640, 370);
                const started = await popup.evaluate(() => performance.now());
                await popup.mouse.move(target.x, target.y);
                const latencyMs = await popup.waitForFunction((origin) => {
                    const element = window.__qsmSurface();
                    const probe = document.createElement('canvas');
                    probe.width = 1; probe.height = 1;
                    const context = probe.getContext('2d', { willReadFrequently: true });
                    try {
                        // Interior of the fixture's #ff00ff popup at (780, 350).
                        context.drawImage(element, 780, 350, 1, 1, 0, 0, 1, 1);
                        const pixel = context.getImageData(0, 0, 1, 1).data;
                        return pixel[0] > 180 && pixel[1] < 100 && pixel[2] > 180 ? performance.now() - origin : false;
                    } catch (_error) { return false; }
                }, started, { timeout: config.timeout, polling: 'raf' }).then((handle) => handle.jsonValue());
                report.hover.push(Number(Number(latencyMs).toFixed(1)));
            }
            const sorted = report.hover.slice().sort((a, b) => a - b);
            report.hoverMedianMs = sorted[Math.floor(sorted.length / 2)];
        }
        phase = 'PASS';
        if (config.report) { fs.writeFileSync(config.report, JSON.stringify(report, null, 1)); }
        process.stdout.write(`QSM_SCENARIO_MEASUREMENT ${JSON.stringify(report)}\n`);
    } finally {
        password = '';
        if (peerWatcher) { clearInterval(peerWatcher); }
        if (context) { await context.close(); }
        if (browser) { await browser.close(); }
    }
}

main().catch((error) => {
    const code = error && /^[A-Z0-9_]+$/.test(error.code || '') ? error.code : 'FAILED';
    const events = lastReport && lastReport.events ? JSON.stringify(lastReport.events.slice(-6)) : '[]';
    process.stderr.write(`QSM_SCENARIO_MEASUREMENT_FAIL code=${code} phase=${phase} message=${String(error && error.message || '').slice(0, 160)} events=${events}\n`);
    process.exitCode = 1;
});
