#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Measure the stock Proxmox noVNC route from a real Chromium browser.
 *
 * This is deliberately not a synthetic WebSocket client.  It signs into the
 * visible PVE UI, selects the existing Console split-menu's noVNC item, and
 * observes the resulting RFB-over-WebSocket traffic.  Its sibling,
 * measure-direct-browser-e2e.py, measures the same browser/VM/network path
 * for QSM Direct WebRTC.
 *
 * Output contains aggregate counts only: it never prints the PVE password,
 * cookie, ticket, WebSocket URL, or RFB payload.
 */
'use strict';

const fs = require('node:fs');

const fail = (code) => { const error = new Error(code); error.code = code; throw error; };
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const nodePattern = /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/;
const userPattern = /^[^\s@/:\\\x00-\x1f]{1,64}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
let phase = 'INITIALIZING';
let canvasDiagnostic = 'none';

function options(argv) {
    const result = { durationMs: 10_000, timeoutMs: 45_000, hoverRuns: 0, chrome: process.env.QSM_DIRECT_CHROME || '' };
    const keys = new Set(['--pve-url', '--user', '--password-file', '--vmid', '--duration-ms', '--timeout-ms', '--hover-runs', '--chrome']);
    const seen = new Set();
    for (let index = 0; index < argv.length; index += 1) {
        const key = argv[index];
        if (!keys.has(key) || seen.has(key) || index + 1 >= argv.length) fail('INVALID_ARGUMENTS');
        seen.add(key);
        result[key.slice(2)] = argv[++index];
    }
    if (!/^https:\/\/[^/?#]+(?::[0-9]{1,5})?$/.test(result['pve-url'] || '') ||
        !userPattern.test(result.user || '') || !/^[1-9][0-9]*$/.test(result.vmid || '')) fail('INVALID_ARGUMENTS');
    result.vmid = Number(result.vmid);
    result.durationMs = Number(result['duration-ms'] || result.durationMs);
    result.timeoutMs = Number(result['timeout-ms'] || result.timeoutMs);
    result.hoverRuns = Number(result['hover-runs'] || result.hoverRuns);
    if (!Number.isSafeInteger(result.vmid) || result.vmid < 100 || result.vmid > 999999999 ||
        !Number.isSafeInteger(result.durationMs) || result.durationMs < 1000 || result.durationMs > 120000 ||
        !Number.isSafeInteger(result.timeoutMs) || result.timeoutMs < 5000 || result.timeoutMs > 120000 ||
        !Number.isSafeInteger(result.hoverRuns) || result.hoverRuns < 0 || result.hoverRuns > 20) {
        fail('INVALID_ARGUMENTS');
    }
    return result;
}

function readPassword(file) {
    let value;
    try { value = fs.readFileSync(file, 'utf8').replace(/[\r\n]+$/, ''); }
    catch (_) { fail('PASSWORD_UNREADABLE'); }
    if (!value || value.length > 1024) fail('PASSWORD_UNREADABLE');
    return value;
}

async function selectRealm(page, realm, timeout) {
    const description = await page.waitForFunction((wanted) => {
        const combo = window.Ext?.getCmp?.('pveloginrealm');
        const record = combo?.getStore?.().findRecord?.('realm', wanted, 0, false, true, true);
        return record ? String(record.get('descr') || '') : null;
    }, realm, { timeout }).then((handle) => handle.jsonValue());
    if (!description) fail('REALM_UNAVAILABLE');
    await page.locator('#pveloginrealm-inputEl:visible').click();
    await page.locator('.x-boundlist:visible .x-boundlist-item').filter({ hasText: description }).first().click();
}

async function selectVm(page, vmid, timeout) {
    const node = await page.waitForFunction((wanted) => {
        const tree = window.Ext?.ComponentQuery?.query('pveResourceTree')?.[0];
        const record = tree?.getStore?.().getRootNode()?.findChild?.('id', `qemu/${wanted}`, true);
        if (!record || record.data.type !== 'qemu' || Number(record.data.vmid) !== wanted || !record.data.node) return null;
        tree.selectById(record.data.id);
        return String(record.data.node);
    }, vmid, { timeout }).then((handle) => handle.jsonValue());
    if (!node || !nodePattern.test(node)) fail('QEMU_VM_UNAVAILABLE');
    return node;
}

function payloadBytes(frame) {
    const payload = frame?.payload;
    if (typeof payload === 'string') return Buffer.byteLength(payload, 'utf8');
    if (Buffer.isBuffer(payload)) return payload.length;
    if (payload instanceof Uint8Array) return payload.byteLength;
    return 0;
}

async function openStockNoVnc(page, vmid, timeout) {
    const selector = await page.waitForFunction((wanted) => {
        const button = window.Ext?.ComponentQuery?.query('pveConsoleButton')?.find((candidate) =>
            candidate?.consoleType === 'kvm' && Number(candidate.vmid) === wanted && candidate.rendered && !candidate.disabled);
        return button?.getEl?.().dom?.id || null;
    }, vmid, { timeout }).then((handle) => handle.jsonValue());
    if (!selector) fail('CONSOLE_BUTTON_UNAVAILABLE');
    const button = page.locator(`#${selector}`);
    const box = await button.boundingBox();
    if (!box) fail('CONSOLE_BUTTON_UNAVAILABLE');
    await page.mouse.click(box.x + box.width - 6, box.y + box.height / 2);
    const items = page.locator('.x-menu:visible .x-menu-item');
    const noVnc = items.filter({ hasText: /^noVNC$/ });
    if (await noVnc.count() !== 1) fail('NOVNC_MENU_UNAVAILABLE');
    const popupPromise = page.waitForEvent('popup');
    await noVnc.click();
    const popup = await popupPromise;
    popup.setDefaultTimeout(timeout);
    return popup;
}

async function waitForRfbCanvas(popup, timeout) {
    try {
        await popup.waitForFunction(() => {
        const frame = document.querySelector('iframe');
        const canvas = document.querySelector('canvas') || frame?.contentDocument?.querySelector('canvas');
        if (!canvas || canvas.width < 64 || canvas.height < 64) return false;
        try {
            const context = canvas.getContext('2d', { willReadFrequently: true });
            const pixels = context?.getImageData(0, 0, Math.min(canvas.width, 32), Math.min(canvas.height, 32)).data;
            return !!pixels && Array.from(pixels).some((value) => value !== 0);
        } catch (_) { return false; }
        }, undefined, { timeout });
    } catch (error) {
        canvasDiagnostic = await popup.evaluate(() => {
            const tags = Array.from(document.querySelectorAll('iframe,canvas')).map((element) => {
                if (element.tagName === 'CANVAS') return `canvas:${element.width}x${element.height}`;
                try {
                    const inner = element.contentDocument;
                    return `iframe:canvas=${inner?.querySelectorAll('canvas').length || 0}`;
                } catch (_) { return 'iframe:inaccessible'; }
            });
            return tags.join(',') || 'no-canvas-or-iframe';
        }).catch(() => 'diagnostic-unavailable');
        throw error;
    }
    return popup.evaluate(() => {
        const canvas = document.querySelector('canvas') || document.querySelector('iframe')?.contentDocument?.querySelector('canvas');
        return { width: canvas?.width || 0, height: canvas?.height || 0 };
    });
}

async function measureHover(popup, timeout) {
    const canvas = popup.locator('canvas').first();
    const geometry = await popup.evaluate(() => {
        const value = document.querySelector('canvas');
        return value ? { width: value.width, height: value.height } : null;
    });
    const box = await canvas.boundingBox();
    if (!geometry || geometry.width !== 1280 || geometry.height < 480 || !box) fail('HOVER_FIXTURE_UNAVAILABLE');
    const point = (x, y) => ({
        x: box.x + ((x + 0.5) * box.width / geometry.width),
        y: box.y + ((y + 0.5) * box.height / geometry.height),
    });
    const reset = point(40, 40);
    await popup.mouse.move(reset.x, reset.y);
    await wait(250);
    const target = point(640, 370);
    const started = await popup.evaluate(() => performance.now());
    await popup.mouse.move(target.x, target.y);
    const latencyMs = await popup.waitForFunction((origin) => {
        const value = document.querySelector('canvas');
        if (!value) return false;
        try {
            const context = value.getContext('2d', { willReadFrequently: true });
            // Interior of the fixture's #ff00ff hover popup.  Tolerances
            // retain this check if an RFB server chooses a 16-bit colour map.
            const pixel = context?.getImageData(780, 350, 1, 1).data;
            return pixel && pixel[0] > 180 && pixel[1] < 100 && pixel[2] > 180
                ? performance.now() - origin : false;
        } catch (_) { return false; }
    }, started, { timeout }).then((handle) => handle.jsonValue());
    if (!Number.isFinite(latencyMs)) fail('HOVER_FIXTURE_UNAVAILABLE');
    return { latencyMs: Number(latencyMs) };
}

async function main() {
    const config = options(process.argv.slice(2));
    let password = readPassword(config['password-file']);
    let browser;
    let context;
    let popup;
    const received = { rfbFrames: 0, rfbBytes: 0, otherFrames: 0, otherBytes: 0 };
    try {
        phase = 'OPENING_BROWSER';
        // The disposable browser VM deliberately carries playwright-core
        // rather than a bundled Chromium.  A PVE development node normally
        // has the Debian ``playwright`` package.  Supporting both keeps the
        // same probe runnable from either side of the laboratory topology.
        let chromium;
        try {
            ({ chromium } = require('playwright'));
        } catch (_) {
            ({ chromium } = require('/opt/qsm-browser/node_modules/playwright-core'));
        }
        browser = await chromium.launch({ headless: true, executablePath: config.chrome || undefined, args: [
            '--autoplay-policy=no-user-gesture-required', '--no-proxy-server', '--no-sandbox',
            '--disable-setuid-sandbox', '--disable-seccomp-filter-sandbox', '--disable-dev-shm-usage',
        ] });
        context = await browser.newContext({ ignoreHTTPSErrors: true, locale: 'en-US', viewport: { width: 1280, height: 800 } });
        const page = await context.newPage();
        page.setDefaultTimeout(config.timeoutMs);
        const observeWebSocket = (socket) => {
            const isRfb = /\/vncwebsocket(?:\?|$)/.test(socket.url());
            socket.on('framereceived', (frame) => {
                const bytes = payloadBytes(frame);
                if (isRfb) { received.rfbFrames += 1; received.rfbBytes += bytes; }
                else { received.otherFrames += 1; received.otherBytes += bytes; }
            });
        };
        page.on('websocket', observeWebSocket);
        phase = 'OPENING_PVE_UI';
        await page.goto(`${config['pve-url']}/`, { waitUntil: 'domcontentloaded' });
        const [username, realm] = config.user.split('@');
        await page.locator('input[name="username"]:visible').fill(username);
        await selectRealm(page, realm, config.timeoutMs);
        await page.locator('input[name="password"]:visible').fill(password);
        password = '';
        phase = 'AUTHENTICATING_PVE';
        await page.locator('.x-window:visible .x-btn').filter({ hasText: /^Login$/ }).last().click();
        await page.waitForFunction((expected) => window.Proxmox?.UserName === expected && !!window.PVE, config.user);
        const notice = page.locator('.x-message-box:visible');
        if (await notice.count()) await notice.locator('.x-btn').filter({ hasText: /^OK$/ }).click();
        phase = 'SELECTING_VM';
        await selectVm(page, config.vmid, config.timeoutMs);
        phase = 'OPENING_NOVNC';
        const consoleOpenedAt = await page.evaluate(() => performance.now());
        popup = await openStockNoVnc(page, config.vmid, config.timeoutMs);
        popup.on('websocket', observeWebSocket);
        phase = 'WAITING_FOR_RFB_CANVAS';
        const canvas = await waitForRfbCanvas(popup, config.timeoutMs);
        const firstFrameMs = await popup.evaluate((started) => performance.now() - started, consoleOpenedAt);
        phase = 'SAMPLING_RFB';
        const start = performance.now();
        await wait(config.durationMs);
        const elapsedMs = performance.now() - start;
        if (received.rfbFrames < 1 || received.rfbBytes < 1) fail('RFB_WEBSOCKET_UNOBSERVED');
        const result = {
            transport: 'stock-noVNC-RFB-over-WebSocket',
            canvas,
            firstCanvasMs: Number(firstFrameMs.toFixed(3)),
            sampleDurationMs: Math.round(elapsedMs),
            rfbFrames: received.rfbFrames,
            rfbReceivedBytes: received.rfbBytes,
            rfbReceivedMbps: Number((received.rfbBytes * 8 / (elapsedMs * 1000)).toFixed(6)),
        };
        if (config.hoverRuns) {
            result.hover = [];
            for (let index = 0; index < config.hoverRuns; index += 1) {
                result.hover.push(await measureHover(popup, config.timeoutMs));
            }
        }
        phase = 'PASS';
        process.stdout.write(`QSM_LAB_NOVNC_BROWSER_E2E ${JSON.stringify(result)}\n`);
    } finally {
        password = '';
        if (context) await context.close();
        if (browser) await browser.close();
    }
}

main().catch((error) => {
    const code = error && /^[A-Z0-9_]+$/.test(error.code || '') ? error.code : 'FAILED';
    process.stderr.write(`QSM_LAB_NOVNC_BROWSER_E2E_FAIL code=${code} phase=${phase} canvas=${canvasDiagnostic}\n`);
    process.exitCode = 1;
});
