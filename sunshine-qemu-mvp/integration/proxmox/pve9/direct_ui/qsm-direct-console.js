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
    const guestArguments = (vmid) => [
        `-chardev socket,id=${DIRECT_AGENT_ID},path=${RUNTIME_PREFIX}/${vmid}/qsm-agent.sock,server=on,wait=off`,
        '-device virtio-serial-pci,id=qsm-direct-serial',
        `-device virtserialport,chardev=${DIRECT_AGENT_ID},name=org.qsm.direct.agent`,
    ];
    const displayPattern = (vmid) => new RegExp(
        `(?:^|\\s)-display\\s+dbus,addr=unix:path=${escapeRegExp(RUNTIME_PREFIX)}/${vmid}/qemu-display1\\.bus,gl=on,rendernode=(/dev/dri/renderD[0-9]{1,4})(?=\\s|$)`,
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
    const removeManagedGuestChannel = (args, vmid) => guestArguments(vmid).reduce((current, value) =>
        current.replace(new RegExp(`(?:^|\\s)${escapeRegExp(value)}(?=\\s|$)`), ''), args,
    ).trim().replace(/\s{2,}/g, ' ');
    const addManagedGuestChannel = (args, vmid) => {
        if (args.includes(DIRECT_AGENT_ID) && !managedGuestChannel(args, vmid)) {
            throw new Error('unsafe QSM guest channel arguments');
        }
        return managedGuestChannel(args, vmid) ? args : `${args} ${guestArguments(vmid).join(' ')}`.trim();
    };
    const displayState = (args, vmid) => {
        if (typeof args !== 'string' || !validVmid(vmid)) {
            return { managed: false, legacy: false, legacyGpu: false, rendernode: DEFAULT_RENDER_NODE };
        }
        const match = args.match(displayPattern(vmid));
        const gpu = virtioVgaGlArguments(args).map(normaliseArgument);
        if (match && displayCount(args) === 1) {
            if (gpu.length === 1 && gpu[0] === gpuArgument()) {
                return { managed: true, legacy: false, legacyGpu: false, guest: managedGuestChannel(args, vmid), rendernode: match[1] };
            }
            // git20 emitted the unlabelled VirtIO-GPU argument. It is safe to
            // migrate only that exact historical form; any device options or
            // a second adapter belong to an administrator and must not be
            // silently claimed or duplicated by this UI overlay.
            if (gpu.length === 1 && gpu[0] === '-device virtio-vga-gl') {
                return { managed: false, legacy: true, legacyGpu: true, guest: managedGuestChannel(args, vmid), rendernode: match[1] };
            }
            if (gpu.length === 0) {
                return { managed: false, legacy: true, legacyGpu: false, guest: managedGuestChannel(args, vmid), rendernode: match[1] };
            }
        }
        return { managed: false, legacy: false, legacyGpu: false, rendernode: DEFAULT_RENDER_NODE };
    };
    const updateDisplayArgument = (args, vmid, rendernode, want) => {
        args = args === undefined || args === null ? '' : args;
        if (typeof args !== 'string' || args.length > MAX_QEMU_ARGS_BYTES || /[\x00-\x1f\x7f]/.test(args)) {
            throw new Error('unsafe QEMU display arguments');
        }
        const existing = displayState(args, vmid);
        const count = displayCount(args);
        const gpu = virtioVgaGlArguments(args).map(normaliseArgument);
        const wanted = displayArgument(vmid, rendernode);
        if (want) {
            if (count === 0) {
                if (gpu.length !== 0) { throw new Error('unsafe QEMU Display1 device arguments'); }
                const added = `${gpuArgument()} ${wanted} ${guestArguments(vmid).join(' ')}`;
                return args ? `${args} ${added}` : added;
            }
            if (!existing.managed && !existing.legacy) {
                throw new Error('another QEMU display is configured');
            }
            const previous = displayArgument(vmid, existing.rendernode);
            const updated = args.replace(new RegExp(`(?:^|\\s)${escapeRegExp(previous)}(?=\\s|$)`), (value) =>
                value.startsWith(' ') ? ` ${wanted}` : wanted);
            if (existing.managed) { return addManagedGuestChannel(updated, vmid); }
            if (existing.legacyGpu) {
                return addManagedGuestChannel(updated.replace(
                    /(?:^|\s)-device\s+virtio-vga-gl(?=\s|$)/,
                    (value) => value.startsWith(' ') ? ` ${gpuArgument()}` : gpuArgument(),
                ), vmid);
            }
            return addManagedGuestChannel(`${updated} ${gpuArgument()}`, vmid);
        }
        if (!existing.managed && !existing.legacy) {
            if (gpu.length !== 0) { throw new Error('unsafe QEMU Display1 device arguments'); }
            return args;
        }
        const previous = displayArgument(vmid, existing.rendernode);
        const withoutDisplay = args.replace(
            new RegExp(`(?:^|\\s)${escapeRegExp(previous)}(?=\\s|$)`), '').trim();
        const withoutGpu = existing.legacyGpu
            ? withoutDisplay.replace(/(?:^|\s)-device\s+virtio-vga-gl(?=\s|$)/, '')
            : withoutDisplay.replace(
                new RegExp(`(?:^|\\s)${escapeRegExp(gpuArgument())}(?=\\s|$)`), '');
        return removeManagedGuestChannel(withoutGpu, vmid);
    };

    const displayFields = () => [
        {
            xtype: 'proxmoxcheckbox', name: 'qsm_direct_display1', uncheckedValue: 0,
            defaultValue: 0, deleteDefaultValue: true, fieldLabel: gettext('QSM Display1'),
            boxLabel: gettext('Replace VNC with D-Bus Display1 and VirGL GPU (PVE display becomes None)'),
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
            'When enabled, PVE Graphic card is intentionally saved as None: this prevents PVE from adding an incompatible VNC backend. QSM Direct owns the private D-Bus display, VirtIO-GPU (VirGL), and an optional guest-tools serial channel for clipboard and files. Restart the VM after changing this setting. To return to VNC, disable QSM Display1 and select a PVE graphic card.'),
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

    const openConsole = function (button, node, vmid) {
        const windowId = `qsm-direct-${node}-${vmid}-${Date.now()}`;
        // `window.open` must run synchronously in the Console-menu click
        // handler. Opening it after an await makes ordinary browsers treat it
        // as an unsolicited popup. It is an about:blank, same-origin popup:
        // PVE credentials and signalling remain in the parent PVE page.
        const popup = window.open('', windowId,
            'popup=yes,width=1280,height=800,resizable=yes,scrollbars=no');
        if (!popup) {
            Ext.Msg.alert(gettext('QSM Direct'), gettext(
                'The browser blocked the separate console window. Allow popups for this Proxmox site and try again.'));
            return;
        }
        const document = popup.document;
        document.title = gettext('QSM Direct Console');
        document.documentElement.style.cssText = 'height:100%;background:#000';
        // Keep the video viewport equal to the entire popup.  The controls
        // deliberately float above it: a desktop console must not silently
        // lose a row of guest pixels merely because its window has controls.
        document.body.style.cssText = 'height:100%;margin:0;position:relative;overflow:hidden;background:#000;color:#fff;font:13px sans-serif';
        const toolbar = document.createElement('div');
        toolbar.setAttribute('aria-label', gettext('Console controls'));
        toolbar.style.cssText = 'position:absolute;z-index:10;top:0;left:0;right:0;display:flex;align-items:center;gap:8px;padding:6px 8px;background:rgba(17,24,39,.88);box-shadow:0 1px 6px rgba(0,0,0,.55);opacity:1;transform:translateY(0);transition:opacity .16s ease,transform .16s ease';
        const status = document.createElement('span');
        status.textContent = gettext('Connecting…');
        status.style.flex = '1 1 auto';
        const fullscreen = document.createElement('button');
        fullscreen.type = 'button';
        fullscreen.style.cssText = 'padding:4px 9px;cursor:pointer';
        const video = document.createElement('video');
        video.autoplay = true;
        video.playsInline = true;
        // The SDP has an Opus m-line as well as video. Chromium and Safari
        // are allowed to reject an asynchronous, unmuted `play()` in a popup
        // even when that popup itself was opened by a Console-menu gesture.
        // Muting before the first track makes the visual console autoplay
        // deterministically; audio can be enabled explicitly afterwards.
        video.muted = true;
        video.tabIndex = 0;
        video.style.cssText = 'display:block;width:100%;height:100%;background:#000;object-fit:contain;outline:none';
        const audio = document.createElement('button');
        audio.type = 'button';
        audio.style.cssText = 'padding:4px 9px;cursor:pointer';
        const setAudioLabel = () => {
            audio.textContent = video.muted ? gettext('Enable Audio') : gettext('Mute Audio');
        };
        setAudioLabel();
        audio.addEventListener('click', () => {
            video.muted = !video.muted;
            setAudioLabel();
            // A user gesture on this explicit control satisfies the browser
            // audio-autoplay policy without making video startup depend on it.
            video.play().catch(() => undefined);
        });
        const setFullscreenLabel = () => {
            fullscreen.textContent = document.fullscreenElement
                ? gettext('Exit Full Screen') : gettext('Full Screen');
        };
        setFullscreenLabel();
        fullscreen.addEventListener('click', () => {
            const action = document.fullscreenElement
                ? document.exitFullscreen()
                : document.documentElement.requestFullscreen();
            if (action && typeof action.catch === 'function') { action.catch(() => undefined); }
        });
        document.addEventListener('fullscreenchange', setFullscreenLabel);
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.textContent = gettext('Copy');
        copy.title = gettext('Copy guest clipboard to this browser');
        copy.style.cssText = 'padding:4px 9px;cursor:pointer';
        const paste = document.createElement('button');
        paste.type = 'button';
        paste.textContent = gettext('Paste');
        paste.title = gettext('Paste this browser clipboard into the guest');
        paste.style.cssText = 'padding:4px 9px;cursor:pointer';
        const upload = document.createElement('button');
        upload.type = 'button';
        upload.textContent = gettext('Upload');
        upload.title = gettext('Upload a file to the guest exchange folder');
        upload.style.cssText = 'padding:4px 9px;cursor:pointer';
        const download = document.createElement('button');
        download.type = 'button';
        download.textContent = gettext('Download');
        download.title = gettext('Download a named file from the guest exchange folder');
        download.style.cssText = 'padding:4px 9px;cursor:pointer';
        const fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.style.display = 'none';
        toolbar.append(status, copy, paste, upload, download, audio, fullscreen);
        document.body.append(video, toolbar, fileInput);
        popup.focus();

        let peer = null;
        let control = null;
        let pointer = null;
        let observer = null;
        let closed = false;
        let closeWatcher = null;
        let pointerFrame = null;
        let pendingPointer = null;
        let resizeTimer = null;
        let firstFrameTimer = null;
        let toolbarTimer = null;
        let guestRequestNumber = 0;
        const guestRequests = new Map();
        const guestDownloads = new Map();
        const guestUploadChunkBytes = 32 * 1024;
        let lastResize = '';
        const revealToolbar = () => {
            if (closed) { return; }
            if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); toolbarTimer = null; }
            toolbar.style.opacity = '1';
            toolbar.style.transform = 'translateY(0)';
        };
        const hideToolbarSoon = () => {
            if (closed || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) { return; }
            if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); }
            toolbarTimer = popup.setTimeout(() => {
                toolbarTimer = null;
                toolbar.style.opacity = '0';
                toolbar.style.transform = 'translateY(-100%)';
            }, 1800);
        };
        toolbar.addEventListener('pointerenter', revealToolbar);
        toolbar.addEventListener('pointerleave', hideToolbarSoon);
        const send = (value) => {
            if (control && control.readyState === 'open') { control.send(JSON.stringify(value)); }
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
        const guestUpload = (file, data) => new Promise((resolve, reject) => {
            if (!control || control.readyState !== 'open') {
                reject(new Error('guest tools are not connected')); return;
            }
            const bytes = new Uint8Array(data);
            const requestId = `qsm-${Date.now()}-${guestRequestNumber += 1}`;
            const transferId = `upload-${Date.now()}-${guestRequestNumber}`;
            const timer = popup.setTimeout(() => {
                guestRequests.delete(requestId);
                reject(new Error('guest tools did not respond'));
            }, 40000);
            guestRequests.set(requestId, { resolve, reject, timer });
            // A browser is allowed to negotiate an SCTP max-message-size far
            // below a file's 2 MiB product limit. Send an ordered sequence
            // whose JSON/base64 envelope is safely below the 64 KiB baseline.
            for (let offset = 0; offset < Math.max(1, bytes.length); offset += guestUploadChunkBytes) {
                const end = Math.min(bytes.length, offset + guestUploadChunkBytes);
                control.send(JSON.stringify({
                    op: 'qsm_guest_file_upload_chunk', request_id: requestId, transfer_id: transferId,
                    name: file.name, size: bytes.length, offset,
                    data_b64: bytesToB64(bytes.subarray(offset, end)),
                }));
            }
        });
        const copyToBrowser = async (text) => {
            if (!popup.navigator.clipboard || !popup.navigator.clipboard.writeText) {
                throw new Error('browser clipboard access is unavailable');
            }
            await popup.navigator.clipboard.writeText(text);
        };
        const pasteFromBrowser = async () => {
            if (!popup.navigator.clipboard || !popup.navigator.clipboard.readText) {
                throw new Error('browser clipboard access is unavailable');
            }
            const text = await popup.navigator.clipboard.readText();
            await guestRequest('qsm_guest_clipboard_set', { text_b64: bytesToB64(new TextEncoder().encode(text)) });
            status.textContent = gettext('Clipboard pasted into guest');
        };
        const guestClipboardToBrowser = async () => {
            const result = await guestRequest('qsm_guest_clipboard_get');
            if (!result || typeof result.text_b64 !== 'string') { throw new Error('invalid guest clipboard'); }
            await copyToBrowser(new TextDecoder('utf-8', { fatal: true }).decode(b64ToBytes(result.text_b64)));
            status.textContent = gettext('Guest clipboard copied');
        };
        copy.addEventListener('click', () => { guestClipboardToBrowser().catch(() => {
            status.textContent = gettext('Guest clipboard is unavailable. Install and start QSM Guest Agent.');
        }); });
        paste.addEventListener('click', () => { pasteFromBrowser().catch(() => {
            status.textContent = gettext('Browser clipboard is unavailable.');
        }); });
        upload.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', () => {
            const file = fileInput.files && fileInput.files[0];
            fileInput.value = '';
            if (!file) { return; }
            if (file.size > 2 * 1024 * 1024) {
                status.textContent = gettext('File transfer is limited to 2 MiB per file.'); return;
            }
            file.arrayBuffer().then((data) => guestUpload(file, data)).then(() => {
                status.textContent = gettext('File uploaded to guest exchange folder'); }).catch(() => {
                status.textContent = gettext('File upload failed. Install and start QSM Guest Agent.');
            });
        });
        download.addEventListener('click', () => {
            const name = popup.prompt(gettext('Guest exchange file name:'));
            if (!name) { return; }
            guestRequest('qsm_guest_file_download', { name }).then((result) => {
                if (!result || typeof result.name !== 'string' || typeof result.data_b64 !== 'string') {
                    throw new Error('invalid guest file');
                }
                const link = document.createElement('a');
                link.href = URL.createObjectURL(new Blob([b64ToBytes(result.data_b64)]));
                link.download = result.name;
                link.click();
                popup.setTimeout(() => URL.revokeObjectURL(link.href), 0);
                status.textContent = gettext('Guest file download started');
            }).catch(() => { status.textContent = gettext('Guest file is unavailable.'); });
        });
        const sendPointer = (value) => {
            if (pointer && pointer.readyState === 'open') { pointer.send(JSON.stringify(value)); }
        };
        const close = () => {
            if (closed) { return; }
            closed = true;
            if (observer) { observer.disconnect(); }
            if (peer) { peer.close(); }
            if (closeWatcher !== null) { window.clearInterval(closeWatcher); }
            if (pointerFrame !== null) { popup.cancelAnimationFrame(pointerFrame); }
            if (resizeTimer !== null) { popup.clearTimeout(resizeTimer); }
            if (firstFrameTimer !== null) { popup.clearTimeout(firstFrameTimer); }
            if (toolbarTimer !== null) { popup.clearTimeout(toolbarTimer); }
            for (const request of guestRequests.values()) {
                popup.clearTimeout(request.timer);
                request.reject(new Error('console closed'));
            }
            guestRequests.clear();
            guestDownloads.clear();
        };
        const closeForStoppedVm = () => {
            if (closed) { return; }
            status.textContent = gettext('The virtual machine was stopped. Closing console…');
            // A state transition is dispatched while the peer is processing
            // transport shutdown. Defer the actual close one task so browsers
            // consistently complete that transition before the popup goes.
            window.setTimeout(() => {
                close();
                if (!popup.closed) { popup.close(); }
            }, 0);
        };
        popup.addEventListener('beforeunload', close, { once: true });
        window.addEventListener('beforeunload', close, { once: true });
        closeWatcher = window.setInterval(() => {
            if (popup.closed) { close(); }
        }, 500);

        const updateMediaStatus = () => {
            if (closed || !peer) { return; }
            if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
                video.videoWidth > 0 && video.videoHeight > 0) {
                status.textContent = gettext('Connected');
                if (firstFrameTimer !== null) {
                    popup.clearTimeout(firstFrameTimer);
                    firstFrameTimer = null;
                }
                hideToolbarSoon();
            } else if (peer.connectionState === 'connected') {
                status.textContent = gettext('Connected — waiting for guest video…');
            }
        };

        const connect = async () => {
            try {
            peer = new RTCPeerConnection();
            control = peer.createDataChannel('qsm-control', { ordered: true });
            // Cursor positions are latest-state samples. Sending them as a
            // reliable ordered stream lets one congested packet make every
            // later gesture visibly stale, while keys/clicks must remain
            // ordered and reliable on qsm-control.
            pointer = peer.createDataChannel('qsm-pointer', { ordered: false, maxRetransmits: 0 });
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
                if (event.track.kind === 'video' && event.receiver &&
                    'playoutDelayHint' in event.receiver) {
                    event.receiver.playoutDelayHint = 0;
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
            const resize = (immediate = false) => {
                const dispatch = () => {
                    resizeTimer = null;
                    const value = dimensions(video);
                    const identity = `${value.width}x${value.height}@${value.fps}`;
                    if (identity !== lastResize) {
                        lastResize = identity;
                        send({ op: 'resize', ...value });
                    }
                };
                if (resizeTimer !== null) { popup.clearTimeout(resizeTimer); resizeTimer = null; }
                if (immediate) { dispatch(); }
                else { resizeTimer = popup.setTimeout(dispatch, 150); }
            };
            const flushPointer = () => {
                pointerFrame = null;
                if (pendingPointer) { sendPointer(pendingPointer); pendingPointer = null; }
            };
            const queuePointer = (value) => {
                pendingPointer = value;
                if (pointerFrame === null) { pointerFrame = popup.requestAnimationFrame(flushPointer); }
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
                } else if (message.op === 'qsm_guest_file_download_chunk' &&
                    typeof message.request_id === 'string') {
                    const request = guestRequests.get(message.request_id);
                    const reject = () => {
                        if (!request) { return; }
                        guestRequests.delete(message.request_id);
                        guestDownloads.delete(message.request_id);
                        popup.clearTimeout(request.timer);
                        request.reject(new Error('invalid guest file transfer'));
                    };
                    if (!request || typeof message.name !== 'string' || !Number.isInteger(message.size) ||
                        !Number.isInteger(message.offset) || typeof message.data_b64 !== 'string' ||
                        message.size < 0 || message.size > 2 * 1024 * 1024 ||
                        message.offset < 0 || message.offset > message.size) {
                        reject(); return;
                    }
                    let chunk;
                    try { chunk = b64ToBytes(message.data_b64); } catch (_error) { reject(); return; }
                    let transfer = guestDownloads.get(message.request_id);
                    if (!transfer) {
                        if (message.offset !== 0) { reject(); return; }
                        transfer = { name: message.name, size: message.size, bytes: [], received: 0 };
                        guestDownloads.set(message.request_id, transfer);
                    }
                    if (transfer.name !== message.name || transfer.size !== message.size ||
                        transfer.received !== message.offset || chunk.length > transfer.size - transfer.received ||
                        (chunk.length === 0 && transfer.received !== transfer.size)) {
                        reject(); return;
                    }
                    transfer.bytes.push(chunk);
                    transfer.received += chunk.length;
                    if (transfer.received === transfer.size) {
                        const bytes = new Uint8Array(transfer.size);
                        let cursor = 0;
                        for (const part of transfer.bytes) { bytes.set(part, cursor); cursor += part.length; }
                        guestDownloads.delete(message.request_id);
                        guestRequests.delete(message.request_id);
                        popup.clearTimeout(request.timer);
                        request.resolve({ name: transfer.name, data_b64: bytesToB64(bytes), bytes: bytes.length });
                    }
                } else if (message.op === 'qsm_guest_clipboard' && typeof message.text_b64 === 'string') {
                    try { copyToBrowser(new TextDecoder('utf-8', { fatal: true }).decode(b64ToBytes(message.text_b64))).catch(() => undefined); }
                    catch (_error) { /* Ignore malformed guest clipboard notifications. */ }
                }
            });
            // aiortc closes every server-side data channel while retiring a
            // VM session. Track-end is the normal media signal, but this is
            // an independent browser-visible lifecycle signal for codecs or
            // browsers that postpone a track's `ended` event.
            control.addEventListener('close', closeForStoppedVm, { once: true });
            pointer.addEventListener('close', closeForStoppedVm, { once: true });
            observer = new ResizeObserver(() => resize(false));
            observer.observe(video);
            video.addEventListener('mousemove', (event) => {
                revealToolbar();
                hideToolbarSoon();
                const box = video.getBoundingClientRect();
                const x = Math.max(0, Math.min(Math.floor(event.clientX - box.left), Math.floor(box.width) - 1));
                const y = Math.max(0, Math.min(Math.floor(event.clientY - box.top), Math.floor(box.height) - 1));
                queuePointer({ op: 'mouse_position', x, y, width: Math.max(1, Math.floor(box.width)), height: Math.max(1, Math.floor(box.height)) });
            });
            video.addEventListener('mousedown', (event) => { flushPointer(); video.focus(); send({ op: 'mouse_button', button: event.button + 1, down: true }); event.preventDefault(); });
            video.addEventListener('mouseup', (event) => { send({ op: 'mouse_button', button: event.button + 1, down: false }); event.preventDefault(); });
            video.addEventListener('wheel', (event) => { send({ op: 'scroll', vertical: Math.max(-32768, Math.min(32767, Math.trunc(event.deltaY))), horizontal: Math.max(-32768, Math.min(32767, Math.trunc(event.deltaX))) }); event.preventDefault(); }, { passive: false });
            video.addEventListener('paste', (event) => {
                const text = event.clipboardData && event.clipboardData.getData('text/plain');
                if (typeof text !== 'string') { return; }
                event.preventDefault();
                guestRequest('qsm_guest_clipboard_set', {
                    text_b64: bytesToB64(new TextEncoder().encode(text)),
                }).catch(() => { status.textContent = gettext('Guest clipboard is unavailable.'); });
            });
            for (const name of ['keydown', 'keyup']) {
                video.addEventListener(name, (event) => {
                    const key = qemuKey(event);
                    const pasteShortcut = event.code === 'KeyV' && (event.ctrlKey || event.metaKey);
                    if (pasteShortcut) {
                        event.preventDefault();
                        if (name === 'keydown') { pasteFromBrowser().catch(() => {
                            status.textContent = gettext('Browser clipboard is unavailable.');
                        }); }
                    } else if (key !== null) {
                        send({ op: 'keyboard', key, down: name === 'keydown', modifiers: 0 }); event.preventDefault();
                    }
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
            status.textContent = gettext('Negotiating guest media…');
            // A transport connection is not visual output. Keep the popup
            // usable for a slow guest, but make a Display1/GL capture stall
            // explicit instead of reporting a misleading plain "Connected".
            firstFrameTimer = popup.setTimeout(() => {
                if (!closed && video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
                    status.textContent = gettext('Connected, but the guest has not produced a video frame.');
                }
            }, 7000);
            video.focus();
            } catch (_error) {
                status.textContent = gettext('Could not create a direct browser console.');
                close();
                popup.close();
                Ext.Msg.alert(gettext('QSM Direct'), gettext('Could not create a direct browser console.'));
            }
        };
        connect();
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
            const refreshQsmDirect = () => {
                const button = me.down('#qsm-direct-console-button');
                if (!button || !validNode(vm && vm.node) || !validVmid(vmid)) { return false; }
                Proxmox.Utils.API2Request({
                    url: `/nodes/${encodeURIComponent(vm.node)}/qemu/${encodeURIComponent(vmid)}/config`,
                    method: 'GET',
                    success: ({ result }) => {
                        const data = result && result.data;
                        button.setEnableQsmDirect(displayState(data && data.args, vmid).managed);
                    },
                    failure: () => button.setEnableQsmDirect(false),
                });
                return true;
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
}());
