#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Real-browser qualification for the PVE Console -> q-sunshine handoff.
 *
 * This is intentionally a lab tool, rather than a mocked unit test.  It uses
 * Playwright Chromium against the supplied PVE URL, submits the visible PVE
 * login form, selects an already-present QEMU VM from PVE's live resource
 * tree, opens the visible Console split menu, and clicks the q-sunshine menu
 * entry.  It never installs a page.route handler, sends a direct PVE API
 * request, prints request bodies, or writes a PVE password/ticket/cookie to
 * stdout/stderr. The menu must use the same-origin protected PVE route; it
 * must not recreate a VNC-ticket or browser-to-terminal authorization path.
 */
'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { TextDecoder } = require('node:util');

const MAX_PASSWORD_BYTES = 4096;
const MAX_DESCRIPTOR_BYTES = 96 * 1024;
const MAX_CA_PEM_BYTES = 64 * 1024;
const MAX_DESCRIPTOR_LIFETIME_MS = 5 * 60 * 1000 + 30 * 1000;
const DEFAULT_TIMEOUT_MS = 30_000;
const MIN_TIMEOUT_MS = 5_000;
const MAX_TIMEOUT_MS = 120_000;
const MIN_VMID = 100;
const MAX_VMID = 999_999_999;
const CLAIM_PATTERN = /^qsd1\.[A-Za-z0-9_-]{43}$/;
const NODE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/;
const REALM_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const USER_PART_PATTERN = /^[^\s@/:\\\x00-\x1f]{1,64}$/;
const DNS_HOST_PATTERN = /^(?=.{1,253}$)(?!.*\.\.)[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$/;

class QualificationError extends Error {
    constructor(code) {
        super(code);
        this.code = code;
    }
}

const fail = (code) => {
    throw new QualificationError(code);
};

let currentPhase = 'INITIALIZING';

const status = (message) => {
    // Fixed status messages only: arguments and browser/network payloads can
    // contain confidential PVE authentication material.
    process.stdout.write(`q-sunshine PVE browser qualification: ${message}\n`);
};

const beginPhase = (name, message) => {
    currentPhase = name;
    status(message);
};

const usage = () => {
    process.stdout.write(`Usage:\n\n` +
        `  node lab/proxmox9/qualify-pve-console-launch.cjs \\\n` +
        `    --pve-url https://pve.example:8006 \\\n` +
        `    --user qsmconsole@pve \\\n` +
        `    --password-file /secure/path/pve.password \\\n` +
        `    --vmid 100 \\\n` +
        `    --download-dir /secure/empty/download-directory \\\n` +
        `    [--ignore-https-errors] [--headed] [--timeout-ms 30000]\n\n` +
        `The password is read only from the file; do not pass it through argv or environment.\n`);
};

const parseInteger = (value, lower, upper) => {
    if (!/^[1-9][0-9]*$/.test(value)) {
        return undefined;
    }
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) && parsed >= lower && parsed <= upper ? parsed : undefined;
};

const parsePveUrl = (value) => {
    let parsed;
    try {
        parsed = new URL(value);
    } catch (_error) {
        fail('INVALID_PVE_URL');
    }
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password ||
        parsed.search || parsed.hash || (parsed.pathname !== '/' && parsed.pathname !== '')) {
        fail('INVALID_PVE_URL');
    }
    return parsed.origin;
};

const parseUser = (value) => {
    const separator = value.lastIndexOf('@');
    if (separator <= 0 || separator !== value.indexOf('@')) {
        fail('INVALID_PVE_USER');
    }
    const username = value.slice(0, separator);
    const realm = value.slice(separator + 1);
    if (!USER_PART_PATTERN.test(username) || !REALM_PATTERN.test(realm)) {
        fail('INVALID_PVE_USER');
    }
    return { full: value, username, realm };
};

