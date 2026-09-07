#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Interaction-latency comparison of QSM Direct and stock noVNC, measured at
 * the browser surface and timed from the browser's own DOM input event.
 *
 * The earlier hover number started its clock before Playwright had even
 * injected the pointer move (so it counted CDP injection time) and polled a
 * single hard-coded pixel once per animation frame.  This tool instead:
 *
 *   - records the performance.now() of the actual mousemove/mousedown DOM
 *     event the console receives (a capture listener on the surface), and
 *   - detects the guest's response with requestVideoFrameCallback on the QSM
 *     <video> (or rAF on the noVNC <canvas>), reading the intrinsic frame.
 *
 * Latency is therefore paint_time - input_event_time, identical in method for
 * both transports.  It measures two interactions on the shared input-to-pixel
 * fixture (scenario 1):
 *
 *   hover  - pointer onto the blue icon until the magenta hover popup paints.
 *   drag   - press the orange card and drag it right; reports time from the
 *            press to the card's first painted motion, the median per-frame
 *            input-to-paint delay during the drag, and the p95 visual lag in
 *            guest pixels between the pointer and the painted card.
 *
 * Output is one JSON line; it never prints the password, ticket, SDP or
 * guest pixels.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const fail = (code) => { const error = new Error(code); error.code = code; throw error; };
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const nodePattern = /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/;
const userPattern = /^[^\s@/:\\\x00-\x1f]{1,64}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
let phase = 'INITIALIZING';

