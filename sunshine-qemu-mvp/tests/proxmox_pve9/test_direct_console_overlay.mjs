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
assert.ok(displayOverlay, 'PVE Display editor must receive the qsm Display1 overlay');

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
        vga: 'type=virtio-gl',
        args: '-cpu host -display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD129',
    },
    'enabling Display1 without a VGA memory field must remain API-valid',
);

assert.deepEqual(
    displayOverlay.onGetValues.call(displayPanel, {
        type: 'virtio',
        memory: '256',
        qsm_direct_display1: 0,
    }),
    { vga: 'type=virtio,memory=256' },
    'a supplied VGA memory value is retained as an integer',
);

console.log('QSM_DIRECT_PVE_UI_OVERLAY_OK');
