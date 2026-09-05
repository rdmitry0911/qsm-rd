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
let diagnostic = {
    requests: 0, responses: 0, classes: new Set(), failures: 0, outcome: 'none', envelope: 'none',
    contentType: 'none', responseBytes: -1, apiSuccess: 'none', apiStatus: 'none', messageBytes: -1,
};

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
        if (key === '--wait-for-vm-start') {
            if (seen.has(key)) { fail('INVALID_ARGUMENTS'); }
            seen.add(key);
            result.waitForVmStart = true;
            continue;
        }
        if (key === '--headful') {
            if (seen.has(key)) { fail('INVALID_ARGUMENTS'); }
            seen.add(key);
            result.headful = true;
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
    // The fixture's window cannot cross either desktop edge. A former
    // 200↔1000 target selected its direction from the initial side and could
    // demand an impossible final position when the guest started at 200.
    // Its central 600 px marker is reachable from both fixture placements
    // and still crosses enough pixels to expose a coalesced or stale drag.
    const target = { x: Math.round(fixture.width / 2), y: 215 };
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
        if (result) {
            process.stderr.write(`QSM_POPUP_DRAG_METRICS first_ms=${Math.round(result.firstMotionLatencyMs || -1)} gap_ms=${Math.round(result.largestMotionGapMs || -1)} frames=${result.observedMotionFrames || 0} final=${Math.round(result.finalCenter || -1)} target=${Math.round(result.targetX || -1)}\n`);
        }
        fail('POPUP_DRAG_NOT_SMOOTH');
    }
}

