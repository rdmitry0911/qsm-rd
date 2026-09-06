#!/usr/bin/env node
'use strict';

/*
 * Tiny JSON-lines Playwright peer used only by the local browser E2E gate.
 * It has no network listener and no PVE credentials: the Python driver owns
 * offer/answer signalling in the same local process tree.
 */

const readline = require('node:readline');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

let browser;
let page;
let pageDirectory;
let closing = false;
let nativeWindowSession;
let nativeWindowId;

async function startLocalPage() {
    // A file origin is potentially trustworthy and does not require Chrome's
    // HTTP network service to traverse loopback, which is unavailable in
    // some unprivileged LXC profiles. WebRTC still uses its actual local
    // UDP/DTLS path, so this remains a browser media qualification.
    pageDirectory = await fs.mkdtemp(path.join(os.tmpdir(), 'qsm-webrtc-page-'));
    const pagePath = path.join(pageDirectory, 'index.html');
    // Match the shipped Console popup's geometry rather than relying on a
    // browser's 300x150 default video box.  The E2E gate must be able to
    // reject a stream whose pixels decode correctly but occupy only part of
    // a resized or full-screen browser viewport.
    await fs.writeFile(pagePath, `<!doctype html>
<style>html{width:100%;height:100%;background:#000}body{width:100vw;height:100vh;min-width:100vw;min-height:100vh;margin:0;position:relative;overflow:hidden;background:#000}#remote{position:fixed;inset:0;display:block;width:100vw;height:100vh;max-width:none;max-height:none;background:#000;object-fit:contain}</style>
<video id="remote" autoplay muted playsinline></video>`);
    return `file://${pagePath}`;
}

function reply(payload) {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
}

async function waitForIceComplete() {
    await page.evaluate(async () => {
        const pc = window.qsmPeerConnection;
        if (pc.iceGatheringState === 'complete' || window.qsmIceCandidateSeen) {
            return;
        }
        await new Promise((resolve) => {
            // Chrome may keep gathering IPv6 candidates well after it has a
            // usable loopback candidate. For this direct local gate, the
            // first candidate is sufficient and avoids inventing a STUN/TURN
            // dependency solely for qualification.
            const timer = window.setTimeout(resolve, 5000);
            pc.addEventListener('icegatheringstatechange', () => {
                if (pc.iceGatheringState === 'complete') {
                    window.clearTimeout(timer);
                    resolve();
                }
            });
            pc.addEventListener('icecandidate', (event) => {
                if (event.candidate) {
                    window.qsmIceCandidateSeen = true;
                    window.clearTimeout(timer);
                    resolve();
                }
            });
        });
    });
}

