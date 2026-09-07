#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Guest display power-off recovery probe.
 *
 * Opens the shipped QSM Direct popup for one VM through the visible PVE UI,
 * records the console's own status text and <video> geometry on a fixed
 * cadence, then injects ordinary browser mouse motion and one Shift key edge
 * through the popup's real DOM listeners.  The resulting timeline shows
 * whether a guest whose compositor turned its output off (DPMS) resumes
 * producing video, and how the console reports the interval in between.
 *
 * Output is one JSON document with fixed keys.  It contains no credentials,
 * cookies, SDP, or guest pixels; the only picture-derived value is a mean
 * luma of a 64x36 downscale, used to tell a black placeholder from a desktop.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execSync } = require('node:child_process');

const fail = (code) => { const error = new Error(code); error.code = code; throw error; };
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const nodePattern = /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/;
const userPattern = /^[^\s@/:\\\x00-\x1f]{1,64}@[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

function options(argv) {
    const result = {
        timeout: 45_000, chrome: process.env.QSM_DIRECT_CHROME || '',
        sampleMs: 250, observeMs: 40_000, wakeAfterMs: 12_000, wake: 'mouse,key', report: '',
        width: 1280, height: 800,
    };
    const keys = new Set(['--pve-url', '--user', '--password-file', '--vmid', '--timeout-ms', '--chrome',
        '--sample-ms', '--observe-ms', '--wake-after-ms', '--wake', '--report', '--width', '--height',
        // Mid-session scenario: run a host command that turns the guest
        // output off while this Console is connected, then change the popup
        // viewport so the Console must negotiate a mode with a sleeping guest.
        '--sleep-cmd', '--sleep-after-ms', '--resize-after-ms', '--resize-to']);
    for (let index = 0; index < argv.length; index += 1) {
        const key = argv[index];
        if (!keys.has(key) || index + 1 === argv.length) { fail('INVALID_ARGUMENTS'); }
        const value = argv[++index];
        switch (key) {
        case '--timeout-ms': result.timeout = Number(value); break;
        case '--sample-ms': result.sampleMs = Number(value); break;
        case '--observe-ms': result.observeMs = Number(value); break;
        case '--wake-after-ms': result.wakeAfterMs = Number(value); break;
        case '--sleep-after-ms': result.sleepAfterMs = Number(value); break;
        case '--resize-after-ms': result.resizeAfterMs = Number(value); break;
        case '--width': result.width = Number(value); break;
        case '--height': result.height = Number(value); break;
        default: result[key.slice(2)] = value;
        }
    }
    if (result['resize-to'] && !/^[0-9]{2,5}x[0-9]{2,5}$/.test(result['resize-to'])) { fail('INVALID_ARGUMENTS'); }
    for (const name of ['sleepAfterMs', 'resizeAfterMs']) {
        if (result[name] !== undefined && (!Number.isSafeInteger(result[name]) || result[name] <= 0)) { fail('INVALID_ARGUMENTS'); }
    }
    if (!result['pve-url'] || !result.user || !result['password-file'] || !result.vmid ||
        !/^https:\/\/[^/?#]+(?::[0-9]{1,5})?$/.test(result['pve-url']) || !userPattern.test(result.user) ||
        !/^[1-9][0-9]*$/.test(result.vmid)) { fail('INVALID_ARGUMENTS'); }
    result.vmid = Number(result.vmid);
    for (const name of ['timeout', 'sampleMs', 'observeMs', 'wakeAfterMs', 'width', 'height']) {
        if (!Number.isSafeInteger(result[name]) || result[name] <= 0) { fail('INVALID_ARGUMENTS'); }
    }
    if (!/^(none|mouse|key|click|mouse,key|key,mouse|mouse,click|click,mouse)$/.test(result.wake)) { fail('INVALID_ARGUMENTS'); }
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

const sampleConsole = (popup) => popup.evaluate(() => {
    const video = document.querySelector('video');
    const status = document.querySelector('div[aria-label="Console controls"] span');
    let luma = -1;
    try {
        if (video && video.videoWidth > 0 && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
            const probe = window.qsmProbe;
            if (!probe.canvas) {
                probe.canvas = document.createElement('canvas');
                probe.canvas.width = 64; probe.canvas.height = 36;
                probe.context = probe.canvas.getContext('2d', { willReadFrequently: true });
            }
            probe.context.drawImage(video, 0, 0, 64, 36);
            const pixels = probe.context.getImageData(0, 0, 64, 36).data;
            let sum = 0;
            for (let index = 0; index < pixels.length; index += 4) {
                sum += 0.299 * pixels[index] + 0.587 * pixels[index + 1] + 0.114 * pixels[index + 2];
            }
            luma = Math.round(sum / (pixels.length / 4));
        }
    } catch (_error) { luma = -2; }
    return {
        at: Math.round(performance.now()),
        status: status ? status.textContent : null,
        width: video ? video.videoWidth : -1,
        height: video ? video.videoHeight : -1,
        ready: video ? video.readyState : -1,
        presented: window.qsmProbe ? window.qsmProbe.presented : -1,
        luma,
    };
});

async function main() {
    const config = options(process.argv.slice(2));
    let password = readPassword(config['password-file']);
    let browser;
    let context;
    const report = { vmid: config.vmid, wake: config.wake, samples: [], events: [], summary: {} };
    try {
        // A PVE node carries the Debian playwright package; the disposable
        // browser VM carries only playwright-core under /opt/qsm-browser.
        let chromium;
        try { ({ chromium } = require('playwright')); }
        catch (_error) { ({ chromium } = require('/opt/qsm-browser/node_modules/playwright-core')); }
        browser = await chromium.launch({ headless: true, executablePath: config.chrome || undefined,
            args: [
                '--autoplay-policy=no-user-gesture-required', '--no-proxy-server',
                '--no-sandbox', '--disable-setuid-sandbox',
                '--disable-seccomp-filter-sandbox', '--disable-dev-shm-usage',
                '--disable-features=NetworkServiceSandbox,WebRtcHideLocalIpsWithMdns',
                '--force-webrtc-ip-handling-policy=default',
            ] });
        context = await browser.newContext({ ignoreHTTPSErrors: true, locale: 'en-US',
            viewport: { width: config.width, height: config.height } });
        const page = await context.newPage();
        page.setDefaultTimeout(config.timeout);
        // Record only the envelope of the protected direct-console answer:
        // its HTTP status and the API message text, never the SDP.
        page.on('response', (response) => {
            let pathname = '';
            try { pathname = new URL(response.url()).pathname; } catch (_error) { return; }
            if (!/\/qemu\/[0-9]+\/qsm-direct$/.test(pathname)) { return; }
            response.text().then((text) => {
                let body = null;
                try { body = JSON.parse(text); } catch (_error) { body = null; }
                report.events.push({ at: Date.now() - (report.openedAt || Date.now()), name: 'api-response',
                    http: response.status(), success: body ? body.success : null,
                    message: body && typeof body.message === 'string' ? body.message.slice(0, 160) : null });
            }).catch(() => undefined);
        });
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
        await item.first().waitFor({ state: 'visible', timeout: config.timeout }).catch(() => fail('QSM_DIRECT_MENU_UNAVAILABLE'));
        if (await item.count() !== 1) { fail('QSM_DIRECT_MENU_UNAVAILABLE'); }
        const popupPromise = page.waitForEvent('popup');
        const openedAt = Date.now();
        report.openedAt = openedAt;
        await item.click();
        const popup = await popupPromise;
        popup.setDefaultTimeout(config.timeout);
        let popupClosed = false;
        popup.on('close', () => { popupClosed = true; report.events.push({ at: Date.now() - openedAt, name: 'popup-closed' }); });
        popup.on('console', (message) => {
            if (message.type() === 'error' || message.type() === 'warning') {
                report.events.push({ at: Date.now() - openedAt, name: `console-${message.type()}`, text: message.text().slice(0, 160) });
            }
        });
        popup.on('pageerror', (error) => report.events.push({ at: Date.now() - openedAt, name: 'pageerror', text: String(error.message || '').slice(0, 160) }));
        // PVE reports a rejected direct console through an ExtJS alert on the
        // opener page; record its visible text so a policy mismatch is
        // distinguishable from a transport failure.
        page.on('dialog', (dialog) => { report.events.push({ at: Date.now() - openedAt, name: 'dialog', text: dialog.message().slice(0, 160) }); dialog.dismiss().catch(() => undefined); });
        await popup.waitForFunction(() => !!document.querySelector('video'), undefined, { timeout: config.timeout });
        await popup.evaluate(() => {
            const video = document.querySelector('video');
            window.qsmProbe = { presented: 0 };
            const tick = () => { window.qsmProbe.presented += 1; video.requestVideoFrameCallback(tick); };
            video.requestVideoFrameCallback(tick);
        });
        const event = (name, extra = {}) => report.events.push({ at: Date.now() - openedAt, name, ...extra });
        event('popup-opened');
        let woke = false;
        let slept = false;
        let resized = false;
        let lastStatus = null;
        while (Date.now() - openedAt < config.observeMs) {
            if (popupClosed) {
                const alertText = await page.evaluate(() => {
                    const box = document.querySelector('.x-message-box');
                    return box ? box.textContent.replace(/\s+/g, ' ').trim().slice(0, 200) : null;
                }).catch(() => null);
                event('observe-stopped', { alert: alertText });
                break;
            }
            let sample;
            try { sample = await sampleConsole(popup); } catch (error) {
                event('sample-error', { text: String(error.message || '').slice(0, 120) });
                await wait(config.sampleMs);
                continue;
            }
            sample.at = Date.now() - openedAt;
            if (sample.status !== lastStatus) {
                event('status', { status: sample.status });
                lastStatus = sample.status;
            }
            report.samples.push(sample);
            if (!slept && config['sleep-cmd'] && config.sleepAfterMs && Date.now() - openedAt >= config.sleepAfterMs) {
                slept = true;
                event('sleep-cmd-begin');
                try {
                    execSync(config['sleep-cmd'], { stdio: ['ignore', 'ignore', 'ignore'], timeout: 20_000 });
                    event('sleep-cmd-done', { ok: true });
                } catch (error) {
                    event('sleep-cmd-done', { ok: false, text: String(error.message || '').slice(0, 120) });
                }
            }
            if (!resized && config['resize-to'] && config.resizeAfterMs && Date.now() - openedAt >= config.resizeAfterMs) {
                resized = true;
                const [width, height] = config['resize-to'].split('x').map(Number);
                event('viewport-resize', { to: `${width}x${height}` });
                await popup.setViewportSize({ width, height });
            }
            if (!woke && config.wake !== 'none' && Date.now() - openedAt >= config.wakeAfterMs) {
                woke = true;
                const video = popup.locator('video');
                const videoBox = await video.boundingBox();
                if (!videoBox) { fail('VIDEO_UNAVAILABLE'); }
                for (const action of config.wake.split(',')) {
                    if (action === 'mouse') {
                        event('wake-mouse-begin');
                        for (let step = 0; step <= 12; step += 1) {
                            const fraction = step / 12;
                            await popup.mouse.move(videoBox.x + videoBox.width * (0.3 + 0.4 * fraction),
                                videoBox.y + videoBox.height * (0.3 + 0.3 * fraction));
                            await wait(40);
                        }
                        event('wake-mouse-end');
                    } else if (action === 'key') {
                        await video.focus();
                        event('wake-key-shift');
                        await video.press('Shift');
                    } else if (action === 'click') {
                        event('wake-click');
                        await popup.mouse.click(videoBox.x + videoBox.width * 0.7, videoBox.y + videoBox.height * 0.6);
                    }
                }
                event('wake-done');
            }
            await wait(config.sampleMs);
        }
        const before = report.samples.filter((sample) => sample.at < config.wakeAfterMs);
        const after = report.samples.filter((sample) => sample.at >= config.wakeAfterMs);
        const firstVideo = report.samples.find((sample) => sample.width > 0 && sample.ready >= 2);
        const firstBright = report.samples.find((sample) => sample.luma > 8);
        const wakeDone = report.events.find((entry) => entry.name === 'wake-done');
        const firstBrightAfterWake = wakeDone ? report.samples.find((sample) => sample.at > wakeDone.at && sample.luma > 8) : null;
        const last = report.samples[report.samples.length - 1];
        report.summary = {
            firstVideoMs: firstVideo ? firstVideo.at : null,
            firstVideoSize: firstVideo ? `${firstVideo.width}x${firstVideo.height}` : null,
            firstBrightFrameMs: firstBright ? firstBright.at : null,
            presentedBeforeWake: before.length ? before[before.length - 1].presented : null,
            presentedAfterWake: after.length && before.length ? last.presented - before[before.length - 1].presented : null,
            lumaBeforeWake: before.length ? before[before.length - 1].luma : null,
            lumaFinal: last ? last.luma : null,
            wakeDoneMs: wakeDone ? wakeDone.at : null,
            brightAfterWakeMs: firstBrightAfterWake ? firstBrightAfterWake.at - wakeDone.at : null,
            finalSize: last ? `${last.width}x${last.height}` : null,
            finalStatus: last ? last.status : null,
            statuses: report.events.filter((entry) => entry.name === 'status').map((entry) => `${entry.at}:${entry.status}`),
        };
        const text = JSON.stringify(report, null, 1);
        if (config.report) { fs.writeFileSync(config.report, text); }
        process.stdout.write(`QSM_DISPLAY_SLEEP_PROBE ${JSON.stringify(report.summary)}\n`);
    } finally {
        password = '';
        if (context) { await context.close(); }
        if (browser) { await browser.close(); }
    }
}

main().catch((error) => {
    const code = error && /^[A-Z0-9_]+$/.test(error.code || '') ? error.code : 'FAILED';
    process.stderr.write(`QSM_DISPLAY_SLEEP_PROBE_FAIL code=${code} message=${String(error && error.message || '').slice(0, 200)}\n`);
    process.exitCode = 1;
});