async function verifyFullscreenRecoveryAndGuestCursor(popup) {
    /*
     * Reproduce the failure reported by users: native popup full screen,
     * Escape back to a window, and then normal mouse input.  The following
     * drag fixture is deliberately executed only after this round trip, so a
     * successful result proves the visible guest still receives button edges
     * instead of merely proving that the browser exited full screen.
     *
     * The guest supplies the cursor shape, but the browser draws it at the
     * physical OS pointer. This avoids a delayed Display1 MouseSet canvas
     * jumping away from a native popup edge.
     */
    const setKeyboardPriority = (priority) => popup.evaluate((value) => {
        const select = document.getElementById('qsm-direct-keyboard-priority');
        if (!select) return false;
        select.value = value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
        return select.value === value;
    }, priority);
    const guestFirstReady = await setKeyboardPriority('guest-first');
    if (!guestFirstReady) fail('KEYBOARD_PRIORITY_CONTROL_UNAVAILABLE');
    const fullscreen = popup.locator('button').filter({ hasText: /^Full Screen$/ }).last();
    if (await fullscreen.count() !== 1) fail('FULLSCREEN_CONTROL_UNAVAILABLE');
    // Guest-first gives ordinary Escape to the VM. Its explicit capture-exit
    // chord must instead leave browser full screen and focus a client control.
    await fullscreen.click();
    const guestEntered = await popup.waitForFunction(() => !!document.fullscreenElement, undefined, { timeout: 5000 })
        .then(() => true).catch(() => false);
    if (!guestEntered) fail('GUEST_FIRST_FULLSCREEN_ENTRY_NOT_OBSERVED');
    const guestFirstVideo = popup.locator('video');
    await guestFirstVideo.focus();
    await guestFirstVideo.press('Control+Alt+Shift+Escape');
    const guestExited = await popup.waitForFunction(() => !document.fullscreenElement, undefined, { timeout: 5000 })
        .then(() => true).catch(() => false);
    const guestCaptureReleased = guestExited && await popup.waitForFunction(() =>
        document.activeElement?.textContent === 'Settings', undefined, { timeout: 1000 })
        .then(() => true).catch(() => false);
    if (!guestCaptureReleased) fail('GUEST_FIRST_CAPTURE_EXIT_NOT_OBSERVED');

    // This second scenario proves the documented client-first route: browser
    // Escape leaves native full screen and the guest cannot retain a pressed
    // button.
    const clientFirstReady = await setKeyboardPriority('client-first');
    if (!clientFirstReady) fail('KEYBOARD_PRIORITY_CONTROL_UNAVAILABLE');
    await fullscreen.click();
    const entered = await popup.waitForFunction(() => !!document.fullscreenElement, undefined, { timeout: 5000 })
        .then(() => true).catch(() => false);
    if (!entered) {
        const state = await popup.evaluate(() => ({
            enabled: document.fullscreenEnabled === true,
            active: document.fullscreenElement !== null,
            visible: document.visibilityState === 'visible',
        }));
        process.stderr.write(`QSM_FULLSCREEN_ENTRY_FLAGS enabled=${Number(state.enabled)} active=${Number(state.active)} visible=${Number(state.visible)}\n`);
        fail('FULLSCREEN_ENTRY_NOT_OBSERVED');
    }
    // The product full-screenchange handler deliberately restores focus to
    // the video after the toolbar click. Wait one browser task and assert it
    // before sending Escape; Playwright otherwise can target the stale
    // toolbar button while a real user already has the Console surface.
    await wait(80);
    const fullscreenVideo = popup.locator('video');
    await fullscreenVideo.focus();
    const fullscreenBox = await fullscreenVideo.boundingBox();
    if (!fullscreenBox || fullscreenBox.width < 128 || fullscreenBox.height < 128) {
        fail('FULLSCREEN_VIDEO_UNAVAILABLE');
    }
    // Escape frequently interrupts a real drag, rather than an idle cursor.
    // Hold a button in an inert lower-right part of the fixture and exit
    // before mouseup. The Console must synthesize the release over its
    // ordered control channel; the subsequent normal drag is the causal
    // proof that the guest did not retain a grabbed pointer.
    await popup.mouse.move(fullscreenBox.x + Math.round(fullscreenBox.width * 0.88),
        fullscreenBox.y + Math.round(fullscreenBox.height * 0.88));
    await popup.evaluate(() => {
        const video = document.querySelector('video');
        window.qsmFullscreenEscapeE2e = [];
        video.addEventListener('keydown', (event) => {
            if (event.code !== 'Escape') return;
            queueMicrotask(() => window.qsmFullscreenEscapeE2e.push({
                prevented: event.defaultPrevented,
                fullscreen: !!document.fullscreenElement,
                focused: document.activeElement === video,
            }));
        }, { once: true });
    });
    await popup.mouse.down();
    // A browser popup is an independent top-level page. Locator.press()
    // targets its focused guest surface explicitly; Page.keyboard can remain
    // attached to the PVE opener after a native-fullscreen transition.
    await fullscreenVideo.press('Escape');
    const exited = await popup.waitForFunction(() => !document.fullscreenElement, undefined, { timeout: 5000 })
        .then(() => true).catch(() => false);
    if (!exited) {
        const flags = await popup.evaluate(() => ({
            keyboard: document.getElementById('qsm-direct-keyboard-priority')?.value || 'missing',
            escape: window.qsmFullscreenEscapeE2e || [],
            fullscreen: !!document.fullscreenElement,
            videoFocused: document.activeElement === document.querySelector('video'),
        }));
        process.stderr.write(`QSM_FULLSCREEN_ESCAPE_FLAGS keyboard=${flags.keyboard} event=${flags.escape.length ? Number(flags.escape[0].prevented) : -1}${flags.escape.length ? Number(flags.escape[0].fullscreen) : -1}${flags.escape.length ? Number(flags.escape[0].focused) : -1} fullscreen=${Number(flags.fullscreen)} focus=${Number(flags.videoFocused)}\n`);
        fail('FULLSCREEN_EXIT_NOT_OBSERVED');
    }
    await popup.mouse.up();
    const video = popup.locator('video');
    const box = await video.boundingBox();
    if (!box || box.width < 128 || box.height < 128) fail('FULLSCREEN_VIDEO_UNAVAILABLE');
    // Verify both native-window resize corners as well as an ordinary inner
    // point. The video must retain a browser-native cursor at every point;
    // the actual OS frame outside it remains free to choose its resize cursor.
    const targets = [
        { x: box.x + Math.round(box.width * 0.04), y: box.y + Math.round(box.height * 0.04) },
        { x: box.x + Math.round(box.width * 0.96), y: box.y + Math.round(box.height * 0.96) },
        { x: box.x + Math.round(box.width * 0.73), y: box.y + Math.round(box.height * 0.67) },
    ];
    for (const target of targets) {
        await popup.mouse.move(target.x, target.y);
        await wait(25);
        const result = await popup.evaluate((point) => {
            const video = document.querySelector('video');
            if (!video) return null;
            const videoBox = video.getBoundingClientRect();
            return {
                videoFocused: document.activeElement === video,
                nativeCursor: getComputedStyle(video).cursor !== 'none',
                videoContainsPointer: point.x >= videoBox.left && point.x <= videoBox.right &&
                    point.y >= videoBox.top && point.y <= videoBox.bottom,
            };
        }, target);
        if (!result || !result.videoFocused ||
            !result.nativeCursor ||
            !result.videoContainsPointer) {
            if (result) {
                process.stderr.write(`QSM_FULLSCREEN_CURSOR_FLAGS focus=${Number(result.videoFocused)} native=${Number(result.nativeCursor)} video=${Number(result.videoContainsPointer)}\n`);
            }
            fail('FULLSCREEN_INPUT_OR_CURSOR_RECOVERY_FAILED');
        }
    }
}

