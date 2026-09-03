// PVE 9 Display editor contract for the browser-native qsm direct transport.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const overlayPath = new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url);
const source = fs.readFileSync(overlayPath, 'utf8');
const definitions = new Map();

globalThis.gettext = (text) => text;
globalThis.Ext = {
    Msg: { alert: () => undefined },
    apply: (target, sourceObject) => Object.assign(target, sourceObject),
    define: (name, definition) => definitions.set(name, definition),
};
globalThis.PVE = {
    Parser: {
        printPropertyString: (values) => Object.entries(values)
            .map(([key, value]) => `${key}=${value}`)
            .join(','),
    },
};

vm.runInThisContext(source, { filename: String(overlayPath) });

const displayOverlay = definitions.get('PVE.qsmDirect.DisplayInputPanelOverlay');
const displayEditOverlay = definitions.get('PVE.qsmDirect.DisplayEditOverlay');
const consoleOverlay = definitions.get('PVE.qsmDirect.ConsoleButtonOverlay');
const qemuConfigOverlay = definitions.get('PVE.qsmDirect.QemuConfigOverlay');
assert.ok(displayOverlay, 'PVE Display editor must receive the qsm Display1 overlay');
assert.ok(displayEditOverlay, 'PVE Display edit loader must restore qsm Display1 state');
assert.ok(consoleOverlay, 'PVE Console menu must receive the qsm Direct entry');
assert.ok(qemuConfigOverlay, 'PVE VM view must gate QSM Direct on saved Display1 args');

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
        type: 'std',
        qsm_direct_display1: 0,
    }),
    { vga: 'type=std' },
    'a VGA type without a memory field must not serialize memory=undefined',
);

assert.deepEqual(
    displayOverlay.onGetValues.call(displayPanel, {
        type: 'std',
        qsm_direct_display1: 1,
        qsm_direct_rendernode: '/dev/dri/renderD129',
    }),
    {
        vga: 'type=none',
        args: '-cpu host -device virtio-vga-gl,id=qsm-direct-gpu -display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD129',
    },
    'enabling Display1 must replace PVE VNC with one managed VirGL/Display1 pair',
);

vmWindow.vmconfig.args =
    '-display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128';
assert.deepEqual(
    displayOverlay.onGetValues.call(displayPanel, {
        type: 'virtio-gl',
        qsm_direct_display1: 1,
        qsm_direct_rendernode: '/dev/dri/renderD128',
    }),
    {
        vga: 'type=none',
        args: '-display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128 -device virtio-vga-gl,id=qsm-direct-gpu',
    },
    'saving an old Display1-only setting must migrate it away from PVE VNC',
);
vmWindow.vmconfig.args = '-cpu host';

assert.deepEqual(
    displayOverlay.onGetValues.call(displayPanel, {
        type: 'virtio',
        memory: '256',
        qsm_direct_display1: 0,
    }),
    { vga: 'type=virtio,memory=256' },
    'a supplied VGA memory value is retained as an integer',
);

let restoredValues;
let renderNodeDisabled;
const displayEdit = {
    pveSelNode: { data: { vmid: 321 } },
    load: (options) => options.success({
        result: {
            data: {
                args: '-cpu host -device virtio-vga-gl,id=qsm-direct-gpu -display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD130',
            },
        },
    }),
    callParent() {
        this.load({ success: () => undefined });
    },
    setValues: (values) => { restoredValues = values; },
    down: (query) => {
        assert.equal(query, '[name=qsm_direct_rendernode]');
        return { setDisabled: (value) => { renderNodeDisabled = value; } };
    },
};
displayEditOverlay.initComponent.call(displayEdit);
assert.deepEqual(restoredValues, {
    qsm_direct_display1: 1,
    qsm_direct_rendernode: '/dev/dri/renderD130',
});
assert.equal(renderNodeDisabled, false, 'reopening a configured VM must preserve the enabled render node');

let qsmDirectDisabled;
const consoleButton = {
    consoleType: 'kvm',
    nodename: 'pve-a',
    vmid: 321,
    menu: [{ text: 'noVNC' }],
    callParent: () => undefined,
    down: (query) => {
        assert.equal(query, '#qsm-direct');
        return { setDisabled: (value) => { qsmDirectDisabled = value; } };
    },
};
consoleOverlay.initComponent.call(consoleButton);
assert.equal(consoleButton.itemId, 'qsm-direct-console-button');
assert.equal(consoleButton.menu.at(-1).itemId, 'qsm-direct');
assert.equal(consoleButton.menu.at(-1).disabled, true, 'unconfigured VMs must not offer QSM Direct');
consoleOverlay.setEnableQsmDirect.call(consoleButton, true);
assert.equal(qsmDirectDisabled, false, 'a configured VM enables QSM Direct explicitly');

let qsmDirectEnabled;
globalThis.Proxmox = {
    Utils: {
        API2Request: (request) => {
            assert.equal(request.method, 'GET');
            assert.equal(request.url, '/nodes/pve-a/qemu/321/config');
            request.success({
                result: {
                    data: {
                        args: '-device virtio-vga-gl,id=qsm-direct-gpu -display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128',
                    },
                },
            });
        },
    },
};
qemuConfigOverlay.initComponent.call({
    pveSelNode: { data: { node: 'pve-a', vmid: 321 } },
    callParent: () => undefined,
    down: (query) => {
        assert.equal(query, '#qsm-direct-console-button');
        return { setEnableQsmDirect: (value) => { qsmDirectEnabled = value; } };
    },
});
assert.equal(qsmDirectEnabled, true, 'only the managed Display1 argument enables QSM Direct');

console.log('QSM_DIRECT_PVE_UI_OVERLAY_OK');