async function createOffer() {
    const executablePath = process.env.QSM_BROWSER_E2E_EXECUTABLE;
    const headful = process.env.QSM_BROWSER_E2E_HEADFUL === '1';
    browser = await chromium.launch({
        // Default to headless for a lightweight CI gate, but permit the
        // latency harness to exercise Chrome's actual visible compositor and
        // video presentation path on an isolated Xvfb display.
        headless: !headful,
        // The Linux Chromium build bundled with Playwright intentionally
        // excludes H.264.  This qualification uses a normal Chrome/Edge
        // executable selected by the Python driver, because it is the actual
        // browser compatibility contract being tested.
        ...(executablePath ? { executablePath } : {}),
        args: [
            // The qualification is often executed in an unprivileged LXC,
            // where Chrome's network namespace sandbox is unavailable even
            // though the ordinary browser/WebRTC stack itself is usable.
            // This applies only to the disposable local test process; the
            // packaged PVE Console never supplies browser launch flags.
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-seccomp-filter-sandbox',
            '--disable-dev-shm-usage',
            '--autoplay-policy=no-user-gesture-required',
            // Only the local test needs aiortc to see the loopback host
            // candidate instead of Chrome's privacy-preserving mDNS alias.
            '--disable-features=WebRtcHideLocalIpsWithMdns,NetworkServiceSandbox',
            '--force-webrtc-ip-handling-policy=default',
        ],
    });
    // `page.setViewportSize()` is a DevTools emulation primitive. In headed
    // Chromium it can enlarge the screenshot canvas without resizing the
    // native video compositor surface, which is the opposite of an operator
    // dragging a Console popup edge. For visual qualification use an actual
    // browser window and set its native bounds below.
    page = headful ? await browser.newPage({ viewport: null }) :
        await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await page.goto(await startLocalPage());
    if (headful) {
        nativeWindowSession = await page.context().newCDPSession(page);
        const result = await nativeWindowSession.send('Browser.getWindowForTarget');
        nativeWindowId = result.windowId;
        await setNativeViewport({ width: 1280, height: 800 });
    }
    const iceServer = process.env.QSM_BROWSER_E2E_ICE_SERVER;
    const offer = await page.evaluate(async (iceUrl) => {
        const video = document.getElementById('remote');
        // The normal browser contract is host ICE only.  A remote laboratory
        // peer may opt into a STUN URL when its LXC policy prevents Chrome
        // from exposing a host candidate; that is an E2E-lab plumbing switch,
        // never a setting emitted by the packaged PVE Console.
        const pc = new RTCPeerConnection({ iceServers: iceUrl ? [{ urls: iceUrl }] : [] });
        window.qsmPeerConnection = pc;
        window.qsmControl = pc.createDataChannel('qsm-control', { ordered: true });
        window.qsmPointer = pc.createDataChannel('qsm-pointer', { ordered: false, maxRetransmits: 0 });
        window.qsmGuestRequests = new Map();
        window.qsmGuestDownloads = new Map();
        window.qsmGuestClipboardEvents = [];
        window.qsmControl.addEventListener('message', (event) => {
            if (typeof event.data !== 'string') {
                return;
            }
            try {
                const message = JSON.parse(event.data);
                if (message?.op === 'qsm_guest_result' && typeof message.request_id === 'string') {
                    const pending = window.qsmGuestRequests.get(message.request_id);
                    if (pending) {
                        window.qsmGuestRequests.delete(message.request_id);
                        pending.resolve(message);
                    }
                } else if (message?.op === 'qsm_guest_file_download_chunk' &&
                    typeof message.request_id === 'string') {
                    const pending = window.qsmGuestRequests.get(message.request_id);
                    const reject = () => {
                        if (!pending) return;
                        window.qsmGuestRequests.delete(message.request_id);
                        window.qsmGuestDownloads.delete(message.request_id);
                        pending.resolve({ ok: false });
                    };
                    if (!pending || typeof message.name !== 'string' || !Number.isInteger(message.size) ||
                        !Number.isInteger(message.offset) || typeof message.data_b64 !== 'string' ||
                        message.size < 0 || message.size > 2 * 1024 * 1024 ||
                        message.offset < 0 || message.offset > message.size) {
                        reject(); return;
                    }
                    let binary;
                    try { binary = atob(message.data_b64); } catch (_) { reject(); return; }
                    const bytes = new Uint8Array(binary.length);
                    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
                    let transfer = window.qsmGuestDownloads.get(message.request_id);
                    if (!transfer) {
                        if (message.offset !== 0) { reject(); return; }
                        transfer = { name: message.name, size: message.size, parts: [], received: 0 };
                        window.qsmGuestDownloads.set(message.request_id, transfer);
                    }
                    if (transfer.name !== message.name || transfer.size !== message.size ||
                        transfer.received !== message.offset || bytes.length > transfer.size - transfer.received ||
                        (bytes.length === 0 && transfer.received !== transfer.size)) {
                        reject(); return;
                    }
                    transfer.parts.push(bytes);
                    transfer.received += bytes.length;
                    if (transfer.received === transfer.size) {
                        const merged = new Uint8Array(transfer.size);
                        let cursor = 0;
                        for (const part of transfer.parts) { merged.set(part, cursor); cursor += part.length; }
                        let encoded = '';
                        for (let index = 0; index < merged.length; index += 0x8000) {
                            encoded += String.fromCharCode(...merged.subarray(index, index + 0x8000));
                        }
                        window.qsmGuestDownloads.delete(message.request_id);
                        window.qsmGuestRequests.delete(message.request_id);
                        pending.resolve({ ok: true, result: {
                            name: transfer.name, bytes: merged.length, data_b64: btoa(encoded),
                        }});
                    }
                } else if (message?.op === 'qsm_guest_clipboard') {
                    window.qsmGuestClipboardEvents.push(message.text_b64);
                }
            } catch (_) {
                // Product code must ignore an unrelated or malformed SCTP
                // message. The harness only observes the QSM guest schema.
            }
        });
        window.qsmTrackKinds = [];
        window.qsmIceCandidateSeen = false;
        pc.addEventListener('track', (event) => {
            window.qsmTrackKinds.push(event.track.kind);
            if (event.track.kind === 'video') {
                // Chromium otherwise chooses a conservative receiver playout
                // target for a general WebRTC call. A PVE console is an
                // interactive desktop: prefer the freshest decodable frame.
                // Older browsers simply do not expose this optional hint.
                if (event.receiver && 'playoutDelayHint' in event.receiver) {
                    event.receiver.playoutDelayHint = 0;
                }
                window.qsmVideoReceiver = event.receiver;
                video.srcObject = event.streams[0];
                video.play().catch(() => {});
            }
        });
        pc.addTransceiver('video', { direction: 'recvonly' });
        pc.addTransceiver('audio', { direction: 'recvonly' });
        const localOffer = await pc.createOffer();
        await pc.setLocalDescription(localOffer);
        return { type: pc.localDescription.type, sdp: pc.localDescription.sdp };
    }, iceServer || '');
    await waitForIceComplete();
    return page.evaluate(() => ({
        type: window.qsmPeerConnection.localDescription.type,
        sdp: window.qsmPeerConnection.localDescription.sdp,
        candidateCount: (window.qsmPeerConnection.localDescription.sdp.match(/^a=candidate:/gm) || []).length,
        iceGatheringState: window.qsmPeerConnection.iceGatheringState,
    }));
}

async function setAnswer(answer) {
    if (!page || !answer || answer.type !== 'answer' || typeof answer.sdp !== 'string') {
        throw new Error('invalid local test answer');
    }
    await page.evaluate(async (description) => {
        await window.qsmPeerConnection.setRemoteDescription(description);
    }, answer);
}

async function status() {
    if (!page) {
        throw new Error('browser peer is not initialized');
    }
    return page.evaluate(() => {
        const video = document.getElementById('remote');
        const rect = video.getBoundingClientRect();
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;
        const objectFit = getComputedStyle(video).objectFit;
        let contentWidth = rect.width;
        let contentHeight = rect.height;
        if (objectFit === 'contain' && video.videoWidth > 0 && video.videoHeight > 0) {
            const scale = Math.min(rect.width / video.videoWidth, rect.height / video.videoHeight);
            contentWidth = video.videoWidth * scale;
            contentHeight = video.videoHeight * scale;
        }
        return {
            connectionState: window.qsmPeerConnection.connectionState,
            iceConnectionState: window.qsmPeerConnection.iceConnectionState,
            trackKinds: window.qsmTrackKinds.slice().sort(),
            readyState: video.readyState,
            videoWidth: video.videoWidth,
            videoHeight: video.videoHeight,
            currentTime: video.currentTime,
            playbackRate: video.playbackRate,
            controlReady: window.qsmControl?.readyState === 'open',
            pointerReady: window.qsmPointer?.readyState === 'open',
            playoutDelayHint: window.qsmVideoReceiver?.playoutDelayHint ?? null,
            renderer: 'native-video',
            layout: {
                viewportWidth,
                viewportHeight,
                renderedLeft: rect.left,
                renderedTop: rect.top,
                renderedWidth: rect.width,
                renderedHeight: rect.height,
                objectFit,
                fillsViewport: Math.abs(rect.left) < 0.5 && Math.abs(rect.top) < 0.5 &&
                    Math.abs(rect.width - viewportWidth) < 0.5 && Math.abs(rect.height - viewportHeight) < 0.5,
                contentWidth,
                contentHeight,
                contentFillsViewport: Math.abs(contentWidth - viewportWidth) < 1.5 &&
                    Math.abs(contentHeight - viewportHeight) < 1.5,
            },
        };
    });
}

