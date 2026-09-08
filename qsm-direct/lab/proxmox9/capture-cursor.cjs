#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Headful driver for the cursor-switch screen recording.  The guest cursor is
 * a CSS cursor (QSM sets video.style.cursor; noVNC sets its own), so it is the
 * real X pointer, not a pixel in the video surface — only an OS screen capture
 * that draws the mouse can show it.  This script runs a HEADFUL browser on an
 * Xvfb display, opens the shipped Console (QSM Direct or noVNC), moves the
 * pointer between the blue icon (default arrow) and the teal resize handle
 * (cursor: ew-resize) so the switch is visible, and flashes a white marker in
 * the top-left at each move so the recording carries the input timestamp.
 * The surrounding shell script records the Xvfb screen with ffmpeg.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const fail = (code) => { const error = new Error(code); error.code = code; throw error; };
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function readPassword(file) {
    const text = fs.readFileSync(file, 'utf8').replace(/\r?\n$/, '');
    if (!text) { fail('PASSWORD_FILE_UNAVAILABLE'); }
    return text;
}

async function main() {
    const [transport, pveUrl, user, passwordFile, vmidRaw, readyFile, goFile] = process.argv.slice(2);
    if (!['qsm', 'novnc'].includes(transport)) { fail('INVALID_ARGUMENTS'); }
    const vmid = Number(vmidRaw);
    const password = readPassword(passwordFile);
    let chromium;
    try { ({ chromium } = require('playwright')); }
    catch (_error) { ({ chromium } = require('/opt/qsm-browser/node_modules/playwright-core')); }
    // Headful on the Xvfb display so the X cursor exists and ffmpeg can draw it.
    const browser = await chromium.launch({ headless: false, executablePath: '/usr/bin/google-chrome', args: [
        '--no-sandbox', '--no-proxy-server', '--disable-dev-shm-usage', '--start-maximized',
        '--window-position=0,0', '--autoplay-policy=no-user-gesture-required',
        '--disable-features=NetworkServiceSandbox,WebRtcHideLocalIpsWithMdns',
        '--force-webrtc-ip-handling-policy=default',
    ] });
    const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: null });
    const page = await context.newPage();
    page.setDefaultTimeout(60000);
    await page.goto(`${pveUrl}/`, { waitUntil: 'domcontentloaded' });
    const [username, realm] = user.split('@');
    await page.locator('input[name="username"]:visible').fill(username);
    const descr = await page.waitForFunction((r) => { const c = window.Ext?.getCmp?.('pveloginrealm');
        const rec = c?.getStore?.().findRecord?.('realm', r, 0, false, true, true); return rec ? String(rec.get('descr')) : null; }, realm).then((h) => h.jsonValue());
    await page.locator('#pveloginrealm-inputEl:visible').click();
    await page.locator('.x-boundlist:visible .x-boundlist-item').filter({ hasText: descr }).first().click();
    await page.locator('input[name="password"]:visible').fill(password);
    await page.locator('.x-window:visible .x-btn').filter({ hasText: /^Login$/ }).last().click();
    await page.waitForFunction((x) => window.Proxmox?.UserName === x && !!window.PVE, user);
    const notice = page.locator('.x-message-box:visible'); if (await notice.count()) { await notice.locator('.x-btn').filter({ hasText: /^OK$/ }).click(); }
    await page.waitForFunction((w) => { const t = window.Ext?.ComponentQuery?.query('pveResourceTree')?.[0];
        const r = t?.getStore?.().getRootNode()?.findChild?.('id', `qemu/${w}`, true); if (!r) return false; t.selectById(r.data.id); return true; }, vmid);
    const selector = await page.waitForFunction((w) => { const b = window.Ext?.ComponentQuery?.query('pveConsoleButton')?.find((c) =>
        c?.consoleType === 'kvm' && Number(c.vmid) === w && c.rendered && !c.disabled); return b?.getEl?.().dom?.id || null; }, vmid).then((h) => h.jsonValue());
    const box = await page.locator(`#${selector}`).boundingBox();
    await page.mouse.click(box.x + box.width - 6, box.y + box.height / 2);
    const label = transport === 'qsm' ? /^QSM Direct$/ : /^noVNC$/;
    const item = page.locator('.x-menu:visible .x-menu-item').filter({ hasText: label });
    await item.first().waitFor({ state: 'visible', timeout: 15000 });
    const popupPromise = page.waitForEvent('popup');
    await item.click();
    const popup = await popupPromise;
    popup.setDefaultTimeout(60000);
    await popup.waitForFunction((kind) => {
        const el = document.querySelector('video') || document.querySelector('canvas') || document.querySelector('iframe')?.contentDocument?.querySelector('canvas');
        if (!el) { return false; }
        if (kind === 'qsm') { return el.tagName === 'VIDEO' && el.readyState >= 2 && el.videoWidth > 0; }
        if (el.tagName !== 'CANVAS' || el.width < 64) { return false; }
        try { return Array.from(el.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, 32, 32).data).some((v) => v !== 0); } catch (_e) { return false; }
    }, transport, { timeout: 60000 });
    await wait(1500);
    // A white input marker in the popup, plus the surface geometry to map
    // guest coordinates onto screen pixels.
    const geo = await popup.evaluate(() => {
        const el = document.querySelector('video') || document.querySelector('canvas') || document.querySelector('iframe')?.contentDocument?.querySelector('canvas');
        const box = el.getBoundingClientRect();
        const src = el.tagName === 'VIDEO' ? { w: el.videoWidth, h: el.videoHeight } : { w: el.width, h: el.height };
        const marker = document.createElement('div');
        marker.id = 'qsm-cap-marker';
        marker.style.cssText = 'position:fixed;left:0;top:0;width:60px;height:60px;z-index:99999;background:#000;pointer-events:none';
        document.body.appendChild(marker);
        window.__mark = (on) => { marker.style.background = on ? '#fff' : '#000'; };
        return { box: { x: box.left, y: box.top, w: box.width, h: box.height }, src };
    });
    const guest = (gx, gy) => {
        const scale = Math.min(geo.box.w / geo.src.w, geo.box.h / geo.src.h);
        return { x: geo.box.x + (geo.box.w - geo.src.w * scale) / 2 + gx * scale,
                 y: geo.box.y + (geo.box.h - geo.src.h * scale) / 2 + gy * scale };
    };
    const icon = guest(640, 370);       // blue icon centre → default arrow
    const handle = guest(770, 370);     // teal resize handle → ew-resize
    // Signal the recorder we are ready, then wait for it to start ffmpeg.
    fs.writeFileSync(readyFile, 'ready');
    for (let i = 0; i < 120 && !fs.existsSync(goFile); i += 1) { await wait(100); }
    await wait(600);
    // Three slow arrow→resize→arrow cycles, marker flashing at each move.
    for (let cycle = 0; cycle < 3; cycle += 1) {
        await popup.mouse.move(icon.x, icon.y);
        await popup.evaluate(() => window.__mark(false));
        await wait(1300);
        await popup.evaluate(() => window.__mark(true));
        await popup.mouse.move(handle.x, handle.y);
        await wait(200);
        await popup.evaluate(() => window.__mark(false));
        await wait(1500);
    }
    await popup.mouse.move(icon.x, icon.y);
    await wait(800);
    await context.close();
    await browser.close();
    process.exitCode = 0;
}

main().catch((error) => {
    process.stderr.write(`QSM_CAPTURE_FAIL ${error && (error.code || error.message)}\n`);
    process.exitCode = 1;
});
