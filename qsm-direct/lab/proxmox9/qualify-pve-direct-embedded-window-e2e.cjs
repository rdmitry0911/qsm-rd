#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Exercise both PVE Console presentations of one running Display1 VM at the
 * same time.  A previous raw-DOM iframe bypassed ExtJS's fit layout and was
 * left at the empty-panel height; this gate also catches a second browser
 * peer incorrectly retiring the first peer's shared VM media transport.
 *
 * The script deliberately reports only fixed phase/error labels and geometry
 * classes. It never emits the password, PVE cookie, SDP, video pixels, or
 * peer addresses.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const fail = (code) => { const error = new Error(code); error.code = code; throw error; };
const nodePattern = /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/;
const userPattern = /^[^\s@/:\\\x00-\x1f]{1,64}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
let phase = 'INITIALIZING';

function options(argv) {
    const value = { timeout: 45_000, chrome: process.env.QSM_DIRECT_CHROME || '' };
    const flags = new Set(['--pve-url', '--user', '--password-file', '--vmid', '--timeout-ms', '--chrome']);
    const seen = new Set();
    for (let index = 0; index < argv.length; index += 1) {
        const flag = argv[index];
        if (!flags.has(flag) || index + 1 === argv.length || seen.has(flag)) { fail('INVALID_ARGUMENTS'); }
        seen.add(flag);
        value[flag.slice(2)] = argv[++index];
    }
    if (!value['pve-url'] || !value.user || !value['password-file'] || !value.vmid ||
        !/^https:\/\/[^/?#]+(?::[0-9]{1,5})?$/.test(value['pve-url']) ||
        !userPattern.test(value.user) || !/^[1-9][0-9]*$/.test(value.vmid)) {
        fail('INVALID_ARGUMENTS');
    }
    value.vmid = Number(value.vmid);
    value.timeout = Number(value['timeout-ms'] || value.timeout);
    if (!Number.isSafeInteger(value.vmid) || value.vmid < 100 || value.vmid > 999999999 ||
        !Number.isSafeInteger(value.timeout) || value.timeout < 5000 || value.timeout > 120000) {
        fail('INVALID_ARGUMENTS');
    }
    return value;
}

function readPassword(file) {
    const descriptor = fs.openSync(path.resolve(file), fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
    try {
        const stat = fs.fstatSync(descriptor);
        if (!stat.isFile() || stat.size < 1 || stat.size > 4096 || (stat.mode & 0o077) !== 0) {
            fail('UNSAFE_PASSWORD_FILE');
        }
        const bytes = Buffer.alloc(stat.size);
        if (fs.readSync(descriptor, bytes, 0, bytes.length, 0) !== bytes.length) {
            fail('PASSWORD_FILE_UNAVAILABLE');
        }
        const password = bytes.toString('utf8').replace(/\r?\n$/, '');
        bytes.fill(0);
        if (!password || /[\r\n\0]/.test(password)) { fail('PASSWORD_FILE_UNAVAILABLE'); }
        return password;
    } finally { fs.closeSync(descriptor); }
}

async function selectRealm(page, realm, timeout) {
    const description = await page.waitForFunction((wanted) => {
        const combo = window.Ext?.getCmp?.('pveloginrealm');
        const entry = combo?.getStore?.().findRecord?.('realm', wanted, 0, false, true, true);
        return entry ? String(entry.get('descr') || '') : null;
    }, realm, { timeout }).then((handle) => handle.jsonValue());
    if (!description) { fail('PVE_REALM_UNAVAILABLE'); }
    await page.locator('#pveloginrealm-inputEl:visible').click();
    await page.locator('.x-boundlist:visible .x-boundlist-item').filter({ hasText: description }).first().click();
}

async function dismissNotice(page) {
    const notice = page.locator('.x-message-box:visible');
    if (await notice.count()) {
        await notice.locator('.x-btn').filter({ hasText: /^OK$/ }).last().click();
    }
}

async function selectVm(page, vmid, timeout) {
    const node = await page.waitForFunction((wanted) => {
        const tree = window.Ext?.ComponentQuery?.query('pveResourceTree')?.[0];
        const record = tree?.getStore?.().getRootNode()?.findChild?.('id', `qemu/${wanted}`, true);
        if (!record || record.data.type !== 'qemu' || Number(record.data.vmid) !== wanted || !record.data.node) {
            return null;
        }
        tree.selectById(record.data.id);
        return String(record.data.node);
    }, vmid, { timeout }).then((handle) => handle.jsonValue());
    if (!node || !nodePattern.test(node)) { fail('QEMU_VM_UNAVAILABLE'); }
    return node;
}

async function preparePage(browser, config, password) {
    const context = await browser.newContext({
        ignoreHTTPSErrors: true,
        locale: 'en-US',
        viewport: { width: 1280, height: 800 },
    });
    const page = await context.newPage();
    page.setDefaultTimeout(config.timeout);
    await page.goto(`${config['pve-url']}/`, { waitUntil: 'domcontentloaded' });
    const [username, realm] = config.user.split('@');
    await page.locator('input[name="username"]:visible').fill(username);
    await selectRealm(page, realm, config.timeout);
    await page.locator('input[name="password"]:visible').fill(password);
    await page.locator('.x-window:visible .x-btn').filter({ hasText: /^Login$/ }).last().click();
    await page.waitForFunction((expected) => window.Proxmox?.UserName === expected && !!window.PVE, config.user);
    await page.waitForTimeout(500);
    await dismissNotice(page);
    const node = await selectVm(page, config.vmid, config.timeout);
    return { context, page, node };
}

async function waitVideo(surface, timeout) {
    await surface.waitForFunction(() => {
        const video = document.querySelector('video');
        return !!video && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
            video.videoWidth > 0 && video.videoHeight > 0;
    }, undefined, { timeout });
}

async function openEmbedded(page, timeout) {
    phase = 'OPENING_EMBEDDED_CONSOLE';
    const navigation = page.locator('.x-treelist-item-text').filter({ hasText: /^Console$/ });
    if (await navigation.count() !== 1) { fail('CONSOLE_NAVIGATION_UNAVAILABLE'); }
    await navigation.click();
    await page.waitForFunction(() => document.querySelectorAll('iframe').length === 1, undefined, { timeout });
    const frame = page.frames().find((candidate) => candidate !== page.mainFrame());
    if (!frame) { fail('EMBEDDED_FRAME_UNAVAILABLE'); }
    await waitVideo(frame, timeout);
    const [outer, inner] = await Promise.all([
        page.evaluate(() => {
            const frame = document.querySelector('iframe');
            const box = frame?.getBoundingClientRect();
            return box ? { width: Math.round(box.width), height: Math.round(box.height) } : null;
        }),
        frame.evaluate(() => {
            const video = document.querySelector('video');
            const box = video?.getBoundingClientRect();
            return video && box ? {
                width: Math.round(box.width), height: Math.round(box.height),
                decodedWidth: video.videoWidth, decodedHeight: video.videoHeight,
            } : null;
        }),
    ]);
    // A Console card can be narrow on a small browser, but its fit child must
    // never collapse to the empty Ext panel's 150px minimum height.
    if (!outer || !inner || outer.width < 320 || outer.height < 320 ||
        inner.width < 320 || inner.height < 320 || inner.decodedWidth < 64 || inner.decodedHeight < 64) {
        fail('EMBEDDED_VIEWPORT_COLLAPSED');
    }
    return { frame, outer, inner };
}

async function consoleButton(page, vmid, timeout) {
    const id = await page.waitForFunction((wanted) => {
        const button = window.Ext?.ComponentQuery?.query('pveConsoleButton')?.find((candidate) =>
            candidate?.consoleType === 'kvm' && Number(candidate.vmid) === wanted &&
            candidate.rendered && !candidate.disabled);
        return button?.getEl?.().dom?.id || null;
    }, vmid, { timeout }).then((handle) => handle.jsonValue());
    if (!id) { fail('CONSOLE_BUTTON_UNAVAILABLE'); }
    return page.locator(`#${id}`);
}

async function openWindow(page, config) {
    phase = 'OPENING_WINDOW_CONSOLE';
    const button = await consoleButton(page, config.vmid, config.timeout);
    const box = await button.boundingBox();
    if (!box) { fail('CONSOLE_BUTTON_UNAVAILABLE'); }
    await page.mouse.click(box.x + box.width - 6, box.y + box.height / 2);
    const item = page.locator('.x-menu:visible .x-menu-item').filter({ hasText: /^QSM Direct$/ });
    if (await item.count() !== 1) { fail('QSM_DIRECT_MENU_UNAVAILABLE'); }
    const popupPromise = page.waitForEvent('popup');
    await item.click();
    const popup = await popupPromise;
    popup.setDefaultTimeout(config.timeout);
    await waitVideo(popup, config.timeout);
    return popup;
}

async function verifyWindow(popup) {
    return popup.evaluate(() => {
        const video = document.querySelector('video');
        const box = video?.getBoundingClientRect();
        return video && box ? {
            width: Math.round(box.width), height: Math.round(box.height),
            decodedWidth: video.videoWidth, decodedHeight: video.videoHeight,
            state: window.RTCPeerConnection ? 'browser' : 'unavailable',
        } : null;
    });
}

async function verifySequence(browser, config, password, order) {
    phase = `PREPARING_${order}`;
    const { context, page } = await preparePage(browser, config, password);
    let popup;
    try {
        let embedded;
        if (order === 'EMBEDDED_THEN_WINDOW') {
            embedded = await openEmbedded(page, config.timeout);
            popup = await openWindow(page, config);
        } else {
            popup = await openWindow(page, config);
            embedded = await openEmbedded(page, config.timeout);
        }
        phase = `VERIFYING_BOTH_${order}`;
        await page.waitForTimeout(1200);
        await waitVideo(embedded.frame, config.timeout);
        await waitVideo(popup, config.timeout);
        const window = await verifyWindow(popup);
        if (!window || window.width < 320 || window.height < 320 ||
            window.decodedWidth < 64 || window.decodedHeight < 64) {
            fail('WINDOW_VIEWPORT_COLLAPSED');
        }
        return { order, embedded: embedded.outer, window: { width: window.width, height: window.height } };
    } finally {
        if (popup && !popup.isClosed()) { await popup.close().catch(() => undefined); }
        await context.close();
    }
}

async function main() {
    const config = options(process.argv.slice(2));
    let password = readPassword(config['password-file']);
    let browser;
    try {
        phase = 'OPENING_BROWSER';
        const { chromium } = await import('playwright');
        browser = await chromium.launch({
            headless: true,
            executablePath: config.chrome || undefined,
            args: [
                '--autoplay-policy=no-user-gesture-required', '--no-proxy-server',
                '--no-sandbox', '--disable-setuid-sandbox', '--disable-seccomp-filter-sandbox',
                '--disable-dev-shm-usage',
                '--disable-features=NetworkServiceSandbox,WebRtcHideLocalIpsWithMdns',
                '--force-webrtc-ip-handling-policy=default',
            ],
        });
        const frameFirst = await verifySequence(browser, config, password, 'EMBEDDED_THEN_WINDOW');
        const windowFirst = await verifySequence(browser, config, password, 'WINDOW_THEN_EMBEDDED');
        password = '';
        phase = 'PASS';
        process.stdout.write(
            `QSM_PVE_DIRECT_EMBEDDED_WINDOW_E2E_OK frame_first=${frameFirst.embedded.width}x${frameFirst.embedded.height} ` +
            `window_first=${windowFirst.embedded.width}x${windowFirst.embedded.height}\n`,
        );
    } finally {
        password = '';
        if (browser) { await browser.close(); }
    }
}

main().catch((error) => {
    const code = error && /^[A-Z0-9_]+$/.test(error.code || '') ? error.code : 'FAILED';
    process.stderr.write(`QSM_PVE_DIRECT_EMBEDDED_WINDOW_E2E_FAIL code=${code} phase=${phase}\n`);
    process.exitCode = 1;
});
