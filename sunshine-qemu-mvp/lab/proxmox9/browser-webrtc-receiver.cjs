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

async function startLocalPage() {
    // A file origin is potentially trustworthy and does not require Chrome's
    // HTTP network service to traverse loopback, which is unavailable in
    // some unprivileged LXC profiles. WebRTC still uses its actual local
    // UDP/DTLS path, so this remains a browser media qualification.
    pageDirectory = await fs.mkdtemp(path.join(os.tmpdir(), 'qsm-webrtc-page-'));
    const pagePath = path.join(pageDirectory, 'index.html');
    await fs.writeFile(pagePath, '<!doctype html><video id="remote" autoplay muted playsinline></video>');
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
    page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await page.goto(await startLocalPage());
    const offer = await page.evaluate(async () => {
        const video = document.getElementById('remote');
        const pc = new RTCPeerConnection({ iceServers: [] });
        window.qsmPeerConnection = pc;
        window.qsmControl = pc.createDataChannel('qsm-control', { ordered: true });
        window.qsmPointer = pc.createDataChannel('qsm-pointer', { ordered: false, maxRetransmits: 0 });
        window.qsmTrackKinds = [];
        window.qsmIceCandidateSeen = false;
        pc.addEventListener('track', (event) => {
            window.qsmTrackKinds.push(event.track.kind);
            if (event.track.kind === 'video') {
                video.srcObject = event.streams[0];
                video.play().catch(() => {});
            }
        });
        pc.addTransceiver('video', { direction: 'recvonly' });
        pc.addTransceiver('audio', { direction: 'recvonly' });
        const localOffer = await pc.createOffer();
        await pc.setLocalDescription(localOffer);
        return { type: pc.localDescription.type, sdp: pc.localDescription.sdp };
    });
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
    return page.evaluate(() => {
        const video = document.getElementById('remote');
        return {
            connectionState: window.qsmPeerConnection.connectionState,
            iceConnectionState: window.qsmPeerConnection.iceConnectionState,
            trackKinds: window.qsmTrackKinds.slice().sort(),
            readyState: video.readyState,
            videoWidth: video.videoWidth,
            videoHeight: video.videoHeight,
            currentTime: video.currentTime,
            controlReady: window.qsmControl?.readyState === 'open',
            pointerReady: window.qsmPointer?.readyState === 'open',
        };
    });
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
        return {
            samples,
            lumaMin,
            lumaMax,
            lumaMean: lumaSum / samples,
            nonBlack,
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
            totalDecodeTime: number(video.totalDecodeTime),
            jitterBufferDelay: delay,
            jitterBufferEmittedCount: emitted,
            jitterBufferMeanDelayMs: delay !== null && emitted !== null && emitted > 0
                ? (delay * 1000) / emitted : null,
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
        const sendPosition = (x, y) => window.qsmPointer.send(JSON.stringify({
            op: 'mouse_position', x, y, width, height,
        }));
        const awaitFrame = (timeoutMs) => new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('video did not present a new frame')), timeoutMs);
            const complete = () => {
                clearTimeout(timer);
                resolve();
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
        while (popupBounds()) {
            if (performance.now() >= resetDeadline) {
                throw new Error('hover popup did not clear');
            }
            await awaitFrame(Math.max(1, resetDeadline - performance.now()));
        }

        const started = performance.now();
        sendPosition(targetX, targetY);
        let observedFrames = 0;
        let bounds = popupBounds();
        while (!bounds) {
            if (performance.now() - started >= timeoutMs) {
                throw new Error('hover popup was not presented before timeout');
            }
            await awaitFrame(Math.max(1, timeoutMs - (performance.now() - started)));
            observedFrames += 1;
            bounds = popupBounds();
        }
        return {
            latencyMs: performance.now() - started,
            observedFrames,
            popupBounds: bounds,
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
    measure_hover: async (message) => measureHover(message.message),
    control: async (message) => control(message.message),
    pointer: async (message) => pointer(message.message),
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