async function setViewport(message) {
    if (!page || !message || typeof message !== 'object') {
        throw new Error('invalid browser viewport command');
    }
    const { width, height } = message;
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 64 || height < 64 ||
        width > 16384 || height > 16384 || width % 2 !== 0 || height % 2 !== 0) {
        throw new Error('invalid browser viewport geometry');
    }
    if (nativeWindowSession && nativeWindowId !== undefined) {
        await setNativeViewport({ width, height });
    } else {
        await page.setViewportSize({ width, height });
    }
    await page.evaluate(({ width: targetWidth, height: targetHeight }) => {
        const video = document.getElementById('remote');
        video.width = targetWidth;
        video.height = targetHeight;
    }, { width, height });
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));
    return status();
}

async function setNativeViewport({ width, height }) {
    if (!page || !nativeWindowSession || nativeWindowId === undefined) {
        throw new Error('native browser window is unavailable');
    }
    const chrome = await page.evaluate(() => ({
        width: window.outerWidth - window.innerWidth,
        height: window.outerHeight - window.innerHeight,
    }));
    await nativeWindowSession.send('Browser.setWindowBounds', {
        windowId: nativeWindowId,
        bounds: { width: width + Math.max(0, chrome.width), height: height + Math.max(0, chrome.height) },
    });
    for (let attempt = 0; attempt < 30; attempt += 1) {
        const actual = await page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight }));
        if (actual.width === width && actual.height === height) {
            return;
        }
        await new Promise((resolve) => setTimeout(resolve, 20));
    }
    throw new Error('native browser window did not reach the requested viewport');
}

async function screenshot(message) {
    if (!page || !message || typeof message.path !== 'string' ||
        !/^\/tmp\/qsm-browser-e2e-[A-Za-z0-9._-]{1,80}\.png$/.test(message.path)) {
        throw new Error('invalid browser screenshot path');
    }
    await page.screenshot({ path: message.path });
    return status();
}

async function frameStats() {
    if (!page) {
        throw new Error('browser peer is not initialized');
    }
    return page.evaluate(() => {
        const video = document.getElementById('remote');
        if (!video || video.videoWidth < 1 || video.videoHeight < 1 || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
            throw new Error('decoded video frame is unavailable');
        }
        // Sampling a small, fixed canvas both keeps the JSON response tiny
        // and proves that Chrome presented actual decoded pixels.  Merely
        // observing videoWidth/currentTime accepts an all-black capture, the
        // precise failure a Display1/GL regression can otherwise hide.
        const width = Math.min(video.videoWidth, 64);
        const height = Math.min(video.videoHeight, 64);
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) {
            throw new Error('2D canvas is unavailable');
        }
        context.drawImage(video, 0, 0, width, height);
        const pixels = context.getImageData(0, 0, width, height).data;
        let lumaSum = 0;
        let lumaMin = 255;
        let lumaMax = 0;
        let nonBlack = 0;
        const edgeLuma = {};
        const lumaAt = (x, y) => {
            const offset = 4 * (y * width + x);
            return (77 * pixels[offset] + 150 * pixels[offset + 1] + 29 * pixels[offset + 2]) >> 8;
        };
        const cornerMean = (startX, startY) => {
            let sum = 0;
            let count = 0;
            for (let y = startY; y < Math.min(height, startY + 4); y += 1) {
                for (let x = startX; x < Math.min(width, startX + 4); x += 1) {
                    sum += lumaAt(x, y);
                    count += 1;
                }
            }
            return count ? sum / count : 0;
        };
        for (let index = 0; index < pixels.length; index += 4) {
            // Integer BT.601 luma; alpha is deliberately ignored.
            const luma = (77 * pixels[index] + 150 * pixels[index + 1] + 29 * pixels[index + 2]) >> 8;
            lumaSum += luma;
            lumaMin = Math.min(lumaMin, luma);
            lumaMax = Math.max(lumaMax, luma);
            if (luma > 10) {
                nonBlack += 1;
            }
        }
        const samples = width * height;
        edgeLuma.topLeft = cornerMean(0, 0);
        edgeLuma.topRight = cornerMean(Math.max(0, width - 4), 0);
        edgeLuma.bottomLeft = cornerMean(0, Math.max(0, height - 4));
        edgeLuma.bottomRight = cornerMean(Math.max(0, width - 4), Math.max(0, height - 4));
        return {
            samples,
            lumaMin,
            lumaMax,
            lumaMean: lumaSum / samples,
            nonBlack,
            edgeLuma,
        };
    });
}