async function verifyPopupControls(popup) {
    /*
     * This is deliberately against the delivered PVE popup rather than a
     * DOM mock. It proves that ordinary pointer motion cannot reveal the
     * toolbar, while a sustained dwell in the small top strip can, and that
     * all browser-local tuning controls remain reachable afterwards.
     */
    const result = await popup.evaluate(async () => {
        const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
        const video = document.querySelector('video');
        const toolbar = document.querySelector('[aria-label="Console controls"]');
        if (!video || !toolbar) return null;
        const visible = () => Number.parseFloat(getComputedStyle(toolbar).opacity || '0') > 0.5;
        const move = (x, y) => video.dispatchEvent(new MouseEvent('mousemove', {
            clientX: x, clientY: y, bubbles: true, cancelable: true,
        }));
        const box = video.getBoundingClientRect();
        // The initial status may be visible until the first decoded frame.
        // Wait through its documented default auto-hide interval and CSS
        // transition, then test real event handlers rather than CSS text.
        await wait(2200);
        const initiallyHidden = !visible();
        move(Math.floor(box.width / 2), Math.floor(box.height / 2));
        await wait(180);
        const centerStayedHidden = !visible();
        move(Math.floor(box.width / 2), Math.max(1, Math.floor(box.top) + 1));
        await wait(350);
        const hiddenBeforeDwell = !visible();
        await wait(450);
        const shownAfterDwell = visible();
        const settings = [...toolbar.querySelectorAll('button')].find((button) => button.textContent === 'Settings');
        if (!settings) return { initiallyHidden, centerStayedHidden, hiddenBeforeDwell, shownAfterDwell };
        settings.click();
        await wait(40);
        const panel = document.querySelector('[aria-label="Console settings"]');
        const inputNames = [
            'toolbarHotZonePx', 'toolbarRevealDelayMs', 'toolbarHideDelayMs', 'targetFps',
            'playoutDelayMs', 'resizeDebounceMs', 'resizeSettleMs',
        ];
        const settingsInputs = inputNames.every((name) => {
            const input = document.getElementById(`qsm-direct-setting-${name}`);
            return input && input.type === 'number' && Number(input.min) >= 0 && Number(input.max) >= Number(input.min);
        });
        const keyboardPriority = document.getElementById('qsm-direct-keyboard-priority');
        const dispatchKey = (options) => {
            const event = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...options });
            video.dispatchEvent(event);
            return event.defaultPrevented;
        };
        const guestFirstDefault = keyboardPriority && keyboardPriority.value === 'guest-first';
        const guestFirstPreventsF5 = dispatchKey({ code: 'F5', key: 'F5' });
        const escapeCapture = dispatchKey({ code: 'Escape', key: 'Escape', ctrlKey: true, altKey: true, shiftKey: true });
        const captureEscapeMovesFocus = escapeCapture && document.activeElement === settings;
        keyboardPriority.value = 'client-first';
        keyboardPriority.dispatchEvent(new Event('change', { bubbles: true }));
        video.focus();
        const clientFirstLeavesF5 = !dispatchKey({ code: 'F5', key: 'F5' });
        keyboardPriority.value = 'guest-first';
        keyboardPriority.dispatchEvent(new Event('change', { bubbles: true }));
        const guestCursor = document.querySelector('canvas[aria-hidden="true"]');
        return {
            initiallyHidden,
            centerStayedHidden,
            hiddenBeforeDwell,
            shownAfterDwell,
            settingsOpen: !!panel && getComputedStyle(panel).display !== 'none',
            settingsInputs,
            guestFirstDefault,
            guestFirstPreventsF5,
            captureEscapeMovesFocus,
            clientFirstLeavesF5,
            localResizeHandles: document.querySelectorAll('[data-qsm-resize-edge]').length,
            guestCursorLayer: !!guestCursor && getComputedStyle(guestCursor).pointerEvents === 'none',
            guestCursorVisible: !!guestCursor && guestCursor.width > 0 && guestCursor.height > 0 &&
                getComputedStyle(guestCursor).display !== 'none',
        };
    });
    if (!result || !result.initiallyHidden || !result.centerStayedHidden || !result.hiddenBeforeDwell ||
        !result.shownAfterDwell || !result.settingsOpen || !result.settingsInputs ||
        !result.guestFirstDefault || !result.guestFirstPreventsF5 ||
        !result.captureEscapeMovesFocus || !result.clientFirstLeavesF5 ||
        !result.guestCursorLayer || result.localResizeHandles !== 0) {
        // These are fixed booleans from the popup DOM, not PVE credentials,
        // SDP, guest pixels, or user input. They make a failed lab gate
        // actionable without widening its deliberately redacted diagnostics.
        if (result) {
            process.stderr.write(`QSM_POPUP_CONTROL_FLAGS toolbar=${Number(result.initiallyHidden)}${Number(result.centerStayedHidden)}${Number(result.hiddenBeforeDwell)}${Number(result.shownAfterDwell)} settings=${Number(result.settingsOpen)}${Number(result.settingsInputs)} keyboard=${Number(result.guestFirstDefault)}${Number(result.guestFirstPreventsF5)}${Number(result.captureEscapeMovesFocus)}${Number(result.clientFirstLeavesF5)} cursor=${Number(result.guestCursorLayer)}${Number(result.guestCursorVisible)} resize=${result.localResizeHandles}\n`);
        }
        fail('POPUP_CONTROLS_INVALID');
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
            browser = await chromium.launch({ headless: !config.headful, executablePath: config.chrome || undefined,
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
            const rawContentType = response.headers()['content-type'] || 'none';
            observed.contentType = /^[A-Za-z0-9./;=+ -]{1,120}$/.test(rawContentType)
                ? rawContentType.replace(/\s+/g, '_') : 'invalid';
            response.text().then((text) => {
                observed.responseBytes = Buffer.byteLength(text, 'utf8');
                let body;
                try { body = JSON.parse(text); } catch (_) {
                    observed.outcome = 'invalid';
                    // This is deliberately just a format signature, never
                    // payload data. PVE's XSSI prefix is valid for ExtJS but
                    // is not accepted by JSON.parse/Playwright Response.json.
                    observed.envelope = /^\s*\)\]\}',/.test(text) ? 'xssi-prefix' : 'non-json';
                    return;
                }
                // Keep diagnostics safe for a protected WebRTC endpoint: the
                // SDP and response body never leave this process.  The
                // envelope class is sufficient to distinguish an ExtJS API
                // handoff from an HTML/login fallback or a malformed proxy
                // response.
                const keys = body && typeof body === 'object' && !Array.isArray(body)
                    ? Object.keys(body).sort().join('+') : typeof body;
                observed.envelope = /^[a-z+]{1,80}$/.test(keys) ? keys : 'invalid';
                observed.apiSuccess = body && typeof body.success === 'boolean' ? String(body.success) : typeof body?.success;
                observed.apiStatus = body && Number.isInteger(body.status) ? String(body.status) : typeof body?.status;
                observed.messageBytes = body && typeof body.message === 'string'
                    ? Buffer.byteLength(body.message, 'utf8') : -1;
                if (body && (body.success === true || body.success === 1 ||
                    (body.data && body.data.type === 'answer' && typeof body.data.sdp === 'string'))) {
                    observed.outcome = 'success';
                }
                else if (body && typeof body.message === 'string' && body.message === 'qsm direct console is unavailable') { observed.outcome = 'unavailable'; }
                else if (body && body.errors && typeof body.errors === 'object') { observed.outcome = 'validation'; }
                else { observed.outcome = 'other'; }
            }).catch(() => { observed.outcome = 'invalid'; observed.envelope = 'unreadable'; });
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
        if (config.waitForVmStart) {
            phase = 'VERIFYING_VM_START_WAIT';
            await popup.waitForFunction(() => document.body &&
                document.body.textContent.includes('Virtual machine is stopped. Waiting for it to start…'),
            undefined, { timeout: config.timeout });
        }
        phase = 'WAITING_FOR_GUEST_VIDEO';
        await popup.waitForFunction(() => {
            const video = document.querySelector('video');
            return !!video && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0 && video.videoHeight > 0;
        }, undefined, { timeout: config.timeout });
        phase = 'VERIFYING_POPUP_CONTROLS';
        await verifyPopupControls(popup);
        if (config.dragFixture) {
            phase = 'VERIFYING_FULLSCREEN_INPUT_RECOVERY';
            await verifyFullscreenRecoveryAndGuestCursor(popup);
            phase = 'MEASURING_POPUP_DRAG';
            await measurePopupDrag(popup, config.timeout);
        }
        if (observed.requests !== 1 || observed.responses !== 1 || observed.failures !== 0 || !observed.classes.has('2xx') || observed.outcome !== 'success') {
            fail('INVALID_PVE_DIRECT_HANDOFF');
        }
        phase = 'PASS';
        process.stdout.write(`QSM_PVE_DIRECT_BROWSER_E2E_OK protected_route=1 popup_video=1 response=2xx fullscreen=${config.dragFixture ? 1 : 0} drag=${config.dragFixture ? 1 : 0}\n`);
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
    process.stderr.write(`QSM_PVE_DIRECT_BROWSER_E2E_FAIL code=${code} phase=${phase} requests=${diagnostic.requests} responses=${diagnostic.responses} response_classes=${classes} outcome=${diagnostic.outcome} envelope=${diagnostic.envelope} api_success=${diagnostic.apiSuccess} api_status=${diagnostic.apiStatus} message_bytes=${diagnostic.messageBytes} content_type=${diagnostic.contentType} bytes=${diagnostic.responseBytes} failures=${diagnostic.failures}\n`);
    process.exitCode = 1;
});