const parseArguments = (arguments_) => {
    const values = Object.create(null);
    const flags = new Set();
    const takesValue = new Set(['--pve-url', '--user', '--password-file', '--vmid', '--download-dir', '--timeout-ms']);
    const takesFlag = new Set(['--ignore-https-errors', '--headed']);

    for (let index = 0; index < arguments_.length; index += 1) {
        const argument = arguments_[index];
        if (argument === '--help' || argument === '-h') {
            usage();
            process.exit(0);
        }
        if (takesFlag.has(argument)) {
            if (flags.has(argument)) {
                fail('INVALID_ARGUMENTS');
            }
            flags.add(argument);
            continue;
        }
        if (!takesValue.has(argument) || index + 1 >= arguments_.length ||
            Object.hasOwn(values, argument)) {
            fail('INVALID_ARGUMENTS');
        }
        const value = arguments_[index + 1];
        if (!value || value.startsWith('--')) {
            fail('INVALID_ARGUMENTS');
        }
        values[argument] = value;
        index += 1;
    }

    for (const required of ['--pve-url', '--user', '--password-file', '--vmid', '--download-dir']) {
        if (!Object.hasOwn(values, required)) {
            fail('INVALID_ARGUMENTS');
        }
    }
    const vmid = parseInteger(values['--vmid'], MIN_VMID, MAX_VMID);
    const timeout = Object.hasOwn(values, '--timeout-ms')
        ? parseInteger(values['--timeout-ms'], MIN_TIMEOUT_MS, MAX_TIMEOUT_MS)
        : DEFAULT_TIMEOUT_MS;
    if (vmid === undefined || timeout === undefined) {
        fail('INVALID_ARGUMENTS');
    }
    return {
        pveOrigin: parsePveUrl(values['--pve-url']),
        user: parseUser(values['--user']),
        passwordFile: path.resolve(values['--password-file']),
        vmid,
        downloadDirectory: path.resolve(values['--download-dir']),
        timeout,
        ignoreHttpsErrors: flags.has('--ignore-https-errors'),
        headed: flags.has('--headed'),
    };
};

const safeOpenReadOnly = (file, unavailableCode) => {
    const noFollow = fs.constants.O_NOFOLLOW || 0;
    try {
        return fs.openSync(file, fs.constants.O_RDONLY | noFollow);
    } catch (_error) {
        fail(unavailableCode);
    }
};

const readPassword = (file) => {
    const descriptor = safeOpenReadOnly(file, 'PASSWORD_FILE_UNAVAILABLE');
    try {
        const metadata = fs.fstatSync(descriptor);
        if (!metadata.isFile() || metadata.size <= 0 || metadata.size > MAX_PASSWORD_BYTES ||
            (metadata.mode & 0o077) !== 0) {
            fail('UNSAFE_PASSWORD_FILE');
        }
        const source = Buffer.alloc(metadata.size);
        let offset = 0;
        while (offset < source.length) {
            const count = fs.readSync(descriptor, source, offset, source.length - offset, null);
            if (count <= 0) {
                fail('PASSWORD_FILE_UNAVAILABLE');
            }
            offset += count;
        }
        let end = source.length;
        if (source[end - 1] === 0x0a) {
            end -= 1;
            if (end > 0 && source[end - 1] === 0x0d) {
                end -= 1;
            }
        }
        if (end <= 0) {
            source.fill(0);
            fail('PASSWORD_FILE_UNAVAILABLE');
        }
        const password = Buffer.from(source.subarray(0, end));
        source.fill(0);
        try {
            const decoded = new TextDecoder('utf-8', { fatal: true }).decode(password);
            if (decoded.includes('\u0000') || decoded.includes('\n') || decoded.includes('\r')) {
                password.fill(0);
                fail('PASSWORD_FILE_UNAVAILABLE');
            }
        } catch (error) {
            password.fill(0);
            if (error instanceof QualificationError) {
                throw error;
            }
            fail('PASSWORD_FILE_UNAVAILABLE');
        }
        return password;
    } finally {
        fs.closeSync(descriptor);
    }
};

const assertSecureDownloadDirectory = (directory) => {
    let metadata;
    try {
        metadata = fs.lstatSync(directory);
    } catch (_error) {
        fail('DOWNLOAD_DIRECTORY_UNAVAILABLE');
    }
    if (!metadata.isDirectory() || metadata.isSymbolicLink() ||
        (metadata.mode & 0o022) !== 0 ||
        (typeof process.getuid === 'function' && metadata.uid !== process.getuid())) {
        fail('UNSAFE_DOWNLOAD_DIRECTORY');
    }
};

const cssId = (value) => {
    if (typeof value !== 'string' || !/^[A-Za-z_][A-Za-z0-9_-]{0,127}$/.test(value)) {
        fail('CONSOLE_BUTTON_UNAVAILABLE');
    }
    return `#${value}`;
};