async function webrtcStats() {
    if (!page) {
        throw new Error('browser peer is not initialized');
    }
    return page.evaluate(async () => {
        const reports = await window.qsmPeerConnection.getStats();
        let video;
        for (const report of reports.values()) {
            if (report.type === 'inbound-rtp' && report.kind === 'video' && !report.isRemote) {
                video = report;
                break;
            }
        }
        if (!video) {
            throw new Error('inbound video stats are unavailable');
        }
        const number = (value) => typeof value === 'number' && Number.isFinite(value) ? value : null;
        const emitted = number(video.jitterBufferEmittedCount);
        const delay = number(video.jitterBufferDelay);
        return {
            framesDecoded: number(video.framesDecoded),
            framesDropped: number(video.framesDropped),
            framesReceived: number(video.framesReceived),
            packetsReceived: number(video.packetsReceived),
            packetsLost: number(video.packetsLost),
            // These are receiver-side recovery counters.  They let the
            // loss-injection gate distinguish a healthy RTX/NACK repair
            // from a merely connected stream that happened not to lose a
            // packet during the sample interval.
            nackCount: number(video.nackCount),
            pliCount: number(video.pliCount),
            firCount: number(video.firCount),
            retransmittedPacketsReceived: number(video.retransmittedPacketsReceived),
            retransmittedBytesReceived: number(video.retransmittedBytesReceived),
            totalDecodeTime: number(video.totalDecodeTime),
            jitterBufferDelay: delay,
            jitterBufferEmittedCount: emitted,
            jitterBufferMeanDelayMs: delay !== null && emitted !== null && emitted > 0
                ? (delay * 1000) / emitted : null,
            jitter: number(video.jitter),
            estimatedPlayoutTimestamp: number(video.estimatedPlayoutTimestamp),
            framesPerSecond: number(video.framesPerSecond),
            freezeCount: number(video.freezeCount),
            totalFreezesDuration: number(video.totalFreezesDuration),
        };
    });
}

async function measureHover(message) {
    if (!page || !message || typeof message !== 'object') {
        throw new Error('invalid hover measurement command');
    }
    return page.evaluate(async (payload) => {
        const integer = (name, minimum, maximum) => {
            const value = payload[name];
            if (!Number.isInteger(value) || value < minimum || value > maximum) {
                throw new Error(`invalid hover measurement ${name}`);
            }
            return value;
        };
        const width = integer('width', 1, 32767);
        const height = integer('height', 1, 32767);
        const resetX = integer('resetX', 0, width - 1);
        const resetY = integer('resetY', 0, height - 1);
        const targetX = integer('targetX', 0, width - 1);
        const targetY = integer('targetY', 0, height - 1);
        const probeX = integer('probeX', 0, width - 1);
        const probeY = integer('probeY', 0, height - 1);
        const timeoutMs = integer('timeoutMs', 50, 10000);
        const video = document.getElementById('remote');
        if (!video || video.videoWidth !== width || video.videoHeight !== height ||
            !window.qsmPointer || window.qsmPointer.readyState !== 'open') {
            throw new Error('browser hover peer is not ready');
        }
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) {
            throw new Error('hover measurement canvas is unavailable');
        }
        const popupBounds = () => {
            context.drawImage(video, 0, 0, width, height);
            const pixels = context.getImageData(0, 0, width, height).data;
            // The dedicated lab popup is #ff00ff.  Tolerance makes this
            // robust to H.264 4:2:0 conversion without accepting its dark
            // background or the blue application icon. Scan rather than
            // rely on one coordinate: a compositor or capture orientation
            // regression must not turn a latency result into a false pass.
            let minX = width;
            let minY = height;
            let maxX = -1;
            let maxY = -1;
            for (let y = 0; y < height; y += 4) {
                for (let x = 0; x < width; x += 4) {
                    const index = (y * width + x) * 4;
                    if (pixels[index] > 180 && pixels[index + 1] < 100 && pixels[index + 2] > 180) {
                        minX = Math.min(minX, x);
                        minY = Math.min(minY, y);
                        maxX = Math.max(maxX, x);
                        maxY = Math.max(maxY, y);
                    }
                }
            }
            return maxX < 0 ? null : { minX, minY, maxX, maxY };
        };
        const popupVisible = () => {
            // A full 1280x800 canvas readback can cost several browser frames
            // on a software compositor. The fixture deliberately provides a
            // known interior pixel; use it in the timing loop, then scan the
            // whole frame once only after the causal event is observed.
            context.drawImage(video, 0, 0, width, height);
            const pixel = context.getImageData(probeX, probeY, 1, 1).data;
            return pixel[0] > 180 && pixel[1] < 100 && pixel[2] > 180;
        };
        const sendPosition = (x, y) => window.qsmPointer.send(JSON.stringify({
            op: 'mouse_position', x, y, width, height,
        }));
        const awaitFrame = (timeoutMs) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('video did not present a new frame')), timeoutMs);
            const complete = (now, metadata) => {
                clearTimeout(timer);
                // requestVideoFrameCallback is tied to compositor
                // presentation rather than a canvas readback. Retaining this
                // small diagnostic record makes a measured hover delay
                // attributable to RTP/decode or to browser presentation.
                resolve({
                    callbackMs: now,
                    callbackEpochMs: performance.timeOrigin + now,
                    expectedDisplayTimeMs: Number(metadata?.expectedDisplayTime),
                    presentationTimeMs: Number(metadata?.presentationTime),
                    mediaTime: Number(metadata?.mediaTime),
                    presentedFrames: Number(metadata?.presentedFrames),
                    processingDurationMs: Number(metadata?.processingDuration) * 1000,
                    captureTimeMs: Number(metadata?.captureTime),
                    receiveTimeMs: Number(metadata?.receiveTime),
                    receiveEpochMs: Number.isFinite(Number(metadata?.receiveTime))
                        ? performance.timeOrigin + Number(metadata.receiveTime) : null,
                });
            };
            if (typeof video.requestVideoFrameCallback === 'function') {
                video.requestVideoFrameCallback(complete);
            } else {
                requestAnimationFrame(complete);
            }
        });

        // Reset away from the icon.  A fresh decoded frame makes the test
        // independent of a pointer left over from a prior iteration.
        sendPosition(resetX, resetY);
        const resetDeadline = performance.now() + timeoutMs;
        while (popupVisible()) {
            if (performance.now() >= resetDeadline) {
                throw new Error('hover popup did not clear');
            }
            await awaitFrame(Math.max(1, resetDeadline - performance.now()));
        }

        const started = performance.now();
        const startedEpochMs = Date.now();
        sendPosition(targetX, targetY);
        let observedFrames = 0;
        let presentation = null;
        let visible = popupVisible();
        while (!visible) {
            if (performance.now() - started >= timeoutMs) {
                throw new Error('hover popup was not presented before timeout');
            }
            presentation = await awaitFrame(Math.max(1, timeoutMs - (performance.now() - started)));
            observedFrames += 1;
            visible = popupVisible();
        }
        const bounds = popupBounds();
        if (!bounds) {
            throw new Error('hover probe was visible but popup geometry was not present');
        }
        const videoArrivalLatencyMs = presentation && Number.isFinite(presentation.receiveEpochMs)
            ? presentation.receiveEpochMs - startedEpochMs : null;
        return {
            latencyMs: performance.now() - started,
            videoArrivalLatencyMs,
            startedEpochMs,
            completedEpochMs: Date.now(),
            observedFrames,
            popupBounds: bounds,
            presentation,
        };
    }, message);
}

