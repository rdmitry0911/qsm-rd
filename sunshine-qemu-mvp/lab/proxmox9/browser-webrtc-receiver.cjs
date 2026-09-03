#!/usr/bin/env node
'use strict';

/*
 * Tiny JSON-lines Playwright peer used only by the local browser E2E gate.
 * It has no network listener and no PVE credentials: the Python driver owns
 * offer/answer signalling in the same local process tree.
 */

const readline = require('node:readline');
const http = require('node:http');
const { chromium } = require('playwright');

let browser;
let page;
let pageServer;

async function startLocalPage() {
    pageServer = http.createServer((_request, response) => {
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        response.end('<!doctype html><video id="remote" autoplay muted playsinline></video>');
    });
    await new Promise((resolve, reject) => {
        pageServer.once('error', reject);
        pageServer.listen(0, '127.0.0.1', resolve);
    });
    const address = pageServer.address();
    if (!address || typeof address === 'string') {
        throw new Error('could not bind local browser E2E page');
    }
    return `http://127.0.0.1:${address.port}/`;
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
    browser = await chromium.launch({
        headless: true,
        // The Linux Chromium build bundled with Playwright intentionally
        // excludes H.264.  This qualification uses a normal Chrome/Edge
        // executable selected by the Python driver, because it is the actual
        // browser compatibility contract being tested.
        ...(executablePath ? { executablePath } : {}),
        args: [
            '--autoplay-policy=no-user-gesture-required',
            // Only the local test needs aiortc to see the loopback host
            // candidate instead of Chrome's privacy-preserving mDNS alias.
            '--disable-features=WebRtcHideLocalIpsWithMdns',
        ],
    });
    page = await browser.newPage();
    await page.goto(await startLocalPage());
    const offer = await page.evaluate(async () => {
        const video = document.getElementById('remote');
        const pc = new RTCPeerConnection({ iceServers: [] });
        window.qsmPeerConnection = pc;
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
        };
    });
}

async function close() {
    if (page) {
        await page.evaluate(() => window.qsmPeerConnection?.close());
    }
    if (browser) {
        await browser.close();
    }
    if (pageServer) {
        await new Promise((resolve) => pageServer.close(resolve));
    }
    page = undefined;
    browser = undefined;
    pageServer = undefined;
}

const commands = {
    offer: async () => createOffer(),
    answer: async (message) => {
        await setAnswer(message.answer);
        return { accepted: true };
    },
    status: async () => status(),
    close: async () => {
        await close();
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

process.on('exit', () => {
    // Playwright cannot be awaited from here. The driver sends `close`; this
    // remains only a best-effort guard for an interrupted qualification run.
    if (browser) {
        browser.close().catch(() => {});
    }
});
