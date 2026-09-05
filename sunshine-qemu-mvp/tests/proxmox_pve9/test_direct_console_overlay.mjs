// PVE 9 Display editor contract for the browser-native qsm direct transport.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const overlayPath = new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url);
const source = fs.readFileSync(overlayPath, 'utf8');
const definitions = new Map();
const managedGuestArgs = (vmid) =>
    ` -chardev socket,id=qsm-direct-agent,path=/run/qsm-pve-direct/${vmid}/qsm-agent.sock,server=on,wait=off` +
    ' -device virtio-serial-pci,id=qsm-direct-serial' +
    ' -device virtserialport,chardev=qsm-direct-agent,name=org.qsm.direct.agent';
const managedDirectInputArguments =
    ' -machine vmport=off -machine i8042=off -device usb-kbd,id=qsm-direct-keyboard';

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
assert.match(source, /window\.open\('', windowId,/,
    'QSM Direct must create a separate browser popup synchronously from the menu action');
assert.match(source, /const vmStatusUrl = \(\) => `\/nodes\/\$\{encodeURIComponent\(node\)\}\/qemu\/\$\{encodeURIComponent\(vmid\)\}\/status\/current`;/,
    'a popup opened before Power On must use the protected PVE status route before creating WebRTC');
assert.match(source, /Virtual machine is stopped\. Waiting for it to start…/,
    'a stopped VM must leave the popup open with an explicit start wait state');
assert.match(source, /if \(!await waitForVmStart\(\)\) \{ return; \}/,
    'the single-use WebRTC offer must wait until the VM is running');
assert.match(source, /popup=yes,width=1280,height=800,resizable=yes/,
    'the separate console window must be resizable by the operating system');
assert.match(source, /video\.muted = true/,
    'the initial video path must satisfy browser autoplay policy even with an Opus track');
assert.match(source, /Enable Audio/,
    'the popup must provide a user-gesture path to unmute guest audio');
assert.match(source, /event\.streams && event\.streams\[0\]/,
    'the popup must tolerate a valid WebRTC track event without a stream array');
assert.match(source, /new MediaStream\(\)/,
    'a streamless remote track must be attached to a local MediaStream rather than rendering black');
assert.match(source, /document\.documentElement\.requestFullscreen\(\)/,
    'the popup must provide a full-screen action for the entire display');
assert.match(source, /position:absolute;z-index:10;top:0;left:0;right:0/,
    'the console controls must overlay, rather than consume, guest video pixels');
assert.match(source, /video\.style\.cssText = 'position:fixed;inset:0;display:block;width:100vw;height:100vh;max-width:none;max-height:none;background:#000;object-fit:contain;outline:none'/,
    'the guest image must use the browser viewport rather than a stale percentage-layout box after resize');
assert.match(source, /document\.addEventListener\('fullscreenchange', \(\) => \{\s*releaseHeldInput\(\);\s*setFullscreenLabel\(\);[\s\S]*?resizeConsole\(true\);/,
    'full screen must release interrupted input before forcing an immediate Display1 resize');
assert.match(source, /const guestContentBox = \(\) => \{[\s\S]*?const scale = Math\.min\(box\.width \/ sourceWidth, box\.height \/ sourceHeight\);/,
    'cursor and input mapping must account for the real object-fit content rectangle');
assert.match(source, /Math\.floor\(\(event\.clientX - content\.left\) \/ usableScale\)/,
    'pointer X must be mapped through the letterbox-free source rectangle during a resize');
assert.match(source, /Math\.floor\(\(event\.clientY - content\.top\) \/ usableScale\)/,
    'pointer Y must be mapped through the letterbox-free source rectangle during a resize');
assert.doesNotMatch(source, /localGuestCursorAnchor|rememberLocalGuestCursor/,
    'the guest cursor must not use a delayed, separately positioned canvas overlay');
assert.match(source, /popup\.addEventListener\('blur', releaseHeldInput\)/,
    'losing popup focus must release held guest input');
assert.match(source, /video\.addEventListener\('pointercancel', releaseHeldInput\)/,
    'browser pointer cancellation must release held guest input');
assert.match(source, /for \(const button of \[\.\.\.heldMouseButtons\]\) \{ sendMouseButton\(button, false\); \}/,
    'a native transition must send releases for each held mouse button');
assert.match(source, /const fullscreenEscape = event\.code === 'Escape' &&[\s\S]*?document\.fullscreenElement \|\| fullscreenEscapePending/,
    'Escape must be recognized as a browser full-screen action before generic guest-key handling');
assert.match(source, /const action = document\.exitFullscreen\(\);/,
    'Escape in full screen must explicitly request the native popup exit');
assert.match(source, /Guest display did not acknowledge this window size\./,
    'a missed guest resize must be visible rather than silently leaving a letterboxed console');
assert.match(source, /resizeRetryAttempts >= 16/,
    'a direct Console must retry an early Display1 resize until its frame confirms the requested geometry');
assert.match(source, /const hideToolbarSoon = \(\) =>/,
    'an idle connected console must auto-hide its floating controls');
assert.match(source, /toolbar\.style\.transform = 'translateY\(-100%\)'/,
    'auto-hiding controls must move completely outside the guest picture');
assert.match(source, /toolbarHotZonePx: \{ defaultValue: 32, minimum: 4, maximum: 160 \}/,
    'the toolbar activation strip must be a bounded user preference');
assert.match(source, /toolbarRevealDelayMs: \{ defaultValue: 650, minimum: 0, maximum: 5000 \}/,
    'the top-edge hold time must be configurable rather than showing controls on any mousemove');
assert.match(source, /const observeToolbarZone = \(event\) =>/,
    'only the top activation strip may schedule a hidden toolbar reveal');
assert.match(source, /settings\.toolbarRevealDelayMs/,
    'the toolbar reveal timer must use the saved dwell setting');
assert.match(source, /settingsButton\.textContent = gettext\('Settings'\)/,
    'the compact toolbar must expose settings beside full screen');
assert.match(source, /CONSOLE_SETTINGS_STORAGE_KEY/,
    'console preferences must persist per browser without entering VM configuration');
assert.match(source, /const KEYBOARD_PRIORITY = Object\.freeze\(\{[\s\S]*?guest: 'guest-first',[\s\S]*?client: 'client-first'/,
    'the console must expose explicit guest-first and client-first keyboard policies');
assert.match(source, /keyboardPriority: KEYBOARD_PRIORITY\.guest/,
    'a remote desktop console must default to forwarding received keys to the guest');
assert.match(source, /Guest first — forward received keys/,
    'the settings panel must let a user choose guest shortcut priority');
assert.match(source, /Client first — browser shortcuts win/,
    'the settings panel must let a user choose client shortcut priority');
assert.match(source, /Ctrl\+Alt\+Shift\+Esc/,
    'guest-first capture must document an explicit local escape chord');
assert.match(source, /const leaveGuestKeyboardCapture = \(\) => \{[\s\S]*?releaseHeldInput\(\);[\s\S]*?settingsButton\.focus/,
    'the local escape chord must release held guest modifiers before moving focus to client controls');
assert.match(source, /restoreClientFocusAfterFullscreen/,
    'guest-first capture exit must restore client focus after leaving full screen');
assert.match(source, /const leaveGuestKeyboardCapture = \(\) => \{[\s\S]*?document\.fullscreenElement[\s\S]*?document\.exitFullscreen\(\)/,
    'the guest-first capture-exit chord must also leave native full screen');
assert.match(source, /if \(!clientFirst && guestCaptureExitShortcut\(event\)\)/,
    'the escape chord must be local only while guest-first capture is active');
assert.match(source, /if \(clientFirst && pasteShortcut\)/,
    'browser clipboard shortcuts must remain local only in client-first mode');
assert.match(source, /else if \(clientFirst && clientFirstShortcut\(event\)\)/,
    'client-first mode must leave its other browser shortcuts untouched before forwarding remaining keys');
assert.match(source, /macOS Cmd\+Tab, Cmd\+Space, Cmd\+Q,[\s\S]*?Windows Ctrl\+Alt\+Del and Win\+L/,
    'settings must name OS shortcuts which no web console can capture');
assert.doesNotMatch(source, /qsmResizeEdge|popup\.resizeBy\(/,
    'the browser must not overlay host resize controls which would intercept guest cursor input');
assert.match(source, /const guestCursor = document\.createElement\('canvas'\)/,
    'a bounded canvas must convert a Display1 cursor image to a browser cursor URL');
assert.match(source, /guestCursor\.style\.cssText = 'display:none;position:fixed;z-index:5;pointer-events:none/,
    'the conversion canvas must never intercept pointer input or emulate a resize edge');
assert.match(source, /peer\.addEventListener\('datachannel'/,
    'the browser must accept the server-created guest cursor channel');
assert.match(source, /qsm-guest-cursor/,
    'guest cursor positions must use their own latest-state WebRTC channel');
assert.match(source, /qsm_guest_cursor_shape/,
    'guest cursor shape changes must be delivered independently of video frames');
assert.match(source, /message\.sequence < latestGuestCursorSequence/,
    'an unordered stale Display1 cursor position must not pin the cursor at its initial origin');
assert.match(source, /cursor_url: guestCursor\.toDataURL\('image\/png'\)/,
    'the guest cursor image must become a browser-native PNG cursor');
assert.match(source, /const cursor = `url\("\$\{shape\.cursor_url\}"\) \$\{shape\.hotspot_x\} \$\{shape\.hotspot_y\}, default`/,
    'the browser must position the guest cursor image at the physical OS pointer with its guest hotspot');
assert.match(source, /if \(cursor !== appliedGuestCursor\)/,
    'identical mouse-position updates must not repeatedly mutate the browser cursor style');
assert.match(source, /Display1's pixman ARGB word is stored as BGRA bytes/,
    'the browser must convert QEMU cursor pixels to canvas RGBA explicitly');
assert.match(source, /connectionstatechange/,
    'the popup must follow WebRTC shutdown when its VM Display1 source disappears');
assert.match(source, /The virtual machine was stopped\. Closing console…/,
    'a stopped VM must explicitly close the separate browser console');
assert.match(source, /control\.addEventListener\('close', closeForStoppedVm/,
    'a server-side WebRTC data-channel close must also retire the popup');
assert.match(source, /qsm-pointer/,
    'latest-state pointer samples must not queue behind reliable keyboard input');
assert.match(source, /maxRetransmits: 0/,
    'the pointer channel must discard stale samples rather than retransmit them');
assert.match(source, /videoReceiver\.playoutDelayHint = settings\.playoutDelayMs \/ 1000/,
    'the browser receiver must retain an interactive, user-configurable playout delay');
assert.match(source, /pointerFrame = popup\.requestAnimationFrame\(\(\) => flushPointer\(false\)\)/,
    'pointer motion must use the popup compositor clock, which remains active while accelerated video is presented');
assert.match(source, /settings\.resizeDebounceMs/,
    'window dragging must use the configurable end-of-resize debounce');
assert.match(source, /settings\.resizeSettleMs/,
    'different guest modes must use the configurable settle interval');
assert.match(source, /lastResizeSentAt \+ settings\.resizeSettleMs - Date\.now\(\)/,
    'a final viewport request must wait for an in-flight guest mode transition');
assert.match(source, /if \(video\.width !== value\.width\) \{ video\.width = value\.width; \}/,
    'a native popup resize must update the accelerated video layer presentation width');
assert.match(source, /if \(video\.height !== value\.height\) \{ video\.height = value\.height; \}/,
    'a native popup resize must update the accelerated video layer presentation height');
assert.match(source, /A VM has one Display1 scanout, while several PVE Console\n\s*\/\/ windows may observe it\./,
    'a decoded resize from another Console must not be treated as this popup being resized');
assert.doesNotMatch(source, /video\.addEventListener\('resize', \(\) => resize\(false\)\)/,
    'an inactive differently sized Console must not restore its stale resolution');
assert.match(source, /qsm_guest_file_upload_chunk/,
    'file upload must be fragmented for browser WebRTC SCTP message limits');
assert.match(source, /const guestUploadChunkBytes = 32 \* 1024/,
    'each upload fragment must fit beneath the common 64 KiB SCTP ceiling');
assert.match(source, /qsm_guest_file_download_chunk/,
    'guest file downloads must be reassembled from SCTP-safe fragments');
assert.match(source, /qsm_guest_file_list/,
    'the Files panel must enumerate the guest exchange manifest rather than prompt for a filename');
assert.match(source, /for \(const target of \[video, localDrop\]\)/,
    'dropping local files directly on the guest image must start a QSM exchange upload');
assert.match(source, /setData\('DownloadURL'/,
    'prepared guest items must expose Chromium\'s host drag-out transfer when available');
assert.match(source, /Files/,
    'the compact floating toolbar must expose the Files panel');
assert.match(source, /new popup\.ClipboardItem/,
    'guest-to-browser copy must reserve clipboard permission during the initiating click or keydown');
assert.match(source, /sendGuestShortcut\(47\)/,
    'browser-to-guest paste must invoke Ctrl+V after the guest clipboard bridge is updated');
assert.match(source, /sendGuestShortcut\(46\)/,
    'Ctrl+C must ask the focused guest application to publish its selection before browser copy resolves');
assert.match(source, /qsm-direct-settings/,
    'codec and encoder settings must be persisted through a VM-scoped PVE endpoint, not browser storage');
assert.match(source, /Hardware only/,
    'the VM policy must distinguish a required hardware encoder from automatic fallback');
assert.match(source, /HEVC — not available yet/,
    'the UI must not claim HEVC is usable before the browser WebRTC stack can negotiate it');
assert.doesNotMatch(source, /Ext\.create\('Ext\.window\.Window'/,
    'the direct console must not be trapped inside the PVE browser page');

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
        args: '-cpu host -device virtio-vga-gl,id=qsm-direct-gpu -display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD129' + managedGuestArgs(321) + managedDirectInputArguments,
    },
    'enabling Display1 must replace PVE VNC with one managed VirGL/Display1 pair',
);

// PVE's normal list contains display types with and without a memory value.
// Exercise every non-default form accepted by PVE 9 through repeated
// enable/disable cycles: QSM must never append a second GPU or display, and
// disabling must leave the caller-selected stock display intact.
const stockDisplayTypes = [
    'std', 'vmware', 'qxl', 'qxl2', 'qxl3', 'qxl4',
    'virtio', 'virtio-gl', 'serial0', 'serial1', 'serial2', 'serial3', 'none',
];
for (const type of stockDisplayTypes) {
    let args = '-cpu host';
    for (let round = 0; round < 8; round += 1) {
        vmWindow.vmconfig.args = args;
        const enabledResult = displayOverlay.onGetValues.call(displayPanel, {
            type,
            qsm_direct_display1: 1,
            qsm_direct_rendernode: '/dev/dri/renderD128',
        });
        assert.equal(enabledResult.vga, 'type=none', `QSM owns the GPU for ${type}`);
        assert.match(enabledResult.args, /-device virtio-vga-gl,id=qsm-direct-gpu/);
        assert.equal((enabledResult.args.match(/(?:^|\s)-display(?:\s|$)/g) || []).length, 1);
        assert.equal((enabledResult.args.match(/(?:^|\s)-device\s+virtio-vga-gl(?:,|\s|$)/g) || []).length, 1);
        assert.match(enabledResult.args, /-machine vmport=off/);
        assert.match(enabledResult.args, /-machine i8042=off/);
        assert.match(enabledResult.args, /-device usb-kbd,id=qsm-direct-keyboard/);

        vmWindow.vmconfig.args = enabledResult.args;
        const disabledResult = displayOverlay.onGetValues.call(displayPanel, {
            type,
            qsm_direct_display1: 0,
        });
        assert.deepEqual(disabledResult, { vga: `type=${type}`, args: '-cpu host' },
            `round ${round}: disabling QSM restores the selected ${type} display`);
        args = disabledResult.args;
    }
}

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
        args: '-display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128 -device virtio-vga-gl,id=qsm-direct-gpu' + managedGuestArgs(321) + managedDirectInputArguments,
    },
    'saving an old Display1-only setting must migrate it away from PVE VNC',
);

vmWindow.vmconfig.args =
    '-cpu host -device virtio-vga-gl -display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128';
assert.deepEqual(
    displayOverlay.onGetValues.call(displayPanel, {
        type: 'none',
        qsm_direct_display1: 1,
        qsm_direct_rendernode: '/dev/dri/renderD129',
    }),
    {
        vga: 'type=none',
        args: '-cpu host -device virtio-vga-gl,id=qsm-direct-gpu -display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD129' + managedGuestArgs(321) + managedDirectInputArguments,
    },
    'the old unlabelled QSM GPU must migrate in place instead of adding a second adapter',
);
assert.deepEqual(
    displayOverlay.onGetValues.call(displayPanel, {
        type: 'std', qsm_direct_display1: 0,
    }),
    { vga: 'type=std', args: '-cpu host' },
    'disabling the old unlabelled QSM GPU must remove it completely',
);

for (const unsafeArgs of [
    '-device virtio-vga-gl,id=foreign -display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128',
    '-device virtio-vga-gl,id=qsm-direct-gpu -device virtio-vga-gl,id=qsm-direct-gpu -display dbus,addr=unix:path=/run/qsm-pve-direct/321/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128',
    '-display gtk',
]) {
    vmWindow.vmconfig.args = unsafeArgs;
    assert.throws(() => displayOverlay.onGetValues.call(displayPanel, {
        type: 'std', qsm_direct_display1: 1, qsm_direct_rendernode: '/dev/dri/renderD128',
    }), /another QEMU display|unsafe QEMU Display1 device arguments/,
    'foreign or duplicate QEMU display state must not be silently claimed');
}
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

let delayedRefresh;
let delayedEnabled;
qemuConfigOverlay.initComponent.call({
    pveSelNode: { data: { node: 'pve-a', vmid: 321 } },
    callParent: () => undefined,
    down: () => delayedRefresh ? { setEnableQsmDirect: (value) => { delayedEnabled = value; } } : null,
    on: (event, callback, scope, options) => {
        assert.equal(event, 'afterrender');
        assert.equal(scope.pveSelNode.data.vmid, 321);
        assert.deepEqual(options, { single: true });
        delayedRefresh = callback;
    },
});
assert.equal(typeof delayedRefresh, 'function', 'a late Console button must be refreshed after rendering');
delayedRefresh();
assert.equal(delayedEnabled, true, 'the first VM view must enable QSM Direct without a page reload');

console.log('QSM_DIRECT_PVE_UI_OVERLAY_OK');