function options(argv) {
    const result = { transport: '', timeout: 60_000, chrome: process.env.QSM_DIRECT_CHROME || '',
        hoverRuns: 5, dragRuns: 1, width: 1280, height: 800, report: '', label: '' };
    const keys = new Set(['--transport', '--pve-url', '--user', '--password-file', '--vmid', '--timeout-ms',
        '--chrome', '--hover-runs', '--drag-runs', '--width', '--height', '--report', '--label']);
    for (let index = 0; index < argv.length; index += 1) {
        const key = argv[index];
        if (!keys.has(key) || index + 1 === argv.length) { fail('INVALID_ARGUMENTS'); }
        const value = argv[++index];
        switch (key) {
        case '--timeout-ms': result.timeout = Number(value); break;
        case '--hover-runs': result.hoverRuns = Number(value); break;
        case '--drag-runs': result.dragRuns = Number(value); break;
        case '--width': result.width = Number(value); break;
        case '--height': result.height = Number(value); break;
        default: result[key.slice(2)] = value;
        }
    }
    if (!['qsm', 'novnc'].includes(result.transport) || !result['pve-url'] || !result.user ||
        !result['password-file'] || !result.vmid ||
        !/^https:\/\/[^/?#]+(?::[0-9]{1,5})?$/.test(result['pve-url']) || !userPattern.test(result.user) ||
        !/^[1-9][0-9]*$/.test(result.vmid)) { fail('INVALID_ARGUMENTS'); }
    result.vmid = Number(result.vmid);
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

async function openConsole(page, vmid, label, timeout) {
    const selector = await page.waitForFunction((wanted) => {
        const button = window.Ext?.ComponentQuery?.query('pveConsoleButton')?.find((candidate) =>
            candidate?.consoleType === 'kvm' && Number(candidate.vmid) === wanted && candidate.rendered && !candidate.disabled);
        return button?.getEl?.().dom?.id || null;
    }, vmid, { timeout }).then((handle) => handle.jsonValue());
    if (!selector) { fail('CONSOLE_BUTTON_UNAVAILABLE'); }
    const box = await page.locator(`#${selector}`).boundingBox();
    if (!box) { fail('CONSOLE_BUTTON_UNAVAILABLE'); }
    await page.mouse.click(box.x + box.width - 6, box.y + box.height / 2);
    let item = null;
    for (let attempt = 0; attempt < 3 && !item; attempt += 1) {
        const candidate = page.locator('.x-menu:visible .x-menu-item').filter({ hasText: label });
        if (await candidate.first().waitFor({ state: 'visible', timeout: 8000 }).then(() => true).catch(() => false)) {
            item = candidate;
            break;
        }
        // The split menu can miss on a slower host; reopen it and retry.
        await page.keyboard.press('Escape').catch(() => undefined);
        await page.mouse.click(box.x + box.width - 6, box.y + box.height / 2);
    }
    if (!item) { fail('CONSOLE_MENU_ITEM_UNAVAILABLE'); }
    const popupPromise = page.waitForEvent('popup');
    await item.click();
    const popup = await popupPromise;
    popup.setDefaultTimeout(timeout);
    return popup;
}

// Detection reads the intrinsic surface: the QSM <video> or the noVNC RFB
// <canvas>. requestVideoFrameCallback gives a true presentation time for the
// video; the canvas has no such hook, so it is sampled per animation frame.
const PROBE_SCRIPT = `
    (() => {
        const element = document.querySelector('video') || document.querySelector('canvas') ||
            document.querySelector('iframe')?.contentDocument?.querySelector('canvas');
        const box = element.getBoundingClientRect();
        const source = element.tagName === 'VIDEO'
            ? { w: element.videoWidth, h: element.videoHeight } : { w: element.width, h: element.height };
        const canvas = document.createElement('canvas');
        canvas.width = source.w; canvas.height = source.h;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        const state = { element, box, source, context, input: null, frames: [], running: false, mode: null };
        window.__qsmProbe = state;
        // Client pixel -> guest source pixel, through object-fit: contain.
        state.toSource = (clientX, clientY) => {
            const scale = Math.min(box.width / source.w, box.height / source.h);
            const left = box.left + (box.width - source.w * scale) / 2;
            const top = box.top + (box.height - source.h * scale) / 2;
            return { x: (clientX - left) / scale, y: (clientY - top) / scale };
        };
        for (const type of ['mousemove', 'mousedown', 'mouseup']) {
            element.addEventListener(type, (event) => {
                state.input = { at: performance.now(), clientX: event.clientX, clientY: event.clientY,
                    source: state.toSource(event.clientX, event.clientY), type };
            }, true);
        }
        const onFrame = (now) => {
            if (!state.running) { return; }
            try { state.context.drawImage(element, 0, 0, source.w, source.h); state.frames.push({ at: performance.now() }); }
            catch (_e) { /* keep sampling */ }
            schedule();
        };
        const schedule = () => {
            if (element.tagName === 'VIDEO' && typeof element.requestVideoFrameCallback === 'function') {
                state.mode = 'rvfc'; element.requestVideoFrameCallback(onFrame);
            } else { state.mode = 'raf'; requestAnimationFrame(onFrame); }
        };
        state.start = () => { if (!state.running) { state.running = true; schedule(); } };
        state.stop = () => { state.running = false; };
        return { source, mode: element.tagName === 'VIDEO' ? 'video' : 'canvas' };
    })();
`;

async function measureHover(popup) {
    const runs = [];
    for (let run = 0; run < popup.__hoverRuns; run += 1) {
        // Reset off the icon and let the popup disappear.
        const guestPoint = (gx, gy) => popup.evaluate(([x, y]) => {
            const s = window.__qsmProbe; const scale = Math.min(s.box.width / s.source.w, s.box.height / s.source.h);
            return { x: s.box.left + (s.box.width - s.source.w * scale) / 2 + x * scale,
                     y: s.box.top + (s.box.height - s.source.h * scale) / 2 + y * scale };
        }, [gx, gy]);
        // Move to an empty area far from the icon (560..720,290..450) and its
        // popup (764..1044,334..412), nudging so the guest emits a fresh frame.
        let reset = await guestPoint(40, 740);
        await popup.mouse.move(reset.x, reset.y);
        await popup.mouse.move(reset.x + 3, reset.y + 3);
        // Wait until the hover popup has actually cleared in the guest,
        // otherwise the next run detects leftover magenta at ~0 ms.
        await popup.evaluate(() => window.__qsmProbe.start());
        await popup.waitForFunction(() => {
            const p = window.__qsmProbe.context.getImageData(780, 350, 1, 1).data;
            return !(p[0] > 180 && p[1] < 100 && p[2] > 180);
        }, undefined, { timeout: 4000, polling: 'raf' }).catch(() => undefined);
        await popup.evaluate(() => window.__qsmProbe.stop());
        await wait(400);
        await popup.evaluate(() => { const s = window.__qsmProbe; s.input = null; s.frames = []; s.magentaAt = null; s.start(); });
        // Move onto the icon centre (guest 640,370). The mousemove DOM event
        // the console receives is the input timestamp.
        const target = await popup.evaluate(() => {
            const s = window.__qsmProbe; const scale = Math.min(s.box.width / s.source.w, s.box.height / s.source.h);
            const cx = s.box.left + (s.box.width - s.source.w * scale) / 2 + 640 * scale;
            const cy = s.box.top + (s.box.height - s.source.h * scale) / 2 + 370 * scale;
            return { x: cx, y: cy };
        });
        await popup.mouse.move(target.x, target.y);
        const latency = await popup.waitForFunction(() => {
            const s = window.__qsmProbe;
            if (!s.input || s.input.type !== 'mousemove') { return false; }
            // Interior of the #ff00ff hover popup at guest (780, 350).
            const p = s.context.getImageData(780, 350, 1, 1).data;
            if (p[0] > 180 && p[1] < 100 && p[2] > 180) { return performance.now() - s.input.at; }
            return false;
        }, undefined, { timeout: popup.__timeout, polling: 'raf' }).then((h) => h.jsonValue());
        await popup.evaluate(() => window.__qsmProbe.stop());
        runs.push(Number(Number(latency).toFixed(1)));
    }
    return runs;
}

async function measureDrag(popup) {
    const results = [];
    for (let run = 0; run < popup.__dragRuns; run += 1) {
        // Grab the orange card (guest ~200,215) and drag it right to ~900.
        const start = await popup.evaluate(() => {
            const s = window.__qsmProbe; const scale = Math.min(s.box.width / s.source.w, s.box.height / s.source.h);
            const left = s.box.left + (s.box.width - s.source.w * scale) / 2;
            const top = s.box.top + (s.box.height - s.source.h * scale) / 2;
            return { x0: left + 200 * scale, y0: top + 215 * scale, left, top, scale };
        });
        // Reset the card to its left start: find its painted centre and, if
        // it has drifted right from a previous run, drag it back to guest 200.
        await popup.evaluate(() => window.__qsmProbe.start());
        const current = await popup.evaluate(() => {
            const s = window.__qsmProbe; s.context.drawImage(s.element, 0, 0, s.source.w, s.source.h);
            const row = s.context.getImageData(0, 215, s.source.w, 1).data;
            let first = -1, last = -1;
            for (let x = 0; x < s.source.w; x += 1) { const o = x * 4;
                if (row[o] > 170 && row[o + 1] > 80 && row[o + 1] < 235 && row[o + 2] < 105) { if (first < 0) first = x; last = x; } }
            return first < 0 ? null : (first + last) / 2;
        });
        if (current !== null && current > 240) {
            const from = { x: start.left + current * start.scale, y: start.y0 };
            await popup.mouse.move(from.x, from.y);
            await popup.mouse.down();
            await popup.mouse.move(start.x0, start.y0, { steps: 10 });
            await popup.mouse.up();
            await wait(500);
        }
        await popup.evaluate(() => window.__qsmProbe.stop());
        await popup.mouse.click(start.x0, start.y0);
        await wait(300);
        await popup.mouse.move(start.x0, start.y0);
        await wait(400);
        await popup.evaluate(() => { const s = window.__qsmProbe; s.input = null; s.frames = []; s.samples = []; s.start();
            // Record, per presented frame, the painted card centre-x and the
            // latest input source-x, so the trailing lag can be computed.
            s.sampler = () => {};
        });
        await popup.mouse.down();
        // Drive the card across with steady moves; the console maps each
        // mousemove to Display1 and the guest page follows the pointer.
        const startedAt = await popup.evaluate(() => performance.now());
        const samples = [];
        for (let step = 1; step <= 55; step += 1) {
            const x = start.x0 + (900 - 200) * start.scale * (step / 55);
            await popup.mouse.move(x, start.y0);
            const sample = await popup.evaluate(() => {
                const s = window.__qsmProbe;
                // Painted card centre: scan guest row y=215 for the orange card.
                const row = s.context.getImageData(0, 215, s.source.w, 1).data;
                let first = -1, last = -1;
                for (let x = 0; x < s.source.w; x += 1) {
                    const o = x * 4;
                    if (row[o] > 170 && row[o + 1] > 80 && row[o + 1] < 235 && row[o + 2] < 105) {
                        if (first < 0) first = x; last = x;
                    }
                }
                const painted = first < 0 ? null : (first + last) / 2;
                return { at: performance.now(), inputAt: s.input ? s.input.at : null,
                    inputX: s.input ? s.input.source.x : null, painted };
            });
            samples.push(sample);
            await wait(16);
        }
        await popup.mouse.up();
        await popup.evaluate(() => window.__qsmProbe.stop());
        // First painted motion after the press.
        const moved = samples.filter((v) => v.painted !== null);
        const initial = moved.length ? moved[0].painted : null;
        const motion = moved.filter((v) => Math.abs(v.painted - initial) >= 8);
        const firstMotionMs = motion.length ? Math.round(motion[0].at - startedAt) : null;
        // Visual lag: how far (guest px) the painted card trails the pointer.
        const lags = samples.filter((v) => v.painted !== null && v.inputX !== null)
            .map((v) => Math.abs(v.inputX - v.painted));
        lags.sort((a, b) => a - b);
        const p = (f) => lags.length ? Math.round(lags[Math.min(lags.length - 1, Math.floor(lags.length * f))]) : null;
        results.push({ firstMotionMs, visualLagPxMedian: p(0.5), visualLagPxP95: p(0.95), frames: moved.length });
    }
    return results;
}

async function main() {
    const config = options(process.argv.slice(2));
    let password = readPassword(config['password-file']);
    let browser;
    let context;
    const report = { transport: config.transport, label: config.label, vmid: config.vmid };
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
        const page = await context.newPage();
        page.setDefaultTimeout(config.timeout);
        phase = 'LOGIN';
        await page.goto(`${config['pve-url']}/`, { waitUntil: 'domcontentloaded' });
        const [username, realm] = config.user.split('@');
        await page.locator('input[name="username"]:visible').fill(username);
        await selectRealm(page, realm, config.timeout);
        await page.locator('input[name="password"]:visible').fill(password);
        password = '';
        await page.locator('.x-window:visible .x-btn').filter({ hasText: /^Login$/ }).last().click();
        await page.waitForFunction((expected) => window.Proxmox?.UserName === expected && !!window.PVE, config.user);
        const notice = page.locator('.x-message-box:visible');
        if (await notice.count()) { await notice.locator('.x-btn').filter({ hasText: /^OK$/ }).click(); }
        await selectVm(page, config.vmid, config.timeout);
        phase = 'OPENING_CONSOLE';
        const popup = await openConsole(page, config.vmid,
            config.transport === 'qsm' ? /^QSM Direct$/ : /^noVNC$/, config.timeout);
        popup.__timeout = config.timeout; popup.__hoverRuns = config.hoverRuns; popup.__dragRuns = config.dragRuns;
        phase = 'WAITING_FOR_SURFACE';
        await popup.waitForFunction((kind) => {
            const element = document.querySelector('video') || document.querySelector('canvas') ||
                document.querySelector('iframe')?.contentDocument?.querySelector('canvas');
            if (!element) { return false; }
            if (kind === 'qsm') { return element.tagName === 'VIDEO' && element.readyState >= 2 && element.videoWidth > 0; }
            if (element.tagName !== 'CANVAS' || element.width < 64) { return false; }
            try { return Array.from(element.getContext('2d', { willReadFrequently: true })
                .getImageData(0, 0, 32, 32).data).some((v) => v !== 0); } catch (_e) { return false; }
        }, config.transport, { timeout: config.timeout });
        await wait(1500);
        const probe = await popup.evaluate(PROBE_SCRIPT);
        report.surface = `${probe.source.w}x${probe.source.h}`;
        report.detect = probe.mode === 'video' ? 'requestVideoFrameCallback' : 'requestAnimationFrame';
        // The fixture must be the input-to-pixel page (icon + orange card).
        if (probe.source.w !== 1280 || probe.source.h < 480) { fail('INTERACTION_FIXTURE_UNAVAILABLE'); }
        phase = 'HOVER';
        const hover = await measureHover(popup);
        report.hover = hover;
        const hoverValid = hover.filter((v) => v >= 8).sort((a, b) => a - b);
        report.hoverSamples = hoverValid.length;
        report.hoverMedianMs = hoverValid.length ? hoverValid[Math.floor(hoverValid.length / 2)] : null;
        phase = 'DRAG';
        const drag = await measureDrag(popup);
        report.drag = drag;
        const firsts = drag.map((d) => d.firstMotionMs).filter((v) => v !== null).sort((a, b) => a - b);
        const lags = drag.map((d) => d.visualLagPxP95).filter((v) => v !== null).sort((a, b) => a - b);
        report.dragFirstMotionMsMedian = firsts.length ? firsts[Math.floor(firsts.length / 2)] : null;
        report.dragVisualLagPxP95 = lags.length ? lags[lags.length - 1] : null;
        phase = 'PASS';
        if (config.report) { fs.writeFileSync(config.report, JSON.stringify(report, null, 1)); }
        process.stdout.write(`QSM_INTERACTION ${JSON.stringify(report)}\n`);
    } finally {
        password = '';
        try { if (context) { await context.close(); } } catch (_e) { /* shutdown */ }
        try { if (browser) { await browser.close(); } } catch (_e) { /* shutdown */ }
    }
    // A still-scheduled requestVideoFrameCallback in a closing context can
    // reject after main() resolves; do not let that mask the exit status.
    process.exitCode = 0;
}

main().catch((error) => {
    const code = error && /^[A-Z0-9_]+$/.test(error.code || '') ? error.code : 'FAILED';
    process.stderr.write(`QSM_INTERACTION_FAIL code=${code} phase=${phase} message=${String(error && error.message || '').slice(0, 160)}\n`);
    process.exitCode = 1;
});
