/*
 * qsm-pve-direct Console menu integration
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Browser-only PVE Console entry for the direct Display1 transport.
 */
(function () {
    const MIN_VMID = 100;
    const MAX_VMID = 999999999;
    const MAX_QEMU_ARGS_BYTES = 8192;
    const RUNTIME_PREFIX = '/run/qsm-pve-direct';
    const DEFAULT_RENDER_NODE = '/dev/dri/renderD128';
    const DIRECT_GPU_ID = 'qsm-direct-gpu';

    const validNode = (value) => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/.test(value);
    const validVmid = (value) => Number.isInteger(value) && value >= MIN_VMID && value <= MAX_VMID;
    const validRenderNode = (value) => typeof value === 'string' && /^\/dev\/dri\/renderD[0-9]{1,4}$/.test(value);
    const enabled = (value) => value === true || value === 1 || value === '1';
    const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const windowVmid = (window) => {
        const selected = window && window.pveSelNode && window.pveSelNode.data;
        const vmid = selected ? Number(selected.vmid) : NaN;
        return validVmid(vmid) ? vmid : null;
    };
    const displayArgument = (vmid, rendernode) => {
        if (!validVmid(vmid) || !validRenderNode(rendernode)) {
            throw new Error('invalid direct Display1 settings');
        }
        return `-display dbus,addr=unix:path=${RUNTIME_PREFIX}/${vmid}/qemu-display1.bus,gl=on,rendernode=${rendernode}`;
    };
    const gpuArgument = () => `-device virtio-vga-gl,id=${DIRECT_GPU_ID}`;
    const displayPattern = (vmid) => new RegExp(
        `(?:^|\\s)-display\\s+dbus,addr=unix:path=${escapeRegExp(RUNTIME_PREFIX)}/${vmid}/qemu-display1\\.bus,gl=on,rendernode=(/dev/dri/renderD[0-9]{1,4})(?=\\s|$)`,
    );
    const displayCount = (args) => (args.match(/(?:^|\s)-display(?:\s|$)/g) || []).length;
    const directGpuCount = (args) => (args.match(
        new RegExp(`(?:^|\\s)${escapeRegExp(gpuArgument())}(?=\\s|$)`, 'g')) || []).length;
    const displayState = (args, vmid) => {
        if (typeof args !== 'string' || !validVmid(vmid)) {
            return { managed: false, legacy: false, rendernode: DEFAULT_RENDER_NODE };
        }
        const match = args.match(displayPattern(vmid));
        const gpuCount = directGpuCount(args);
        if (match && displayCount(args) === 1 && gpuCount <= 1) {
            return { managed: gpuCount === 1, legacy: gpuCount === 0, rendernode: match[1] };
        }
        return { managed: false, legacy: false, rendernode: DEFAULT_RENDER_NODE };
    };
    const updateDisplayArgument = (args, vmid, rendernode, want) => {
        args = args === undefined || args === null ? '' : args;
        if (typeof args !== 'string' || args.length > MAX_QEMU_ARGS_BYTES || /[\x00-\x1f\x7f]/.test(args)) {
            throw new Error('unsafe QEMU display arguments');
        }
        const existing = displayState(args, vmid);
        const count = displayCount(args);
        const gpuCount = directGpuCount(args);
        const wanted = displayArgument(vmid, rendernode);
        if (want) {
            if (count === 0) {
                if (gpuCount !== 0) { throw new Error('unsafe QEMU Display1 device arguments'); }
                const added = `${gpuArgument()} ${wanted}`;
                return args ? `${args} ${added}` : added;
            }
            if (!existing.managed && !existing.legacy) {
                throw new Error('another QEMU display is configured');
            }
            const previous = displayArgument(vmid, existing.rendernode);
            const updated = args.replace(new RegExp(`(?:^|\\s)${escapeRegExp(previous)}(?=\\s|$)`), (value) =>
                value.startsWith(' ') ? ` ${wanted}` : wanted);
            return existing.legacy ? `${updated} ${gpuArgument()}` : updated;
        }
        if (!existing.managed && !existing.legacy) {
            if (gpuCount !== 0) { throw new Error('unsafe QEMU Display1 device arguments'); }
            return args;
        }
        const previous = displayArgument(vmid, existing.rendernode);
        const withoutDisplay = args.replace(
            new RegExp(`(?:^|\\s)${escapeRegExp(previous)}(?=\\s|$)`), '').trim();
        return withoutDisplay.replace(
            new RegExp(`(?:^|\\s)${escapeRegExp(gpuArgument())}(?=\\s|$)`), '').trim().replace(/\s{2,}/g, ' ');
    };

    const displayFields = () => [
        {
            xtype: 'proxmoxcheckbox', name: 'qsm_direct_display1', uncheckedValue: 0,
            defaultValue: 0, deleteDefaultValue: true, fieldLabel: gettext('QSM Display1'),
            boxLabel: gettext('Replace VNC with D-Bus display and VirGL GPU'),
            listeners: { change: function (_field, value) {
                const node = this.up('inputpanel').down('[name=qsm_direct_rendernode]');
                if (node) { node.setDisabled(!enabled(value)); }
            }},
        },
        {
            xtype: 'textfield', name: 'qsm_direct_rendernode', value: DEFAULT_RENDER_NODE, disabled: true,
            fieldLabel: gettext('Render node'), allowBlank: false,
            validator: (value) => validRenderNode(value) || gettext('Use a DRM render node, for example /dev/dri/renderD128.'),
        },
        { xtype: 'displayfield', userCls: 'pmx-hint', value: gettext(
            'QSM Display1 disables PVE VNC for this VM, then adds its private D-Bus display and VirtIO-GPU (VirGL).'),
        },
    ];

    Ext.define('PVE.qsmDirect.DisplayInputPanelOverlay', {
        override: 'PVE.qemu.DisplayInputPanel',
        initComponent: function () {
            this.advancedItems = (this.advancedItems || []).concat(displayFields());
            this.callParent();
        },
        onGetValues: function (values) {
            const edit = this.up('proxmoxWindowEdit');
            const vmid = windowVmid(edit);
            const active = enabled(values.qsm_direct_display1);
            const rendernode = values.qsm_direct_rendernode || DEFAULT_RENDER_NODE;
            if (!edit || !edit.vmconfig || (active && (!validVmid(vmid) || !validRenderNode(rendernode)))) {
                throw new Error('direct Display1 configuration is invalid');
            }
            // PVE 9 does not populate `memory` for every VGA type. Passing an
            // explicit `undefined` becomes `vga.memory=undefined` at the API
            // boundary, which fails its integer schema validation. Preserve a
            // supplied integer (the ExtJS field may serialize it as a string),
            // but leave the property out when the field is absent.
            // PVE normally appends both egl-headless and VNC for virtio-gl.
            // QEMU rejects VNC next to a GL Display1 backend. With vga=none,
            // PVE emits no VNC and the managed args own the single virgl GPU.
            const result = { type: active ? 'none' : values.type };
            const rawMemory = values.memory;
            if (!active && rawMemory !== undefined && rawMemory !== null && rawMemory !== '') {
                const memory = Number(rawMemory);
                if (Number.isInteger(memory)) { result.memory = memory; }
            }
            const printed = PVE.Parser.printPropertyString(result, 'type');
            const response = printed ? { vga: printed } : { delete: 'vga' };
            const changed = updateDisplayArgument(edit.vmconfig.args, vmid, rendernode, active);
            if (changed !== (edit.vmconfig.args || '')) { response.args = changed; }
            return response;
        },
    });

    // The custom controls are represented by the exact `args` value rather
    // than an unsupported PVE config key. Populate them after DisplayEdit's
    // asynchronous load so reopening Hardware -> Display reflects what was
    // actually saved for this VM.
    Ext.define('PVE.qsmDirect.DisplayEditOverlay', {
        override: 'PVE.qemu.DisplayEdit',
        initComponent: function () {
            const me = this;
            const stockLoad = me.load;
            me.load = function (options) {
                const chained = Ext.apply({}, options);
                const stockSuccess = chained.success;
                chained.success = function (response) {
                    if (stockSuccess) { stockSuccess.apply(this, arguments); }
                    const data = response && response.result && response.result.data;
                    const state = displayState(data && data.args, windowVmid(me));
                    me.setValues({
                        qsm_direct_display1: state.managed || state.legacy ? 1 : 0,
                        qsm_direct_rendernode: state.rendernode,
                    });
                    const rendernode = me.down('[name=qsm_direct_rendernode]');
                    if (rendernode) { rendernode.setDisabled(!state.managed && !state.legacy); }
                };
                return stockLoad.call(me, chained);
            };
            me.callParent();
            me.load = stockLoad;
        },
    });

    // QEMU set-1 scancodes used by the Display1 Keyboard interface. Browser
    // `code` is physical-key based, so keyboard layout does not alter remote
    // shortcut behavior. Unknown keys remain local and are never guessed.
    const scanCodes = {
        Escape: 1, Digit1: 2, Digit2: 3, Digit3: 4, Digit4: 5, Digit5: 6, Digit6: 7,
        Digit7: 8, Digit8: 9, Digit9: 10, Digit0: 11, Minus: 12, Equal: 13, Backspace: 14,
        Tab: 15, KeyQ: 16, KeyW: 17, KeyE: 18, KeyR: 19, KeyT: 20, KeyY: 21, KeyU: 22,
        KeyI: 23, KeyO: 24, KeyP: 25, BracketLeft: 26, BracketRight: 27, Enter: 28,
        ControlLeft: 29, KeyA: 30, KeyS: 31, KeyD: 32, KeyF: 33, KeyG: 34, KeyH: 35,
        KeyJ: 36, KeyK: 37, KeyL: 38, Semicolon: 39, Quote: 40, Backquote: 41,
        ShiftLeft: 42, Backslash: 43, KeyZ: 44, KeyX: 45, KeyC: 46, KeyV: 47, KeyB: 48,
        KeyN: 49, KeyM: 50, Comma: 51, Period: 52, Slash: 53, ShiftRight: 54,
        NumpadMultiply: 55, AltLeft: 56, Space: 57, CapsLock: 58, F1: 59, F2: 60, F3: 61,
        F4: 62, F5: 63, F6: 64, F7: 65, F8: 66, F9: 67, F10: 68, NumLock: 69, ScrollLock: 70,
        Numpad7: 71, Numpad8: 72, Numpad9: 73, NumpadSubtract: 74, Numpad4: 75, Numpad5: 76,
        Numpad6: 77, NumpadAdd: 78, Numpad1: 79, Numpad2: 80, Numpad3: 81, Numpad0: 82,
        NumpadDecimal: 83, F11: 87, F12: 88,
    };
    const extendedScanCodes = {
        ControlRight: 0x11d, AltRight: 0x138, MetaLeft: 0x15b, MetaRight: 0x15c,
        Insert: 0x152, Delete: 0x153, Home: 0x147, End: 0x14f, PageUp: 0x149, PageDown: 0x151,
        ArrowUp: 0x148, ArrowLeft: 0x14b, ArrowRight: 0x14d, ArrowDown: 0x150,
    };
    const qemuKey = (event) => scanCodes[event.code] || extendedScanCodes[event.code] || null;

    const waitForIce = (peer) => new Promise((resolve) => {
        if (peer.iceGatheringState === 'complete') { resolve(); return; }
        const timer = window.setTimeout(resolve, 3000);
        peer.addEventListener('icegatheringstatechange', () => {
            if (peer.iceGatheringState === 'complete') { window.clearTimeout(timer); resolve(); }
        }, { once: true });
    });
    const api = (url, params, target) => new Promise((resolve, reject) => {
        Proxmox.Utils.API2Request({ url, method: 'POST', params, waitMsgTarget: target,
            success: ({ result }) => {
                const answer = result && result.data;
                if (!answer || answer.type !== 'answer' || typeof answer.sdp !== 'string' || answer.sdp.length < 1) {
                    reject(new Error('invalid direct WebRTC answer')); return;
                }
                resolve(answer);
            }, failure: () => reject(new Error('PVE rejected direct console launch')),
        });
    });
    const dimensions = (video) => {
        const box = video.getBoundingClientRect();
        const width = Math.max(64, Math.min(16384, Math.floor(box.width / 2) * 2));
        const height = Math.max(64, Math.min(16384, Math.floor(box.height / 2) * 2));
        return { width, height, fps: 60 };
    };

    const openConsole = async function (button, node, vmid) {
        const windowId = `qsm-direct-${node}-${vmid}-${Date.now()}`;
        const panel = Ext.create('Ext.window.Window', {
            itemId: windowId, title: gettext('QSM Direct Console'), width: 1280, height: 800,
            maximizable: true, modal: false, layout: 'fit', closeAction: 'destroy',
            html: '<video autoplay playsinline tabindex="0" style="width:100%;height:100%;background:#000;object-fit:contain"></video>',
        });
        let peer = null;
        let control = null;
        let observer = null;
        const send = (value) => {
            if (control && control.readyState === 'open') { control.send(JSON.stringify(value)); }
        };
        panel.on('destroy', () => {
            if (observer) { observer.disconnect(); }
            if (peer) { peer.close(); }
        });
        panel.show();
        const video = panel.getEl().down('video').dom;
        try {
            peer = new RTCPeerConnection();
            control = peer.createDataChannel('qsm-control', { ordered: true });
            peer.ontrack = (event) => { video.srcObject = event.streams[0]; };
            const resize = () => send({ op: 'resize', ...dimensions(video) });
            control.addEventListener('open', resize);
            observer = new ResizeObserver(resize);
            observer.observe(video);
            video.addEventListener('mousemove', (event) => {
                const box = video.getBoundingClientRect();
                const x = Math.max(0, Math.min(Math.floor(event.clientX - box.left), Math.floor(box.width) - 1));
                const y = Math.max(0, Math.min(Math.floor(event.clientY - box.top), Math.floor(box.height) - 1));
                send({ op: 'mouse_position', x, y, width: Math.max(1, Math.floor(box.width)), height: Math.max(1, Math.floor(box.height)) });
            });
            video.addEventListener('mousedown', (event) => { video.focus(); send({ op: 'mouse_button', button: event.button + 1, down: true }); event.preventDefault(); });
            video.addEventListener('mouseup', (event) => { send({ op: 'mouse_button', button: event.button + 1, down: false }); event.preventDefault(); });
            video.addEventListener('wheel', (event) => { send({ op: 'scroll', vertical: Math.max(-32768, Math.min(32767, Math.trunc(event.deltaY))), horizontal: Math.max(-32768, Math.min(32767, Math.trunc(event.deltaX))) }); event.preventDefault(); }, { passive: false });
            for (const name of ['keydown', 'keyup']) {
                video.addEventListener(name, (event) => {
                    const key = qemuKey(event);
                    if (key !== null) { send({ op: 'keyboard', key, down: name === 'keydown', modifiers: 0 }); event.preventDefault(); }
                });
            }
            peer.addTransceiver('video', { direction: 'recvonly' });
            peer.addTransceiver('audio', { direction: 'recvonly' });
            const offer = await peer.createOffer();
            await peer.setLocalDescription(offer);
            await waitForIce(peer);
            const requestSize = dimensions(video);
            const answer = await api(`/nodes/${encodeURIComponent(node)}/qemu/${encodeURIComponent(vmid)}/qsm-direct`, {
                sdp: peer.localDescription.sdp, width: requestSize.width, height: requestSize.height, fps: requestSize.fps,
            }, button);
            await peer.setRemoteDescription(answer);
        } catch (_error) {
            panel.close();
            Ext.Msg.alert(gettext('QSM Direct'), gettext('Could not create a direct browser console.'));
        }
    };

    Ext.define('PVE.qsmDirect.ConsoleButtonOverlay', {
        override: 'PVE.button.ConsoleButton',
        enableQsmDirect: false,
        setEnableQsmDirect: function (enable) {
            this.enableQsmDirect = !!enable;
            const item = this.down('#qsm-direct');
            if (item) { item.setDisabled(!this.enableQsmDirect); }
        },
        initComponent: function () {
            const me = this;
            if (me.consoleType === 'kvm' && validNode(me.nodename) && validVmid(Number(me.vmid))) {
                me.itemId = 'qsm-direct-console-button';
                me.menu = (me.menu || []).map((item) => Ext.apply({}, item));
                me.menu.push({ xtype: 'menuitem', itemId: 'qsm-direct', text: 'QSM Direct',
                    iconCls: 'fa fa-desktop', disabled: !me.enableQsmDirect,
                    handler: () => openConsole(me, me.nodename, Number(me.vmid)) });
            }
            me.callParent();
        },
    });

    // Unlike stock Spice/serial capability bits, PVE's status endpoint does
    // not expose arbitrary QEMU `args`. Read the ordinary protected config
    // once when a VM view opens and only enable this menu item if the exact
    // transport-owned Display1 argument is present.
    Ext.define('PVE.qsmDirect.QemuConfigOverlay', {
        override: 'PVE.qemu.Config',
        initComponent: function () {
            const me = this;
            me.callParent();
            const vm = me.pveSelNode && me.pveSelNode.data;
            const vmid = vm ? Number(vm.vmid) : NaN;
            const button = me.down('#qsm-direct-console-button');
            if (!button || !validNode(vm && vm.node) || !validVmid(vmid)) { return; }
            Proxmox.Utils.API2Request({
                url: `/nodes/${encodeURIComponent(vm.node)}/qemu/${encodeURIComponent(vmid)}/config`,
                method: 'GET',
                success: ({ result }) => {
                    const data = result && result.data;
                    button.setEnableQsmDirect(displayState(data && data.args, vmid).managed);
                },
                failure: () => button.setEnableQsmDirect(false),
            });
        },
    });
}());