async function measurePasswordKey(message) {
    if (!page || !message || typeof message !== 'object') {
        throw new Error('invalid password-key measurement command');
    }
    return page.evaluate(async (payload) => {
        const integer = (name, minimum, maximum) => {
            const value = payload[name];
            if (!Number.isInteger(value) || value < minimum || value > maximum) {
                throw new Error(`invalid password-key measurement ${name}`);
            }
            return value;
        };
        const width = integer('width', 1, 32767);
        const height = integer('height', 1, 32767);
        const probeX = integer('probeX', 0, width - 1);
        const probeY = integer('probeY', 0, height - 1);
        const key = integer('key', 1, 255);
        const timeoutMs = integer('timeoutMs', 50, 10000);
        const video = document.getElementById('remote');
        if (!video || video.videoWidth !== width || video.videoHeight !== height ||
            !window.qsmControl || window.qsmControl.readyState !== 'open' ||
            !window.qsmPointer || window.qsmPointer.readyState !== 'open') {
            throw new Error('browser password-key peer is not ready');
        }
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) {
            throw new Error('password-key measurement canvas is unavailable');
        }
        const probeVisible = () => {
            context.drawImage(video, 0, 0, width, height);
            const pixel = context.getImageData(probeX, probeY, 1, 1).data;
            // #00ff00 is an interior pixel of the fixture's password probe.
            // A high threshold tolerates H.264 4:2:0 conversion while still
            // rejecting the dark panel and the blue focus ring.
            return pixel[0] < 100 && pixel[1] > 180 && pixel[2] < 100;
        };
        if (probeVisible()) {
            throw new Error('password probe was already visible before key press');
        }
        // The generic lane smoke test deliberately clicks at (120,104), away
        // from the input. Re-focus this real guest password field before the
        // timed part; that preparation is intentionally excluded from the
        // keyboard-to-pixel interval below.
        window.qsmPointer.send(JSON.stringify({
            op: 'mouse_position', x: 640, y: 640, width, height,
        }));
        window.qsmControl.send(JSON.stringify({ op: 'mouse_button', button: 1, down: true }));
        window.qsmControl.send(JSON.stringify({ op: 'mouse_button', button: 1, down: false }));
        await new Promise((resolve) => setTimeout(resolve, 250));
        if (probeVisible()) {
            throw new Error('password probe changed during focus preparation');
        }
        const awaitFrame = (remainingMs) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('video did not present a new frame')), remainingMs);
            const complete = (now, metadata) => {
                clearTimeout(timer);
                resolve({
                    callbackMs: now,
                    callbackEpochMs: performance.timeOrigin + now,
                    expectedDisplayTimeMs: Number(metadata?.expectedDisplayTime),
                    presentationTimeMs: Number(metadata?.presentationTime),
                    presentedFrames: Number(metadata?.presentedFrames),
                    processingDurationMs: Number(metadata?.processingDuration) * 1000,
                    receiveTimeMs: Number(metadata?.receiveTime),
                    receiveEpochMs: Number.isFinite(Number(metadata?.receiveTime))
                        ? performance.timeOrigin + Number(metadata.receiveTime) : null,
                });
            };
            if (typeof video.requestVideoFrameCallback === 'function') {
                video.requestVideoFrameCallback(complete);
            } else {
                requestAnimationFrame(complete);
            }
        });
        const started = performance.now();
        const startedEpochMs = Date.now();
        window.qsmControl.send(JSON.stringify({ op: 'keyboard', key, down: true, modifiers: 0 }));
        window.qsmControl.send(JSON.stringify({ op: 'keyboard', key, down: false, modifiers: 0 }));
        let observedFrames = 0;
        let presentation = null;
        while (!probeVisible()) {
            const elapsed = performance.now() - started;
            if (elapsed >= timeoutMs) {
                throw new Error('password character was not presented before timeout');
            }
            presentation = await awaitFrame(Math.max(1, timeoutMs - elapsed));
            observedFrames += 1;
        }
        // Leave the deterministic guest fixture ready for the next sample.
        // Backspace itself traverses the same reliable keyboard lane, so this
        // is also a cheap guard against a test accidentally measuring a
        // character left by a previous run.
        window.qsmControl.send(JSON.stringify({ op: 'keyboard', key: 14, down: true, modifiers: 0 }));
        window.qsmControl.send(JSON.stringify({ op: 'keyboard', key: 14, down: false, modifiers: 0 }));
        const cleanupDeadline = performance.now() + timeoutMs;
        let cleanupFrames = 0;
        while (probeVisible()) {
            if (performance.now() >= cleanupDeadline) {
                throw new Error('password probe did not clear after backspace');
            }
            await awaitFrame(Math.max(1, cleanupDeadline - performance.now()));
            cleanupFrames += 1;
        }
        return {
            latencyMs: performance.now() - started,
            videoArrivalLatencyMs: presentation && Number.isFinite(presentation.receiveEpochMs)
                ? presentation.receiveEpochMs - startedEpochMs : null,
            startedEpochMs,
            completedEpochMs: Date.now(),
            observedFrames,
            cleanupFrames,
            presentation,
        };
    }, message);
}

