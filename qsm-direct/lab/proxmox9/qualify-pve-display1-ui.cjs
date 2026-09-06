#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-or-later
/*
 * Real-browser check for the QSM Direct Display1 Advanced panel in PVE 9.
 *
 * This intentionally only opens the stock Hardware -> Display editor and
 * checks its live ExtJS fields. It never clicks OK, posts a VM update, or
 * changes a guest. The supplied PVE identity must have the ordinary VM
 * hardware-edit capability; that is separate from the VM.Console-only test
 * identity used by qualify-pve-console-launch.cjs.
 */
'use strict';

const path = require('node:path');
const {
    QualificationError,
    parsePveUrl,
    parseUser,
    readPassword,
    loadPlaywright,
    selectRealmThroughVisibleControl,
    selectLiveQemuVm,
    waitForAuthenticatedPveUi,
    dismissKnownSubscriptionNotice,
} = require('./qualify-pve-console-launch.cjs');

const MIN_VMID = 100;
const MAX_VMID = 999_999_999;
const DEFAULT_TIMEOUT_MS = 30_000;
const MIN_TIMEOUT_MS = 5_000;
const MAX_TIMEOUT_MS = 120_000;
const RENDER_NODE_PATTERN = /^\/dev\/dri\/renderD[0-9]{1,4}$/;

const fail = (code) => { throw new QualificationError(code); };
const status = (phase) => process.stdout.write(`QSM Direct PVE Display1 UI qualification: ${phase}\n`);

const parseInteger = (value, lower, upper) => {
    if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value)) {
        return undefined;
    }
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) && parsed >= lower && parsed <= upper ? parsed : undefined;
};

const usage = () => {
    process.stdout.write(
        'Usage: node lab/proxmox9/qualify-pve-display1-ui.cjs ' +
        '--pve-url https://pve.example:8006 --user admin@pve ' +
        '--password-file /secure/pve.password --vmid 100 ' +
        '[--ignore-https-errors] [--headed] [--timeout-ms 30000]\n' +
        'The identity needs VM.Config.HWType for the target VM. This check never saves changes.\n',
    );
};

const parseArguments = (arguments_) => {
    const values = Object.create(null);
    const flags = new Set();
    const valueFlags = new Set(['--pve-url', '--user', '--password-file', '--vmid', '--timeout-ms']);
    const booleanFlags = new Set(['--ignore-https-errors', '--headed']);
    for (let index = 0; index < arguments_.length; index += 1) {
        const argument = arguments_[index];
        if (argument === '--help' || argument === '-h') {
            usage();
            process.exit(0);
        }
        if (booleanFlags.has(argument)) {
            if (flags.has(argument)) fail('INVALID_ARGUMENTS');
            flags.add(argument);
            continue;
        }
        if (!valueFlags.has(argument) || index + 1 >= arguments_.length || Object.hasOwn(values, argument)) {
            fail('INVALID_ARGUMENTS');
        }
        const value = arguments_[index + 1];
        if (!value || value.startsWith('--')) fail('INVALID_ARGUMENTS');
        values[argument] = value;
        index += 1;
    }
    for (const required of ['--pve-url', '--user', '--password-file', '--vmid']) {
        if (!Object.hasOwn(values, required)) fail('INVALID_ARGUMENTS');
    }
    const vmid = parseInteger(values['--vmid'], MIN_VMID, MAX_VMID);
    const timeout = Object.hasOwn(values, '--timeout-ms')
        ? parseInteger(values['--timeout-ms'], MIN_TIMEOUT_MS, MAX_TIMEOUT_MS)
        : DEFAULT_TIMEOUT_MS;
    if (vmid === undefined || timeout === undefined) fail('INVALID_ARGUMENTS');
    return {
        pveOrigin: parsePveUrl(values['--pve-url']),
        user: parseUser(values['--user']),
        passwordFile: path.resolve(values['--password-file']),
        vmid,
        timeout,
        ignoreHttpsErrors: flags.has('--ignore-https-errors'),
        headed: flags.has('--headed'),
    };
};

const closeSubscriptionNotice = async (page, timeout) => {
    await dismissKnownSubscriptionNotice(page, timeout);
};