const hasExactKeys = (value, expected) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        return false;
    }
    const actual = Object.keys(value).sort();
    const sortedExpected = [...expected].sort();
    return actual.length === sortedExpected.length && actual.every((key, index) => key === sortedExpected[index]);
};

/*
 * JSON.parse accepts duplicate object keys.  A descriptor is a security
 * boundary, so use a tiny standards-compliant parser that rejects duplicates
 * at every nesting level before its shape is checked.  It deliberately emits
 * only fixed qualification errors and never returns raw parser diagnostics.
 */
class StrictJsonParser {
    constructor(source) {
        this.source = source;
        this.offset = 0;
    }

    parse() {
        this.skipWhitespace();
        const value = this.parseValue();
        this.skipWhitespace();
        if (this.offset !== this.source.length) {
            fail('INVALID_LAUNCH_DESCRIPTOR');
        }
        return value;
    }

    skipWhitespace() {
        while (this.offset < this.source.length && /[\u0020\u0009\u000a\u000d]/.test(this.source[this.offset])) {
            this.offset += 1;
        }
    }

    consume(value) {
        if (!this.source.startsWith(value, this.offset)) {
            fail('INVALID_LAUNCH_DESCRIPTOR');
        }
        this.offset += value.length;
    }

    parseValue() {
        if (this.offset >= this.source.length) {
            fail('INVALID_LAUNCH_DESCRIPTOR');
        }
        const character = this.source[this.offset];
        if (character === '{') {
            return this.parseObject();
        }
        if (character === '[') {
            return this.parseArray();
        }
        if (character === '"') {
            return this.parseString();
        }
        if (character === 't') {
            this.consume('true');
            return true;
        }
        if (character === 'f') {
            this.consume('false');
            return false;
        }
        if (character === 'n') {
            this.consume('null');
            return null;
        }
        if (character === '-' || /[0-9]/.test(character)) {
            return this.parseNumber();
        }
        fail('INVALID_LAUNCH_DESCRIPTOR');
    }

    parseObject() {
        this.consume('{');
        this.skipWhitespace();
        const result = Object.create(null);
        const keys = new Set();
        if (this.source[this.offset] === '}') {
            this.offset += 1;
            return result;
        }
        while (true) {
            this.skipWhitespace();
            if (this.source[this.offset] !== '"') {
                fail('INVALID_LAUNCH_DESCRIPTOR');
            }
            const key = this.parseString();
            if (keys.has(key)) {
                fail('INVALID_LAUNCH_DESCRIPTOR');
            }
            keys.add(key);
            this.skipWhitespace();
            this.consume(':');
            this.skipWhitespace();
            result[key] = this.parseValue();
            this.skipWhitespace();
            if (this.source[this.offset] === '}') {
                this.offset += 1;
                return result;
            }
            this.consume(',');
        }
    }

    parseArray() {
        this.consume('[');
        this.skipWhitespace();
        const result = [];
        if (this.source[this.offset] === ']') {
            this.offset += 1;
            return result;
        }
        while (true) {
            this.skipWhitespace();
            result.push(this.parseValue());
            this.skipWhitespace();
            if (this.source[this.offset] === ']') {
                this.offset += 1;
                return result;
            }
            this.consume(',');
        }
    }

    parseString() {
        const start = this.offset;
        this.consume('"');
        while (this.offset < this.source.length) {
            const character = this.source[this.offset];
            if (character === '"') {
                this.offset += 1;
                try {
                    return JSON.parse(this.source.slice(start, this.offset));
                } catch (_error) {
                    fail('INVALID_LAUNCH_DESCRIPTOR');
                }
            }
            if (character === '\\') {
                this.offset += 1;
                if (this.offset >= this.source.length) {
                    fail('INVALID_LAUNCH_DESCRIPTOR');
                }
                const escaped = this.source[this.offset];
                if ('"\\/bfnrt'.includes(escaped)) {
                    this.offset += 1;
                    continue;
                }
                if (escaped === 'u') {
                    const digits = this.source.slice(this.offset + 1, this.offset + 5);
                    if (!/^[0-9A-Fa-f]{4}$/.test(digits)) {
                        fail('INVALID_LAUNCH_DESCRIPTOR');
                    }
                    this.offset += 5;
                    continue;
                }
                fail('INVALID_LAUNCH_DESCRIPTOR');
            }
            if (character < '\u0020') {
                fail('INVALID_LAUNCH_DESCRIPTOR');
            }
            this.offset += 1;
        }
        fail('INVALID_LAUNCH_DESCRIPTOR');
    }

