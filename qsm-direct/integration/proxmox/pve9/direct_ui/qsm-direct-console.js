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
    const DIRECT_AGENT_ID = 'qsm-direct-agent';
    // VirGL is an optional acceleration profile, not a prerequisite for the
    // browser transport. The CPU profile relies on PVE's stock VGA adapter
    // and explicitly uses QEMU's non-GL Display1 backend.
    const DISPLAY_PROFILE = Object.freeze({
        virgl: 'virgl',
        cpu: 'cpu',
    });
    // The non-GL D-Bus Display1 backend captures the active QEMU console's 2D
    // surface, which is device-agnostic: Standard VGA, VirtIO-GPU (no GL) and
    // VMware SVGA all render to an ordinary console the same way. VMware SVGA
    // therefore works with the CPU profile (it cannot drive the VirGL/GL
    // profile, which needs virtio-vga-gl). qxl is left out for now: its usual
    // home is SPICE and it has not been qualified here.
    const CPU_DISPLAY_VGA_TYPES = new Set(['std', 'virtio', 'vmware']);
    // Console preferences are deliberately browser-local.  They do not
    // contain a credential, VM identifier, SDP, or any host policy: the PVE
    // Console route remains the sole authority for those.  Keeping the UI
    // tuning here also means an administrator can use the same node with a
    // conservative desktop profile while a LAN user selects an interactive
    // one without changing a VM configuration.
    const CONSOLE_SETTINGS_STORAGE_KEY = 'qsm-direct-console-settings-v1';
    const KEYBOARD_PRIORITY = Object.freeze({
        guest: 'guest-first',
        // Raw delivery is deliberately a third choice rather than an
        // implementation detail of guest-first. It is useful while setting
        // up a VM's own shortcuts: every DOM press/release edge is sent to
        // Display1 and QSM reserves no local capture-exit chord.
        raw: 'raw-events',
        client: 'client-first',
    });
    const CONSOLE_SETTING_SCHEMA = Object.freeze({
        toolbarHotZonePx: { defaultValue: 32, minimum: 4, maximum: 160 },
        toolbarNearbyPx: { defaultValue: 36, minimum: 0, maximum: 240 },
        toolbarRevealDelayMs: { defaultValue: 650, minimum: 0, maximum: 5000 },
        toolbarHideDelayMs: { defaultValue: 3000, minimum: 250, maximum: 30000 },
        targetFps: { defaultValue: 60, minimum: 10, maximum: 240 },
        playoutDelayMs: { defaultValue: 0, minimum: 0, maximum: 1000 },
        resizeDebounceMs: { defaultValue: 400, minimum: 100, maximum: 3000 },
        resizeSettleMs: { defaultValue: 1000, minimum: 250, maximum: 5000 },
    });
    // A guest compositor turns its virtual output off after an idle timeout
    // (DPMS/screen blank).  QEMU Display1 keeps emitting a scanout, but every
    // pixel is black and no mode change is applied until the guest wakes.
    // Measured on PVE 9 with a KWin/Wayland guest: the desktop returns about
    // 256 ms after the first browser mouse or key event, and never without
    // one.  This constant is how long an all-black picture is tolerated
    // before the Console explains it; a locked guest that keeps scanning out
    // its last picture is covered by the stalled-resize path instead.
    const GUEST_SLEEP_HINT_MS = 4000;

    const defaultConsoleSettings = () => ({
        ...Object.fromEntries(Object.entries(CONSOLE_SETTING_SCHEMA).map(
            ([name, definition]) => [name, definition.defaultValue],
        )),
        // The direct Console is a remote-desktop window, so the guest gets
        // received keys by default. The explicit client-first mode remains
        // available for users who want browser/OS shortcuts to win.
        keyboardPriority: KEYBOARD_PRIORITY.guest,
    });
    const readConsoleSettings = (storage) => {
        const settings = defaultConsoleSettings();
        try {
            if (!storage) { return settings; }
            const stored = JSON.parse(storage.getItem(CONSOLE_SETTINGS_STORAGE_KEY) || '{}');
            if (!stored || typeof stored !== 'object' || Array.isArray(stored)) { return settings; }
            for (const [name, definition] of Object.entries(CONSOLE_SETTING_SCHEMA)) {
                const value = stored[name];
                if (Number.isInteger(value) && value >= definition.minimum && value <= definition.maximum) {
                    settings[name] = value;
                }
            }
            if (Object.values(KEYBOARD_PRIORITY).includes(stored.keyboardPriority)) {
                settings.keyboardPriority = stored.keyboardPriority;
            }
        } catch (_error) { /* A disabled/private localStorage uses defaults. */ }
        return settings;
    };
    const writeConsoleSettings = (storage, settings) => {
        try {
            if (storage) { storage.setItem(CONSOLE_SETTINGS_STORAGE_KEY, JSON.stringify(settings)); }
        }
        catch (_error) { /* The active popup remains usable without persistence. */ }
    };
    // Display1 injects into QEMU's *active* input devices.  q35 normally
    // supplies PS/2 plus VMware's vmmouse; a guest can select either one and
    // make Display1 pointer events disappear even though the WebRTC channel
    // is healthy.  A direct console owns one explicit USB keyboard and the
    // existing PVE USB tablet, and removes both legacy alternatives.  Keep
    // these exact args managed with the Display1 lifecycle: changing back to
    // a normal PVE display restores PVE's normal input topology.
    const DIRECT_INPUT_ARGUMENTS = [
        '-machine vmport=off',
        '-machine i8042=off',
        '-device usb-kbd,id=qsm-direct-keyboard',
    ];

    const validNode = (value) => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/.test(value);
    const validVmid = (value) => Number.isInteger(value) && value >= MIN_VMID && value <= MAX_VMID;
    const validRenderNode = (value) => typeof value === 'string' && /^\/dev\/dri\/renderD[0-9]{1,4}$/.test(value);
    const enabled = (value) => value === true || value === 1 || value === '1';
    const displayProfile = (value) => Object.values(DISPLAY_PROFILE).includes(value)
        ? value : DISPLAY_PROFILE.virgl;
    const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const windowVmid = (window) => {
        const selected = window && window.pveSelNode && window.pveSelNode.data;
        const vmid = selected ? Number(selected.vmid) : NaN;
        return validVmid(vmid) ? vmid : null;
    };
    const displayArgument = (vmid, profile, rendernode) => {
        if (!validVmid(vmid)) {
            throw new Error('invalid direct Display1 settings');
        }
        if (profile === DISPLAY_PROFILE.cpu) {
            return `-display dbus,addr=unix:path=${RUNTIME_PREFIX}/${vmid}/qemu-display1.bus,gl=off`;
        }
        if (profile !== DISPLAY_PROFILE.virgl || !validRenderNode(rendernode)) {
            throw new Error('invalid direct Display1 settings');
        }
        return `-display dbus,addr=unix:path=${RUNTIME_PREFIX}/${vmid}/qemu-display1.bus,gl=on,rendernode=${rendernode}`;
    };
    const gpuArgument = () => `-device virtio-vga-gl,id=${DIRECT_GPU_ID}`;
    const guestArguments = (vmid) => [
        `-chardev socket,id=${DIRECT_AGENT_ID},path=${RUNTIME_PREFIX}/${vmid}/qsm-agent.sock,server=on,wait=off`,
        '-device virtio-serial-pci,id=qsm-direct-serial',
        `-device virtserialport,chardev=${DIRECT_AGENT_ID},name=org.qsm.direct.agent`,
    ];
    const virglDisplayPattern = (vmid) => new RegExp(
        `(?:^|\\s)-display\\s+dbus,addr=unix:path=${escapeRegExp(RUNTIME_PREFIX)}/${vmid}/qemu-display1\\.bus,gl=on,rendernode=(/dev/dri/renderD[0-9]{1,4})(?=\\s|$)`,
    );
    const cpuDisplayPattern = (vmid) => new RegExp(
        `(?:^|\\s)-display\\s+dbus,addr=unix:path=${escapeRegExp(RUNTIME_PREFIX)}/${vmid}/qemu-display1\\.bus,gl=off(?=\\s|$)`,
    );
    const displayCount = (args) => (args.match(/(?:^|\s)-display(?:\s|$)/g) || []).length;
    const virtioVgaGlArguments = (args) => args.match(
        /(?:^|\s)-device\s+virtio-vga-gl(?:,[^\s]+)?(?=\s|$)/g,
    ) || [];
    const normaliseArgument = (value) => value.trim();
    const containsArgument = (args, value) => new RegExp(
        `(?:^|\\s)${escapeRegExp(value)}(?=\\s|$)`,
    ).test(args);
    const managedGuestChannel = (args, vmid) => guestArguments(vmid).every((value) => containsArgument(args, value));
    const managedDirectInput = (args) => DIRECT_INPUT_ARGUMENTS.every((value) => containsArgument(args, value));
    const removeManagedGuestChannel = (args, vmid) => guestArguments(vmid).reduce((current, value) =>
        current.replace(new RegExp(`(?:^|\\s)${escapeRegExp(value)}(?=\\s|$)`), ''), args,
    ).trim().replace(/\s{2,}/g, ' ');
    const addManagedGuestChannel = (args, vmid) => {
        if (args.includes(DIRECT_AGENT_ID) && !managedGuestChannel(args, vmid)) {
            throw new Error('unsafe QSM guest channel arguments');
        }
        return managedGuestChannel(args, vmid) ? args : `${args} ${guestArguments(vmid).join(' ')}`.trim();
    };
    const addManagedDirectInput = (args) => managedDirectInput(args)
        ? args : `${args} ${DIRECT_INPUT_ARGUMENTS.filter((value) => !containsArgument(args, value)).join(' ')}`.trim();
    const removeManagedDirectInput = (args) => DIRECT_INPUT_ARGUMENTS.reduce((current, value) => current.replace(
        new RegExp(`(?:^|\\s)${escapeRegExp(value)}(?=\\s|$)`), '',
    ), args).trim().replace(/\s{2,}/g, ' ');
    const displayState = (args, vmid) => {
        if (typeof args !== 'string' || !validVmid(vmid)) {
            return { managed: false, legacy: false, legacyGpu: false, profile: DISPLAY_PROFILE.virgl,
                rendernode: DEFAULT_RENDER_NODE };
        }
        const virglMatch = args.match(virglDisplayPattern(vmid));
        const cpuMatch = args.match(cpuDisplayPattern(vmid));
        const gpu = virtioVgaGlArguments(args).map(normaliseArgument);
        if (virglMatch && displayCount(args) === 1) {
            if (gpu.length === 1 && gpu[0] === gpuArgument()) {
                return { managed: true, legacy: false, legacyGpu: false, guest: managedGuestChannel(args, vmid),
                    input: managedDirectInput(args), profile: DISPLAY_PROFILE.virgl, rendernode: virglMatch[1] };
            }
            // git20 emitted the unlabelled VirtIO-GPU argument. It is safe to
            // migrate only that exact historical form; any device options or
            // a second adapter belong to an administrator and must not be
            // silently claimed or duplicated by this UI overlay.
            if (gpu.length === 1 && gpu[0] === '-device virtio-vga-gl') {
                return { managed: false, legacy: true, legacyGpu: true, guest: managedGuestChannel(args, vmid),
                    input: managedDirectInput(args), profile: DISPLAY_PROFILE.virgl, rendernode: virglMatch[1] };
            }
            if (gpu.length === 0) {
                return { managed: false, legacy: true, legacyGpu: false, guest: managedGuestChannel(args, vmid),
                    input: managedDirectInput(args), profile: DISPLAY_PROFILE.virgl, rendernode: virglMatch[1] };
            }
        }
        // The stock Standard VGA/non-GL VirtIO adapter is owned by PVE and is
        // not present in `args`. Reject any GL GPU here instead of claiming a
        // foreign adapter while enabling CPU Display1.
        if (cpuMatch && displayCount(args) === 1 && gpu.length === 0) {
            return { managed: true, legacy: false, legacyGpu: false, guest: managedGuestChannel(args, vmid),
                input: managedDirectInput(args), profile: DISPLAY_PROFILE.cpu,
                rendernode: DEFAULT_RENDER_NODE };
        }
        return { managed: false, legacy: false, legacyGpu: false, profile: DISPLAY_PROFILE.virgl,
            rendernode: DEFAULT_RENDER_NODE };
    };
    const removeArgument = (args, argument) => args.replace(
        new RegExp(`(?:^|\\s)${escapeRegExp(argument)}(?=\\s|$)`), '',
    ).trim().replace(/\s{2,}/g, ' ');
    const replaceArgument = (args, previous, wanted) => args.replace(
        new RegExp(`(?:^|\\s)${escapeRegExp(previous)}(?=\\s|$)`), (value) =>
            value.startsWith(' ') ? ` ${wanted}` : wanted);
    const updateDisplayArgument = (args, vmid, profile, rendernode, want) => {
        args = args === undefined || args === null ? '' : args;
        if (typeof args !== 'string' || args.length > MAX_QEMU_ARGS_BYTES || /[\x00-\x1f\x7f]/.test(args)) {
            throw new Error('unsafe QEMU display arguments');
        }
        profile = displayProfile(profile);
        const existing = displayState(args, vmid);
        const count = displayCount(args);
        const gpu = virtioVgaGlArguments(args).map(normaliseArgument);
        const wanted = displayArgument(vmid, profile, rendernode);
        if (want) {
            if (count === 0) {
                if (gpu.length !== 0) { throw new Error('unsafe QEMU Display1 device arguments'); }
                const ownedGpu = profile === DISPLAY_PROFILE.virgl ? `${gpuArgument()} ` : '';
                const added = `${ownedGpu}${wanted} ${guestArguments(vmid).join(' ')} ${DIRECT_INPUT_ARGUMENTS.join(' ')}`;
                return args ? `${args} ${added}` : added;
            }
            if (!existing.managed && !existing.legacy) {
                throw new Error('another QEMU display is configured');
            }
            const previous = displayArgument(vmid, existing.profile, existing.rendernode);
            let updated = replaceArgument(args, previous, wanted);
            if (profile === DISPLAY_PROFILE.cpu) {
                if (existing.profile === DISPLAY_PROFILE.virgl) {
                    updated = existing.legacyGpu
                        ? updated.replace(/(?:^|\s)-device\s+virtio-vga-gl(?=\s|$)/, '')
                        : removeArgument(updated, gpuArgument());
                }
                return addManagedDirectInput(addManagedGuestChannel(updated, vmid));
            }
            if (existing.profile === DISPLAY_PROFILE.cpu) {
                return addManagedDirectInput(addManagedGuestChannel(`${updated} ${gpuArgument()}`, vmid));
            }
            if (existing.managed) { return addManagedDirectInput(addManagedGuestChannel(updated, vmid)); }
            if (existing.legacyGpu) {
                return addManagedDirectInput(addManagedGuestChannel(updated.replace(
                    /(?:^|\s)-device\s+virtio-vga-gl(?=\s|$)/,
                    (value) => value.startsWith(' ') ? ` ${gpuArgument()}` : gpuArgument(),
                ), vmid));
            }
            return addManagedDirectInput(addManagedGuestChannel(`${updated} ${gpuArgument()}`, vmid));
        }
        if (!existing.managed && !existing.legacy) {
            if (gpu.length !== 0) { throw new Error('unsafe QEMU Display1 device arguments'); }
            return args;
        }
        const previous = displayArgument(vmid, existing.profile, existing.rendernode);
        const withoutDisplay = removeArgument(args, previous);
        if (existing.profile === DISPLAY_PROFILE.cpu) {
            return removeManagedDirectInput(removeManagedGuestChannel(withoutDisplay, vmid));
        }
        const withoutGpu = existing.legacyGpu
            ? withoutDisplay.replace(/(?:^|\s)-device\s+virtio-vga-gl(?=\s|$)/, '')
            : removeArgument(withoutDisplay, gpuArgument());
        return removeManagedDirectInput(removeManagedGuestChannel(withoutGpu, vmid));
    };

    const stockVgaType = (value) => {
        if (typeof value !== 'string' || !value) { return 'none'; }
        const typed = /(?:^|,)type=([a-z0-9-]+)/.exec(value);
        return typed ? typed[1] : value.split(',', 1)[0];
    };
    const effectiveDisplayAdapter = (active, profile, vga) => {
        if (!active) { return gettext('PVE Graphic card: ') + stockVgaType(vga || 'none'); }
        if (profile === DISPLAY_PROFILE.virgl) {
            return gettext('QSM VirtIO-GPU (VirGL, GL) — PVE Graphic card is None');
        }
        const type = stockVgaType(vga || 'none');
        if (type === 'virtio') { return gettext('PVE VirtIO-GPU + QSM Display1 (CPU, no GL)'); }
        if (type === 'vmware') { return gettext('PVE VMware SVGA + QSM Display1 (CPU, no GL)'); }
        return gettext('PVE Standard VGA + QSM Display1 (CPU, no GL)');
    };
    const updateDisplayFields = (panel, active, profile, vga) => {
        const rendernode = panel && panel.down('[name=qsm_direct_rendernode]');
        if (rendernode) { rendernode.setDisabled(!active || profile !== DISPLAY_PROFILE.virgl); }
        const adapter = panel && panel.down('[name=qsm_direct_effective_adapter]');
        if (adapter) {
            const type = vga || (panel.down('[name=type]') && panel.down('[name=type]').getValue());
            adapter.setValue(effectiveDisplayAdapter(active, profile, type));
        }
    };

    const displayFields = () => [
        {
            xtype: 'proxmoxcheckbox', name: 'qsm_direct_display1', uncheckedValue: 0,
            defaultValue: 0, deleteDefaultValue: true, fieldLabel: gettext('QSM Display1'),
            boxLabel: gettext('Enable the private D-Bus Display1 browser console'),
            listeners: { change: function (_field, value) {
                const panel = this.up('inputpanel');
                const profileField = panel && panel.down('[name=qsm_direct_profile]');
                updateDisplayFields(panel, enabled(value), displayProfile(profileField && profileField.getValue()));
            }},
        },
        {
            xtype: 'combo', name: 'qsm_direct_profile', value: DISPLAY_PROFILE.virgl,
            fieldLabel: gettext('Display1 profile'), queryMode: 'local', editable: false, forceSelection: true,
            displayField: 'label', valueField: 'value',
            store: { fields: ['value', 'label'], data: [
                { value: DISPLAY_PROFILE.virgl, label: gettext('VirGL GPU (GL)') },
                { value: DISPLAY_PROFILE.cpu, label: gettext('CPU — Standard VGA, VirtIO or VMware (no GL)') },
            ] },
            listeners: { change: function (_field, value) {
                const panel = this.up('inputpanel');
                const enabledField = panel && panel.down('[name=qsm_direct_display1]');
                updateDisplayFields(panel, enabled(enabledField && enabledField.getValue()), displayProfile(value));
            }},
        },
        {
            xtype: 'textfield', name: 'qsm_direct_rendernode', value: DEFAULT_RENDER_NODE, disabled: true,
            fieldLabel: gettext('Render node'), allowBlank: false,
            validator: (value) => validRenderNode(value) || gettext('Use a DRM render node, for example /dev/dri/renderD128.'),
        },
        {
            xtype: 'displayfield', name: 'qsm_direct_effective_adapter',
            fieldLabel: gettext('Effective display adapter'),
            value: effectiveDisplayAdapter(false, DISPLAY_PROFILE.virgl, 'none'),
        },
        { xtype: 'displayfield', userCls: 'pmx-hint', value: gettext(
            'VirGL owns a private VirtIO-GPU and saves PVE Graphic card as None, because VNC and GL Display1 are incompatible. CPU Display1 uses gl=off and keeps the selected Standard VGA or non-GL VirtIO adapter, so no render node or host GPU is needed. If an adapter does not implement Display1 resize, QSM keeps its fixed guest scanout connected instead of failing the console. The optional QSM Desktop Agent serial channel provides clipboard integration. Restart the VM after changing this setting.'),
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
            const profile = displayProfile(values.qsm_direct_profile);
            const rendernode = values.qsm_direct_rendernode || DEFAULT_RENDER_NODE;
            if (!edit || !edit.vmconfig || (active && (!validVmid(vmid) ||
                (profile === DISPLAY_PROFILE.virgl && !validRenderNode(rendernode))))) {
                throw new Error('direct Display1 configuration is invalid');
            }
            if (active && profile === DISPLAY_PROFILE.cpu && !CPU_DISPLAY_VGA_TYPES.has(values.type)) {
                throw new Error('CPU Display1 requires PVE Graphic card Standard VGA, VirtIO or VMware');
            }
            // PVE 9 does not populate `memory` for every VGA type. Passing an
            // explicit `undefined` becomes `vga.memory=undefined` at the API
            // boundary, which fails its integer schema validation. Preserve a
            // supplied integer (the ExtJS field may serialize it as a string),
            // but leave the property out when the field is absent.
            // PVE normally appends both egl-headless and VNC for virtio-gl.
            // QEMU rejects VNC next to a GL Display1 backend. VirGL therefore
            // owns its GPU and uses vga=none. The CPU DBus backend is gl=off,
            // works alongside PVE VNC, and preserves std/virtio unchanged.
            const result = { type: active && profile === DISPLAY_PROFILE.virgl ? 'none' : values.type };
            const rawMemory = values.memory;
            if ((!active || profile === DISPLAY_PROFILE.cpu) &&
                rawMemory !== undefined && rawMemory !== null && rawMemory !== '') {
                const memory = Number(rawMemory);
                if (Number.isInteger(memory)) { result.memory = memory; }
            }
            const printed = PVE.Parser.printPropertyString(result, 'type');
            const response = printed ? { vga: printed } : { delete: 'vga' };
            const changed = updateDisplayArgument(edit.vmconfig.args, vmid, profile, rendernode, active);
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
                        qsm_direct_profile: state.profile,
                        qsm_direct_rendernode: state.rendernode,
                        qsm_direct_effective_adapter: effectiveDisplayAdapter(
                            state.managed || state.legacy, state.profile, data && data.vga),
                    });
                    updateDisplayFields(me, state.managed || state.legacy, state.profile, data && data.vga);
                };
                return stockLoad.call(me, chained);
            };
            me.callParent();
            me.load = stockLoad;
        },
    });

    // QEMU key numbers used by the Display1 Keyboard interface. Browser
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
        // Display1 takes QEMU's key number, not a raw set-1 byte stream.
        // The E0 prefix is therefore folded into QEMU's extended number
        // (for example Insert is 0xd2, not the VNC-style 0x152).
        ControlRight: 0x9d, AltRight: 0xb8, MetaLeft: 0x5b, MetaRight: 0x5c,
        Insert: 0xd2, Delete: 0xd3, Home: 0xc7, End: 0xcf, PageUp: 0xc9, PageDown: 0xd1,
        ArrowUp: 0xc8, ArrowLeft: 0xcb, ArrowRight: 0xcd, ArrowDown: 0xd0,
    };
    const qemuKey = (event) => scanCodes[event.code] || extendedScanCodes[event.code] || null;
    // A guest-first console forwards every DOM key the browser actually
    // delivers. These chords are the intentionally local set in client-first
    // mode; shortcuts claimed by an OS/compositor before the browser are
    // documented in the console Settings panel and cannot be recovered here.
    const clientFirstShortcut = (event) => {
        const primary = event.ctrlKey || event.metaKey;
        if (primary && [
            'KeyC', 'KeyV', 'KeyL', 'KeyT', 'KeyW', 'KeyR', 'KeyN', 'KeyP', 'KeyF',
            'KeyG', 'KeyH', 'KeyJ', 'KeyK', 'KeyD', 'Equal', 'Minus', 'Digit0',
            'BracketLeft', 'BracketRight',
        ].includes(event.code)) { return true; }
        if (['F5', 'F6', 'F11', 'F12'].includes(event.code)) { return true; }
        if (event.altKey && ['ArrowLeft', 'ArrowRight'].includes(event.code)) { return true; }
        return event.ctrlKey && ['Tab', 'PageUp', 'PageDown'].includes(event.code);
    };
    const guestCaptureExitShortcut = (event) => event.code === 'Escape' &&
        event.ctrlKey && event.altKey && event.shiftKey && !event.metaKey;

    const waitForIce = (peer) => new Promise((resolve) => {
        if (peer.iceGatheringState === 'complete') { resolve(); return; }
        const timer = window.setTimeout(resolve, 3000);
        peer.addEventListener('icegatheringstatechange', () => {
            if (peer.iceGatheringState === 'complete') { window.clearTimeout(timer); resolve(); }
        }, { once: true });
    });
    // The SDP exchange and the per-VM codec policy go to the node-local QSM
    // signalling service, not to a PVE API route: PVE has no supported route
    // plug-in ABI, so nothing is registered inside pveproxy/pvedaemon.  The
    // service runs on the same node with the node's own TLS certificate and
    // authorises each request by relaying this browser's PVE ticket to the
    // node's own /access/ticket endpoint.  The ticket travels in an
    // Authorization header (never the URL) and the endpoints take no ambient
    // cookie, so they are CSRF-safe.  waitMsgTarget spinners are handled by
    // the callers; these helpers only perform the cross-origin request.
    const SIGNAL_PORT = 8007;
    const signalRequest = async (method, path, params) => {
        const cookieName = (window.Proxmox && Proxmox.Setup && Proxmox.Setup.auth_cookie_name)
            || 'PVEAuthCookie';
        const ticket = Ext.util.Cookies.get(cookieName);
        const user = window.Proxmox && Proxmox.UserName;
        if (!ticket || !user) { throw new Error('not authenticated'); }
        const headers = { 'Authorization': `Bearer ${ticket}`, 'X-QSM-User': user };
        const options = { method, mode: 'cors', cache: 'no-store', credentials: 'omit', headers };
        if (method !== 'GET') {
            headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(params || {});
        }
        const response = await fetch(`https://${location.hostname}:${SIGNAL_PORT}${path}`, options);
        if (!response.ok) { throw new Error(`signal ${response.status}`); }
        const body = await response.json();
        return body && body.data;
    };
    const api = async (url, params) => {
        const answer = await signalRequest('POST', url, params);
        if (!answer || answer.type !== 'answer' || typeof answer.sdp !== 'string' || answer.sdp.length < 1) {
            throw new Error('invalid direct WebRTC answer');
        }
        return answer;
    };
    // Ordinary PVE reads (VM run state) still use the stock protected API; only
    // the QSM SDP exchange and codec policy use the signalling service above.
    const apiValue = (url, method, params, target) => new Promise((resolve, reject) => {
        Proxmox.Utils.API2Request({ url, method, params, waitMsgTarget: target,
            success: ({ result }) => resolve(result && result.data),
            failure: () => reject(new Error('PVE request failed')),
        });
    });
    const dimensions = (video, fps) => {
        const box = video.getBoundingClientRect();
        const width = Math.max(64, Math.min(16384, Math.floor(box.width / 2) * 2));
        const height = Math.max(64, Math.min(16384, Math.floor(box.height / 2) * 2));
        return { width, height, fps };
    };

    // kind: 'qemu' (a VM's Display1) or 'lxc' (a container's headless
    // display).  It only selects the PVE/signalling URL segment; the terminal
    // service tells the two apart from the node's own guest configs.
    const openConsole = function (button, node, vmid, embeddedFrame = null, kind = 'qemu') {
        const guestKind = kind === 'lxc' ? 'lxc' : 'qemu';
        const embedded = Boolean(embeddedFrame);
        let popup;
        if (embedded) {
            // The ordinary VM Console card is an in-page PVE area, just as
            // noVNC is. An about:blank same-origin frame gives the existing
            // self-contained console DOM a private document and viewport
            // without opening a system popup or expanding PVE's page scope.
            popup = embeddedFrame.contentWindow;
            if (!popup) { return false; }
        } else {
            const windowId = `qsm-direct-${node}-${vmid}-${Date.now()}`;
            // The top Console split-menu remains an optional separate-window
            // action. `window.open` must execute synchronously in that menu
            // click, otherwise browsers treat it as an unsolicited popup.
            popup = window.open('', windowId,
                'popup=yes,width=1280,height=800,resizable=yes,scrollbars=no');
            if (!popup) {
                Ext.Msg.alert(gettext('QSM Direct'), gettext(
                    'The browser blocked the separate console window. Allow popups for this Proxmox site and try again.'));
                return false;
            }
        }
        const closeSurface = () => {
            if (embedded) {
                if (embeddedFrame.parentNode) { embeddedFrame.parentNode.removeChild(embeddedFrame); }
            } else if (!popup.closed) {
                popup.close();
            }
        };
        const document = popup.document;
        document.title = gettext('QSM Direct Console');
        document.documentElement.style.cssText = 'width:100%;height:100%;background:#000';
        // A viewport meta is added only for the separate WINDOW console: there
        // the document IS the top-level page, so width=device-width makes it map
        // 1:1 on a phone.  It must NOT be added for the embedded card iframe —
        // an iframe honours its own viewport meta, so width=device-width there
        // makes the iframe's internal layout viewport differ from the iframe
        // element's size, and getBoundingClientRect() then no longer matches the
        // touch clientX, sending the guest the wrong coordinates.
        if (!embedded) {
            try {
                let viewportMeta = document.querySelector('meta[name="viewport"]');
                if (!viewportMeta) {
                    viewportMeta = document.createElement('meta');
                    viewportMeta.setAttribute('name', 'viewport');
                    (document.head || document.documentElement).appendChild(viewportMeta);
                }
                viewportMeta.setAttribute('content',
                    'width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover');
            } catch (_error) { /* A missing head is non-fatal; desktop is unaffected. */ }
        }
        let browserStorage = null;
        try { browserStorage = popup.localStorage; } catch (_error) { /* Defaults remain available. */ }
        const settings = readConsoleSettings(browserStorage);
        // ---- Touch gesture -> action mapping -----------------------------
        // The single touch behaviour is "direct" (a tap acts where you touch,
        // the guest cursor is shown as an overlay).  Which finger gesture
        // triggers which guest action is configurable (Settings), with working
        // defaults: tap=left click, long-press=right click, 1-finger drag=
        // drag/select, 2-finger drag=scroll, pinch=zoom, 3-finger tap=middle.
        const GESTURE_ACTIONS = ['leftClick', 'rightClick', 'middleClick', 'drag', 'scroll', 'zoom'];
        const GESTURES = ['tap', 'longPress', 'drag1', 'tap2', 'drag2', 'pinch', 'tap3', 'none'];
        const DEFAULT_GESTURE_MAP = { leftClick: 'tap', rightClick: 'longPress', middleClick: 'tap3', drag: 'drag1', scroll: 'drag2', zoom: 'pinch' };
        const gestureMap = { ...DEFAULT_GESTURE_MAP };
        try {
            const raw = browserStorage && browserStorage.getItem('qsm-gestures');
            if (raw) { const p = JSON.parse(raw); for (const a of GESTURE_ACTIONS) { if (GESTURES.includes(p[a])) { gestureMap[a] = p[a]; } } }
        } catch (_error) { /* defaults remain */ }
        let gestureToAction = {};
        const rebuildGestureMap = () => {
            gestureToAction = {};
            for (const a of GESTURE_ACTIONS) { if (gestureMap[a] && gestureMap[a] !== 'none') { gestureToAction[gestureMap[a]] = a; } }
        };
        rebuildGestureMap();
        const saveGestureMap = () => {
            rebuildGestureMap();
            try { if (browserStorage) { browserStorage.setItem('qsm-gestures', JSON.stringify(gestureMap)); } } catch (_error) { /* ignore */ }
        };
        // A media worker is shared by every Console watching the same VM, so
        // its RTP clock cannot be mutated under an existing viewer. Freeze
        // the selected FPS for this session; a changed preference applies
        // when the next fresh worker is launched, as stated in the panel.
        const sessionFps = settings.targetFps;
        // Keep the video viewport equal to the entire popup.  The controls
        // deliberately float above it: a desktop console must not silently
        // lose a row of guest pixels merely because its window has controls.
        document.body.style.cssText = 'width:100vw;height:100vh;min-width:100vw;min-height:100vh;margin:0;position:relative;overflow:hidden;background:#000;color:#fff;font:13px sans-serif';
        const toolbar = document.createElement('div');
        toolbar.setAttribute('aria-label', gettext('Console controls'));
        // The control strip floats over the guest video.  Its container is
        // click-through (pointer-events:none) so moving the mouse across the
        // strip still reaches the guest; only the actual buttons below opt
        // back in (pointer-events:auto).  Otherwise the revealed toolbar left
        // a dead band down the left edge where guest input was swallowed.
        toolbar.style.cssText = 'position:absolute;z-index:10;top:12px;left:0;display:flex;flex-direction:column;align-items:stretch;gap:5px;padding:6px;background:rgba(17,24,39,.88);box-shadow:1px 0 6px rgba(0,0,0,.55);border-radius:0 7px 7px 0;opacity:1;transform:translateX(0);transition:opacity .16s ease,transform .16s ease;pointer-events:none';
        // Always-visible reveal handle: the toolbar reveals on mouse hover, but
        // touchscreens have no hover, so on a tablet/phone the controls (⌨
        // keyboard, settings, clipboard) would be unreachable once the toolbar
        // auto-hid.  This small handle toggles it and works with a finger.
        const revealHandle = document.createElement('button');
        revealHandle.type = 'button';
        revealHandle.textContent = '☰';
        revealHandle.setAttribute('aria-label', gettext('Show console controls'));
        revealHandle.title = gettext('Show console controls');
        revealHandle.style.cssText = 'position:fixed;z-index:11;left:0;top:50%;transform:translateY(-50%);width:26px;height:52px;padding:0;border:0;border-radius:0 8px 8px 0;cursor:pointer;font-size:16px;line-height:1;color:#e5e7eb;background:rgba(17,24,39,.55);pointer-events:auto';
        const status = document.createElement('span');
        status.textContent = gettext('Connecting…');
        status.style.cssText = 'min-width:0;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
        // Diagnostics toggle: shows the connection/input panel on demand so a
        // client with no picture or no input can be inspected without remote
        // dev tools (mostly phones/tablets/TVs).
        const diagButton = document.createElement('button');
        diagButton.type = 'button';
        diagButton.textContent = '🛈';
        diagButton.setAttribute('aria-label', gettext('Toggle diagnostics'));
        diagButton.title = gettext('Toggle diagnostics');
        diagButton.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:16px;line-height:1;pointer-events:auto';
        diagButton.addEventListener('click', () => { toggleDiag(); });
        // On-screen keyboard for touch devices with no physical keyboard: a
        // focusable off-screen input raises the device keyboard, and its
        // keydown/beforeinput events are translated to guest key scancodes
        // (mobile keyboards mostly emit beforeinput, not keydown with a code).
        const kbButton = document.createElement('button');
        kbButton.type = 'button';
        kbButton.textContent = '⌨';
        kbButton.setAttribute('aria-label', gettext('Toggle on-screen keyboard'));
        kbButton.title = gettext('Toggle on-screen keyboard');
        kbButton.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:16px;line-height:1;pointer-events:auto';
        const kbInput = document.createElement('input');
        kbInput.type = 'text';
        kbInput.setAttribute('autocomplete', 'off');
        kbInput.setAttribute('autocapitalize', 'off');
        kbInput.setAttribute('autocorrect', 'off');
        kbInput.setAttribute('spellcheck', 'false');
        kbInput.setAttribute('aria-hidden', 'true');
        // Kept in the layout and focusable (not display:none, no negative
        // z-index, opacity>0 but ~invisible) so mobile browsers reliably raise
        // their keyboard when it is focused from the ⌨ button's tap gesture.
        kbInput.style.cssText = 'position:fixed;left:2px;bottom:2px;width:16px;height:16px;opacity:0.01;border:0;padding:0;margin:0;background:transparent;color:transparent;caret-color:transparent';
        const fullscreen = document.createElement('button');
        fullscreen.type = 'button';
        fullscreen.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:18px;line-height:1;pointer-events:auto';
        const settingsButton = document.createElement('button');
        settingsButton.type = 'button';
        settingsButton.textContent = '⚙';
        settingsButton.setAttribute('aria-label', gettext('Console settings'));
        settingsButton.title = gettext('Console settings');
        settingsButton.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:18px;line-height:1;pointer-events:auto';
        // Assigned once the WebRTC control channel is created.  A full-screen
        // transition is not consistently reported by ResizeObserver across
        // Chromium/Safari, so this explicit hook is part of the resize
        // contract rather than merely a toolbar-label update.
        let resizeConsole = () => undefined;
        // Browser full-screen transitions can cancel a DOM mouse/key
        // sequence before its corresponding `up` event.  This hook is
        // assigned once the authenticated control channel exists; keeping a
        // harmless early default makes an Escape pressed during connection
        // setup safe too.
        let releaseHeldInput = () => undefined;
        const video = document.createElement('video');
        video.autoplay = true;
        video.playsInline = true;
        // Mobile engines (Android Chrome, Samsung Internet) honour the HTML
        // attributes, not only the properties, and require inline playback to
        // be declared or they refuse to start a muted stream — which showed as
        // a connected console that "receives no guest data" on tablets/phones.
        video.setAttribute('playsinline', '');
        video.setAttribute('webkit-playsinline', '');
        video.setAttribute('autoplay', '');
        video.setAttribute('muted', '');
        // The SDP has an Opus m-line as well as video. Chromium and Safari
        // are allowed to reject an asynchronous, unmuted `play()` in a popup
        // even when that popup itself was opened by a Console-menu gesture.
        // Muting before the first track makes the visual console autoplay
        // deterministically; audio can be enabled explicitly afterwards.
        video.muted = true;
        video.tabIndex = 0;
        // Mobile browsers commonly block even a muted autoplay until a user
        // gesture.  Any press on the console counts as that gesture, so retry
        // play() from the input handlers below; this makes a tap start the
        // video instead of leaving it paused with "no guest data".
        const ensurePlaying = () => { const p = video.play(); if (p && typeof p.catch === 'function') { p.catch(() => undefined); } };
        // Preserve the guest's pixel aspect ratio.  Resize retry/acknowledge
        // below converges Display1 to this exact box; after convergence
        // `contain` occupies it completely without stretching an image just
        // because a user dragged one window edge.
        // `touch-action:none` is essential on touch devices: without it the
        // browser claims a finger drag for panning/zooming the page and never
        // delivers pointer motion to the guest, so nothing can be selected on
        // an Android tablet or phone.  user-select/touch-callout off keeps a
        // long press from starting a browser text selection over the video.
        video.style.cssText = 'position:fixed;inset:0;display:block;width:100vw;height:100vh;max-width:none;max-height:none;background:#000;object-fit:contain;outline:none;touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none';
        // QEMU Display1 supplies the cursor shape and position separately
        // from scanout damage. This canvas is deliberately visual-only: it
        // never participates in hit testing, resize, or input forwarding.
        // The operating system still owns the cursor on the actual browser
        // window frame, while the guest owns it inside this video rectangle.
        const guestCursor = document.createElement('canvas');
        guestCursor.setAttribute('aria-hidden', 'true');
        guestCursor.style.cssText = 'display:none;position:fixed;z-index:5;pointer-events:none;image-rendering:auto';
        // On touch there is no OS cursor to style and VirGL does not draw the
        // cursor into the video, so the guest's own cursor bitmap is shown as
        // this overlay at the guest-reported position (scaled with the content).
        const guestPointer = document.createElement('div');
        guestPointer.setAttribute('aria-hidden', 'true');
        guestPointer.style.cssText = 'display:none;position:fixed;z-index:6;pointer-events:none;background-repeat:no-repeat;background-size:100% 100%;image-rendering:auto;filter:drop-shadow(0 0 1px rgba(0,0,0,.6))';
        const audio = document.createElement('button');
        audio.type = 'button';
        audio.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:16px;line-height:1;pointer-events:auto';
        const setAudioLabel = () => {
            const label = video.muted ? gettext('Enable Audio') : gettext('Mute Audio');
            audio.textContent = video.muted ? '🔇' : '🔊';
            audio.setAttribute('aria-label', label);
            audio.title = label;
        };
        setAudioLabel();
        audio.addEventListener('click', () => {
            video.muted = !video.muted;
            setAudioLabel();
            // A user gesture on this explicit control satisfies the browser
            // audio-autoplay policy without making video startup depend on it.
            video.play().catch(() => undefined);
        });
        // The embedded card console maximises within the page instead of going
        // to exclusive OS full screen: it fills the viewport below and right of
        // its current position, leaving the PVE navigation and header visible.
        let embeddedMaximized = false;
        let embeddedFrameSavedStyle = null;
        const setFullscreenToggle = () => {
            const active = Boolean(document.fullscreenElement) || embeddedMaximized;
            // Keep the compact control's name stable while exposing a real
            // pressed/unpressed state to both assistive technology and CSS.
            fullscreen.textContent = active ? '⤢' : '⛶';
            fullscreen.setAttribute('aria-label', active ? gettext('Exit Full Screen') : gettext('Enter Full Screen'));
            fullscreen.setAttribute('aria-pressed', String(active));
            fullscreen.title = active ? gettext('Exit Full Screen') : gettext('Enter Full Screen');
            fullscreen.style.background = active ? '#1d4ed8' : '';
        };
        setFullscreenToggle();
        const toggleEmbeddedMaximize = () => {
            const frameEl = embeddedFrame;
            if (!frameEl) { return; }
            if (!embeddedMaximized) {
                embeddedFrameSavedStyle = frameEl.getAttribute('style') || '';
                const rect = frameEl.getBoundingClientRect();
                const left = Math.max(0, Math.round(rect.left));
                const top = Math.max(0, Math.round(rect.top));
                // Page-level maximise, not OS full screen: fill the viewport
                // from the console's current top-left to the bottom-right edge,
                // so the PVE chrome above and to the left stays on screen.  The
                // larger iframe drives the guest resize (this console becomes
                // the reference size) without taking over the whole monitor.
                frameEl.style.cssText = `position:fixed;left:${left}px;top:${top}px;` +
                    `width:calc(100vw - ${left}px);height:calc(100vh - ${top}px);` +
                    `border:0;background:#000;z-index:2147483000`;
                embeddedMaximized = true;
            } else {
                frameEl.setAttribute('style', embeddedFrameSavedStyle ||
                    'display:block;width:100%;height:100%;border:0;background:#000');
                embeddedMaximized = false;
            }
            setFullscreenToggle();
            resizeConsole(true);
            placeGuestCursor();
            video.focus({ preventScroll: true });
        };
        fullscreen.addEventListener('click', () => {
            // The embedded card maximises within the page; only the separate
            // window console uses exclusive OS full screen.
            if (embedded) { toggleEmbeddedMaximize(); return; }
            const action = document.fullscreenElement
                ? document.exitFullscreen()
                : document.documentElement.requestFullscreen();
            if (action && typeof action.catch === 'function') { action.catch(() => undefined); }
        });
        let restoreClientFocusAfterFullscreen = false;
        document.addEventListener('fullscreenchange', () => {
            releaseHeldInput();
            updateGuestKeyboardLock();
            setFullscreenToggle();
            resizeConsole(true);
            // Safari and Chromium can leave the video without focus after
            // Escape restores the native popup.  Re-focus after the browser
            // has completed its layout transition, otherwise the pointer can
            // still move while clicks and keys go to the parent page.
            popup.setTimeout(() => {
                if (restoreClientFocusAfterFullscreen) {
                    restoreClientFocusAfterFullscreen = false;
                    settingsButton.focus({ preventScroll: true });
                } else {
                    video.focus({ preventScroll: true });
                }
            }, 0);
        });
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.textContent = '⧉';
        copy.setAttribute('aria-label', gettext('Copy guest clipboard to this browser'));
        copy.title = gettext('Copy guest clipboard to this browser');
        copy.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:18px;line-height:1;pointer-events:auto';
        const paste = document.createElement('button');
        paste.type = 'button';
        paste.textContent = '⇩';
        paste.setAttribute('aria-label', gettext('Paste this browser clipboard into the guest'));
        paste.title = gettext('Paste this browser clipboard into the guest');
        paste.style.cssText = 'width:32px;height:30px;padding:0;cursor:pointer;font-size:18px;line-height:1;pointer-events:auto';
        const settingsPanel = document.createElement('aside');
        settingsPanel.setAttribute('aria-label', gettext('Console settings'));
        settingsPanel.style.cssText = 'display:none;position:absolute;z-index:21;left:12px;top:52px;width:min(440px,calc(100% - 24px));max-height:calc(100% - 64px);overflow:auto;box-sizing:border-box;padding:12px;border:1px solid rgba(148,163,184,.55);border-radius:8px;background:rgba(15,23,42,.97);box-shadow:0 8px 28px rgba(0,0,0,.65);color:#f8fafc';
        const settingsTitle = document.createElement('div');
        settingsTitle.textContent = gettext('Console settings');
        settingsTitle.style.cssText = 'font-weight:600;font-size:15px;margin:0 0 8px';
        const settingsHint = document.createElement('p');
        settingsHint.textContent = gettext('Display controls are saved only in this browser. The VM media policy is saved on this Proxmox node and is applied after this console is closed and reopened.');
        settingsHint.style.cssText = 'margin:0 0 10px;color:#cbd5e1;line-height:1.35';
        const mediaPolicyTitle = document.createElement('div');
        mediaPolicyTitle.textContent = gettext('This virtual machine — media policy');
        mediaPolicyTitle.style.cssText = 'font-weight:600;margin:0 0 8px';
        const mediaPolicyHint = document.createElement('p');
        mediaPolicyHint.textContent = gettext('Automatic prefers HEVC when this browser offers WebRTC H.265 and this node has a tested hardware encoder, otherwise H.264. Verified with Chrome on Apple silicon and hevc_nvenc. Hardware mode never silently falls back to CPU.');
        mediaPolicyHint.style.cssText = 'margin:0 0 8px;color:#cbd5e1;line-height:1.35';
        const mediaPolicyForm = document.createElement('div');
        mediaPolicyForm.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) 170px;gap:8px;align-items:center';
        const codecLabel = document.createElement('label');
        codecLabel.htmlFor = 'qsm-direct-vm-codec';
        codecLabel.textContent = gettext('Video codec');
        const codecInput = document.createElement('select');
        codecInput.id = 'qsm-direct-vm-codec';
        codecInput.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
        const autoCodecOption = document.createElement('option');
        autoCodecOption.value = 'auto';
        autoCodecOption.textContent = gettext('Automatic (HEVC when supported)');
        const h264Option = document.createElement('option');
        h264Option.value = 'h264';
        h264Option.textContent = gettext('H.264 (browser WebRTC)');
        const hevcOption = document.createElement('option');
        hevcOption.value = 'hevc';
        hevcOption.textContent = gettext('HEVC (hardware encoder required)');
        codecInput.append(autoCodecOption, h264Option, hevcOption);
        const encoderLabel = document.createElement('label');
        encoderLabel.htmlFor = 'qsm-direct-vm-encoder';
        encoderLabel.textContent = gettext('Encoder');
        const encoderInput = document.createElement('select');
        encoderInput.id = 'qsm-direct-vm-encoder';
        encoderInput.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
        for (const [value, label] of [
            ['auto', gettext('Automatic (tested backend)')],
            ['hardware', gettext('Hardware only')],
            ['software', gettext('Software (H.264 libx264)')],
        ]) {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = label;
            encoderInput.append(option);
        }
        mediaPolicyForm.append(codecLabel, codecInput, encoderLabel, encoderInput);
        const saveMediaPolicy = document.createElement('button');
        saveMediaPolicy.type = 'button';
        saveMediaPolicy.textContent = gettext('Save VM media policy');
        saveMediaPolicy.style.cssText = 'margin:0 0 14px;padding:4px 9px;cursor:pointer';
        const keyboardPolicyTitle = document.createElement('div');
        keyboardPolicyTitle.textContent = gettext('Keyboard shortcut priority');
        keyboardPolicyTitle.style.cssText = 'font-weight:600;margin:0 0 8px';
        const keyboardPolicyForm = document.createElement('div');
        keyboardPolicyForm.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) 170px;gap:8px;align-items:center';
        const keyboardPriorityLabel = document.createElement('label');
        keyboardPriorityLabel.htmlFor = 'qsm-direct-keyboard-priority';
        keyboardPriorityLabel.textContent = gettext('When guest picture has focus');
        const keyboardPriorityInput = document.createElement('select');
        keyboardPriorityInput.id = 'qsm-direct-keyboard-priority';
        keyboardPriorityInput.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
        for (const [value, label] of [
            [KEYBOARD_PRIORITY.guest, gettext('Guest first — forward received keys')],
            [KEYBOARD_PRIORITY.raw, gettext('Raw input — send each key and mouse press/release')],
            [KEYBOARD_PRIORITY.client, gettext('Client first — browser shortcuts win')],
        ]) {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = label;
            keyboardPriorityInput.append(option);
        }
        keyboardPriorityInput.value = settings.keyboardPriority;
        keyboardPolicyForm.append(keyboardPriorityLabel, keyboardPriorityInput);
        const keyboardPolicyHint = document.createElement('p');
        keyboardPolicyHint.textContent = gettext('Guest first forwards every key event received by this window, including Ctrl/Alt/Meta. To leave guest keyboard capture, press Ctrl+Alt+Shift+Esc; focus moves to Settings. Raw input sends one physical down and up edge for every received keyboard or mouse action and reserves no QSM chord; use the Settings button to return to client controls. In native full screen Chrome locks Escape for both guest modes when the browser supports Keyboard Lock. In client-first mode, browser Copy/Paste, tab/window/navigation, refresh, full-screen and developer-tool shortcuts stay local; all other received keys go to the guest.');
        keyboardPolicyHint.style.cssText = 'margin:8px 0;color:#cbd5e1;line-height:1.35';
        const keyboardUnavailableHint = document.createElement('p');
        keyboardUnavailableHint.textContent = gettext('Always unavailable to a web console when claimed before the browser: macOS Cmd+Tab, Cmd+Space, Cmd+Q, Ctrl+Cmd+Q and Cmd+Option+Esc; Windows Ctrl+Alt+Del and Win+L; compositor Super/secure-attention shortcuts. Browser policy can also reserve its own full-screen or window-management shortcuts.');
        keyboardUnavailableHint.style.cssText = 'margin:0 0 14px;color:#fbbf24;line-height:1.35';
        const settingsForm = document.createElement('div');
        settingsForm.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) 92px;gap:8px;align-items:center';
        const settingLabels = {
            toolbarHotZonePx: gettext('Left activation zone (px)'),
            toolbarNearbyPx: gettext('Controls nearby area (px)'),
            toolbarRevealDelayMs: gettext('Left hold delay (ms)'),
            toolbarHideDelayMs: gettext('Controls hide delay (ms)'),
            targetFps: gettext('Target frame rate (FPS)'),
            playoutDelayMs: gettext('Decoder playout delay (ms)'),
            resizeDebounceMs: gettext('Resize end delay (ms)'),
            resizeSettleMs: gettext('Guest resize settle time (ms)'),
        };
        const settingInputs = new Map();
        for (const [name, definition] of Object.entries(CONSOLE_SETTING_SCHEMA)) {
            const label = document.createElement('label');
            const input = document.createElement('input');
            const inputId = `qsm-direct-setting-${name}`;
            label.htmlFor = inputId;
            label.textContent = settingLabels[name];
            label.style.cssText = 'min-width:0';
            input.id = inputId;
            input.type = 'number';
            input.min = String(definition.minimum);
            input.max = String(definition.maximum);
            input.step = '1';
            input.value = String(settings[name]);
            input.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
            settingsForm.append(label, input);
            settingInputs.set(name, input);
        }
        // Touch gesture assignment: for each guest action, choose which finger
        // gesture triggers it.  Working defaults are pre-selected; a gesture can
        // be used by only one action (picking it elsewhere frees it here).
        const gesturesTitle = document.createElement('div');
        gesturesTitle.textContent = gettext('Touch gestures');
        gesturesTitle.style.cssText = 'font-weight:600;margin:16px 0 8px';
        const gesturesHint = document.createElement('p');
        gesturesHint.textContent = gettext('On a touchscreen the guest cursor is shown and a tap acts where you touch. Assign a gesture to each action; each gesture can drive only one action.');
        gesturesHint.style.cssText = 'margin:0 0 8px;color:#cbd5e1;line-height:1.35';
        const gesturesForm = document.createElement('div');
        gesturesForm.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) 160px;gap:8px;align-items:center';
        const GESTURE_ACTION_LABELS = {
            leftClick: gettext('Left click'),
            rightClick: gettext('Right click'),
            middleClick: gettext('Middle click'),
            drag: gettext('Drag / select'),
            scroll: gettext('Scroll'),
            zoom: gettext('Zoom (magnify)'),
        };
        const GESTURE_LABELS = {
            tap: gettext('1-finger tap'),
            longPress: gettext('Long press'),
            drag1: gettext('1-finger drag'),
            tap2: gettext('2-finger tap'),
            drag2: gettext('2-finger drag'),
            pinch: gettext('Pinch (spread)'),
            tap3: gettext('3-finger tap'),
            none: gettext('(off)'),
        };
        const gestureSelects = new Map();
        const refreshGestureSelects = () => {
            for (const [action, select] of gestureSelects) { select.value = gestureMap[action] || 'none'; }
        };
        for (const action of GESTURE_ACTIONS) {
            const label = document.createElement('label');
            const select = document.createElement('select');
            const inputId = `qsm-direct-gesture-${action}`;
            label.htmlFor = inputId;
            label.textContent = GESTURE_ACTION_LABELS[action] || action;
            label.style.cssText = 'min-width:0';
            select.id = inputId;
            select.style.cssText = 'width:100%;box-sizing:border-box;padding:4px 6px';
            for (const gesture of GESTURES) {
                const option = document.createElement('option');
                option.value = gesture;
                option.textContent = GESTURE_LABELS[gesture] || gesture;
                select.append(option);
            }
            select.value = gestureMap[action] || 'none';
            select.addEventListener('change', () => {
                const chosen = select.value;
                if (chosen !== 'none') {
                    // A gesture drives one action: free it from any other action.
                    for (const other of GESTURE_ACTIONS) {
                        if (other !== action && gestureMap[other] === chosen) { gestureMap[other] = 'none'; }
                    }
                }
                gestureMap[action] = chosen;
                saveGestureMap();
                refreshGestureSelects();
            });
            gesturesForm.append(label, select);
            gestureSelects.set(action, select);
        }
        const resetGestures = document.createElement('button');
        resetGestures.type = 'button';
        resetGestures.textContent = gettext('Reset gesture defaults');
        resetGestures.style.cssText = 'margin:10px 0 4px;padding:4px 9px;cursor:pointer';
        resetGestures.addEventListener('click', () => {
            for (const action of GESTURE_ACTIONS) { gestureMap[action] = DEFAULT_GESTURE_MAP[action] || 'none'; }
            saveGestureMap();
            refreshGestureSelects();
        });
        const settingsActions = document.createElement('div');
        settingsActions.style.cssText = 'display:flex;gap:8px;margin-top:12px;justify-content:flex-end';
        const resetSettings = document.createElement('button');
        resetSettings.type = 'button';
        resetSettings.textContent = gettext('Reset defaults');
        resetSettings.style.cssText = 'padding:4px 9px;cursor:pointer';
        const closeSettings = document.createElement('button');
        closeSettings.type = 'button';
        closeSettings.textContent = gettext('Close');
        closeSettings.style.cssText = 'padding:4px 9px;cursor:pointer';
        settingsActions.append(resetSettings, closeSettings);
        settingsPanel.append(settingsTitle, settingsHint, mediaPolicyTitle, mediaPolicyHint,
            mediaPolicyForm, saveMediaPolicy, keyboardPolicyTitle, keyboardPolicyForm,
            keyboardPolicyHint, keyboardUnavailableHint, gesturesTitle, gesturesHint,
            gesturesForm, resetGestures, settingsForm, settingsActions);
        toolbar.append(status, copy, paste, audio, kbButton, fullscreen, diagButton, settingsButton);
        // On-screen diagnostics for the case a client shows no picture (mostly
        // phones/tablets/TVs where remote logs are unreachable). Hidden until a
        // few seconds pass with no decoded frame, then it prints the video and
        // WebRTC receive state right on the black console so it can be read or
        // photographed. It removes itself as soon as a frame decodes.
        const diag = document.createElement('pre');
        diag.setAttribute('aria-hidden', 'true');
        diag.hidden = true;
        // Top-RIGHT so it never covers the left-edge toolbar/handle buttons
        // (the diagnostics toggle among them), which made it impossible to turn
        // off on a phone.  max-width keeps it off the controls entirely.
        diag.style.cssText = 'position:fixed;right:8px;top:8px;z-index:30;margin:0;max-width:min(70vw,520px);white-space:pre-wrap;word-break:break-word;padding:10px 12px;background:rgba(0,0,0,.82);color:#e5e7eb;font:13px/1.45 monospace;border:1px solid #334155;border-radius:8px;pointer-events:none';
        document.body.append(video, guestCursor, guestPointer, toolbar, settingsPanel, diag, kbInput, revealHandle);
        popup.focus();

        let peer = null;
        let control = null;
        let pointer = null;
        let videoReceiver = null;
        let observer = null;
        let closed = false;
        let closeWatcher = null;
        let pointerFrame = null;
        // Fallback flush timer.  requestAnimationFrame is paused by the browser
        // when this console's window stops compositing — which is exactly what
        // happens to the embedded-card console while a second Console (a
        // separate window, or one in full screen) is on top.  Without a timer
        // racing the frame callback, the occluded console's pointer motion is
        // queued but never delivered, so the guest cursor freezes at the last
        // position sent before the switch.
        let pointerTimer = null;
        let pendingPointer = null;
        let latestPointer = null;
        const heldMouseButtons = new Set();
        const heldKeys = new Set();
        let fullscreenEscapePending = false;
        // `qsm-pointer` is deliberately unordered and non-retransmitted.
        // Preserve an explicit sequence number with every latest-state sample
        // so a packet which took a longer SCTP path cannot move a held window
        // backwards after a newer coordinate already reached the guest.
        let pointerSequence = 0;
        let resizeTimer = null;
        let resizeRetryTimer = null;
        let resizeRetryIdentity = '';
        let resizeRetryAttempts = 0;
        // Set once the fast retry budget is spent without the guest adopting
        // the requested mode; the next wake attempt (input) re-issues it.
        let resizeStalled = false;
        let stalledRetryCount = 0;
        let lastStalledRetryAt = 0;
        let retryStalledResize = () => undefined;
        // True while the status line carries the resize text, so convergence
        // restores "Connected" without overwriting an unrelated message.
        let resizeStatusShown = false;
        // Last moment this Console forwarded pointer or key input.  A black
        // picture right after input is a booting or genuinely black guest,
        // not a sleeping one, so the wake hint is withheld for a few seconds.
        let lastInputSentAt = 0;
        let noteGuestInput = () => undefined;
        let lastResizeSentAt = 0;
        let firstFrameTimer = null;
        let diagTimer = null;
        let diagPinned = false;
        let diagPointerSent = 0;
        let diagButtonSent = 0;
        let diagKeySent = 0;
        let diagLastInput = '';
        let diagLastPtr = '';
        let diagZoom = '1.00';
        let negotiatedVideoCodec = '';
        // Fill the on-screen diagnostics panel from the live video + WebRTC
        // receive state.  Used to debug "connected but no picture" on clients
        // whose console cannot be inspected remotely.
        const updateDiag = async () => {
            if (closed) { return; }
            const lines = [];
            lines.push('QSM console diagnostics');
            lines.push(`conn=${peer ? peer.connectionState : '-'} ice=${peer ? peer.iceConnectionState : '-'} gather=${peer ? peer.iceGatheringState : '-'}`);
            lines.push(`video paused=${video.paused} readyState=${video.readyState} size=${video.videoWidth}x${video.videoHeight}`);
            lines.push(`answer codec=${negotiatedVideoCodec || '?'}`);
            lines.push(`chan control=${control ? control.readyState : '-'} pointer=${pointer ? pointer.readyState : '-'}`);
            lines.push(`input focus=${popup.document.activeElement === video} sent ptr=${diagPointerSent} btn=${diagButtonSent} key=${diagKeySent} last=${diagLastInput || '-'}`);
            lines.push(`touch zoom=${diagZoom} cursor=${usingTouchInput ? 'overlay' : 'os'}`);
            lines.push('ptr ' + (diagLastPtr || '-'));
            // Render the basic lines NOW, before awaiting getStats(): if getStats
            // hangs (it can on some engines) the panel must still update, or it
            // looks frozen and the pointer line can never be read.
            diag.textContent = lines.join('\n');
            try {
                if (peer) {
                    const stats = await peer.getStats();
                    let inbound = null; const codecs = new Map();
                    const cands = new Map(); const pairs = [];
                    stats.forEach((report) => {
                        if (report.type === 'codec') { codecs.set(report.id, report.mimeType); }
                        if (report.type === 'inbound-rtp' && report.kind === 'video') { inbound = report; }
                        if (report.type === 'local-candidate' || report.type === 'remote-candidate') { cands.set(report.id, report); }
                        if (report.type === 'candidate-pair') { pairs.push(report); }
                    });
                    // ICE detail: a "connecting/checking" stall is almost always
                    // a candidate-reachability problem (different subnet/Wi-Fi
                    // isolation), so surface the candidates and pair states.
                    const fmt = (c) => c ? `${c.candidateType||'?'} ${c.address||c.ip||'?'}:${c.port||'?'}/${c.protocol||'?'}` : '?';
                    const locals = [...cands.values()].filter((c) => c.type === 'local-candidate');
                    const remotes = [...cands.values()].filter((c) => c.type === 'remote-candidate');
                    lines.push('local: ' + (locals.map(fmt).join(' | ') || 'none'));
                    lines.push('remote:' + (remotes.map(fmt).join(' | ') || 'none'));
                    if (pairs.length) {
                        lines.push('pairs: ' + pairs.map((p) => `${p.state}${p.nominated ? '*' : ''}`).join(','));
                    }
                    if (inbound) {
                        lines.push(`recv=${inbound.framesReceived || 0} decoded=${inbound.framesDecoded || 0} keyDec=${inbound.keyFramesDecoded || 0} dropped=${inbound.framesDropped || 0}`);
                        lines.push(`asm=${inbound.framesAssembledFromMultiplePackets || 0} pkts=${inbound.packetsReceived || 0} lost=${inbound.packetsLost || 0} bytes=${inbound.bytesReceived || 0}`);
                        lines.push(`pli=${inbound.pliCount || 0} nack=${inbound.nackCount || 0} fir=${inbound.firCount || 0} frame=${inbound.frameWidth || 0}x${inbound.frameHeight || 0}`);
                        lines.push(`decoder=${inbound.decoderImplementation || '?'} power=${inbound.powerEfficientDecoder}`);
                        lines.push(`codec=${codecs.get(inbound.codecId) || inbound.codecId || '?'}`);
                    } else {
                        lines.push('no inbound-rtp video report');
                    }
                }
            } catch (error) { lines.push('getStats: ' + String(error).slice(0, 80)); }
            diag.textContent = lines.join('\n');
        };
        const startDiag = () => {
            if (diagTimer !== null || closed) { return; }
            diag.hidden = false;
            updateDiag();
            diagTimer = popup.setInterval(updateDiag, 1000);
        };
        const stopDiag = () => {
            if (diagPinned) { return; }
            if (diagTimer !== null) { popup.clearInterval(diagTimer); diagTimer = null; }
            diag.hidden = true;
        };
        const toggleDiag = () => {
            diagPinned = !diagPinned;
            if (diagPinned) { startDiag(); }
            else { diagPinned = false; if (diagTimer !== null) { popup.clearInterval(diagTimer); diagTimer = null; } diag.hidden = true; }
        };
        // Guest sleep detection: an all-black decoded picture for longer than
        // GUEST_SLEEP_HINT_MS means the guest output is off, not that the
        // transport failed.  The monitor owns the wake hint and clears it
        // as soon as a non-black frame is decoded again.
        let livenessTimer = null;
        let livenessCanvas = null;
        let livenessContext = null;
        let darkFrameSince = null;
        let guestAsleepHint = false;
        let toolbarTimer = null;
        let toolbarRevealTimer = null;
        let toolbarVisible = true;
        // Pinned by the touch reveal handle: the hover-based auto-hide must not
        // apply on touchscreens, where there is no hover to bring it back.
        let toolbarPinned = false;
        let pointerInToolbarZone = false;
        let pointerInToolbar = false;
        let pointerNearToolbar = false;
        let settingsPanelOpen = false;
        let guestRequestNumber = 0;
        let lastGuestClipboardText = null;
        let guestClipboardEventSequence = 0;
        const guestRequests = new Map();
        const guestClipboardWaiters = new Set();
        const guestCursorShapes = new Map();
        let latestGuestCursor = null;
        let appliedGuestCursor = 'default';
        // ``qsm-guest-cursor`` is intentionally unordered.  A delayed first
        // MouseSet is commonly (0,0); without a sequence guard it could land
        // after the actual pointer position and pin a resize/edge cursor in
        // the top-left corner of the guest image.
        let latestGuestCursorSequence = -1;
        let lastResize = '';
        // The last guest scanout size this console actually saw applied.  It
        // distinguishes "the guest is ignoring my resize (still my previous
        // size)" — which should keep retrying — from "another console resized
        // the shared guest" (a new size that is neither my request nor my
        // previous one) — which this console must FOLLOW rather than fight, so
        // the console that is being stretched stays the reference and does not
        // end up showing a scaled picture because a background console kept
        // re-asserting its own smaller size.
        let lastAppliedSize = '';
        let mediaPolicyLoading = false;
        const cancelToolbarReveal = () => {
            if (toolbarRevealTimer !== null) {
                popup.clearTimeout(toolbarRevealTimer);
                toolbarRevealTimer = null;
            }
        };
        const revealToolbar = () => {
            if (closed) { return; }
            cancelToolbarReveal();
            if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); toolbarTimer = null; }
            toolbarVisible = true;
            toolbar.style.opacity = '1';
            toolbar.style.transform = 'translateX(0)';
        };
        const hideToolbarNow = () => {
            toolbarVisible = false;
            toolbar.style.opacity = '0';
            toolbar.style.transform = 'translateX(-100%)';
        };
        // The handle pins the toolbar open (touch has no hover to re-reveal it);
        // a second tap unpins and hides it, giving the guest the full screen.
        revealHandle.addEventListener('click', () => {
            toolbarPinned = !toolbarPinned;
            if (toolbarPinned) {
                revealToolbar();
                revealHandle.style.background = 'rgba(29,78,216,.9)';
            } else {
                revealHandle.style.background = 'rgba(17,24,39,.55)';
                hideToolbarNow();
            }
        });
        const hideToolbarSoon = () => {
            if (closed || toolbarPinned || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA ||
                pointerNearToolbar || pointerInToolbar || settingsPanelOpen || toolbarTimer !== null) { return; }
            toolbarTimer = popup.setTimeout(() => {
                toolbarTimer = null;
                if (toolbarPinned || pointerNearToolbar || pointerInToolbar || settingsPanelOpen) { return; }
                hideToolbarNow();
            }, settings.toolbarHideDelayMs);
        };
        const observeToolbarZone = (event) => {
            const box = video.getBoundingClientRect();
            const inside = event.clientX >= box.left && event.clientX < box.left + settings.toolbarHotZonePx;
            pointerInToolbarZone = inside;
            // The timeout measures continuous absence from the controls and
            // their immediate vicinity, not absence of mouse events. Moving
            // the pointer around the remote desktop therefore cannot keep a
            // visible toolbar alive indefinitely, while an intentional move
            // toward its left edge never hides it just before a click.
            const near = event.clientX >= box.left &&
                event.clientX <= box.left + toolbar.offsetWidth + settings.toolbarNearbyPx &&
                event.clientY >= box.top + 12 - settings.toolbarNearbyPx &&
                event.clientY <= box.top + 12 + toolbar.offsetHeight + settings.toolbarNearbyPx;
            pointerNearToolbar = near;
            if (near) {
                if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); toolbarTimer = null; }
            }
            if (!inside) {
                cancelToolbarReveal();
                hideToolbarSoon();
                return;
            }
            if (toolbarVisible) {
                if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); toolbarTimer = null; }
                return;
            }
            if (toolbarRevealTimer === null) {
                toolbarRevealTimer = popup.setTimeout(() => {
                    toolbarRevealTimer = null;
                    if (pointerInToolbarZone && !closed) { revealToolbar(); }
                }, settings.toolbarRevealDelayMs);
            }
        };
        toolbar.addEventListener('pointerenter', () => {
            pointerInToolbar = true;
            pointerNearToolbar = true;
            revealToolbar();
        });
        toolbar.addEventListener('pointerleave', () => {
            pointerInToolbar = false;
            pointerNearToolbar = false;
            hideToolbarSoon();
        });
        const send = (value) => {
            if (control && control.readyState === 'open') { control.send(JSON.stringify(value)); }
        };
        const updateGuestKeyboardLock = () => {
            // Native full screen normally lets the browser reserve Escape.
            // Chrome's Keyboard Lock API is the standards-based exception:
            // in either guest-priority mode retain Escape for the VM, so its
            // own shortcut editor sees the real key edge.
            const keyboard = popup.navigator && popup.navigator.keyboard;
            if (!keyboard) { return; }
            if (document.fullscreenElement && settings.keyboardPriority !== KEYBOARD_PRIORITY.client &&
                typeof keyboard.lock === 'function') {
                Promise.resolve(keyboard.lock(['Escape'])).catch(() => undefined);
            } else if (typeof keyboard.unlock === 'function') {
                keyboard.unlock();
            }
        };
        const bytesToB64 = (bytes) => {
            let binary = '';
            const view = new Uint8Array(bytes);
            for (let index = 0; index < view.length; index += 0x8000) {
                binary += String.fromCharCode(...view.subarray(index, index + 0x8000));
            }
            return btoa(binary);
        };
        const b64ToBytes = (encoded) => {
            const binary = atob(encoded);
            const result = new Uint8Array(binary.length);
            for (let index = 0; index < binary.length; index += 1) { result[index] = binary.charCodeAt(index); }
            return result;
        };
        const hideGuestCursor = () => {
            guestCursor.style.display = 'none';
            guestPointer.style.display = 'none';
            if (appliedGuestCursor !== 'default') {
                appliedGuestCursor = 'default';
                video.style.cursor = appliedGuestCursor;
            }
        };
        const guestContentBox = () => {
            const box = video.getBoundingClientRect();
            const sourceWidth = Math.max(1, video.videoWidth || Math.floor(box.width));
            const sourceHeight = Math.max(1, video.videoHeight || Math.floor(box.height));
            const scale = Math.min(box.width / sourceWidth, box.height / sourceHeight);
            return {
                box, sourceWidth, sourceHeight, scale,
                left: box.left + (box.width - sourceWidth * scale) / 2,
                top: box.top + (box.height - sourceHeight * scale) / 2,
            };
        };
        // ---- Client-side zoom / pan (touch) ------------------------------
        // A view transform on the video magnifies part of the guest for
        // precise touch targeting.  transform-origin is 0,0 so the
        // screen<->guest mapping stays analytic (screen = t + scale*local,
        // where local is the object-fit:contain viewport space); the
        // pointer math undoes it.  Off (scale 1) leaves behaviour identical
        // to the un-zoomed console.
        let zoomScale = 1;
        let zoomTx = 0;
        let zoomTy = 0;
        const MIN_ZOOM = 1;
        const MAX_ZOOM = 6;
        // True while the last input came from touch: the guest cursor is then
        // drawn as an on-screen overlay (there is no OS cursor to style, and
        // VirGL does not composite the cursor into the video).
        let usingTouchInput = false;
        // The last guest coordinate we drove the pointer to.  On touch the
        // overlay falls back to a synthetic arrow here whenever the guest has
        // not (yet) streamed its own cursor bitmap, so a cursor is ALWAYS
        // visible where the finger acted, not only once the guest reports.
        let lastSentGuestX = null;
        let lastSentGuestY = null;
        const SYNTHETIC_CURSOR = 'data:image/svg+xml,' + encodeURIComponent(
            "<svg xmlns='http://www.w3.org/2000/svg' width='12' height='19' viewBox='0 0 12 19'>" +
            "<path d='M0 0 L0 16 L4 12 L6.5 17.5 L8.7 16.5 L6.1 11 L11 11 Z' fill='white' stroke='black' stroke-width='1' stroke-linejoin='round'/></svg>");
        const SYNTHETIC_CURSOR_W = 20; // on-screen px (fixed; fallback arrow)
        const SYNTHETIC_CURSOR_H = 31;
        // object-fit:contain letterbox metrics (screen px per guest px = contain).
        const viewMetrics = () => {
            const viewW = popup.innerWidth || video.clientWidth || 1;
            const viewH = popup.innerHeight || video.clientHeight || 1;
            const sw = Math.max(1, video.videoWidth || viewW);
            const sh = Math.max(1, video.videoHeight || viewH);
            const raw = Math.min(viewW / sw, viewH / sh);
            const contain = Number.isFinite(raw) && raw > 0 ? raw : 1;
            return { viewW, viewH, sw, sh, contain, contentLeft: (viewW - sw * contain) / 2, contentTop: (viewH - sh * contain) / 2 };
        };
        // guest px -> on-screen px (inverse of pointerForMouseEvent).
        const guestToScreen = (gx, gy) => {
            const m = viewMetrics();
            const localX = gx * m.contain + m.contentLeft;
            const localY = gy * m.contain + m.contentTop;
            return { x: zoomTx + zoomScale * localX, y: zoomTy + zoomScale * localY };
        };
        const clampZoom = () => {
            const viewW = popup.innerWidth || video.clientWidth || 1;
            const viewH = popup.innerHeight || video.clientHeight || 1;
            zoomScale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoomScale));
            // Keep the (letterboxed) video covering the viewport: no gap.
            zoomTx = Math.max(viewW * (1 - zoomScale), Math.min(0, zoomTx));
            zoomTy = Math.max(viewH * (1 - zoomScale), Math.min(0, zoomTy));
            if (zoomScale <= 1.0001) { zoomScale = 1; zoomTx = 0; zoomTy = 0; }
        };
        const applyViewTransform = () => {
            if (zoomScale === 1 && zoomTx === 0 && zoomTy === 0) {
                video.style.transform = '';
                video.style.transformOrigin = '';
            } else {
                video.style.transformOrigin = '0 0';
                video.style.transform = `translate(${zoomTx}px, ${zoomTy}px) scale(${zoomScale})`;
            }
            diagZoom = zoomScale.toFixed(2);
            placeGuestCursor();
        };
        const zoomAroundPoint = (mx, my, newScale) => {
            const s0 = zoomScale || 1;
            const lx = (mx - zoomTx) / s0;
            const ly = (my - zoomTy) / s0;
            zoomScale = newScale;
            zoomTx = mx - newScale * lx;
            zoomTy = my - newScale * ly;
            clampZoom();
            applyViewTransform();
        };
        // Re-clamp the pan and re-place the guest cursor after the viewport or
        // the guest scanout changes size (full screen, window resize, guest
        // mode switch), so a magnified view never leaves a gap.
        const reflowView = () => { clampZoom(); applyViewTransform(); };
        const placeGuestCursor = () => {
            const state = latestGuestCursor;
            const shape = state && guestCursorShapes.get(state.shape_id);
            const guestReady = !!(state && state.visible && shape && video.videoWidth && video.videoHeight);
            if (usingTouchInput) {
                // Touch: there is no OS cursor to style and VirGL does not draw
                // the cursor into the video, so ALWAYS render a cursor overlay
                // where the pointer is.  Prefer the guest's own bitmap at its
                // reported position; fall back to a synthetic arrow at the last
                // position we drove the pointer to, so a cursor is never missing
                // while the guest has not streamed its shape yet.
                guestCursor.style.display = 'none';
                const scale = viewMetrics().contain * zoomScale;
                let img;
                let boxW;
                let boxH;
                let offX;
                let offY;
                let gxp;
                let gyp;
                if (guestReady) {
                    img = shape.cursor_url;
                    boxW = Math.max(1, shape.width * scale);
                    boxH = Math.max(1, shape.height * scale);
                    offX = shape.hotspot_x * scale;
                    offY = shape.hotspot_y * scale;
                    gxp = state.x; gyp = state.y;
                } else if (lastSentGuestX !== null && video.videoWidth) {
                    img = SYNTHETIC_CURSOR;
                    boxW = SYNTHETIC_CURSOR_W;      // fixed on-screen size: the
                    boxH = SYNTHETIC_CURSOR_H;      // fallback is an approximation.
                    offX = 0; offY = 0;             // tip at top-left.
                    gxp = lastSentGuestX; gyp = lastSentGuestY;
                } else {
                    guestPointer.style.display = 'none';
                    return;
                }
                const hot = guestToScreen(gxp, gyp);
                guestPointer.style.backgroundImage = `url("${img}")`;
                guestPointer.style.width = `${boxW}px`;
                guestPointer.style.height = `${boxH}px`;
                guestPointer.style.left = `${hot.x - offX}px`;
                guestPointer.style.top = `${hot.y - offY}px`;
                guestPointer.style.display = 'block';
                if (appliedGuestCursor !== 'default') { appliedGuestCursor = 'default'; video.style.cursor = 'default'; }
                return;
            }
            // Desktop: the browser/OS draws the cursor from CSS.  A canvas
            // overlay was avoided here because it visibly jumps at a native
            // window edge when only one guest pixel moves; the OS cursor does not.
            guestPointer.style.display = 'none';
            if (!guestReady) { hideGuestCursor(); return; }
            guestCursor.style.display = 'none';
            const cursor = `url("${shape.cursor_url}") ${shape.hotspot_x} ${shape.hotspot_y}, default`;
            if (cursor !== appliedGuestCursor) {
                appliedGuestCursor = cursor;
                video.style.cursor = cursor;
            }
        };
        const acceptGuestCursorShape = (message) => {
            if (!Number.isSafeInteger(message.shape_id) || message.shape_id <= 0 ||
                !Number.isInteger(message.width) || !Number.isInteger(message.height) ||
                !Number.isInteger(message.hotspot_x) || !Number.isInteger(message.hotspot_y) ||
                message.width < 1 || message.width > 64 || message.height < 1 || message.height > 64 ||
                message.hotspot_x < 0 || message.hotspot_x >= message.width ||
                message.hotspot_y < 0 || message.hotspot_y >= message.height ||
                typeof message.bgra_b64 !== 'string') { return; }
            let bgra;
            try { bgra = b64ToBytes(message.bgra_b64); } catch (_error) { return; }
            if (bgra.length !== message.width * message.height * 4) { return; }
            guestCursor.width = message.width;
            guestCursor.height = message.height;
            const context = guestCursor.getContext('2d', { alpha: true });
            if (!context) { return; }
            const rgba = context.createImageData(message.width, message.height);
            // Display1's pixman ARGB word is stored as BGRA bytes on the
            // little-endian PVE hosts we support. Canvas expects RGBA.
            for (let index = 0; index < bgra.length; index += 4) {
                rgba.data[index] = bgra[index + 2];
                rgba.data[index + 1] = bgra[index + 1];
                rgba.data[index + 2] = bgra[index];
                rgba.data[index + 3] = bgra[index + 3];
            }
            context.putImageData(rgba, 0, 0);
            guestCursorShapes.set(message.shape_id, {
                width: message.width, height: message.height,
                hotspot_x: message.hotspot_x, hotspot_y: message.hotspot_y,
                cursor_url: guestCursor.toDataURL('image/png'),
            });
            // Keep a bounded cache in case a compositor switches cursor
            // types while a late, unordered position is in flight.
            while (guestCursorShapes.size > 4) {
                guestCursorShapes.delete(guestCursorShapes.keys().next().value);
            }
            placeGuestCursor();
        };
        const acceptGuestCursorPosition = (message) => {
            if (!Number.isSafeInteger(message.sequence) || !Number.isSafeInteger(message.shape_id) ||
                !Number.isInteger(message.x) || !Number.isInteger(message.y) ||
                typeof message.visible !== 'boolean') { return; }
            if (message.sequence < latestGuestCursorSequence) { return; }
            latestGuestCursorSequence = message.sequence;
            latestGuestCursor = {
                sequence: message.sequence, shape_id: message.shape_id,
                visible: message.visible, x: message.x, y: message.y,
            };
            placeGuestCursor();
        };
        const guestRequest = (op, fields = {}) => new Promise((resolve, reject) => {
            if (!control || control.readyState !== 'open') {
                reject(new Error('guest tools are not connected')); return;
            }
            const requestId = `qsm-${Date.now()}-${guestRequestNumber += 1}`;
            const timer = popup.setTimeout(() => {
                guestRequests.delete(requestId);
                reject(new Error('guest tools did not respond'));
            }, 40000);
            guestRequests.set(requestId, { resolve, reject, timer });
            control.send(JSON.stringify({ op, request_id: requestId, ...fields }));
        });
        const applyConsoleSettings = () => {
            writeConsoleSettings(browserStorage, settings);
            updateGuestKeyboardLock();
            if (videoReceiver && 'playoutDelayHint' in videoReceiver) {
                try { videoReceiver.playoutDelayHint = settings.playoutDelayMs / 1000; }
                catch (_error) { /* The browser may clamp an unsupported hint. */ }
            }
            if (toolbarTimer !== null) {
                popup.clearTimeout(toolbarTimer);
                toolbarTimer = null;
                hideToolbarSoon();
            }
        };
        const setSettingsPanelOpen = (open) => {
            settingsPanelOpen = open;
            settingsPanel.style.display = open ? 'block' : 'none';
            if (open) {
                revealToolbar();
                loadVmMediaPolicy();
            } else {
                hideToolbarSoon();
            }
        };
        const commitSetting = (name) => {
            const input = settingInputs.get(name);
            const definition = CONSOLE_SETTING_SCHEMA[name];
            if (!input || !definition) { return; }
            const value = Number(input.value);
            if (!Number.isInteger(value) || value < definition.minimum || value > definition.maximum) {
                input.value = String(settings[name]);
                return;
            }
            settings[name] = value;
            applyConsoleSettings();
        };
        for (const [name, input] of settingInputs) {
            input.addEventListener('change', () => commitSetting(name));
        }
        keyboardPriorityInput.addEventListener('change', () => {
            const value = keyboardPriorityInput.value;
            if (!Object.values(KEYBOARD_PRIORITY).includes(value)) {
                keyboardPriorityInput.value = settings.keyboardPriority;
                return;
            }
            settings.keyboardPriority = value;
            applyConsoleSettings();
        });
        resetSettings.addEventListener('click', () => {
            Object.assign(settings, defaultConsoleSettings());
            for (const [name, input] of settingInputs) { input.value = String(settings[name]); }
            keyboardPriorityInput.value = settings.keyboardPriority;
            applyConsoleSettings();
        });
        settingsButton.addEventListener('click', () => setSettingsPanelOpen(!settingsPanelOpen));
        closeSettings.addEventListener('click', () => setSettingsPanelOpen(false));
        const vmMediaPolicyUrl = () => `/nodes/${encodeURIComponent(node)}/${guestKind}/${encodeURIComponent(vmid)}/qsm-direct-settings`;
        const validVmMediaPolicy = (value) => value && ['auto', 'h264', 'hevc'].includes(value.codec) &&
            ['auto', 'hardware', 'software'].includes(value.encoder);
        const loadVmMediaPolicy = async () => {
            if (mediaPolicyLoading || closed) { return; }
            mediaPolicyLoading = true;
            codecInput.disabled = true;
            encoderInput.disabled = true;
            saveMediaPolicy.disabled = true;
            try {
                const value = await signalRequest('GET', vmMediaPolicyUrl(), {});
                if (!validVmMediaPolicy(value)) { throw new Error('invalid VM media policy'); }
                codecInput.value = value.codec;
                encoderInput.value = value.encoder;
            } catch (_error) {
                // A Console-only user may read an older node during a rolling
                // upgrade. Keep the active console usable and make the policy
                // failure explicit instead of pretending a browser-local value
                // was saved to the VM.
                status.textContent = gettext('VM media policy is unavailable on this node.');
            } finally {
                mediaPolicyLoading = false;
                codecInput.disabled = false;
                encoderInput.disabled = false;
                saveMediaPolicy.disabled = false;
            }
        };
        saveMediaPolicy.addEventListener('click', async () => {
            if (mediaPolicyLoading || !['auto', 'h264', 'hevc'].includes(codecInput.value) ||
                !['auto', 'hardware', 'software'].includes(encoderInput.value) ||
                (codecInput.value === 'hevc' && encoderInput.value === 'software')) { return; }
            mediaPolicyLoading = true;
            codecInput.disabled = true;
            encoderInput.disabled = true;
            saveMediaPolicy.disabled = true;
            try {
                const value = await signalRequest('PUT', vmMediaPolicyUrl(), {
                    codec: codecInput.value, encoder: encoderInput.value,
                });
                if (!validVmMediaPolicy(value)) { throw new Error('invalid saved VM media policy'); }
                encoderInput.value = value.encoder;
                status.textContent = gettext('VM media policy saved. Close and reopen this console to apply it.');
            } catch (_error) {
                status.textContent = gettext('Could not save VM media policy. You need VM configuration permission.');
            } finally {
                mediaPolicyLoading = false;
                codecInput.disabled = false;
                encoderInput.disabled = false;
                saveMediaPolicy.disabled = false;
            }
        });
        const copyToBrowser = async (text) => {
            if (!popup.navigator.clipboard || !popup.navigator.clipboard.writeText) {
                throw new Error('browser clipboard access is unavailable');
            }
            await popup.navigator.clipboard.writeText(text);
        };
        const guestClipboardText = async () => {
            const result = await guestRequest('qsm_guest_clipboard_get');
            if (!result || typeof result.text_b64 !== 'string') {
                throw new Error('invalid guest clipboard');
            }
            return new TextDecoder('utf-8', { fatal: true }).decode(b64ToBytes(result.text_b64));
        };
        const waitForNextGuestClipboard = () => new Promise((resolve, reject) => {
            const baseline = guestClipboardEventSequence;
            const waiter = { baseline, resolve, reject, timer: null };
            // This is a fail-safe for an unavailable desktop session, not a
            // propagation delay. A normal Copy resolves from the guest's
            // concrete clipboard-change event as soon as it is emitted.
            waiter.timer = popup.setTimeout(() => {
                guestClipboardWaiters.delete(waiter);
                reject(new Error('guest clipboard did not change'));
            }, 5000);
            guestClipboardWaiters.add(waiter);
        });
        const resolveGuestClipboardWaiters = (text) => {
            guestClipboardEventSequence += 1;
            for (const waiter of [...guestClipboardWaiters]) {
                if (guestClipboardEventSequence <= waiter.baseline) { continue; }
                guestClipboardWaiters.delete(waiter);
                popup.clearTimeout(waiter.timer);
                waiter.resolve(text);
            }
        };
        const sendGuestChord = (modifiers, key) => {
            // Send an explicit physical chord.  The reliable control channel
            // preserves these edges, while the worker serializes them to the
            // Display1 keyboard endpoint.
            for (const modifier of modifiers) {
                send({ op: 'keyboard', key: modifier, down: true, modifiers: 0 });
            }
            send({ op: 'keyboard', key, down: true, modifiers: 0 });
            send({ op: 'keyboard', key, down: false, modifiers: 0 });
            for (const modifier of [...modifiers].reverse()) {
                send({ op: 'keyboard', key: modifier, down: false, modifiers: 0 });
            }
        };
        const sendGuestCopyShortcut = () => sendGuestChord([29], 46); // Ctrl+C
        // Shift+Insert is the standard Linux clipboard paste accelerator in
        // both graphical editors and terminal emulators.  Ctrl+V is a literal
        // control character in Konsole/xterm (shown as ^V), so it cannot be
        // used by a universal remote-desktop Paste action.
        const sendGuestPasteShortcut = () => sendGuestChord([42], 0xd2); // Shift+Insert
        const writePendingClipboard = (textPromise) => {
            if (!popup.navigator.clipboard) {
                return Promise.reject(new Error('browser clipboard access is unavailable'));
            }
            // Clipboard permission is tied to the *initial* click/keydown.
            // Calling writeText only after a WebRTC guest round-trip loses
            // that user activation in Chromium. ClipboardItem accepts a
            // promise, so authorize the write now and resolve its contents
            // only once the guest agent returns the UTF-8 text.
            if (popup.navigator.clipboard.write && typeof popup.ClipboardItem === 'function') {
                const item = new popup.ClipboardItem({
                    'text/plain': Promise.resolve(textPromise).then((text) => new Blob([text], {
                        type: 'text/plain;charset=utf-8',
                    })),
                });
                return popup.navigator.clipboard.write([item]);
            }
            // Older engines may allow writeText after an asynchronous click;
            // retain it as a portable fallback, but never claim that a denied
            // browser permission reached the local clipboard.
            return Promise.resolve(textPromise).then(copyToBrowser);
        };
        const pasteFromBrowser = async () => {
            if (!popup.navigator.clipboard || !popup.navigator.clipboard.readText) {
                throw new Error('browser clipboard access is unavailable');
            }
            const text = await popup.navigator.clipboard.readText();
            return pasteTextIntoGuest(text);
        };
        const pasteTextIntoGuest = async (text) => {
            if (typeof text !== 'string') { throw new Error('browser clipboard is unavailable'); }
            const result = await guestRequest('qsm_guest_clipboard_set', {
                text_b64: bytesToB64(new TextEncoder().encode(text)),
            });
            if (!result || result.applied !== true) {
                throw new Error('guest desktop clipboard bridge is not ready');
            }
            // The result is a concrete acknowledgement from the desktop
            // bridge, not a guessed compositor delay. It is now safe to send
            // the universal Linux Paste accelerator on the ordered channel.
            sendGuestPasteShortcut();
            status.textContent = gettext('Clipboard pasted into guest');
        };
        const guestClipboardToBrowser = async () => {
            await writePendingClipboard(guestClipboardText());
            status.textContent = gettext('Guest clipboard copied');
        };
        const guestSelectionToBrowser = async () => {
            // Start the authorized browser write before asking the guest to
            // Register first, then emit Ctrl+C. The subsequent guest event
            // carries the selection after the desktop broker has actually
            // published it; no compositor-duration timer is involved.
            const text = waitForNextGuestClipboard();
            sendGuestCopyShortcut();
            await writePendingClipboard(text);
            status.textContent = gettext('Guest selection copied');
        };
        copy.addEventListener('click', () => {
            // Toolbar Copy is the same operation as Cmd/Ctrl+C: request the
            // focused guest application's current selection, not an old
            // clipboard value captured before the user made that selection.
            video.focus({ preventScroll: true });
            guestSelectionToBrowser().catch(() => {
                status.textContent = gettext('Guest clipboard is unavailable. Install and start QSM Desktop Agent.');
            });
        });
        paste.addEventListener('click', () => {
            // Retain this click's Clipboard API activation while restoring
            // the guest surface before synthesizing the guest Paste action.
            video.focus({ preventScroll: true });
            pasteFromBrowser().catch(() => {
                status.textContent = gettext('Browser clipboard is unavailable.');
            });
        });
        const sendPointer = (value) => {
            if (pointer && pointer.readyState === 'open') { pointer.send(JSON.stringify(value)); diagPointerSent += 1; diagLastInput = value.op || 'pointer'; }
        };
        const sendMouseButton = (button, down) => {
            if (!Number.isInteger(button) || button < 1 || button > 5) { return; }
            noteGuestInput();
            if (down) {
                if (heldMouseButtons.has(button)) { return; }
                heldMouseButtons.add(button);
            } else {
                if (!heldMouseButtons.has(button)) { return; }
                heldMouseButtons.delete(button);
            }
            send({ op: 'mouse_button', button, down });
            diagButtonSent += 1; diagLastInput = 'button' + button + (down ? '↓' : '↑');
        };
        const sendKeyboard = (key, down) => {
            if (!Number.isInteger(key) || key < 0 || key > 0xffff) { return; }
            noteGuestInput();
            if (down) {
                if (heldKeys.has(key)) { return; }
                heldKeys.add(key);
            } else {
                if (!heldKeys.has(key)) { return; }
                heldKeys.delete(key);
            }
            send({ op: 'keyboard', key, down, modifiers: 0 });
            diagKeySent += 1; diagLastInput = 'key' + key + (down ? '↓' : '↑');
        };
        // Map a printable character to a US-layout scancode + shift flag, so
        // the on-screen keyboard (which reports typed text, not key codes) can
        // drive the guest.  Built once from the shared scanCodes table.
        const charToKey = (() => {
            const map = new Map();
            const put = (ch, code, shift) => { if (scanCodes[code] !== undefined) { map.set(ch, { key: scanCodes[code], shift: !!shift }); } };
            for (let c = 97; c <= 122; c += 1) { const l = String.fromCharCode(c); put(l, 'Key' + l.toUpperCase(), false); put(l.toUpperCase(), 'Key' + l.toUpperCase(), true); }
            const digits = '0123456789'; for (let i = 0; i < 10; i += 1) { put(digits[i], 'Digit' + digits[i], false); }
            [[')', 'Digit0'], ['!', 'Digit1'], ['@', 'Digit2'], ['#', 'Digit3'], ['$', 'Digit4'], ['%', 'Digit5'], ['^', 'Digit6'], ['&', 'Digit7'], ['*', 'Digit8'], ['(', 'Digit9']].forEach(([ch, code]) => put(ch, code, true));
            [[' ', 'Space'], ['-', 'Minus'], ['=', 'Equal'], ['[', 'BracketLeft'], [']', 'BracketRight'], [';', 'Semicolon'], ["'", 'Quote'], ['`', 'Backquote'], ['\\', 'Backslash'], [',', 'Comma'], ['.', 'Period'], ['/', 'Slash']].forEach(([ch, code]) => put(ch, code, false));
            [['_', 'Minus'], ['+', 'Equal'], ['{', 'BracketLeft'], ['}', 'BracketRight'], [':', 'Semicolon'], ['"', 'Quote'], ['~', 'Backquote'], ['|', 'Backslash'], ['<', 'Comma'], ['>', 'Period'], ['?', 'Slash']].forEach(([ch, code]) => put(ch, code, true));
            return map;
        })();
        const shiftKey = scanCodes.ShiftLeft;
        // Type one character into the guest as a press/release, wrapping it in a
        // Shift press/release when the US layout needs Shift for that glyph.
        const sendChar = (ch) => {
            const mapped = charToKey.get(ch);
            if (!mapped) { return; }
            if (mapped.shift) { sendKeyboard(shiftKey, true); }
            sendKeyboard(mapped.key, true);
            sendKeyboard(mapped.key, false);
            if (mapped.shift) { sendKeyboard(shiftKey, false); }
        };
        // Named keys the on-screen keyboard emits by name rather than as text.
        const namedKeyCode = (name) => {
            const table = { Enter: scanCodes.Enter, Backspace: scanCodes.Backspace, Tab: scanCodes.Tab,
                Escape: scanCodes.Escape, ArrowUp: scanCodes.ArrowUp, ArrowDown: scanCodes.ArrowDown,
                ArrowLeft: scanCodes.ArrowLeft, ArrowRight: scanCodes.ArrowRight, ' ': scanCodes.Space,
                Spacebar: scanCodes.Space };
            return table[name];
        };
        const tapKey = (code) => { if (code === undefined) { return; } sendKeyboard(code, true); sendKeyboard(code, false); };
        // Raise/hide the device keyboard by focusing/blurring the hidden input.
        kbButton.addEventListener('click', () => {
            if (popup.document.activeElement === kbInput) { kbInput.blur(); return; }
            kbInput.value = '';
            try { kbInput.focus({ preventScroll: true }); } catch (_error) { kbInput.focus(); }
            kbButton.style.background = '#1d4ed8';
        });
        kbInput.addEventListener('blur', () => { kbButton.style.background = ''; });
        // Physical/BT keyboards and on-screen keys that report a code map
        // straight through; the rest arrive as text via beforeinput below.
        for (const name of ['keydown', 'keyup']) {
            kbInput.addEventListener(name, (event) => {
                const code = scanCodes[event.code] !== undefined ? scanCodes[event.code]
                    : (extendedScanCodes[event.code] !== undefined ? extendedScanCodes[event.code] : null);
                if (code !== null) { sendKeyboard(code, name === 'keydown'); event.preventDefault(); }
                // Keep the field empty so the OS keyboard never shows a caret
                // position that fights composition on the next character.
                kbInput.value = '';
            });
        }
        // beforeinput is the primary path (Gboard/Samsung emit it, cancelable).
        // A suppression flag stops the input fallback below from re-sending the
        // same edit on engines where preventDefault still lets input fire.
        let kbHandledEdit = false;
        const applyEdit = (type, data) => {
            if (type === 'insertText' || type === 'insertCompositionText' || type === 'insertFromComposition') {
                for (const ch of (data || '')) { sendChar(ch); }
            } else if (type === 'deleteContentBackward') { tapKey(scanCodes.Backspace); }
            else if (type === 'deleteContentForward') { tapKey(extendedScanCodes.Delete); }
            else if (type === 'insertLineBreak' || type === 'insertParagraph') { tapKey(scanCodes.Enter); }
            else { return false; }
            return true;
        };
        kbInput.addEventListener('beforeinput', (event) => {
            kbHandledEdit = applyEdit(event.inputType || '', event.data);
            event.preventDefault();
            kbInput.value = '';
        });
        kbInput.addEventListener('input', (event) => {
            if (kbHandledEdit) { kbHandledEdit = false; kbInput.value = ''; return; }
            applyEdit(event.inputType || '', event.data);
            kbInput.value = '';
        });
        releaseHeldInput = () => {
            // Do not rely on the browser to deliver mouseup/keyup when a
            // native full-screen, focus, or close transition interrupts the
            // DOM sequence.  The ordered control channel preserves these
            // release edges ahead of a transport close.
            for (const button of [...heldMouseButtons]) { sendMouseButton(button, false); }
            for (const key of [...heldKeys]) { sendKeyboard(key, false); }
        };
        const leaveGuestKeyboardCapture = () => {
            // The escape chord is intentionally local only in guest-first
            // mode. Releasing modifiers before moving focus prevents a
            // half-held Ctrl/Alt/Shift from affecting the next client action.
            releaseHeldInput();
            if (document.fullscreenElement) {
                // Guest-first deliberately gives ordinary Escape to the VM.
                // The capture-exit chord is therefore also the reliable way
                // to return from a native full-screen guest to client UI.
                restoreClientFocusAfterFullscreen = true;
                const action = document.exitFullscreen();
                if (action && typeof action.catch === 'function') {
                    action.catch(() => {
                        restoreClientFocusAfterFullscreen = false;
                        settingsButton.focus({ preventScroll: true });
                    });
                }
            } else {
                settingsButton.focus({ preventScroll: true });
            }
            status.textContent = gettext('Client keyboard shortcuts active — click the guest picture to resume.');
        };
        const close = () => {
            if (closed) { return; }
            releaseHeldInput();
            closed = true;
            if (observer) { observer.disconnect(); }
            if (peer) { peer.close(); }
            if (closeWatcher !== null) { window.clearInterval(closeWatcher); }
            if (pointerFrame !== null) { popup.cancelAnimationFrame(pointerFrame); }
            if (pointerTimer !== null) { popup.clearTimeout(pointerTimer); }
            if (resizeTimer !== null) { popup.clearTimeout(resizeTimer); }
            if (resizeRetryTimer !== null) { popup.clearTimeout(resizeRetryTimer); }
            if (firstFrameTimer !== null) { popup.clearTimeout(firstFrameTimer); }
            if (livenessTimer !== null) { popup.clearInterval(livenessTimer); }
            if (diagTimer !== null) { popup.clearInterval(diagTimer); diagTimer = null; }
            if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); }
            if (toolbarRevealTimer !== null) { popup.clearTimeout(toolbarRevealTimer); }
            for (const request of guestRequests.values()) {
                popup.clearTimeout(request.timer);
                request.reject(new Error('console closed'));
            }
            guestRequests.clear();
            for (const waiter of guestClipboardWaiters) {
                popup.clearTimeout(waiter.timer);
                waiter.reject(new Error('console closed'));
            }
            guestClipboardWaiters.clear();
        };
        const closeForStoppedVm = () => {
            if (closed) { return; }
            status.textContent = gettext('The virtual machine was stopped. Closing console…');
            // A state transition is dispatched while the peer is processing
            // transport shutdown. Defer the actual close one task so browsers
            // consistently complete that transition before the popup goes.
            window.setTimeout(() => {
                close();
                closeSurface();
            }, 0);
        };
        popup.addEventListener('beforeunload', close, { once: true });
        window.addEventListener('beforeunload', close, { once: true });
        if (!embedded) {
            closeWatcher = window.setInterval(() => {
                if (popup.closed) { close(); }
            }, 500);
        }

        const vmStatusUrl = () => `/nodes/${encodeURIComponent(node)}/${guestKind}/${encodeURIComponent(vmid)}/status/current`;
        const waitForVmStart = async () => {
            // Opening Console is a valid action before Power On.  Do not
            // create a single-use WebRTC offer until QEMU exists: PVE would
            // correctly reject that offer, but reporting it as a console
            // failure forces an operator to close and reopen the popup.
            // Keep this window alive and begin negotiation as soon as the
            // normal protected PVE status route reports the VM as running.
            while (!closed) {
                try {
                    const state = await apiValue(vmStatusUrl(), 'GET', {}, null);
                    if (state && state.status === 'running') { return true; }
                    status.textContent = gettext('Virtual machine is stopped. Waiting for it to start…');
                } catch (_error) {
                    // A node that is still finishing its own start-up has no
                    // useful display yet either. Keep the same Console
                    // popup rather than mislabelling a transient status read
                    // as a WebRTC or permission error.
                    status.textContent = gettext('Waiting for virtual machine status…');
                }
                await new Promise((resolve) => popup.setTimeout(resolve, 1000));
            }
            return false;
        };

        // Returns true when the current decoded frame is entirely black,
        // false when any sampled pixel carries light, and null when there is
        // no decodable frame yet.  A 32×18 downscale of the intrinsic video
        // frame is sampled, so CSS letterboxing never counts as darkness.
        // Only exact DPMS black (every channel ≤ 8) qualifies: an encoded
        // dark movie scene keeps small non-zero values and does not trigger.
        const guestFrameIsDark = () => {
            if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA ||
                !video.videoWidth || !video.videoHeight) { return null; }
            try {
                if (!livenessCanvas) {
                    livenessCanvas = document.createElement('canvas');
                    livenessCanvas.width = 32;
                    livenessCanvas.height = 18;
                    livenessContext = livenessCanvas.getContext('2d', { willReadFrequently: true });
                }
                if (!livenessContext) { return null; }
                livenessContext.drawImage(video, 0, 0, 32, 18);
                const pixels = livenessContext.getImageData(0, 0, 32, 18).data;
                for (let index = 0; index < pixels.length; index += 4) {
                    if (pixels[index] > 8 || pixels[index + 1] > 8 || pixels[index + 2] > 8) { return false; }
                }
                return true;
            } catch (_error) {
                // A same-origin WebRTC stream is readable; if a browser ever
                // refuses the readback the monitor simply stays inert.
                return null;
            }
        };
        const monitorGuestLiveness = () => {
            if (closed) { return; }
            const dark = guestFrameIsDark();
            if (dark === true) {
                if (darkFrameSince === null) { darkFrameSince = Date.now(); }
                if (!guestAsleepHint && Date.now() - darkFrameSince >= GUEST_SLEEP_HINT_MS &&
                    Date.now() - lastInputSentAt > 5000) {
                    guestAsleepHint = true;
                    status.textContent = gettext('Guest display looks asleep. Move the mouse or press a key here to wake it.');
                    revealToolbar();
                }
            } else if (dark === false) {
                darkFrameSince = null;
                if (guestAsleepHint) {
                    guestAsleepHint = false;
                    updateMediaStatus();
                    // The guest just woke: give a stalled size request its
                    // one re-issue now rather than waiting for more input.
                    retryStalledResize();
                }
            }
        };
        const startLivenessMonitor = () => {
            if (livenessTimer !== null || closed) { return; }
            livenessTimer = popup.setInterval(monitorGuestLiveness, 1000);
        };
        const updateMediaStatus = () => {
            if (closed || !peer) { return; }
            if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
                video.videoWidth > 0 && video.videoHeight > 0) {
                // The sleep monitor owns the status line while the guest
                // output is off; a decoded frame alone is not proof of a
                // visible desktop.
                if (!guestAsleepHint) { status.textContent = gettext('Connected'); }
                if (firstFrameTimer !== null) {
                    popup.clearTimeout(firstFrameTimer);
                    firstFrameTimer = null;
                }
                stopDiag();
                startLivenessMonitor();
                hideToolbarSoon();
            } else if (peer.connectionState === 'connected') {
                status.textContent = gettext('Connected — waiting for guest video…');
            }
        };

        const connect = async () => {
            try {
            if (!await waitForVmStart()) { return; }
            peer = new RTCPeerConnection();
            control = peer.createDataChannel('qsm-control', { ordered: true });
            // Cursor positions are latest-state samples. Sending them as a
            // reliable ordered stream lets one congested packet make every
            // later gesture visibly stale, while keys/clicks must remain
            // ordered and reliable on qsm-control.
            pointer = peer.createDataChannel('qsm-pointer', { ordered: false, maxRetransmits: 0 });
            peer.addEventListener('datachannel', (event) => {
                const channel = event.channel;
                if (!channel || channel.label !== 'qsm-guest-cursor' ||
                    channel.ordered !== false || channel.maxRetransmits !== 0) { return; }
                channel.addEventListener('message', (cursorEvent) => {
                    if (typeof cursorEvent.data !== 'string') { return; }
                    try {
                        const message = JSON.parse(cursorEvent.data);
                        if (message && message.op === 'qsm_guest_cursor') {
                            acceptGuestCursorPosition(message);
                        }
                    } catch (_error) { /* An unordered cursor sample is disposable. */ }
                });
                channel.addEventListener('close', hideGuestCursor, { once: true });
            });
            peer.addEventListener('connectionstatechange', () => {
                if (peer.connectionState === 'failed' || peer.connectionState === 'closed') {
                    closeForStoppedVm();
                } else {
                    updateMediaStatus();
                }
            });
            peer.addEventListener('iceconnectionstatechange', () => {
                if (peer.iceConnectionState === 'failed' || peer.iceConnectionState === 'closed') {
                    closeForStoppedVm();
                }
            });
            peer.ontrack = (event) => {
                // `RTCTrackEvent.streams` is permitted to be empty. aiortc
                // and browser versions disagree on when a remote stream ID
                // is emitted, so attaching only streams[0] produced a
                // connected but black `<video>` on affected Chrome builds.
                // A remote-desktop console must prefer the newest decodable
                // picture over WebRTC's conference-call playout cushion.
                // This is an optional Chromium API, hence feature-detect it
                // rather than making an older browser unable to connect.
                if (event.track.kind === 'video' && event.receiver) {
                    videoReceiver = event.receiver;
                    if ('playoutDelayHint' in videoReceiver) {
                        try { videoReceiver.playoutDelayHint = settings.playoutDelayMs / 1000; }
                        catch (_error) { /* Older browsers may reject the hint. */ }
                    }
                }
                let stream = event.streams && event.streams[0];
                if (!stream) {
                    stream = video.srcObject instanceof MediaStream
                        ? video.srcObject : new MediaStream();
                    if (!stream.getTracks().some((track) => track.id === event.track.id)) {
                        stream.addTrack(event.track);
                    }
                }
                video.srcObject = stream;
                event.track.addEventListener('ended', closeForStoppedVm, { once: true });
                video.play().catch(() => undefined);
                updateMediaStatus();
            };
            video.addEventListener('loadeddata', updateMediaStatus);
            video.addEventListener('playing', updateMediaStatus);
            // KWin/SDDM applies a virtio-gpu hotplug event asynchronously.
            // QEMU exposes its new scanout almost immediately, but a second
            // distinct mode sent during that compositor transition can leave
            // the greeter painting its previous canvas into the new buffer.
            // Treat an operating-system resize as an end-of-drag action and
            // serialize mode changes.  The initial/full-screen request still
            // dispatches immediately when no transition is pending.
            const resize = (immediate = false) => {
                const dispatch = () => {
                    resizeTimer = null;
                    const value = dimensions(video, sessionFps);
                    const identity = `${value.width}x${value.height}@${value.fps}`;
                    // In addition to CSS layout, update the media element's
                    // intrinsic presentation box. Chromium allocates the
                    // accelerated video layer from these dimensions on some
                    // platforms; CSS alone can leave the decoded plane at a
                    // previous popup size although getBoundingClientRect()
                    // already reports the new one.
                    if (video.width !== value.width) { video.width = value.width; }
                    if (video.height !== value.height) { video.height = value.height; }
                    if (identity !== lastResize) {
                        const remainingSettle = Math.max(0,
                            lastResizeSentAt + settings.resizeSettleMs - Date.now());
                        if (remainingSettle > 0) {
                            // Read dimensions again when the compositor has
                            // settled: a user may have continued dragging.
                            resizeTimer = popup.setTimeout(dispatch, remainingSettle);
                            return;
                        }
                        if (identity !== resizeRetryIdentity) {
                            // A genuinely new window geometry restarts the
                            // fast budget and the stalled bookkeeping.
                            resizeRetryAttempts = 0;
                            resizeStalled = false;
                            stalledRetryCount = 0;
                            // This is a fresh stretch of THIS console: the guest
                            // size we are changing away from is the current one,
                            // so record it as the baseline.  The retry then
                            // distinguishes "guest has not applied my request
                            // yet" (still this baseline → keep pushing) from
                            // "another console changed it" (a different size →
                            // yield), instead of mistaking our own not-yet-
                            // applied request for someone else's change.
                            if (video.videoWidth > 0 && video.videoHeight > 0) {
                                lastAppliedSize = `${video.videoWidth}x${video.videoHeight}`;
                            }
                        }
                        resizeRetryIdentity = identity;
                        lastResize = identity;
                        lastResizeSentAt = Date.now();
                        send({ op: 'resize', ...value });
                    }
                    // Control SCTP is reliable, but a D-Bus/guest mode
                    // change can race a resize immediately after a window or
                    // full-screen transition. Do not permanently letterbox
                    // the Console after one early request: retry until the
                    // decoded frame itself confirms the target geometry.
                    if (resizeRetryTimer !== null) { popup.clearTimeout(resizeRetryTimer); }
                    resizeRetryTimer = popup.setTimeout(() => {
                        resizeRetryTimer = null;
                        if (closed || resizeRetryIdentity !== identity) { return; }
                        const current = dimensions(video, sessionFps);
                        const currentIdentity = `${current.width}x${current.height}@${current.fps}`;
                        if (currentIdentity !== identity) { resize(false); return; }
                        if (video.videoWidth === current.width && video.videoHeight === current.height) {
                            resizeRetryAttempts = 0;
                            resizeStalled = false;
                            lastAppliedSize = `${video.videoWidth}x${video.videoHeight}`;
                            // The geometry converged: clear a "kept its own
                            // size" line left behind by an earlier stall,
                            // and only that line — a keyboard-capture or
                            // clipboard message must survive a resize.
                            if (resizeStatusShown) {
                                resizeStatusShown = false;
                                updateMediaStatus();
                            }
                            return;
                        }
                        // The guest is a different size than we asked for.  If
                        // it moved to a size that is neither our request nor the
                        // size we previously saw, another Console resized the
                        // shared guest — it is the reference now.  Follow it:
                        // re-sending our size here would drag the stretched
                        // console back and leave it showing a scaled desktop.
                        const guestSize = `${video.videoWidth}x${video.videoHeight}`;
                        if (video.videoWidth > 0 && video.videoHeight > 0 &&
                            lastAppliedSize && guestSize !== lastAppliedSize) {
                            lastAppliedSize = guestSize;
                            resizeRetryAttempts = 0;
                            resizeStalled = false;
                            resizeRetryIdentity = '';
                            lastResize = '';
                            if (resizeStatusShown) { resizeStatusShown = false; updateMediaStatus(); }
                            return;
                        }
                        if (resizeRetryAttempts < 16) {
                            resizeRetryAttempts += 1;
                            lastResize = '';
                            resize(true);
                            return;
                        }
                        // Sixteen fast retries cover an ordinary compositor
                        // transition.  A guest that still keeps its own mode
                        // is almost always one whose session is locked or
                        // whose output is off: it applies the pending mode
                        // when it wakes, and re-sending every 250 ms until
                        // then only makes it re-evaluate a request it is
                        // ignoring.  Stop here; the input path re-issues the
                        // request once per wake attempt (retryStalledResize).
                        resizeStalled = true;
                        if (guestAsleepHint || guestFrameIsDark() === null) { return; }
                        status.textContent = gettext('Guest display kept its own size. If the guest is asleep or locked, move the mouse or press a key here.');
                        if (!resizeStatusShown) { revealToolbar(); }
                        resizeStatusShown = true;
                    }, 250);
                };
                if (resizeTimer !== null) { popup.clearTimeout(resizeTimer); resizeTimer = null; }
                if (immediate) { dispatch(); }
                else { resizeTimer = popup.setTimeout(dispatch, settings.resizeDebounceMs); }
            };
            resizeConsole = resize;
            // A locked or sleeping guest adopts the last requested mode when
            // it wakes.  Ordinary input is the wake signal, so every input
            // burst re-issues a stalled request exactly once (3 s throttle)
            // with a short verification burst instead of an endless loop.
            retryStalledResize = () => {
                if (!resizeStalled || closed) { return; }
                // If the guest is now at a size other than the one we last saw
                // applied, another Console is driving the shared resolution.
                // Yield: this console follows the reference instead of shoving
                // its own size back on every mouse move.
                if (video.videoWidth > 0 && lastAppliedSize &&
                    `${video.videoWidth}x${video.videoHeight}` !== lastAppliedSize) {
                    resizeStalled = false;
                    lastAppliedSize = `${video.videoWidth}x${video.videoHeight}`;
                    return;
                }
                const now = Date.now();
                if (now - lastStalledRetryAt < 3000) { return; }
                // A guest that keeps its own mode after several wake
                // attempts is one that cannot follow the window at all
                // (Standard VGA rejects Display1 resize requests); leave it
                // letterboxed rather than asking again on every gesture.
                if (stalledRetryCount >= 6) { return; }
                stalledRetryCount += 1;
                lastStalledRetryAt = now;
                resizeRetryAttempts = 8;
                lastResize = '';
                resize(true);
            };
            noteGuestInput = () => {
                lastInputSentAt = Date.now();
                retryStalledResize();
            };
            const flushPointer = (reliable = false) => {
                if (pointerFrame !== null) {
                    popup.cancelAnimationFrame(pointerFrame);
                    pointerFrame = null;
                }
                if (pointerTimer !== null) {
                    popup.clearTimeout(pointerTimer);
                    pointerTimer = null;
                }
                const value = pendingPointer || latestPointer;
                pendingPointer = null;
                if (!value) { return; }
                // The position immediately preceding a button edge is sent
                // on the ordered channel.  SCTP does not order different
                // data channels, therefore only flushing `qsm-pointer` here
                // could press on the old position under loss or reordering.
                if (reliable) { send(value); }
                else { sendPointer(value); }
            };
            const queuePointer = (value) => {
                noteGuestInput();
                const sample = { ...value, sequence: pointerSequence };
                pointerSequence = (pointerSequence + 1) >>> 0;
                latestPointer = sample;
                pendingPointer = sample;
                if (pointerFrame === null && pointerTimer === null) {
                    // Input delivery is paced to the browser compositor, not to
                    // its separately throttleable timer queue: while this window
                    // is visible the frame callback wins and one latest sample
                    // is retained per presented frame (click edges flush it
                    // synchronously below).  A timer races it purely as a
                    // fallback for an occluded/background window, whose
                    // requestAnimationFrame the browser pauses — there the timer
                    // keeps the guest pointer following the mouse instead of
                    // freezing it at the pre-switch position.  Whichever fires
                    // first flushes and cancels the other.
                    pointerFrame = popup.requestAnimationFrame(() => flushPointer(false));
                    pointerTimer = popup.setTimeout(() => flushPointer(false), 50);
                }
            };
            const pointerForMouseEvent = (event) => {
                // The video is position:fixed;inset:0;width:100vw;height:100vh, so
                // its on-screen box is exactly the popup/iframe viewport
                // (popup.innerWidth x innerHeight) — the SAME coordinate space as
                // event.clientX/Y and the touch marker (which the user confirmed
                // lands under the finger).  Mapping through this viewport is
                // therefore guaranteed consistent with the marker, unlike
                // getBoundingClientRect() or offsetX, which are in a different
                // (scaled) space inside a mobile iframe and collapsed every touch
                // to one guest coordinate.  object-fit:contain letterboxes to the
                // source aspect, handled below.
                const viewW = popup.innerWidth || video.clientWidth || 1;
                const viewH = popup.innerHeight || video.clientHeight || 1;
                const sourceWidth = Math.max(1, video.videoWidth || viewW);
                const sourceHeight = Math.max(1, video.videoHeight || viewH);
                const rawScale = Math.min(viewW / sourceWidth, viewH / sourceHeight);
                const scale = Number.isFinite(rawScale) && rawScale > 0 ? rawScale : 1;
                const contentLeft = (viewW - sourceWidth * scale) / 2;
                const contentTop = (viewH - sourceHeight * scale) / 2;
                // Undo the client-side zoom/pan (transform-origin 0,0):
                // screen -> local viewport coordinates the letterbox math uses.
                const localX = (event.clientX - zoomTx) / zoomScale;
                const localY = (event.clientY - zoomTy) / zoomScale;
                const gx = Math.max(0, Math.min(sourceWidth - 1, Math.floor((localX - contentLeft) / scale)));
                const gy = Math.max(0, Math.min(sourceHeight - 1, Math.floor((localY - contentTop) / scale)));
                lastSentGuestX = gx; lastSentGuestY = gy;
                diagLastPtr = `client=${Math.round(event.clientX)},${Math.round(event.clientY)} view=${Math.round(viewW)}x${Math.round(viewH)} zoom=${zoomScale.toFixed(2)} src=${sourceWidth}x${sourceHeight} scale=${scale.toFixed(3)} -> guest=${gx},${gy}`;
                // Refresh the panel immediately (its synchronous part runs before
                // any await) so each touch's coordinates show at once instead of
                // waiting for the interval — essential for reading them on-device.
                if (diag && !diag.hidden) { updateDiag(); }
                // Keep the touch cursor overlay under the finger as it acts,
                // even before the guest streams its own cursor for this move.
                if (usingTouchInput) { placeGuestCursor(); }
                return { op: 'mouse_position', x: gx, y: gy, width: sourceWidth, height: sourceHeight };
            };
            control.addEventListener('open', () => resize(true));
            control.addEventListener('message', (event) => {
                if (typeof event.data !== 'string') { return; }
                let message;
                try { message = JSON.parse(event.data); } catch (_error) { return; }
                if (!message || typeof message !== 'object') { return; }
                if (message.op === 'qsm_guest_result' && typeof message.request_id === 'string') {
                    const request = guestRequests.get(message.request_id);
                    if (!request) { return; }
                    guestRequests.delete(message.request_id);
                    popup.clearTimeout(request.timer);
                    if (message.ok === true && message.result && typeof message.result === 'object') {
                        request.resolve(message.result);
                    } else {
                        request.reject(new Error('guest operation failed'));
                    }
                } else if (message.op === 'qsm_guest_clipboard' && typeof message.text_b64 === 'string') {
                    try {
                        // A website is not permitted to replace the user's
                        // system clipboard without a click/key gesture. Keep
                        // the notification for the explicit Copy action
                        // rather than silently discarding a browser rejection.
                        const text = new TextDecoder('utf-8', { fatal: true }).decode(b64ToBytes(message.text_b64));
                        resolveGuestClipboardWaiters(text);
                        // Klipper can report a transient selection while a
                        // window is being dragged. It must not repeatedly
                        // repaint the console toolbar or look like a local
                        // clipboard operation. The explicit Copy control
                        // remains available for the final guest selection.
                        if (text !== lastGuestClipboardText) {
                            lastGuestClipboardText = text;
                            if (!heldMouseButtons.size) {
                                status.textContent = gettext('Guest clipboard changed — press Copy to copy it here');
                            }
                        }
                    } catch (_error) { /* Ignore malformed guest clipboard notifications. */ }
                } else if (message.op === 'qsm_guest_cursor_shape') {
                    acceptGuestCursorShape(message);
                }
            });
            // aiortc closes every server-side data channel while retiring a
            // VM session. Track-end is the normal media signal, but this is
            // an independent browser-visible lifecycle signal for codecs or
            // browsers that postpone a track's `ended` event.
            control.addEventListener('close', closeForStoppedVm, { once: true });
            pointer.addEventListener('close', closeForStoppedVm, { once: true });
            observer = new ResizeObserver(() => { placeGuestCursor(); resize(false); });
            observer.observe(video);
            popup.addEventListener('resize', () => { resize(false); reflowView(); });
            // A VM has one Display1 scanout, while several PVE Console
            // windows may observe it.  A decoded-frame resize means that
            // *some* Console already changed the guest mode; it is not a
            // resize of this popup.  Feeding that event back into `resize()`
            // made an older, differently sized viewer immediately restore
            // its stale dimensions and left the window that the user had
            // just resized letterboxed.  Only this popup's CSS box and its
            // explicit full-screen transition are authority to request a
            // mode.  The video event merely lets its outstanding retry yield
            // to the newer Console request.
            video.addEventListener('resize', () => {
                placeGuestCursor();
                reflowView();
                // The retry identity carries the session FPS; a literal
                // "@60" here left the convergence branch unreachable for
                // any other target frame rate.
                const visible = `${video.videoWidth}x${video.videoHeight}@${sessionFps}`;
                if (visible === resizeRetryIdentity) {
                    resizeRetryAttempts = 0;
                    resizeStalled = false;
                    if (resizeStatusShown) {
                        // A guest that woke up adopted the stalled request
                        // on its own: drop the "kept its own size" line.
                        resizeStatusShown = false;
                        updateMediaStatus();
                    }
                    return;
                }
                if (resizeRetryTimer !== null) {
                    popup.clearTimeout(resizeRetryTimer);
                    resizeRetryTimer = null;
                }
                resizeRetryIdentity = '';
                resizeRetryAttempts = 0;
            });
            // Control follows the pointer into the console: entering the guest
            // rectangle takes keyboard and clipboard focus at once, without
            // waiting for a click.  Relying on a click to move focus made
            // switching between two consoles feel like it lagged by seconds
            // (the previous surface kept focus until the guest was clicked).
            // Focusing an element in a window that is not the active one is a
            // harmless no-op, so no window-state test is needed; the settings
            // panel is skipped so hovering the video cannot steal its inputs.
            video.addEventListener('pointerenter', () => {
                if (settingsPanelOpen) { return; }
                video.focus({ preventScroll: true });
            });
            video.addEventListener('mousemove', (event) => {
                observeToolbarZone(event);
                // A real mouse is present: hand the cursor back to the OS/CSS
                // path (hide the touch overlay) so a desktop user is unaffected.
                if (usingTouchInput) { usingTouchInput = false; placeGuestCursor(); }
                // A missed button-up from a previous native full-screen
                // transition becomes observable on the next ordinary move.
                // Clear it before it can make every later click a drag.
                if (event.buttons === 0 && heldMouseButtons.size) { releaseHeldInput(); }
                queuePointer(pointerForMouseEvent(event));
            });
            video.addEventListener('mouseleave', () => {
                pointerInToolbarZone = false;
                pointerNearToolbar = false;
                cancelToolbarReveal();
                hideToolbarSoon();
            });
            video.addEventListener('mousedown', (event) => {
                ensurePlaying();
                queuePointer(pointerForMouseEvent(event));
                flushPointer(true);
                video.focus();
                sendMouseButton(event.button + 1, true);
                event.preventDefault();
            });
            video.addEventListener('mouseup', (event) => {
                queuePointer(pointerForMouseEvent(event));
                flushPointer(true);
                sendMouseButton(event.button + 1, false);
                event.preventDefault();
            });
            video.addEventListener('contextmenu', (event) => {
                // Button 3 was already sent on mousedown/mouseup above.
                // Suppress Chrome's menu without stopping the genuine guest
                // context-menu click, so one right click maps to one guest
                // right click rather than a browser menu followed by QEMU.
                event.preventDefault();
                event.stopPropagation();
            });
            // Touch and pen input (Android tablets/phones and other
            // touchscreens).  The mouse handlers above stay the desktop path;
            // these fire only for non-mouse pointers.  Gestures: a quick tap is
            // a left click at the touched point (two quick taps read as a
            // double click on the guest naturally); dragging holds the left
            // button so a finger can select or drag; a long press is a right
            // click — the only way to raise a guest context menu from touch.
            // A brief marker shows exactly where each touch registered.
            const touchMarker = document.createElement('div');
            touchMarker.setAttribute('aria-hidden', 'true');
            touchMarker.style.cssText = 'position:fixed;z-index:6;width:26px;height:26px;margin:-13px 0 0 -13px;border:2px solid rgba(56,189,248,.95);border-radius:50%;background:rgba(56,189,248,.25);pointer-events:none;opacity:0;transition:opacity .25s ease';
            document.body.append(touchMarker);
            let touchMarkerTimer = null;
            const showTouchMarker = (x, y) => {
                touchMarker.style.left = `${x}px`;
                touchMarker.style.top = `${y}px`;
                touchMarker.style.opacity = '1';
                if (touchMarkerTimer !== null) { popup.clearTimeout(touchMarkerTimer); }
                touchMarkerTimer = popup.setTimeout(() => { touchMarker.style.opacity = '0'; touchMarkerTimer = null; }, 350);
            };
            // ---- Touch gesture recognizer ------------------------------------
            // One "direct" behaviour: the guest cursor is shown (overlay) and a
            // gesture acts where you touch.  Which finger gesture triggers which
            // guest action is configurable (gestureMap); this classifies the raw
            // gesture and dispatches to the bound action.
            const touchPoints = new Map(); // pointerId -> {startX,startY,x,y}
            let peakFingers = 0;
            let oneMoved = false;
            let oneLongFired = false;
            let oneStartX = 0;
            let oneStartY = 0;
            let oneLongTimer = null;
            let heldDragButton = 0;
            let contAct = null;      // active continuous action ('drag'|'scroll'|'zoom')
            let contLastX = 0;
            let contLastY = 0;
            let scrollAccumY = 0;
            const SCROLL_STEP_PX = 18;
            let twoLocked = null;    // null | 'pinch' | 'drag2'
            let twoMoved = false;
            let twoStartDist = 1;
            let twoStartMidX = 0;
            let twoStartMidY = 0;
            let twoStartScale = 1;
            let twoStartTx = 0;
            let twoStartTy = 0;
            const clearOneLong = () => { if (oneLongTimer !== null) { popup.clearTimeout(oneLongTimer); oneLongTimer = null; } };
            const twoPoints = () => { const it = touchPoints.values(); return [it.next().value, it.next().value]; };
            const pointDistance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
            // --- action executors ---
            const clickAction = (action, clientX, clientY) => {
                const button = action === 'rightClick' ? 3 : action === 'middleClick' ? 2 : 1;
                queuePointer(pointerForMouseEvent({ clientX, clientY }));
                flushPointer(true);
                sendMouseButton(button, true);
                sendMouseButton(button, false);
            };
            const scrollByDelta = (dy) => {
                // Worker emits one wheel click per op (sign only): accumulate
                // finger travel and send one op per notch, natural direction
                // (drag down = content down = wheel-up = vertical<0), capped/event.
                scrollAccumY += dy;
                let notches = 0;
                while (Math.abs(scrollAccumY) >= SCROLL_STEP_PX && notches < 8) {
                    const dir = scrollAccumY > 0 ? 1 : -1;
                    scrollAccumY -= dir * SCROLL_STEP_PX;
                    send({ op: 'scroll', vertical: -dir, horizontal: 0 });
                    notches += 1;
                    diagLastInput = 'scroll' + (dir > 0 ? '↑' : '↓');
                }
            };
            const beginContinuous = (action, sx, sy) => {
                contAct = action || null;
                contLastX = sx; contLastY = sy; scrollAccumY = 0;
                if (contAct === 'drag') {
                    queuePointer(pointerForMouseEvent({ clientX: sx, clientY: sy }));
                    flushPointer(true);
                    sendMouseButton(1, true);
                    heldDragButton = 1;
                }
            };
            const stepContinuous = (x, y) => {
                const dx = x - contLastX; const dy = y - contLastY;
                contLastX = x; contLastY = y;
                if (contAct === 'drag') {
                    queuePointer(pointerForMouseEvent({ clientX: x, clientY: y }));
                    flushPointer(true);
                } else if (contAct === 'scroll') {
                    scrollByDelta(dy);
                } else if (contAct === 'zoom') {
                    if (zoomScale > 1) { zoomTx += dx; zoomTy += dy; clampZoom(); applyViewTransform(); }
                }
            };
            const endContinuous = (x, y) => {
                if (contAct === 'drag' && heldDragButton) {
                    queuePointer(pointerForMouseEvent({ clientX: x, clientY: y }));
                    flushPointer(true);
                    sendMouseButton(heldDragButton, false);
                    heldDragButton = 0;
                }
                contAct = null;
            };
            const pinchZoom = (dist, mx, my) => {
                const act = gestureToAction.pinch;
                if (act !== 'zoom') { return; }
                const ratio = dist / twoStartDist;
                const newScale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, twoStartScale * ratio));
                const lx = (twoStartMidX - twoStartTx) / twoStartScale;
                const ly = (twoStartMidY - twoStartTy) / twoStartScale;
                zoomScale = newScale;
                zoomTx = mx - newScale * lx;
                zoomTy = my - newScale * ly;
                clampZoom();
                applyViewTransform();
            };
            const resetInteraction = () => {
                clearOneLong();
                if (heldDragButton) { sendMouseButton(heldDragButton, false); heldDragButton = 0; }
                peakFingers = 0; oneMoved = false; oneLongFired = false;
                contAct = null; twoLocked = null; twoMoved = false;
            };
            const finalizeInteraction = (px, py) => {
                clearOneLong();
                if (peakFingers === 1) {
                    if (contAct) { endContinuous(px, py); }
                    else if (!oneMoved && !oneLongFired) {
                        const act = gestureToAction.tap;
                        if (act) { clickAction(act, px, py); }
                    }
                } else if (peakFingers === 2) {
                    if (twoLocked) { endContinuous(px, py); }
                    else if (!twoMoved) {
                        const act = gestureToAction.tap2;
                        if (act) { clickAction(act, twoStartMidX, twoStartMidY); }
                    }
                } else if (peakFingers >= 3) {
                    const act = gestureToAction.tap3;
                    if (act) { clickAction(act, twoStartMidX || px, twoStartMidY || py); }
                }
                resetInteraction();
            };
            // --- pointer events (touch/pen only; mouse keeps the desktop path) ---
            video.addEventListener('pointerdown', (event) => {
                if (event.pointerType === 'mouse') { return; }
                usingTouchInput = true;
                touchPoints.set(event.pointerId, { startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY });
                const n = touchPoints.size;
                if (n > peakFingers) { peakFingers = n; }
                try { video.setPointerCapture(event.pointerId); } catch (_error) { /* older engines */ }
                if (n === 1) {
                    ensurePlaying();
                    video.focus({ preventScroll: true });
                    if (toolbarVisible && !toolbarPinned) { hideToolbarNow(); }
                    oneMoved = false; oneLongFired = false; contAct = null;
                    oneStartX = event.clientX; oneStartY = event.clientY;
                    showTouchMarker(event.clientX, event.clientY);
                    placeGuestCursor();
                    clearOneLong();
                    oneLongTimer = popup.setTimeout(() => {
                        oneLongTimer = null;
                        if (peakFingers !== 1 || oneMoved) { return; }
                        const act = gestureToAction.longPress;
                        if (!act) { return; }
                        oneLongFired = true;
                        const p = touchPoints.values().next().value;
                        clickAction(act, p ? p.x : oneStartX, p ? p.y : oneStartY);
                    }, 500);
                } else if (n === 2) {
                    // A second finger cancels any one-finger action in progress.
                    clearOneLong();
                    if (heldDragButton) { sendMouseButton(heldDragButton, false); heldDragButton = 0; }
                    contAct = null; oneMoved = false;
                    const [a, b] = twoPoints();
                    twoStartDist = pointDistance(a, b) || 1;
                    twoStartMidX = (a.x + b.x) / 2; twoStartMidY = (a.y + b.y) / 2;
                    twoStartScale = zoomScale; twoStartTx = zoomTx; twoStartTy = zoomTy;
                    twoLocked = null; twoMoved = false;
                    contLastX = twoStartMidX; contLastY = twoStartMidY;
                }
                event.preventDefault();
            });
            video.addEventListener('pointermove', (event) => {
                if (event.pointerType === 'mouse') { return; }
                const p = touchPoints.get(event.pointerId);
                if (!p) { return; }
                p.x = event.clientX; p.y = event.clientY;
                const n = touchPoints.size;
                if (n === 1 && peakFingers === 1) {
                    const dx = event.clientX - oneStartX; const dy = event.clientY - oneStartY;
                    if (!oneMoved && (Math.abs(dx) > 18 || Math.abs(dy) > 18)) {
                        oneMoved = true;
                        clearOneLong();
                        if (!oneLongFired) { beginContinuous(gestureToAction.drag1, oneStartX, oneStartY); }
                    }
                    if (oneMoved && !oneLongFired && contAct) { stepContinuous(event.clientX, event.clientY); }
                    showTouchMarker(event.clientX, event.clientY);
                } else if (n === 2) {
                    const [a, b] = twoPoints();
                    if (!a || !b) { return; }
                    const d = pointDistance(a, b) || 1;
                    const mx = (a.x + b.x) / 2; const my = (a.y + b.y) / 2;
                    if (twoLocked === null) {
                        const dDist = Math.abs(d - twoStartDist);
                        const dMid = Math.hypot(mx - twoStartMidX, my - twoStartMidY);
                        if (dDist > 12 || dMid > 12) {
                            twoMoved = true;
                            twoLocked = dDist > dMid ? 'pinch' : 'drag2';
                            if (twoLocked === 'drag2') { beginContinuous(gestureToAction.drag2, twoStartMidX, twoStartMidY); }
                        }
                    }
                    if (twoLocked === 'pinch') { pinchZoom(d, mx, my); }
                    else if (twoLocked === 'drag2') { stepContinuous(mx, my); }
                }
                event.preventDefault();
            });
            const endTouchPointer = (event) => {
                if (event.pointerType === 'mouse') { return; }
                // The same handler is on video and document; a captured pointerup
                // reaches both, so process each event once.
                if (event.__qsmTouchHandled) { return; }
                event.__qsmTouchHandled = true;
                if (!touchPoints.has(event.pointerId)) { return; }
                touchPoints.delete(event.pointerId);
                const remaining = touchPoints.size;
                try { video.releasePointerCapture(event.pointerId); } catch (_error) { /* already released */ }
                if (remaining === 0) {
                    finalizeInteraction(event.clientX, event.clientY);
                } else if (twoLocked && remaining < 2) {
                    // A two-finger gesture dropped to one finger: end it and wait
                    // for all contacts to lift (do not start a stray one-finger act).
                    endContinuous(event.clientX, event.clientY);
                    twoLocked = null;
                }
                event.preventDefault();
            };
            video.addEventListener('pointerup', endTouchPointer);
            // A touch that lifts off the video (or whose pointerup the engine
            // delivers to the document) must still release, else a held button or
            // a stale contact blocks every later tap.
            document.addEventListener('pointerup', endTouchPointer);
            document.addEventListener('pointercancel', (event) => {
                if (!event || event.pointerType === 'mouse') { return; }
                if (!touchPoints.has(event.pointerId)) { return; }
                touchPoints.delete(event.pointerId);
                if (touchPoints.size === 0) {
                    clearOneLong();
                    if (heldDragButton) { sendMouseButton(heldDragButton, false); heldDragButton = 0; }
                    resetInteraction();
                    releaseHeldInput();
                }
            });
            // Mouseup often targets the document instead of the video after
            // Escape leaves native full screen.  Preserve a captured guest
            // button only until that document-level release arrives.
            document.addEventListener('mouseup', (event) => {
                const button = event.button + 1;
                if (!heldMouseButtons.has(button)) { return; }
                queuePointer(pointerForMouseEvent(event));
                flushPointer(true);
                sendMouseButton(button, false);
            });
            video.addEventListener('pointercancel', (event) => {
                if (!event || event.pointerType === 'mouse') { return; }
                if (!touchPoints.has(event.pointerId)) { return; }
                touchPoints.delete(event.pointerId);
                if (touchPoints.size === 0) {
                    clearOneLong();
                    if (heldDragButton) { sendMouseButton(heldDragButton, false); heldDragButton = 0; }
                    resetInteraction();
                    releaseHeldInput();
                }
            });
            popup.addEventListener('blur', releaseHeldInput);
            document.addEventListener('visibilitychange', () => {
                if (document.hidden) { releaseHeldInput(); }
            });
            video.addEventListener('wheel', (event) => { send({ op: 'scroll', vertical: Math.max(-32768, Math.min(32767, Math.trunc(event.deltaY))), horizontal: Math.max(-32768, Math.min(32767, Math.trunc(event.deltaX))) }); event.preventDefault(); }, { passive: false });
            video.addEventListener('paste', (event) => {
                // Raw input is intentionally a literal key/mouse edge mode
                // for configuring guest shortcuts. Its clipboard controls
                // remain the explicit toolbar buttons; a browser Paste event
                // must not turn Ctrl/Cmd+V into a synthetic guest Paste action.
                if (settings.keyboardPriority === KEYBOARD_PRIORITY.raw) { return; }
                const text = event.clipboardData && event.clipboardData.getData('text/plain');
                if (typeof text !== 'string') { return; }
                event.preventDefault();
                pasteTextIntoGuest(text).catch(() => {
                    status.textContent = gettext('Guest clipboard is unavailable.');
                });
            });
            for (const name of ['keydown', 'keyup']) {
                video.addEventListener(name, (event) => {
                    const key = qemuKey(event);
                    const clientFirst = settings.keyboardPriority === KEYBOARD_PRIORITY.client;
                    const rawInput = settings.keyboardPriority === KEYBOARD_PRIORITY.raw;
                    if (!clientFirst && !rawInput && guestCaptureExitShortcut(event)) {
                        event.preventDefault();
                        leaveGuestKeyboardCapture();
                        return;
                    }
                    // Escape belongs to the browser chrome only while this
                    // Console owns native full screen.  Previously the
                    // generic guest-key handler called preventDefault(),
                    // leaving full screen in an ambiguous focus/input state
                    // on Chromium and Safari. Guest-first deliberately tries
                    // to forward Escape; a browser/OS may still reserve it
                    // before DOM, as documented in Settings.
                    const fullscreenEscape = event.code === 'Escape' &&
                        (document.fullscreenElement || fullscreenEscapePending);
                    if (clientFirst && fullscreenEscape) {
                        event.preventDefault();
                        if (name === 'keydown') {
                            fullscreenEscapePending = true;
                            releaseHeldInput();
                            const action = document.exitFullscreen();
                            if (action && typeof action.catch === 'function') { action.catch(() => undefined); }
                        } else {
                            fullscreenEscapePending = false;
                        }
                        return;
                    }
                    const pasteShortcut = event.code === 'KeyV' && (event.ctrlKey || event.metaKey) && !event.altKey;
                    const copyShortcut = event.code === 'KeyC' && (event.ctrlKey || event.metaKey) && !event.altKey;
                    if (!rawInput && pasteShortcut) {
                        event.preventDefault();
                        if (name === 'keydown') {
                            // Release a forwarded Cmd/Ctrl before emitting
                            // the Linux Paste sequence. Otherwise macOS Cmd
                            // can remain held in the guest and turn a paste
                            // into an unrelated desktop shortcut.
                            releaseHeldInput();
                            pasteFromBrowser().catch(() => {
                                status.textContent = gettext('Browser clipboard is unavailable.');
                            });
                        }
                    } else if (!rawInput && copyShortcut) {
                        event.preventDefault();
                        if (name === 'keydown') {
                            releaseHeldInput();
                            guestSelectionToBrowser().catch(() => {
                                status.textContent = gettext('Guest clipboard is unavailable.');
                            });
                        }
                    } else if (clientFirst && clientFirstShortcut(event)) {
                        // Leave the event untouched: the browser receives its
                        // native shortcut and the guest sees no partial key.
                        return;
                    } else if (key !== null) {
                        sendKeyboard(key, name === 'keydown'); event.preventDefault();
                    }
                });
            }
            peer.addTransceiver('video', { direction: 'recvonly' });
            peer.addTransceiver('audio', { direction: 'recvonly' });
            const offer = await peer.createOffer();
            await peer.setLocalDescription(offer);
            await waitForIce(peer);
            const requestSize = dimensions(video, sessionFps);
            const answer = await api(`/nodes/${encodeURIComponent(node)}/${guestKind}/${encodeURIComponent(vmid)}/qsm-direct`, {
                sdp: peer.localDescription.sdp, width: requestSize.width, height: requestSize.height, fps: requestSize.fps,
            }, button);
            await peer.setRemoteDescription(answer);
            try {
                const answerSdp = (answer && answer.sdp) || (peer.remoteDescription && peer.remoteDescription.sdp) || '';
                const rtpmap = answerSdp.match(/a=rtpmap:\d+\s+(H264|H265|VP8|VP9|AV1)\b[^\r\n]*/i);
                if (rtpmap) { negotiatedVideoCodec = rtpmap[0].replace(/^a=rtpmap:\d+\s+/i, ''); }
            } catch (_error) { /* diagnostics only */ }
            status.textContent = gettext('Negotiating guest media…');
            // A transport connection is not visual output. Keep the popup
            // usable for a slow guest, but make a Display1/GL capture stall
            // explicit instead of reporting a misleading plain "Connected".
            firstFrameTimer = popup.setTimeout(async () => {
                if (closed || video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) { return; }
                // Autoplay blocked (usual on mobile): the stream is arriving but
                // the element is paused.  Tell the user a press starts it, and
                // surface the toolbar so the affordance is visible.
                startDiag();
                if (video.paused) {
                    status.textContent = gettext('Connected — tap the console to start the video.');
                    revealToolbar();
                    ensurePlaying();
                    return;
                }
                // Playing but nothing decoded: separate "not receiving" from
                // "receiving but this browser cannot decode the codec" so a
                // mobile decode problem is diagnosable from the status line.
                let message = gettext('Connected, but the guest has not produced a video frame.');
                try {
                    if (peer) {
                        const stats = await peer.getStats();
                        let received = 0; let decoded = 0; let seen = false;
                        stats.forEach((report) => {
                            if (report.type === 'inbound-rtp' && report.kind === 'video') {
                                seen = true;
                                received = report.framesReceived || 0;
                                decoded = report.framesDecoded || 0;
                            }
                        });
                        if (seen && received > 0 && decoded === 0) {
                            message = gettext('Connected and receiving video, but this browser could not decode it.');
                        }
                    }
                } catch (_error) { /* getStats is optional. */ }
                if (!closed && video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
                    status.textContent = message;
                }
            }, 7000);
            video.focus();
            } catch (_error) {
                status.textContent = gettext('Could not create a direct browser console.');
                close();
                closeSurface();
                Ext.Msg.alert(gettext('QSM Direct'), gettext('Could not create a direct browser console.'));
            }
        };
        connect();
        return {
            close: () => {
                close();
                closeSurface();
            },
        };
    };

    // Keep PVE's one, familiar "Console" card in the left navigation.  Its
    // configuration is normally a lazy card held in `savedItems`, so the
    // QEMU Config overlay below can substitute this xtype after it reads the
    // protected VM config and before the user opens Console. This card does
    // not add a second navigation entry and deliberately renders in PVE's
    // existing Console area instead of opening a second browser window.
    Ext.define('PVE.qsmDirect.ConsolePanel', {
        extend: 'Ext.panel.Panel',
        alias: 'widget.pveQsmDirectConsole',
        layout: 'fit',
        border: false,

        initComponent: function () {
            const me = this;
            const showPreparing = () => {
                const status = me.down('#qsm-direct-preparing');
                if (status) { status.update(gettext('Preparing console…')); }
            };
            const startNoVnc = () => {
                if (me.destroyed || me.qsmConsoleMode) { return; }
                me.qsmConsoleMode = 'novnc';
                me.removeAll(true);
                const noVnc = me.add({
                    xtype: 'pveNoVncConsole',
                    itemId: 'qsm-direct-stock-novnc',
                    vmid: Number(me.vmid),
                    consoleType: 'kvm',
                    nodename: me.nodename,
                });
                // PVE.panel.Config activates only the outer lazy card. Its
                // freshly-created noVNC child needs the same activation edge
                // to load the stock RFB iframe.
                noVnc.fireEvent('activate');
            };
            const startEmbeddedConsole = () => {
                if (me.destroyed || me.qsmConsoleMode || !validNode(me.nodename) || !validVmid(Number(me.vmid))) {
                    return;
                }
                const host = me.body && me.body.dom;
                if (!host) { return; }
                // A stopped VM or failed negotiation retires its iframe.
                // Returning to the normal PVE Console card must create a
                // fresh peer after the VM has been started again.
                if (me.qsmDirectFrame && host.contains(me.qsmDirectFrame)) { return; }
                if (me.qsmDirectSession) { me.qsmDirectSession.close(); }
                me.qsmConsoleMode = 'direct';
                me.removeAll(true);
                // A raw DOM iframe is invisible to ExtJS's card/fit layout.
                // The Console card then keeps its empty-panel minimum height
                // (roughly 150px) and negotiates a tiny Display1 surface.
                // Use PVE's own fit-aware IFrame component, as noVNC does,
                // before handing its native frame to the self-contained
                // WebRTC console.  This keeps the embedded viewport equal to
                // the whole existing Console card from its first SDP offer.
                const surface = me.add(Ext.create('Ext.ux.IFrame', {
                    itemId: 'qsm-direct-embedded-surface',
                    flex: 1,
                }));
                me.updateLayout();
                const frame = surface.getFrame();
                if (!frame) {
                    me.qsmConsoleMode = null;
                    surface.destroy();
                    return;
                }
                frame.title = gettext('QSM Direct Console');
                frame.setAttribute('allow', 'autoplay; clipboard-read; clipboard-write; fullscreen');
                frame.setAttribute('allowfullscreen', '');
                // touch-action:none on the iframe ELEMENT (top-page side) stops
                // the browser from claiming a two-finger gesture over the console
                // as a page pinch-zoom, in addition to the same rule on the video
                // inside; without it a tablet zooms the whole PVE page.
                frame.style.cssText = 'display:block;width:100%;height:100%;border:0;background:#000;touch-action:none';
                me.qsmDirectFrame = frame;
                me.qsmDirectSession = openConsole(me, me.nodename, Number(me.vmid), frame);
            };
            const selectBackend = () => {
                if (me.destroyed || me.qsmConsoleLoading || me.qsmConsoleMode ||
                    !validNode(me.nodename) || !validVmid(Number(me.vmid))) { return; }
                me.qsmConsoleLoading = true;
                showPreparing();
                // The Display1 predicate is read through PVE's protected API.
                // Right after a page refresh that read can transiently fail
                // before the app's auth/CSRF state settles.  Committing to
                // noVNC on the first failure permanently letterboxed a
                // managed VM's console after F5, so retry a bounded number of
                // times and only fall back to the stock console once the read
                // has genuinely failed.  A successful read is authoritative:
                // an unmanaged VM still goes straight to noVNC.
                const readConfig = (attempt) => {
                    if (me.destroyed || me.qsmConsoleMode) { return; }
                    Proxmox.Utils.API2Request({
                        url: `/nodes/${encodeURIComponent(me.nodename)}/qemu/${encodeURIComponent(me.vmid)}/config`,
                        method: 'GET',
                        success: ({ result }) => {
                            me.qsmConsoleLoading = false;
                            const data = result && result.data;
                            if (displayState(data && data.args, Number(me.vmid)).managed) {
                                startEmbeddedConsole();
                            } else {
                                startNoVnc();
                            }
                        },
                        failure: () => {
                            if (attempt < 4 && !me.destroyed && !me.qsmConsoleMode) {
                                setTimeout(() => readConfig(attempt + 1), 750);
                                return;
                            }
                            // A Console-only role must always retain its stock
                            // PVE console when the optional Display1 predicate
                            // cannot be read at all. Never expose a blank or
                            // unauthorised QSM view.
                            me.qsmConsoleLoading = false;
                            startNoVnc();
                        },
                    });
                };
                readConfig(0);
            };
            Ext.apply(me, {
                items: [{
                    xtype: 'component',
                    itemId: 'qsm-direct-preparing',
                    html: gettext('Preparing console…'),
                    style: 'display:flex;align-items:center;justify-content:center;background:#000;color:#fff',
                }],
                listeners: {
                    afterrender: selectBackend,
                    activate: selectBackend,
                    beforedestroy: () => {
                        if (me.qsmDirectSession) { me.qsmDirectSession.close(); }
                        me.qsmDirectSession = null;
                        me.qsmDirectFrame = null;
                    },
                },
            });
            me.callParent();
        },
    });

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
            const kind = me.consoleType === 'kvm' ? 'qemu' : (me.consoleType === 'lxc' ? 'lxc' : null);
            if (kind && validNode(me.nodename) && validVmid(Number(me.vmid))) {
                me.itemId = 'qsm-direct-console-button';
                me.menu = (me.menu || []).map((item) => Ext.apply({}, item));
                me.menu.push({ xtype: 'menuitem', itemId: 'qsm-direct', text: 'QSM Direct',
                    iconCls: 'fa fa-desktop', disabled: !me.enableQsmDirect,
                    handler: () => openConsole(me, me.nodename, Number(me.vmid), null, kind) });
            }
            me.callParent();
        },
    });

    // The active VM sub-tab is remembered globally (PVE.qemu.Config uses the
    // shared `kvmtab` state), so switching from another VM whose Console tab
    // was open makes the new VM's PVE.panel.Config activate its `console` card
    // *inside* initComponent — insertNodes() -> menu.setSelection() ->
    // activateCard('console') — before PVE.qemu.Config's own initComponent has
    // returned. QemuConfigOverlay below swaps the card xtype only after that
    // callParent(), which is too late in this path: the card was already built
    // as stock noVNC, so arriving from another VM's console landed on noVNC
    // even for a managed VM. insertNodes() populates savedItems from the item
    // definitions and runs before any card is activated, so rewrite the QEMU
    // KVM console xtype here. The guard keeps this to the QEMU console only
    // (never LXC, whose console carries consoleType 'lxc'); the QSM selector
    // then still mounts stock noVNC for an unmanaged VM or a failed predicate.
    Ext.define('PVE.qsmDirect.PanelConfigConsoleOverlay', {
        override: 'PVE.panel.Config',
        insertNodes: function (items) {
            if (Array.isArray(items)) {
                items.forEach((item) => {
                    if (item && item.itemId === 'console' &&
                        item.xtype === 'pveNoVncConsole' && item.consoleType === 'kvm') {
                        item.xtype = 'pveQsmDirectConsole';
                    }
                });
            }
            this.callParent([items]);
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
            const setConsoleProvider = (managed) => {
                // insertNodes (PanelConfigConsoleOverlay above) has already
                // rewritten the QEMU console card to the QSM selector in
                // savedItems, even when the card was activated during this
                // callParent(). Reassert it defensively and record the managed
                // predicate; the card itself mounts QSM Direct only for a
                // managed Display1 and otherwise keeps stock noVNC.
                const console = me.savedItems && me.savedItems.console;
                if (console) {
                    console.xtype = 'pveQsmDirectConsole';
                    console.qsmDirectConfigured = Boolean(managed);
                }
            };
            // Install the selector synchronously. The protected config read
            // below is asynchronous, while the existing Console node is
            // immediately clickable after PVE renders the VM view.
            setConsoleProvider(false);
            const setQsmDirectAvailable = (managed) => {
                const button = me.down('#qsm-direct-console-button');
                if (button) { button.setEnableQsmDirect(managed); }
                setConsoleProvider(managed);
            };
            const refreshQsmDirect = () => {
                if (!validNode(vm && vm.node) || !validVmid(vmid)) { return false; }
                Proxmox.Utils.API2Request({
                    url: `/nodes/${encodeURIComponent(vm.node)}/qemu/${encodeURIComponent(vmid)}/config`,
                    method: 'GET',
                    success: ({ result }) => {
                        const data = result && result.data;
                        setQsmDirectAvailable(displayState(data && data.args, vmid).managed);
                    },
                    failure: () => setQsmDirectAvailable(false),
                });
                // The top Console split button is created late on first
                // visits. The left-navigation Console definition is already
                // available in savedItems, so it can be switched on this
                // first request even if the toolbar needs a retry.
                return Boolean(me.down('#qsm-direct-console-button'));
            };
            // PVE constructs the Console button late on some first visits to
            // a VM view. A direct lookup then sees no component and leaves
            // the item disabled until a full page refresh. Retry once after
            // this view has rendered; no configuration state is inferred
            // client-side, it is still read through PVE's protected API.
            if (!refreshQsmDirect() && typeof me.on === 'function') {
                me.on('afterrender', refreshQsmDirect, me, { single: true });
            }
        },
    });

    // LXC containers: the Console split button gains "QSM Direct" (added by
    // ConsoleButtonOverlay for consoleType 'lxc'), enabled once the node's
    // signalling service confirms this container is set up for the QSM
    // console (its policy exists; the call is authorised with VM.Console).
    // The container's own Console tab keeps the stock xterm.js terminal.
    Ext.define('PVE.qsmDirect.LxcConfigOverlay', {
        override: 'PVE.lxc.Config',
        initComponent: function () {
            const me = this;
            me.callParent();
            const ct = me.pveSelNode && me.pveSelNode.data;
            const vmid = ct ? Number(ct.vmid) : NaN;
            if (!validNode(ct && ct.node) || !validVmid(vmid)) { return; }
            let enabled = null;
            const apply = () => {
                const button = me.down('#qsm-direct-console-button');
                if (button && enabled !== null) { button.setEnableQsmDirect(enabled); }
                return Boolean(button);
            };
            signalRequest('GET', `/nodes/${encodeURIComponent(ct.node)}/lxc/${encodeURIComponent(vmid)}/qsm-direct-settings`)
                .then(() => { enabled = true; }, () => { enabled = false; })
                .then(() => {
                    if (!apply() && typeof me.on === 'function') {
                        me.on('afterrender', apply, me, { single: true });
                    }
                });
        },
    });
}());