const openDisplayEditor = async (page, timeout) => {
    const hardwareEntry = page.locator('li.x-treelist-item:visible').filter({ hasText: /^Hardware$/ });
    if (await hardwareEntry.count() !== 1) fail('PVE_HARDWARE_NAVIGATION_UNAVAILABLE');
    await hardwareEntry.click();
    status('waiting for Hardware view');
    await page.waitForFunction(() => {
        // PVE's `PVE.qemu.HardwareView` is the runtime xtype in PVE 9, but
        // it is not a ComponentQuery alias. Search the stable grid base type
        // and compare the emitted xtype instead.
        const view = Ext.ComponentQuery.query('gridpanel')
            .find((candidate) => candidate && candidate.xtype === 'PVE.qemu.HardwareView' &&
                candidate.rendered && !candidate.hidden);
        return !!view && view.getStore().getRange().some((record) => record.data.key === 'vga');
    }, undefined, { timeout });
    const display = page.locator('.x-grid-item:visible').filter({ hasText: /^Display/ });
    if (await display.count() !== 1) fail('PVE_DISPLAY_ROW_UNAVAILABLE');
    await display.click();
    status('opening Display form');
    const edit = page.locator('.x-btn:visible').filter({ hasText: /^Edit$/ });
    if (await edit.count() !== 1) fail('PVE_DISPLAY_EDIT_UNAVAILABLE');
    await edit.click();
    status('waiting for Display1 fields');
    const handle = await page.waitForFunction(() => {
        // `PVE.qemu.DisplayEdit` is a class name, not a stable ComponentQuery
        // xtype across PVE 9 point releases. Locate the one visible edit
        // window by the two fields supplied by this overlay instead.
        const editor = Ext.ComponentQuery.query('window').find((candidate) =>
            candidate && candidate.rendered && !candidate.hidden &&
            candidate.down && candidate.down('[name=qsm_direct_display1]') &&
            candidate.down('[name=qsm_direct_rendernode]'));
        const display = editor && editor.down && editor.down('[name=qsm_direct_display1]');
        const profile = editor && editor.down && editor.down('[name=qsm_direct_profile]');
        const renderNode = editor && editor.down && editor.down('[name=qsm_direct_rendernode]');
        const graphicCard = editor && editor.down && editor.down('[name=type]');
        const effectiveAdapter = editor && editor.down && editor.down('[name=qsm_direct_effective_adapter]');
        if (!display || !profile || !renderNode || !effectiveAdapter) return null;
        const store = profile.getStore && profile.getStore();
        const profiles = store && store.getRange ? store.getRange().map((record) => String(
            record.get ? record.get('value') : record.data && record.data.value,
        )) : [];
        return {
            displayEnabled: display.getValue() === true || display.getValue() === 1 || display.getValue() === '1',
            profile: String(profile.getValue() || ''),
            profiles,
            renderNode: String(renderNode.getValue() || ''),
            renderNodeDisabled: renderNode.disabled === true,
            graphicCard: graphicCard ? String(graphicCard.getValue() || '') : '',
            effectiveAdapter: String(effectiveAdapter.getValue() || ''),
        };
    }, undefined, { timeout });
    try {
        return await handle.jsonValue();
    } finally {
        await handle.dispose();
    }
};

const main = async () => {
    const options = parseArguments(process.argv.slice(2));
    let password = readPassword(options.passwordFile);
    let browser;
    let context;
    try {
        const { chromium } = await loadPlaywright();
        browser = await chromium.launch({
            headless: !options.headed,
            executablePath: process.env.QSM_DIRECT_CHROME || undefined,
        });
        context = await browser.newContext({
            ignoreHTTPSErrors: options.ignoreHttpsErrors,
            locale: 'en-US',
        });
        const page = await context.newPage();
        page.setDefaultTimeout(options.timeout);
        status('opening PVE login');
        await page.goto(`${options.pveOrigin}/`, { waitUntil: 'domcontentloaded', timeout: options.timeout });
        const username = page.locator('input[name="username"]:visible');
        const passwordInput = page.locator('input[name="password"]:visible');
        await username.fill(options.user.username);
        await selectRealmThroughVisibleControl(page, options.user.realm, options.timeout);
        let passwordText = new TextDecoder('utf-8', { fatal: true }).decode(password);
        await passwordInput.fill(passwordText);
        passwordText = '';
        await page.locator('.x-window:visible .x-btn').filter({ hasText: /^Login$/ }).last().click();
        await waitForAuthenticatedPveUi(page, options.user.full, options.timeout);
        status('authenticated');
        await closeSubscriptionNotice(page, options.timeout);
        status('selecting VM');
        await selectLiveQemuVm(page, options.vmid, options.timeout);
        status('opening Display editor');
        const result = await openDisplayEditor(page, options.timeout);
        if (!result || result.displayEnabled !== true || result.profile !== 'virgl' ||
            !Array.isArray(result.profiles) || !result.profiles.includes('virgl') ||
            !result.profiles.includes('cpu') || result.renderNodeDisabled ||
            !RENDER_NODE_PATTERN.test(result.renderNode) || result.graphicCard !== 'none' ||
            result.effectiveAdapter !== 'QSM VirtIO-GPU (VirGL, GL) — PVE Graphic card is None') {
            fail('PVE_DISPLAY1_FIELDS_INVALID');
        }
        process.stdout.write(
            `QSM_DIRECT_PVE_DISPLAY1_UI_E2E_OK vmid=${options.vmid} profile=${result.profile} cpu_profile=yes rendernode=${result.renderNode} vga=${result.graphicCard} adapter=virgl\n`,
        );
    } finally {
        if (password) {
            password.fill(0);
            password = undefined;
        }
        if (context) await context.close();
        if (browser) await browser.close();
    }
};

if (require.main === module) {
    main().catch((error) => {
        const code = error instanceof QualificationError ? error.code : 'PVE_DISPLAY1_UI_FAILED';
        process.stderr.write(`QSM Direct PVE Display1 UI qualification: FAIL (${code})\n`);
        process.exitCode = 1;
    });
}

module.exports = { openDisplayEditor, parseArguments };
