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
                args: ['--autoplay-policy=no-user-gesture-required'] });
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
        if (observed.requests !== 1 || observed.responses !== 1 || observed.failures !== 0 || !observed.classes.has('2xx') || observed.outcome !== 'success') {
            fail('INVALID_PVE_DIRECT_HANDOFF');
        }
        phase = 'PASS';
        process.stdout.write('QSM_PVE_DIRECT_BROWSER_E2E_OK protected_route=1 popup_video=1 response=2xx\n');
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