async function measurePasswordFieldKey(message) {
    if (!page || !message || typeof message !== 'object') {
        throw new Error('invalid password-field measurement command');
    }
    return page.evaluate(async (payload) => {
        const integer = (name, minimum, maximum) => {
            const value = payload[name];
            if (!Number.isInteger(value) || value < minimum || value > maximum) {
                throw new Error(`invalid password-field measurement ${name}`);
            }
            return value;
        };
        const width = integer('width', 64, 16384);
        const height = integer('height', 64, 16384);
        const focusX = integer('focusX', 0, width - 1);
        const focusY = integer('focusY', 0, height - 1);
        const fieldX = integer('fieldX', 0, width - 1);
        const fieldY = integer('fieldY', 0, height - 1);
        const fieldWidth = integer('fieldWidth', 8, width - fieldX);
        const fieldHeight = integer('fieldHeight', 8, height - fieldY);
        const key = integer('key', 1, 255);
        const timeoutMs = integer('timeoutMs', 50, 10000);
        const video = document.getElementById('remote');
        if (!video || video.videoWidth !== width || video.videoHeight !== height ||
            !window.qsmControl || window.qsmControl.readyState !== 'open' ||
            !window.qsmPointer || window.qsmPointer.readyState !== 'open') {
            throw new Error('browser password-field peer is not ready');
        }
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) {
            throw new Error('password-field measurement canvas is unavailable');
        }
        const captureField = () => {
            context.drawImage(video, 0, 0, width, height);
            return context.getImageData(fieldX, fieldY, fieldWidth, fieldHeight).data;
        };
        // Count only substantial RGB changes.  This rejects sub-threshold
        // H.264 texture noise while accepting the visibly painted password
        // bullet.  The threshold is deliberately expressed as pixels, not a
        // single probe coordinate: real greeters differ in glyph placement.
        const changedPixels = (baseline, current) => {
            let changed = 0;
            for (let index = 0; index < baseline.length; index += 4) {
                if (Math.abs(baseline[index] - current[index]) +
                    Math.abs(baseline[index + 1] - current[index + 1]) +
                    Math.abs(baseline[index + 2] - current[index + 2]) >= 90) {
                    changed += 1;
                }
            }
            return changed;
        };
        const awaitFrame = (remainingMs) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('video did not present a new frame')), remainingMs);
            video.requestVideoFrameCallback((_, metadata) => {
                clearTimeout(timer);
                resolve({ receiveEpochMs: Date.now(), mediaTime: metadata.mediaTime });
            });
        });
        // Make the real password input active before starting the stopwatch;
        // focus delivery is intentionally not charged to key-to-pixel delay.
        window.qsmPointer.send(JSON.stringify({
            op: 'mouse_position', x: focusX, y: focusY, width, height,
        }));
        window.qsmControl.send(JSON.stringify({ op: 'mouse_button', button: 1, down: true }));
        window.qsmControl.send(JSON.stringify({ op: 'mouse_button', button: 1, down: false }));
        // Flush video which may have preceded the focus event.  Without this
        // barrier a delayed focus-border repaint can be mistaken for the
        // subsequent key's password bullet, especially in a stream already
        // suffering from the latency this test is meant to expose.
        for (let frame = 0; frame < 3; frame += 1) {
            await awaitFrame(timeoutMs);
        }
        const baseline = captureField();
        const started = performance.now();
        const startedEpochMs = Date.now();
        window.qsmControl.send(JSON.stringify({ op: 'keyboard', key, down: true, modifiers: 0 }));
        window.qsmControl.send(JSON.stringify({ op: 'keyboard', key, down: false, modifiers: 0 }));
        let observedFrames = 0;
        let presentation = null;
        let changed = 0;
        while (changed < 50) {
            const elapsed = performance.now() - started;
            if (elapsed >= timeoutMs) {
                throw new Error(`password character was not presented before timeout (changed=${changed})`);
            }
            presentation = await awaitFrame(Math.max(1, timeoutMs - elapsed));
            observedFrames += 1;
            changed = changedPixels(baseline, captureField());
        }
        const completed = performance.now();
        // Restore the guest's password field without submitting it.  This is
        // deliberately sent after the result has been observed, therefore it
        // cannot shorten the measured input-to-photon interval.
        window.qsmControl.send(JSON.stringify({ op: 'keyboard', key: 14, down: true, modifiers: 0 }));
        window.qsmControl.send(JSON.stringify({ op: 'keyboard', key: 14, down: false, modifiers: 0 }));
        const cleanupStarted = performance.now();
        let cleanupFrames = 0;
        while (changedPixels(baseline, captureField()) >= 50) {
            const elapsed = performance.now() - cleanupStarted;
            if (elapsed >= timeoutMs) {
                throw new Error('password character did not clear after backspace');
            }
            await awaitFrame(Math.max(1, timeoutMs - elapsed));
            cleanupFrames += 1;
        }
        return {
            latencyMs: completed - started,
            videoArrivalLatencyMs: presentation && Number.isFinite(presentation.receiveEpochMs)
                ? presentation.receiveEpochMs - startedEpochMs : null,
            observedFrames,
            changedPixels: changed,
            cleanupFrames,
            startedEpochMs,
            completedEpochMs: Date.now(),
        };
    }, message);
}

