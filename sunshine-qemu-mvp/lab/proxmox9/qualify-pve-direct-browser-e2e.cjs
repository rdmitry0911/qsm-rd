#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Browser-to-PVE-to-Display1 qualification.  This deliberately drives the
 * visible PVE UI: it catches mistakes hidden by direct terminal-socket tests,
 * in particular PVE's protected pveproxy -> pvedaemon forwarding boundary.
 * Diagnostic output contains only fixed labels and status classes, never a
 * password, cookie, SDP, response body, or WebRTC candidate.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const fail = (code) => { const error = new Error(code); error.code = code; throw error; };
const nodePattern = /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/;
const userPattern = /^[^\s@/:\\\x00-\x1f]{1,64}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
let phase = 'INITIALIZING';
let diagnostic = { requests: 0, responses: 0, classes: new Set(), failures: 0, outcome: 'none' };

function options(argv) {
    const result = { timeout: 45_000, chrome: process.env.QSM_DIRECT_CHROME || '' };
    const keys = new Set(['--pve-url', '--user', '--password-file', '--vmid', '--timeout-ms', '--chrome', '--cdp-url']);
    const seen = new Set();
    for (let index = 0; index < argv.length; index += 1) {
        const key = argv[index];
        if (key === '--drag-fixture') {
            if (seen.has(key)) { fail('INVALID_ARGUMENTS'); }
            seen.add(key);
            result.dragFixture = true;
            continue;
        }
        if (!keys.has(key) || index + 1 === argv.length || seen.has(key)) { fail('INVALID_ARGUMENTS'); }
        seen.add(key);
        result[key.slice(2)] = argv[++index];
    }
    if (!result['pve-url'] || !result.user || !result['password-file'] || !result.vmid ||
        !/^https:\/\/[^/?#]+(?::[0-9]{1,5})?$/.test(result['pve-url']) || !userPattern.test(result.user) ||
        !/^[1-9][0-9]*$/.test(result.vmid)) { fail('INVALID_ARGUMENTS'); }
    result.vmid = Number(result.vmid);
    result.timeout = Number(result['timeout-ms'] || result.timeout);
    if (!Number.isSafeInteger(result.vmid) || result.vmid < 100 || result.vmid > 999999999 ||
        !Number.isSafeInteger(result.timeout) || result.timeout < 5000 || result.timeout > 120000) { fail('INVALID_ARGUMENTS'); }
    return result;
}

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function measurePopupDrag(popup, timeout) {
    /*
     * Exercise the actual shipped popup's mouse listeners, rAF batching and
     * two SCTP data channels.  The separate browser-receiver gate measures
     * the transport itself; this one prevents a UI-only coalescing regression
     * from turning a visually smooth H.264 stream into a jerky desktop drag.
     */
    const fixture = await popup.evaluate(() => {
        const video = document.querySelector('video');
        if (!video || video.videoWidth !== 1280 || video.videoHeight < 480) return null;
        const scan = (context, width, y) => {
            context.drawImage(video, 0, y, width, 1, 0, 0, width, 1);
            const pixels = context.getImageData(0, 0, width, 1).data;
            let first = -1;
            let last = -1;
            for (let x = 0; x < width; x += 1) {
                const offset = x * 4;
                if (pixels[offset] > 170 && pixels[offset + 1] > 80 &&
                    pixels[offset + 1] < 235 && pixels[offset + 2] < 105) {
                    if (first < 0) first = x;
                    last = x;
                }
            }
            return first < 0 ? null : (first + last) / 2;
        };
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth;
        canvas.height = 1;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) return null;
        const initial = scan(context, video.videoWidth, 215);
        if (initial === null) return null;
        window.qsmPopupDragE2e = { running: false, observations: [], scan, context, canvas, initial };
        return { initial, width: video.videoWidth, height: video.videoHeight };
    });
    if (!fixture) fail('DRAG_FIXTURE_UNAVAILABLE');
    const video = popup.locator('video');
    const box = await video.boundingBox();
    if (!box || box.width < 64 || box.height < 64) fail('DRAG_FIXTURE_UNAVAILABLE');
    const toCss = (x, y) => ({
        x: box.x + ((x + 0.5) * box.width / fixture.width),
        y: box.y + ((y + 0.5) * box.height / fixture.height),
    });
    const start = { x: Math.round(fixture.initial), y: 215 };
    const target = { x: fixture.initial < fixture.width / 2 ? 1000 : 200, y: 215 };
    await popup.mouse.move(toCss(start.x, start.y).x, toCss(start.x, start.y).y);
    // Let mouse positioning reach the guest before the button edge. A human
    // naturally performs this same short approach before beginning a drag;
    // it keeps the metric focused on continuous held-pointer motion.
    await wait(150);
    await popup.evaluate(() => {
        const tracker = window.qsmPopupDragE2e;
        const video = document.querySelector('video');
        tracker.running = true;
        tracker.started = performance.now();
        const record = (now, metadata) => {
            if (!tracker.running) return;
            const center = tracker.scan(tracker.context, video.videoWidth, 215);
            if (center !== null) tracker.observations.push({ at: performance.now(), center,
                presentedFrames: Number(metadata?.presentedFrames) });
            video.requestVideoFrameCallback(record);
        };
        video.requestVideoFrameCallback(record);
    });
    await popup.mouse.down();
    for (let index = 1; index <= 60; index += 1) {
        const fraction = index / 60;
        const position = toCss(start.x + (target.x - start.x) * fraction, start.y);
        await popup.mouse.move(position.x, position.y);
        await wait(16);
    }
    await popup.mouse.up();
    await wait(500);
    const result = await popup.evaluate((targetX) => {
        const tracker = window.qsmPopupDragE2e;
        const video = document.querySelector('video');
        if (!tracker) return null;
        tracker.running = false;
        const finalCenter = tracker.scan(tracker.context, video.videoWidth, 215);
        const transitions = [];
        for (const item of tracker.observations) {
            const previous = transitions[transitions.length - 1];
            if (!previous || Math.abs(item.center - previous.center) >= 3) transitions.push(item);
        }
        const motion = transitions.filter((item) => Math.abs(item.center - tracker.initial) >= 8);
        let largestGapMs = 0;
        for (let index = 1; index < motion.length; index += 1) {
            largestGapMs = Math.max(largestGapMs, motion[index].at - motion[index - 1].at);
        }
        return {
            firstMotionLatencyMs: motion.length ? motion[0].at - tracker.started : null,
            largestMotionGapMs: largestGapMs,
            observedMotionFrames: motion.length,
            finalCenter,
            targetX,
        };
    }, target.x);
    if (!result || !Number.isFinite(result.firstMotionLatencyMs) || result.firstMotionLatencyMs > 300 ||
        !Number.isFinite(result.largestMotionGapMs) || result.largestMotionGapMs > 140 ||
        !Number.isInteger(result.observedMotionFrames) || result.observedMotionFrames < 20 ||
        !Number.isFinite(result.finalCenter) || Math.abs(result.finalCenter - target.x) > 24) {
        fail('POPUP_DRAG_NOT_SMOOTH');
    }
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

async function main() {
    const config = options(process.argv.slice(2));
    let password = readPassword(config['password-file']);
    let browser;
    let context;
    let remoteBrowser = false;
    const observed = diagnostic;
    try {
        phase = 'OPENING_BROWSER';
        const { chromium } = await import('playwright');
        if (config['cdp-url']) {
            remoteBrowser = true;
            browser = await chromium.connectOverCDP(config['cdp-url']);
        } else {
            browser = await chromium.launch({ headless: true, executablePath: config.chrome || undefined,
                // The lab process must reach its local PVE endpoint directly.
                // A workstation-wide HTTPS proxy can reject loopback before
                // Playwright has even loaded the authenticated PVE page.
                args: [
                    '--autoplay-policy=no-user-gesture-required', '--no-proxy-server',
                    // The disposable Chrome peer runs the qualification as
                    // root in an unprivileged VM/LXC-style lab image.
                    '--no-sandbox', '--disable-setuid-sandbox',
                    '--disable-seccomp-filter-sandbox', '--disable-dev-shm-usage',
                    '--disable-features=NetworkServiceSandbox,WebRtcHideLocalIpsWithMdns',
                    '--force-webrtc-ip-handling-policy=default',
                ] });
        }
        context = await browser.newContext({ ignoreHTTPSErrors: true, locale: 'en-US', viewport: { width: 1280, height: 800 } });
        const page = await context.newPage();
        page.setDefaultTimeout(config.timeout);
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
        const node = await selectVm(page, config.vmid, config.timeout);
        const target = `/api2/extjs/nodes/${encodeURIComponent(node)}/qemu/${config.vmid}/qsm-direct`;
        const isTarget = (request) => { try { return new URL(request.url()).pathname === target; } catch (_) { return false; } };
        page.on('request', (request) => { if (isTarget(request)) { observed.requests += 1; } });
        page.on('response', (response) => {
            if (!isTarget(response.request())) { return; }
            observed.responses += 1;
            observed.classes.add(`${Math.floor(response.status() / 100)}xx`);
            response.json().then((body) => {
                if (body && (body.success === true || body.success === 1)) { observed.outcome = 'success'; }
                else if (body && typeof body.message === 'string' && body.message === 'qsm direct console is unavailable') { observed.outcome = 'unavailable'; }
                else if (body && body.errors && typeof body.errors === 'object') { observed.outcome = 'validation'; }
                else { observed.outcome = 'other'; }
            }).catch(() => { observed.outcome = 'invalid'; });
        });
        page.on('requestfailed', (request) => { if (isTarget(request)) { observed.failures += 1; } });
        phase = 'OPENING_CONSOLE_MENU';
        const selector = await page.waitForFunction((wanted) => {
            const button = window.Ext?.ComponentQuery?.query('pveConsoleButton')?.find((candidate) =>
                candidate?.consoleType === 'kvm' && Number(candidate.vmid) === wanted && candidate.rendered && !candidate.disabled);
            return button?.getEl?.().dom?.id || null;
        }, config.vmid).then((handle) => handle.jsonValue());
        if (!selector) { fail('CONSOLE_BUTTON_UNAVAILABLE'); }
        const button = page.locator(`#${selector}`);
        const box = await button.boundingBox();
        if (!box) { fail('CONSOLE_BUTTON_UNAVAILABLE'); }
        await page.mouse.click(box.x + box.width - 6, box.y + box.height / 2);
        const item = page.locator('.x-menu:visible .x-menu-item').filter({ hasText: /^QSM Direct$/ });
        if (await item.count() !== 1) { fail('QSM_DIRECT_MENU_UNAVAILABLE'); }
        phase = 'REQUESTING_DIRECT_CONSOLE';
        const popupPromise = page.waitForEvent('popup');
        await item.click();
        const popup = await popupPromise;
        popup.setDefaultTimeout(config.timeout);
        phase = 'WAITING_FOR_GUEST_VIDEO';
        await popup.waitForFunction(() => {
            const video = document.querySelector('video');
            return !!video && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0 && video.videoHeight > 0;
        }, undefined, { timeout: config.timeout });
        if (config.dragFixture) {
            phase = 'MEASURING_POPUP_DRAG';
            await measurePopupDrag(popup, config.timeout);
        }
        if (observed.requests !== 1 || observed.responses !== 1 || observed.failures !== 0 || !observed.classes.has('2xx') || observed.outcome !== 'success') {
            fail('INVALID_PVE_DIRECT_HANDOFF');
        }
        phase = 'PASS';
        process.stdout.write(`QSM_PVE_DIRECT_BROWSER_E2E_OK protected_route=1 popup_video=1 response=2xx drag=${config.dragFixture ? 1 : 0}\n`);
    } finally {
        password = '';
        if (context) { await context.close(); }
        if (browser) {
            // connectOverCDP owns only a DevTools connection, not the
            // Chromium process.  Playwright deliberately exposes no public
            // Browser.disconnect(); closing it would terminate the lab's
            // browser instead of merely releasing this driver.
            if (!remoteBrowser) { await browser.close(); }
        }
    }
}

main().catch((error) => {
    const code = error && /^[A-Z0-9_]+$/.test(error.code || '') ? error.code : 'FAILED';
    const classes = [...diagnostic.classes].sort().join('+') || 'none';
    process.stderr.write(`QSM_PVE_DIRECT_BROWSER_E2E_FAIL code=${code} phase=${phase} requests=${diagnostic.requests} responses=${diagnostic.responses} response_classes=${classes} outcome=${diagnostic.outcome} failures=${diagnostic.failures}\n`);
    process.exitCode = 1;
});