    parseNumber() {
        const remaining = this.source.slice(this.offset);
        const match = remaining.match(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/);
        if (!match) {
            fail('INVALID_LAUNCH_DESCRIPTOR');
        }
        this.offset += match[0].length;
        const value = Number(match[0]);
        if (!Number.isFinite(value)) {
            fail('INVALID_LAUNCH_DESCRIPTOR');
        }
        return value;
    }
}

const isValidRouteHost = (value) => {
    if (typeof value !== 'string' || value.length === 0 || value.length > 253 || value.trim() !== value ||
        /[\s\u0000-\u001f]/.test(value) || /[/?#@\[\]]/.test(value)) {
        return false;
    }
    if (net.isIP(value) !== 0) {
        return true;
    }
    if (value.includes(':')) {
        // A bare DNS name may never contain a colon.  IPv6 literals were
        // accepted above and are stored without URL brackets.
        return false;
    }
    return DNS_HOST_PATTERN.test(value);
};

const stringLeaves = (value, result = []) => {
    if (typeof value === 'string') {
        result.push(value);
    } else if (Array.isArray(value)) {
        for (const child of value) {
            stringLeaves(child, result);
        }
    } else if (value && typeof value === 'object') {
        for (const child of Object.values(value)) {
            stringLeaves(child, result);
        }
    }
    return result;
};

const containsSecretBytes = (values, secret) => {
    if (!Buffer.isBuffer(secret) || secret.length === 0) {
        return false;
    }
    return values.some((value) => Buffer.from(value, 'utf8').indexOf(secret) >= 0);
};

const validatePem = (pem) => {
    if (typeof pem !== 'string' || pem.length === 0 || Buffer.byteLength(pem, 'utf8') > MAX_CA_PEM_BYTES ||
        pem.includes('PRIVATE KEY')) {
        return false;
    }
    const certificates = pem.match(/-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g);
    if (!certificates || certificates.length === 0) {
        return false;
    }
    if (pem.replace(/-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g, '').trim() !== '') {
        return false;
    }
    try {
        certificates.forEach((certificate) => new crypto.X509Certificate(certificate));
        return true;
    } catch (_error) {
        return false;
    }
};

const validateDescriptor = (source, sensitiveValues) => {
    if (Buffer.byteLength(source, 'utf8') === 0 || Buffer.byteLength(source, 'utf8') > MAX_DESCRIPTOR_BYTES) {
        fail('INVALID_LAUNCH_DESCRIPTOR');
    }
    const descriptor = new StrictJsonParser(source).parse();
    if (!hasExactKeys(descriptor, ['version', 'kind', 'endpoint', 'claim', 'expires_at_utc_ms']) ||
        descriptor.version !== 1 || descriptor.kind !== 'q-sunshine-pve-launch' ||
        !hasExactKeys(descriptor.endpoint, ['host', 'port', 'server_name', 'ca_pem']) ||
        !Number.isSafeInteger(descriptor.endpoint.port) || descriptor.endpoint.port < 1 || descriptor.endpoint.port > 65535 ||
        !Number.isSafeInteger(descriptor.expires_at_utc_ms) ||
        descriptor.expires_at_utc_ms <= Date.now() ||
        descriptor.expires_at_utc_ms > Date.now() + MAX_DESCRIPTOR_LIFETIME_MS ||
        !isValidRouteHost(descriptor.endpoint.host) || !isValidRouteHost(descriptor.endpoint.server_name) ||
        typeof descriptor.claim !== 'string' || !CLAIM_PATTERN.test(descriptor.claim) ||
        !validatePem(descriptor.endpoint.ca_pem)) {
        fail('INVALID_LAUNCH_DESCRIPTOR');
    }
    const values = stringLeaves(descriptor);
    if (containsSecretBytes(values, sensitiveValues.password) ||
        sensitiveValues.cookies.some((cookie) => containsSecretBytes(values, cookie)) ||
        values.some((value) => /PVEVNC|PVEAuthCookie|CSRFPreventionToken/i.test(value))) {
        fail('SENSITIVE_MATERIAL_IN_DESCRIPTOR');
    }
    return descriptor;
};

const readDownloadedDescriptor = (file) => {
    const descriptor = safeOpenReadOnly(file, 'INVALID_LAUNCH_DESCRIPTOR');
    try {
        const metadata = fs.fstatSync(descriptor);
        if (!metadata.isFile() || metadata.size <= 0 || metadata.size > MAX_DESCRIPTOR_BYTES ||
            (metadata.mode & 0o022) !== 0) {
            fail('INVALID_LAUNCH_DESCRIPTOR');
        }
        const content = Buffer.alloc(metadata.size);
        let offset = 0;
        while (offset < content.length) {
            const count = fs.readSync(descriptor, content, offset, content.length - offset, null);
            if (count <= 0) {
                content.fill(0);
                fail('INVALID_LAUNCH_DESCRIPTOR');
            }
            offset += count;
        }
        return content;
    } finally {
        fs.closeSync(descriptor);
    }
};

const waitForLiveRealm = async (page, realm, timeout) => {
    const handle = await page.waitForFunction((wantedRealm) => {
        const combo = window.Ext && window.Ext.getCmp && window.Ext.getCmp('pveloginrealm');
        const store = combo && combo.getStore && combo.getStore();
        const record = store && store.findRecord && store.findRecord('realm', wantedRealm, 0, false, true, true);
        return record ? String(record.get('descr') || '') : null;
    }, realm, { timeout });
    try {
        const description = await handle.jsonValue();
        if (typeof description !== 'string' || description.length === 0 || description.length > 512) {
            fail('PVE_REALM_UNAVAILABLE');
        }
        return description;
    } finally {
        await handle.dispose();
    }
};

const selectRealmThroughVisibleControl = async (page, realm, timeout) => {
    const description = await waitForLiveRealm(page, realm, timeout);
    const realmInput = page.locator('#pveloginrealm-inputEl:visible');
    await realmInput.waitFor({ state: 'visible', timeout });
    await realmInput.click();
    const realmOption = page.locator('.x-boundlist:visible .x-boundlist-item').filter({ hasText: description });
    await realmOption.first().waitFor({ state: 'visible', timeout });
    await realmOption.first().click();
    await page.waitForFunction((wantedRealm) => {
        const combo = window.Ext && window.Ext.getCmp && window.Ext.getCmp('pveloginrealm');
        return !!combo && combo.getValue && combo.getValue() === wantedRealm;
    }, realm, { timeout });
};

const selectLiveQemuVm = async (page, vmid, timeout) => {
    const handle = await page.waitForFunction((wantedVmid) => {
        const trees = window.Ext && window.Ext.ComponentQuery && window.Ext.ComponentQuery.query('pveResourceTree');
        const tree = trees && trees[0];
        const root = tree && tree.getStore && tree.getStore().getRootNode();
        const record = root && root.findChild && root.findChild('id', `qemu/${wantedVmid}`, true);
        if (!record) {
            return null;
        }
        if (record.data.type !== 'qemu' || record.data.template || !record.data.node ||
            Number(record.data.vmid) !== wantedVmid) {
            return { invalid: true };
        }
        // This uses the already rendered, live PVE resource tree and triggers
        // its normal selection handler.  It makes no direct HTTP/API call and
        // cannot fabricate a VM that PVE did not expose to the signed-in user.
        tree.selectById(record.data.id);
        return { node: String(record.data.node) };
    }, vmid, { timeout });
    try {
        const selection = await handle.jsonValue();
        if (!selection || selection.invalid || typeof selection.node !== 'string' || !NODE_PATTERN.test(selection.node)) {
            fail('QEMU_VM_UNAVAILABLE');
        }
        return selection.node;
    } finally {
        await handle.dispose();
    }
};

const findLiveConsoleButton = async (page, vmid, timeout) => {
    const handle = await page.waitForFunction((wantedVmid) => {
        const buttons = window.Ext && window.Ext.ComponentQuery && window.Ext.ComponentQuery.query('pveConsoleButton');
        if (!buttons) {
            return null;
        }
        const button = buttons.find((candidate) => candidate && candidate.consoleType === 'kvm' &&
            Number(candidate.vmid) === wantedVmid && candidate.rendered && !candidate.hidden && !candidate.disabled);
        return button && button.getEl && button.getEl().dom ? String(button.getEl().dom.id || '') : null;
    }, vmid, { timeout });
    try {
        const id = await handle.jsonValue();
        return cssId(id);
    } finally {
        await handle.dispose();
    }
};

const waitForConsoleArrowToBeClickable = async (page, consoleSelector, timeout) => {
    // Selecting a guest creates its PVE config panel asynchronously.  The
    // panel's real ExtJS loading mask is intentionally allowed to finish; a
    // forced click would qualify a click that a human browser could not make.
    await page.waitForFunction((selector) => {
        const root = document.querySelector(selector);
        const arrow = root && root.querySelector('.x-btn-arrow-el');
        if (!arrow) {
            return false;
        }
        const rectangle = arrow.getBoundingClientRect();
        if (rectangle.width <= 0 || rectangle.height <= 0) {
            return false;
        }
        const hit = document.elementFromPoint(
            rectangle.left + rectangle.width / 2,
            rectangle.top + rectangle.height / 2,
        );
        // In current ExtJS the split-arrow element is visually on top, but
        // the button wrap is the browser hit target.  Accept either live
        // element from this one Console button, while rejecting a load mask or
        // modal overlay that covers it.
        return !!hit && root.contains(hit);
    }, consoleSelector, { timeout });
};

const openConsoleSplitMenu = async (page, consoleButton, timeout) => {
    // ExtJS 6/7 dispatches SplitButton's arrow action through the button-wrap
    // hit target rather than the visually separate arrow span. Click its real
    // right-side hit region; do not use force:true or call an ExtJS handler.
    const box = await consoleButton.boundingBox();
    if (!box || box.width < 16 || box.height < 8) {
        fail('CONSOLE_BUTTON_UNAVAILABLE');
    }
    await page.mouse.click(box.x + box.width - 6, box.y + box.height / 2);
    await page.locator('.x-menu:visible').waitFor({ state: 'visible', timeout });
};

const waitForDownload = async (page, menuItem, timeout) => {
    const downloadPromise = page.waitForEvent('download', { timeout });
    await menuItem.click();
    return downloadPromise;
};

const validSuggestedFilename = (name, vmid) => {
    const pattern = new RegExp(`^q-sunshine-[A-Za-z0-9][A-Za-z0-9.-]{0,62}-${vmid}\\.qsm$`);
    return pattern.test(name);
};

const expectedQsunshineRequest = (request, expected, observed) => {
    try {
        const requestUrl = new URL(request.url());
        if (request.method() !== 'POST') {
            return false;
        }
        // Proxmox.Utils.API2Request wraps the API-relative overlay URL in
        // PVE's ExtJS envelope. Do not accept a raw `/api2/json` request:
        // its JSON shape bypasses the normal API2Request success contract.
        const qsunshinePath = `/api2/extjs/nodes/${encodeURIComponent(expected.node)}/qemu/${expected.vmid}/q-sunshine`;
        if (requestUrl.origin === expected.pveOrigin && requestUrl.pathname === qsunshinePath) {
            return true;
        }
        if (requestUrl.pathname.endsWith('/vncproxy') || requestUrl.pathname === '/pve/v1/launch') {
            observed.legacyRoute = true;
        }
    } catch (_error) {
        observed.invalidRequest = true;
    }
    return false;
};

const responseStatusClass = (statusCode) => {
    if (!Number.isInteger(statusCode) || statusCode < 100 || statusCode > 599) {
        return 'other';
    }
    return `${Math.floor(statusCode / 100)}xx`;
};

const browserHandoffDiagnostic = (phase, observed) => {
    // Keep this suitable for a lab log: these are fixed labels plus counters
    // and HTTP *classes* only. In particular, do not add URL, header, body,
    // cookie, exception, ticket, claim, or descriptor values here.
    const statusClasses = [...observed.qsunshineResponseStatusClasses].sort().join('+') || 'none';
    return `QSM_BROWSER_HANDOFF_DIAGNOSTIC phase=${phase} expected_post_count=${observed.qsunshine} ` +
        `expected_response_count=${observed.qsunshineResponses} ` +
        `expected_response_status_class=${statusClasses} ` +
        `expected_requestfailed_count=${observed.qsunshineRequestFailures}`;
};

const observeLiveTraffic = (page, expected) => {
    const observed = {
        qsunshine: 0,
        qsunshineResponses: 0,
        qsunshineResponseStatusClasses: new Set(),
        qsunshineRequestFailures: 0,
        invalidRequest: false,
        legacyRoute: false,
    };
    page.on('request', (request) => {
        if (expectedQsunshineRequest(request, expected, observed)) {
            observed.qsunshine += 1;
        }
    });
    page.on('response', (response) => {
        try {
            const request = response.request();
            if (expectedQsunshineRequest(request, expected, observed)) {
                observed.qsunshineResponses += 1;
                observed.qsunshineResponseStatusClasses.add(responseStatusClass(response.status()));
            }
        } catch (_error) {
            // Do not render a browser exception: it can include browser state.
            observed.invalidRequest = true;
        }
    });
    page.on('requestfailed', (request) => {
        if (expectedQsunshineRequest(request, expected, observed)) {
            observed.qsunshineRequestFailures += 1;
        }
    });
    return observed;
};

const waitForAuthenticatedPveUi = async (page, expectedUser, timeout) => {
    await page.waitForFunction((user) => {
        return !!window.Proxmox && window.Proxmox.UserName === user && !!window.PVE;
    }, expectedUser, { timeout });
};

const dismissKnownSubscriptionNotice = async (page, timeout) => {
    // A fresh PVE no-subscription lab commonly presents this standard advisory
    // after login.  Close only this exact English UI notice (the context fixes
    // the language to en-US); never blindly dismiss another modal, because it
    // could be an authentication or destructive-action prompt.
    const deadline = Date.now() + Math.min(timeout, 5_000);
    while (Date.now() < deadline) {
        const notices = page.locator('.x-message-box:visible');
        const count = await notices.count();
        if (count > 1) {
            fail('UNEXPECTED_PVE_MODAL');
        }
        if (count === 1) {
            const notice = notices.first();
            const text = (await notice.innerText()).trim();
            if (!text.startsWith('No valid subscription\n') || !text.endsWith('\nOK')) {
                fail('UNEXPECTED_PVE_MODAL');
            }
            const okay = notice.locator('.x-btn').filter({ hasText: /^OK$/ });
            if (await okay.count() !== 1) {
                fail('UNEXPECTED_PVE_MODAL');
            }
            await okay.click();
            await notice.waitFor({ state: 'hidden', timeout });
            return;
        }
        await page.waitForTimeout(100);
    }
};

const loadPlaywright = async () => {
    try {
        return await import('playwright');
    } catch (_error) {
        fail('PLAYWRIGHT_UNAVAILABLE');
    }
};

const main = async () => {
    const options = parseArguments(process.argv.slice(2));
    assertSecureDownloadDirectory(options.downloadDirectory);
    let password = readPassword(options.passwordFile);
    let browser;
    let context;
    let observed;
    try {
        const { chromium } = await loadPlaywright();
        browser = await chromium.launch({ headless: !options.headed });
        context = await browser.newContext({
            acceptDownloads: true,
            ignoreHTTPSErrors: options.ignoreHttpsErrors,
            locale: 'en-US',
        });
        const page = await context.newPage();
        page.setDefaultTimeout(options.timeout);

        beginPhase('OPENING_PVE_UI', 'opening the PVE login page');
        await page.goto(`${options.pveOrigin}/`, { waitUntil: 'domcontentloaded', timeout: options.timeout });
        const username = page.locator('input[name="username"]:visible');
        const passwordInput = page.locator('input[name="password"]:visible');
        await username.waitFor({ state: 'visible', timeout: options.timeout });
        await passwordInput.waitFor({ state: 'visible', timeout: options.timeout });
        beginPhase('SELECTING_PVE_REALM', 'selecting the PVE login realm');
        await username.fill(options.user.username);
        await selectRealmThroughVisibleControl(page, options.user.realm, options.timeout);
        let passwordText = new TextDecoder('utf-8', { fatal: true }).decode(password);
        await passwordInput.fill(passwordText);
        // Strings cannot be wiped in JavaScript.  Remove the explicit
        // reference immediately; the retained byte buffer is zeroed in finally.
        passwordText = '';
        const loginButton = page.locator('.x-window:visible .x-btn').filter({ hasText: /^Login$/ }).last();
        await loginButton.waitFor({ state: 'visible', timeout: options.timeout });
        beginPhase('AUTHENTICATING_PVE_UI', 'submitting the PVE login form');
        await loginButton.click();
        await waitForAuthenticatedPveUi(page, options.user.full, options.timeout);
        status('authenticated through the PVE UI');

        beginPhase('DISMISSING_SUBSCRIPTION_NOTICE', 'checking the PVE subscription notice');
        await dismissKnownSubscriptionNotice(page, options.timeout);
        beginPhase('SELECTING_QEMU_VM', 'selecting the live QEMU VM');
        const node = await selectLiveQemuVm(page, options.vmid, options.timeout);
        const expected = { pveOrigin: options.pveOrigin, node, vmid: options.vmid, user: options.user.full };
        observed = observeLiveTraffic(page, expected);
        beginPhase('LOCATING_CONSOLE_BUTTON', 'locating the VM Console split button');
        const consoleSelector = await findLiveConsoleButton(page, options.vmid, options.timeout);
        const consoleButton = page.locator(consoleSelector);
        await consoleButton.waitFor({ state: 'visible', timeout: options.timeout });
        await waitForConsoleArrowToBeClickable(page, consoleSelector, options.timeout);
        beginPhase('OPENING_CONSOLE_MENU', 'opening the VM Console menu');
        await openConsoleSplitMenu(page, consoleButton, options.timeout);
        const launchItems = page.locator('.x-menu:visible .x-menu-item').filter({ hasText: /^q-sunshine$/ });
        if (await launchItems.count() !== 1) {
            fail('QSUNSHINE_MENU_UNAVAILABLE');
        }
        beginPhase('REQUESTING_LAUNCH_FILE', 'requesting the launch descriptor');
        const download = await waitForDownload(page, launchItems.first(), options.timeout);
        if (!validSuggestedFilename(download.suggestedFilename(), options.vmid)) {
            fail('INVALID_LAUNCH_DOWNLOAD');
        }
        if (observed.qsunshine !== 1 || observed.invalidRequest || observed.legacyRoute) {
            fail('INVALID_BROWSER_HANDOFF');
        }

        beginPhase('VALIDATING_LAUNCH_FILE', 'validating the downloaded launch descriptor');
        const destination = path.join(options.downloadDirectory, download.suggestedFilename());
        if (fs.existsSync(destination)) {
            fail('DOWNLOAD_DESTINATION_EXISTS');
        }
        await download.saveAs(destination);
        fs.chmodSync(destination, 0o600);
        const rawDescriptor = readDownloadedDescriptor(destination);
        let descriptorText;
        try {
            descriptorText = new TextDecoder('utf-8', { fatal: true }).decode(rawDescriptor);
            const cookies = await context.cookies(options.pveOrigin);
            const pveCookies = cookies
                .filter((cookie) => cookie.name === 'PVEAuthCookie')
                .map((cookie) => Buffer.from(cookie.value, 'utf8'));
            try {
                validateDescriptor(descriptorText, {
                    password,
                    cookies: pveCookies,
                });
            } finally {
                pveCookies.forEach((cookie) => cookie.fill(0));
            }
        } finally {
            rawDescriptor.fill(0);
            descriptorText = '';
        }
        status('downloaded descriptor passed strict confidentiality and schema checks');
        currentPhase = 'PASS';
        status('PASS');
    } catch (error) {
        if (observed) {
            status(browserHandoffDiagnostic(currentPhase, observed));
        }
        throw error;
    } finally {
        if (password) {
            password.fill(0);
            password = undefined;
        }
        if (context) {
            await context.close();
        }
        if (browser) {
            await browser.close();
        }
    }
};

if (require.main === module) {
    main().catch((error) => {
        const code = error instanceof QualificationError ? error.code : currentPhase;
        // Do not print arbitrary Playwright/PVE errors: they may embed a URL,
        // browser state, response body, cookie, or ticket.
        process.stderr.write(`q-sunshine PVE browser qualification: FAIL (${code})\n`);
        process.exitCode = 1;
    });
}

// Pure helpers are exported only for source-tree validation. The browser run
// remains an executable-only real-network test.
module.exports = {
    QualificationError,
    StrictJsonParser,
    isValidRouteHost,
    parseUser,
    parsePveUrl,
    readPassword,
    loadPlaywright,
    selectRealmThroughVisibleControl,
    selectLiveQemuVm,
    waitForAuthenticatedPveUi,
    dismissKnownSubscriptionNotice,
    browserHandoffDiagnostic,
    observeLiveTraffic,
    responseStatusClass,
};