async function measureDrag(message) {
    if (!page || !message || typeof message !== 'object') {
        throw new Error('invalid drag measurement command');
    }
    return page.evaluate(async (payload) => {
        const integer = (name, minimum, maximum) => {
            const value = payload[name];
            if (!Number.isInteger(value) || value < minimum || value > maximum) {
                throw new Error(`invalid drag measurement ${name}`);
            }
            return value;
        };
        const width = integer('width', 64, 16384);
        const height = integer('height', 64, 16384);
        const startX = integer('startX', 0, width - 1);
        const startY = integer('startY', 0, height - 1);
        const targetX = integer('targetX', 0, width - 1);
        const targetY = integer('targetY', 0, height - 1);
        const scanY = integer('scanY', 0, height - 1);
        const samples = integer('samples', 8, 240);
        const sampleIntervalMs = integer('sampleIntervalMs', 4, 50);
        const timeoutMs = integer('timeoutMs', 500, 15000);
        const video = document.getElementById('remote');
        if (!video || video.videoWidth !== width || video.videoHeight !== height ||
            !window.qsmControl || window.qsmControl.readyState !== 'open' ||
            !window.qsmPointer || window.qsmPointer.readyState !== 'open') {
            throw new Error('browser drag peer is not ready');
        }
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = 1;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) throw new Error('drag measurement canvas is unavailable');
        const cardCenter = () => {
            // Sampling only the card's horizontal centre row makes this an
            // actual decoded-pixel test without the full-frame canvas cost
            // itself becoming the source of an apparent drag stutter.
            context.drawImage(video, 0, scanY, width, 1, 0, 0, width, 1);
            const pixels = context.getImageData(0, 0, width, 1).data;
            let first = -1;
            let last = -1;
            for (let x = 0; x < width; x += 1) {
                const offset = x * 4;
                // #ff9f00, with room for H.264's chroma conversion.
                if (pixels[offset] > 170 && pixels[offset + 1] > 80 &&
                    pixels[offset + 1] < 235 && pixels[offset + 2] < 105) {
                    if (first < 0) first = x;
                    last = x;
                }
            }
            return first < 0 ? null : (first + last) / 2;
        };
        const awaitFrame = (remainingMs) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('video did not present a drag frame')), remainingMs);
            const callback = (now, metadata) => {
                clearTimeout(timer);
                resolve({ now, presentedFrames: Number(metadata?.presentedFrames) });
            };
            if (typeof video.requestVideoFrameCallback === 'function') video.requestVideoFrameCallback(callback);
            else requestAnimationFrame((now) => callback(now, {}));
        });
        const sendPointer = (x, y, sequence) => window.qsmPointer.send(JSON.stringify({
            op: 'mouse_position', x, y, width, height, sequence,
        }));
        // The card is an ordinary guest Chromium PointerEvent target. A
        // visible initial location guards against measuring a stale fixture
        // or a failed tablet mapping as a networking problem.
        const initial = cardCenter();
        if (initial === null || Math.abs(initial - startX) > 80) {
            throw new Error('guest drag fixture is not at its initial position');
        }
        let sequence = 1;
        sendPointer(startX, startY, sequence++);
        window.qsmControl.send(JSON.stringify({ op: 'mouse_position', x: startX, y: startY, width, height, sequence: sequence - 1 }));
        window.qsmControl.send(JSON.stringify({ op: 'mouse_button', button: 1, down: true }));
        const started = performance.now();
        const observations = [];
        let observing = true;
        const observe = async () => {
            while (observing) {
                const frame = await awaitFrame(timeoutMs);
                const center = cardCenter();
                if (center !== null) observations.push({ at: performance.now(), center, presentedFrames: frame.presentedFrames });
            }
        };
        const observer = observe();
        for (let index = 1; index <= samples; index += 1) {
            const fraction = index / samples;
            sendPointer(Math.round(startX + (targetX - startX) * fraction),
                Math.round(startY + (targetY - startY) * fraction), sequence++);
            await new Promise((resolve) => setTimeout(resolve, sampleIntervalMs));
        }
        // As in the product popup, repeat the final latest-state sample on
        // the ordered lane before releasing the button. This makes the drag
        // endpoint independent of cross-channel SCTP scheduling.
        const finalSequence = sequence - 1;
        window.qsmControl.send(JSON.stringify({ op: 'mouse_position', x: targetX, y: targetY,
            width, height, sequence: finalSequence }));
        window.qsmControl.send(JSON.stringify({ op: 'mouse_button', button: 1, down: false }));
        const sentFinalAt = performance.now();
        const deadline = sentFinalAt + timeoutMs;
        let finalCenter = cardCenter();
        while ((finalCenter === null || Math.abs(finalCenter - targetX) > 18) && performance.now() < deadline) {
            await awaitFrame(Math.max(1, deadline - performance.now()));
            finalCenter = cardCenter();
        }
        observing = false;
        await Promise.race([observer, new Promise((resolve) => setTimeout(resolve, 100))]);
        if (finalCenter === null || Math.abs(finalCenter - targetX) > 18) {
            throw new Error('dragged guest window did not reach its endpoint');
        }
        const transitions = [];
        for (const item of observations) {
            const previous = transitions[transitions.length - 1];
            if (!previous || Math.abs(item.center - previous.center) >= 3) transitions.push(item);
        }
        const motion = transitions.filter((item) => Math.abs(item.center - initial) >= 8);
        if (motion.length < Math.max(4, Math.floor(samples / 5))) {
            throw new Error('guest window was not presented as a continuous drag');
        }
        let largestGapMs = 0;
        for (let index = 1; index < motion.length; index += 1) {
            largestGapMs = Math.max(largestGapMs, motion[index].at - motion[index - 1].at);
        }
        return {
            firstMotionLatencyMs: motion[0].at - started,
            finalSettleLatencyMs: performance.now() - sentFinalAt,
            observedMotionFrames: motion.length,
            largestMotionGapMs: largestGapMs,
            finalCenter,
            samples,
            sampleIntervalMs,
        };
    }, message);
}

