// Browser-side protocol test for PVE session -> protected q-sunshine API ->
// downloaded .qsm. It uses no PVE credentials or real network.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const overlayPath = new URL('../../integration/proxmox/pve9/ui/q-sunshine-console.js', import.meta.url);
const source = fs.readFileSync(overlayPath, 'utf8');

// ExtJS 6/7 implements callParent() with Function.caller. A strict override
// therefore makes its caller invisible and reaches null.apply(). Verify the
// actual shipped script is a sloppy ExtJS override and can call the captured
// stock initComponent through that legacy mechanism. No browser endpoint map
// is present: PVE's same-origin route chooses the local terminal service.
assert.doesNotMatch(source, /\(function\s*\(\)\s*\{\s*['"]use strict['"];?/);
const inheritanceProbe = vm.runInNewContext(`
    var captured;
    var stockCalls = 0;
    var stockInitComponent = function () { stockCalls += 1; };
    var Ext = {
        apply: function (target, source) { return Object.assign(target, source); },
        define: function (_name, definition) {
            definition.initComponent.$previous = stockInitComponent;
            captured = definition;
        },
        Msg: { alert: function () { throw new Error('unexpected alert'); } },
    };
    var window = {};
    ${source}
    function legacyCallParent() {
        var caller = this.callParent.caller;
        var parent = caller && caller.$previous;
        if (!parent) {
            throw new TypeError('null.apply');
        }
        return parent.apply(this, []);
    }
    var button = {
        consoleType: 'kvm',
        nodename: 'pve-a',
        vmid: 100,
        menu: [{ text: 'noVNC' }],
        callParent: legacyCallParent,
    };
    captured.initComponent.call(button);
    ({ stockCalls: stockCalls, menu: button.menu });
`, {});
assert.equal(inheritanceProbe.stockCalls, 1);
assert.equal(inheritanceProbe.menu.length, 2);
assert.equal(inheritanceProbe.menu[1].itemId, 'q-sunshine');

let overlay;
let apiRequest;
let downloadedBlob;
let downloadedName;
let revoked = false;
let parentCalled = false;
let alertCount = 0;
let responseMode = 'valid';
const definitions = new Map();

globalThis.gettext = (text) => text;
globalThis.Ext = {
    Msg: { alert: () => { alertCount += 1; } },
    apply: (target, sourceObject) => Object.assign(target, sourceObject),
    define: (name, definition) => {
        definitions.set(name, definition);
        if (name === 'PVE.qsunshine.ConsoleButtonOverlay') {
            overlay = definition;
        }
    },
};
globalThis.Proxmox = {
    Utils: {
        API2Request: (request) => {
            apiRequest = request;
            assert.equal(request.method, 'POST');
            assert.equal(request.url, '/nodes/pve-a/qemu/100/q-sunshine');
            assert.equal(Object.hasOwn(request, 'params'), false);
            assert.equal(request.waitMsgTarget, consoleButton);
            request.success({
                result: {
                    data: responseMode === 'valid' ? {
                        version: 1,
                        kind: 'q-sunshine-pve-launch',
                        endpoint: {
                            host: 'terminal-a.example.test',
                            port: 48123,
                            server_name: 'terminal-a.example.test',
                            ca_pem: '-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n',
                        },
                        claim: 'qsc1.abcdefghijklmnopqrstuvwxyz0123456789_-',
                        expires_at_utc_ms: Date.now() + 60_000,
                    } : {
                        version: 1,
                        kind: 'q-sunshine-pve-launch',
                        endpoint: { host: 'terminal-a.example.test', port: 48123 },
                        claim: 'invalid-missing-required-fields',
                        expires_at_utc_ms: Date.now() + 60_000,
                    },
                },
            });
        },
    },
};
globalThis.PVE = {
    Parser: {
        printPropertyString: (values) => {
            if (!values.type || values.type === '__default__') {
                return '';
            }
            return values.memory === undefined
                ? `type=${values.type}`
                : `type=${values.type},memory=${values.memory}`;
        },
    },
};
globalThis.window = {
    setTimeout: (callback, delay) => {
        assert.equal(delay, 0);
        callback();
        return 8;
    },
};
globalThis.Blob = class Blob {
    constructor(parts, options) {
        this.payload = parts.join('');
        this.type = options.type;
    }
};
globalThis.URL = {
    createObjectURL: (blob) => {
        downloadedBlob = blob;
        return 'blob:q-sunshine-launch';
    },
    revokeObjectURL: (url) => {
        assert.equal(url, 'blob:q-sunshine-launch');
        revoked = true;
    },
};
globalThis.document = {
    body: {
        appendChild: () => undefined,
    },
    createElement: () => ({
        style: {},
        click() {
            downloadedName = this.download;
        },
        remove: () => undefined,
    }),
};

vm.runInThisContext(source, { filename: String(overlayPath) });
assert.ok(overlay, 'overlay must register after stock pvemanagerlib.js');

const consoleButton = {
    consoleType: 'kvm',
    nodename: 'pve-a',
    vmid: 100,
    menu: [{ text: 'noVNC' }],
    callParent: () => {
        parentCalled = true;
    },
};
overlay.initComponent.call(consoleButton);
assert.equal(parentCalled, true);
assert.equal(consoleButton.menu.length, 2);
assert.equal(consoleButton.menu[1].itemId, 'q-sunshine');

await overlay.qsunshineLaunch.call(consoleButton);
assert.ok(apiRequest, 'same-origin PVE q-sunshine route must be called');
assert.equal(downloadedName, 'q-sunshine-pve-a-100.qsm');
assert.equal(downloadedBlob.type, 'application/vnd.q-sunshine-launch+json');
const downloaded = JSON.parse(downloadedBlob.payload);
assert.equal(downloaded.kind, 'q-sunshine-pve-launch');
assert.equal(Object.hasOwn(downloaded, 'ticket'), false);
assert.equal(revoked, true);
assert.equal(alertCount, 0);

responseMode = 'invalid';
await overlay.qsunshineLaunch.call(consoleButton);
assert.equal(alertCount, 1, 'malformed PVE response must use the generic launch error');

const displayOverlay = definitions.get('PVE.qsunshine.DisplayInputPanelOverlay');
const createOverlay = definitions.get('PVE.qsunshine.CreateWizardOverlay');
assert.ok(displayOverlay, 'PVE Display editor must receive the Display1 Advanced overlay');
assert.ok(createOverlay, 'PVE VM wizard must receive the Display1 Advanced overlay');

const vmWindow = {
    pveSelNode: { data: { vmid: 321 } },
    vmconfig: { args: '-cpu host' },
};
const displayPanel = {
    up: (query) => {
        assert.equal(query, 'proxmoxWindowEdit');
        return vmWindow;
    },
};
assert.deepEqual(
    displayOverlay.onGetValues.call(displayPanel, {
        type: 'virtio',
        memory: 256,
        qsm_display1: 1,
        qsm_rendernode: '/dev/dri/renderD129',
    }),
    {
        vga: 'type=virtio-gl,memory=256',
        args: '-cpu host -display dbus,addr=unix:path=/run/q-sunshine/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD129',
    },
    'Display1 enable must preserve unrelated args and use PVE-owned virtio-gl',
);

vmWindow.vmconfig.args =
    '-display dbus,addr=unix:path=/run/q-sunshine/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD129';
assert.deepEqual(
    displayOverlay.onGetValues.call(displayPanel, {
        type: 'virtio-gl',
        qsm_display1: 0,
        qsm_rendernode: '/dev/dri/renderD129',
    }),
    { vga: 'type=virtio-gl', args: '' },
    'Display1 disable must remove only its exact managed argument',
);

vmWindow.vmconfig.args = '-display gtk';
assert.throws(
    () => displayOverlay.onGetValues.call(displayPanel, {
        type: 'virtio',
        qsm_display1: 1,
        qsm_rendernode: '/dev/dri/renderD128',
    }),
    /cannot safely change/,
    'a hand-written display argument must never be overwritten',
);

const created = createOverlay.getValues.call({
    callParent: () => ({
        vmid: 654,
        qsm_display1: 1,
        qsm_rendernode: '/dev/dri/renderD128',
        name: 'virgl-browser',
    }),
});
assert.deepEqual(created, {
    vmid: 654,
    name: 'virgl-browser',
    args: '-display dbus,addr=unix:path=/run/q-sunshine/654/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128',
    vga: 'virtio-gl',
});
assert.throws(
    () => createOverlay.getValues.call({
        callParent: () => ({ qsm_display1: 1, qsm_rendernode: '/dev/dri/renderD128' }),
    }),
    /cannot safely change/,
    'wizard must not derive a D-Bus path from an automatic or missing VM ID',
);

console.log('PVE_UI_OVERLAY_PROTOCOL_OK');