async function control(message) {
    if (!page || !message || typeof message !== 'object') {
        throw new Error('invalid browser control command');
    }
    await page.evaluate((payload) => {
        if (!window.qsmControl || window.qsmControl.readyState !== 'open') {
            throw new Error('browser control channel is not open');
        }
        window.qsmControl.send(JSON.stringify(payload));
    }, message);
    return { sent: true };
}

async function pointer(message) {
    if (!page || !message || typeof message !== 'object') {
        throw new Error('invalid browser pointer command');
    }
    await page.evaluate((payload) => {
        if (!window.qsmPointer || window.qsmPointer.readyState !== 'open') {
            throw new Error('browser pointer channel is not open');
        }
        window.qsmPointer.send(JSON.stringify(payload));
    }, message);
    return { sent: true };
}

async function guest(message) {
    if (!page || !message || typeof message !== 'object' || typeof message.op !== 'string') {
        throw new Error('invalid browser guest command');
    }
    return page.evaluate(async (payload) => {
        if (!window.qsmControl || window.qsmControl.readyState !== 'open') {
            throw new Error('browser control channel is not open');
        }
        const requestId = `lab-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const response = await new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                window.qsmGuestRequests.delete(requestId);
                reject(new Error('guest operation timed out'));
            }, 10000);
            window.qsmGuestRequests.set(requestId, {
                resolve: (result) => { clearTimeout(timer); resolve(result); },
            });
            if (payload.op === 'qsm_guest_file_upload' &&
                typeof payload.name === 'string' && typeof payload.data_b64 === 'string') {
                const binary = atob(payload.data_b64);
                const bytes = new Uint8Array(binary.length);
                for (let index = 0; index < binary.length; index += 1) {
                    bytes[index] = binary.charCodeAt(index);
                }
                const transferId = `lab-upload-${Date.now()}`;
                const chunkBytes = 32 * 1024;
                const b64 = (chunk) => {
                    let text = '';
                    for (let index = 0; index < chunk.length; index += 0x8000) {
                        text += String.fromCharCode(...chunk.subarray(index, index + 0x8000));
                    }
                    return btoa(text);
                };
                for (let offset = 0; offset < Math.max(1, bytes.length); offset += chunkBytes) {
                    const end = Math.min(bytes.length, offset + chunkBytes);
                    window.qsmControl.send(JSON.stringify({
                        op: 'qsm_guest_file_upload_chunk', request_id: requestId, transfer_id: transferId,
                        name: payload.name, size: bytes.length, offset,
                        data_b64: b64(bytes.subarray(offset, end)),
                    }));
                }
            } else {
                window.qsmControl.send(JSON.stringify({ ...payload, request_id: requestId }));
            }
        });
        if (response.ok !== true || !response.result || typeof response.result !== 'object') {
            throw new Error('guest operation failed');
        }
        return response.result;
    }, message);
}

async function close() {
    if (page) {
        await page.evaluate(() => window.qsmPeerConnection?.close());
    }
    if (browser) {
        await browser.close();
    }
    if (pageDirectory) {
        await fs.rm(pageDirectory, { recursive: true, force: true });
    }
    page = undefined;
    browser = undefined;
    pageDirectory = undefined;
    nativeWindowSession = undefined;
    nativeWindowId = undefined;
}

async function terminate() {
    if (closing) {
        return;
    }
    closing = true;
    try {
        await close();
    } finally {
        input.close();
    }
}

const commands = {
    offer: async () => createOffer(),
    answer: async (message) => {
        await setAnswer(message.answer);
        return { accepted: true };
    },
    status: async () => status(),
    frame_stats: async () => frameStats(),
    webrtc_stats: async () => webrtcStats(),
    viewport: async (message) => setViewport(message),
    screenshot: async (message) => screenshot(message),
    measure_hover: async (message) => measureHover(message.message),
    measure_password_key: async (message) => measurePasswordKey(message.message),
    measure_password_field_key: async (message) => measurePasswordFieldKey(message.message),
    measure_drag: async (message) => measureDrag(message.message),
    control: async (message) => control(message.message),
    pointer: async (message) => pointer(message.message),
    guest: async (message) => guest(message.message),
    close: async () => {
        // Reply before closing stdout so the driver can distinguish a clean
        // shutdown from an abruptly lost browser peer.  A previous version
        // only closed Playwright; its JSON-lines process then remained alive
        // after an SSH driver had exited, accumulating test Chrome instances.
        setImmediate(() => terminate().catch(() => { process.exitCode = 1; }));
        return { closed: true };
    },
};

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('line', async (line) => {
    let message;
    try {
        message = JSON.parse(line);
        if (!message || typeof message.op !== 'string' || !commands[message.op]) {
            throw new Error('invalid test command');
        }
        reply({ ok: true, result: await commands[message.op](message) });
    } catch (error) {
        reply({ ok: false, error: String(error && error.message ? error.message : error) });
    }
});

// A cancelled driver closes its SSH stdin without sending `close`.  Release
// the browser in that case too; this is test harness hygiene, not product
// console behaviour.
input.on('close', () => {
    terminate().catch(() => { process.exitCode = 1; });
});

process.on('exit', () => {
    // Playwright cannot be awaited from here. The driver sends `close`; this
    // remains only a best-effort guard for an interrupted qualification run.
    if (browser) {
        browser.close().catch(() => {});
    }
});
