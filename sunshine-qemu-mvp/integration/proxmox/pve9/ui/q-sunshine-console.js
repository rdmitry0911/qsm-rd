/*
 * q-sunshine Console menu integration for Proxmox VE 9.
 *
 * This file is loaded after pvemanagerlib.js by the package-managed
 * index.html.tpl diversion. It deliberately does not know a PVE password,
 * PVEAuthCookie, CSRF token, VNC ticket, or terminal endpoint. The protected
 * q-sunshine PVE API route is the authorization boundary. API2Request carries
 * PVE's ordinary authenticated-session and CSRF handling to that same-origin
 * route, which checks VM.Console before it asks the local terminal service to
 * issue a one-use launch descriptor.
 */
/*
 * Do not add a "use strict" directive to this script/IIFE. ExtJS 6/7's legacy
 * `callParent()` discovers the currently executing override through
 * Function.caller. A strict override hides that caller and makes
 * `callParent()` attempt `null.apply(...)`, breaking PVE's Console button
 * before its menu can render. PVE's own pvemanagerlib.js is likewise a
 * non-strict ExtJS script; this overlay uses declarations and explicit checks
 * so it does not need strict-mode-only behavior.
 */
(function () {
    const MIN_VMID = 100;
    const MAX_VMID = 999999999;
    const MAX_QEMU_ARGS_BYTES = 8192;
    const DEFAULT_RENDER_NODE = '/dev/dri/renderD128';

    const exactKeys = function (value, expected) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
            return false;
        }
        const actual = Object.keys(value).sort();
        if (actual.length !== expected.length) {
            return false;
        }
        return expected.every((key, index) => actual[index] === key);
    };

    const validNode = (value) =>
        typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$/.test(value);

    const validPort = (value) => Number.isInteger(value) && value >= 1 && value <= 65535;

    const validVmid = (value) =>
        Number.isInteger(value) && value >= MIN_VMID && value <= MAX_VMID;

    const validRenderNode = (value) =>
        typeof value === 'string' && /^\/dev\/dri\/renderD[0-9]{1,4}$/.test(value);

    const enabled = (value) => value === true || value === 1 || value === '1';

    const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

    const qsunshineDisplayArgument = (vmid, rendernode) => {
        if (!validVmid(vmid) || !validRenderNode(rendernode)) {
            throw new Error('invalid q-sunshine Display1 settings');
        }
        return `-display dbus,addr=unix:path=/run/q-sunshine/${vmid}/qemu-display1.bus,gl=on,rendernode=${rendernode}`;
    };

    const qsunshineDisplayPattern = (vmid) => new RegExp(
        `(?:^|\\s)-display\\s+dbus,addr=unix:path=/run/q-sunshine/${vmid}/qemu-display1\\.bus,gl=on,rendernode=(/dev/dri/renderD[0-9]{1,4})(?=\\s|$)`,
    );

    const displayArgumentCount = (args) => (args.match(/(?:^|\s)-display(?:\s|$)/g) || []).length;

    const qsunshineDisplayState = (args, vmid) => {
        if (typeof args !== 'string' || args.length === 0 || !validVmid(vmid)) {
            return { managed: false, rendernode: DEFAULT_RENDER_NODE };
        }
        const match = args.match(qsunshineDisplayPattern(vmid));
        if (!match || displayArgumentCount(args) !== 1) {
            return { managed: false, rendernode: DEFAULT_RENDER_NODE };
        }
        return { managed: true, rendernode: match[1] };
    };

    const qsunshineDisplayConfigurationError = () => {
        throw new Error('q-sunshine Display1 cannot safely change this VM display configuration');
    };

    // PVE exposes arbitrary QEMU command line arguments as one string.  This
    // helper intentionally never attempts to parse or rewrite arbitrary QEMU
    // syntax: it manages exactly one unquoted Display1 argument, refuses a
    // second -display, and leaves every other argument byte-for-byte intact.
    const updateQsunshineDisplayArgument = (args, vmid, rendernode, shouldEnable) => {
        if (args === undefined || args === null) {
            args = '';
        }
        if (typeof args !== 'string' || args.length > MAX_QEMU_ARGS_BYTES || /[\x00-\x1f\x7f]/.test(args)) {
            qsunshineDisplayConfigurationError();
        }
        const wanted = qsunshineDisplayArgument(vmid, rendernode);
        const existing = qsunshineDisplayState(args, vmid);
        const hasQsunshinePath = qsunshineDisplayPattern(vmid).test(args);
        const count = displayArgumentCount(args);
        if (hasQsunshinePath && !existing.managed) {
            qsunshineDisplayConfigurationError();
        }
        if (shouldEnable) {
            if (count === 0) {
                return args.length === 0 ? wanted : `${args} ${wanted}`;
            }
            if (!existing.managed) {
                qsunshineDisplayConfigurationError();
            }
            const oldArgument = qsunshineDisplayArgument(vmid, existing.rendernode);
            return args.replace(new RegExp(`(?:^|\\s)${escapeRegExp(oldArgument)}(?=\\s|$)`), (value) =>
                value.startsWith(' ') ? ` ${wanted}` : wanted);
        }
        if (!existing.managed) {
            return args;
        }
        const oldArgument = qsunshineDisplayArgument(vmid, existing.rendernode);
        const updated = args.replace(new RegExp(`(?:^|\\s)${escapeRegExp(oldArgument)}(?=\\s|$)`), '');
        return updated.trim().replace(/\s{2,}/g, ' ');
    };

    const windowVmid = (window) => {
        const selected = window && window.pveSelNode && window.pveSelNode.data;
        const vmid = selected ? Number(selected.vmid) : NaN;
        return validVmid(vmid) ? vmid : null;
    };

    const qsunshineDisplayFields = (forCreate) => [
        {
            xtype: 'proxmoxcheckbox',
            name: 'qsm_display1',
            uncheckedValue: 0,
            defaultValue: 0,
            deleteDefaultValue: true,
            fieldLabel: gettext('q-sunshine Display1'),
            boxLabel: gettext('D-Bus display with VirGL GPU'),
            listeners: {
                change: function (field, value) {
                    const panel = field.up('inputpanel');
                    const renderNode = panel && panel.down('[name=qsm_rendernode]');
                    if (renderNode) {
                        renderNode.setDisabled(!enabled(value));
                    }
                },
            },
            validator: function (value) {
                if (!enabled(value)) {
                    return true;
                }
                if (forCreate) {
                    const wizard = this.up('pveQemuCreateWizard');
                    const vmidField = wizard && wizard.down('[name=vmid]');
                    if (!vmidField || !validVmid(Number(vmidField.getValue()))) {
                        return gettext('Choose a fixed valid VM ID before enabling q-sunshine Display1.');
                    }
                } else {
                    const window = this.up('proxmoxWindowEdit');
                    const vmid = windowVmid(window);
                    const renderNode = window && window.down('[name=qsm_rendernode]');
                    try {
                        updateQsunshineDisplayArgument(
                            window && window.vmconfig ? window.vmconfig.args : undefined,
                            vmid,
                            renderNode ? renderNode.getValue() : DEFAULT_RENDER_NODE,
                            true,
                        );
                    } catch (_error) {
                        return gettext('The existing QEMU display arguments cannot be changed safely.');
                    }
                }
                return true;
            },
        },
        {
            xtype: 'textfield',
            name: 'qsm_rendernode',
            value: DEFAULT_RENDER_NODE,
            disabled: true,
            fieldLabel: gettext('Render node'),
            allowBlank: false,
            validator: function (value) {
                return validRenderNode(value) || gettext('Use a DRM render node, for example /dev/dri/renderD128.');
            },
        },
        {
            xtype: 'displayfield',
            userCls: 'pmx-hint',
            value: gettext(
                'Requires a stopped, provisioned q-sunshine VM. This changes only the managed Display1 D-Bus argument and selects VirGL GPU; QSF guest-agent devices remain explicit VM hardware.',
            ),
        },
    ];

    const qsunshineCreateValues = (values) => {
        const result = Ext.apply({}, values);
        const shouldEnable = enabled(result.qsm_display1);
        const rendernode = result.qsm_rendernode || DEFAULT_RENDER_NODE;
        delete result.qsm_display1;
        delete result.qsm_rendernode;
        if (!shouldEnable) {
            return result;
        }
        const vmid = Number(result.vmid);
        if (!validVmid(vmid) || !validRenderNode(rendernode)) {
            qsunshineDisplayConfigurationError();
        }
        result.args = updateQsunshineDisplayArgument(result.args, vmid, rendernode, true);
        // PVE owns creation of the virtio-vga-gl device.  Do not add a second
        // -device argument through args, otherwise a later PVE upgrade can
        // give the VM two GPUs or a duplicate QEMU id.
        result.vga = 'virtio-gl';
        return result;
    };

    const validHost = (value) =>
        typeof value === 'string' && value.length > 0 && value.length <= 253 &&
        /^[A-Za-z0-9:][A-Za-z0-9.:-]*$/.test(value);

    const validServerName = validHost;

    const validLaunchFile = function (data) {
        if (!exactKeys(data, ['claim', 'endpoint', 'expires_at_utc_ms', 'kind', 'version']) ||
            data.version !== 1 || data.kind !== 'q-sunshine-pve-launch' ||
            typeof data.claim !== 'string' || data.claim.length < 16 || data.claim.length > 4096 ||
            !/^[A-Za-z0-9._-]+$/.test(data.claim) ||
            !Number.isInteger(data.expires_at_utc_ms) || data.expires_at_utc_ms <= Date.now()) {
            return false;
        }
        const endpoint = data.endpoint;
        return exactKeys(endpoint, ['ca_pem', 'host', 'port', 'server_name']) &&
            validHost(endpoint.host) && validPort(endpoint.port) && validServerName(endpoint.server_name) &&
            typeof endpoint.ca_pem === 'string' && endpoint.ca_pem.length > 0 &&
            endpoint.ca_pem.length <= 65536 && endpoint.ca_pem.includes('-----BEGIN CERTIFICATE-----');
    };

    const launchError = function () {
        Ext.Msg.alert(gettext('q-sunshine'), gettext('Could not create a q-sunshine launch file.'));
    };

    const requestLaunchFile = function (button, node, vmid) {
        return new Promise((resolve, reject) => {
            Proxmox.Utils.API2Request({
                // API2Request deliberately expects an API-relative path. It
                // supplies PVE's authenticated session and CSRF header and
                // adds `/api2/extjs` itself. Supplying `/api2/json` here looks
                // like an HTTP success but is an ExtJS API failure because its
                // envelope does not carry the `success` field API2Request uses.
                url: `/nodes/${encodeURIComponent(node)}/qemu/${encodeURIComponent(vmid)}/q-sunshine`,
                method: 'POST',
                waitMsgTarget: button,
                success: ({ result }) => {
                    const launchFile = result && result.data;
                    if (!validLaunchFile(launchFile)) {
                        reject(new Error('invalid q-sunshine launch response'));
                        return;
                    }
                    resolve(launchFile);
                },
                failure: () => reject(new Error('PVE rejected console launch')),
            });
        });
    };

    const downloadLaunchFile = function (launchFile, node, vmid) {
        // Keep the claim out of URLs, query strings, referrers, filenames and
        // console logs. It exists only inside the downloaded .qsm payload.
        const blob = new Blob([JSON.stringify(launchFile)], {
            type: 'application/vnd.q-sunshine-launch+json',
        });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `q-sunshine-${node}-${vmid}.qsm`;
        anchor.style.display = 'none';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
    };

    // PVE 9 has a VirGL graphic-card choice but no UI for QEMU's Display1
    // D-Bus backend. Keep this as a narrow UI overlay: the server-side PVE
    // schema and its normal VM.Config ACLs still validate the final vga/args
    // update. This does not invent an unsupported PVE configuration key.
    Ext.define('PVE.qsunshine.DisplayInputPanelOverlay', {
        override: 'PVE.qemu.DisplayInputPanel',

        initComponent: function () {
            const me = this;
            me.advancedItems = (me.advancedItems || []).concat(qsunshineDisplayFields(false));
            me.callParent();
        },

        onGetValues: function (values) {
            const me = this;
            const edit = me.up('proxmoxWindowEdit');
            const vmid = windowVmid(edit);
            const shouldEnable = enabled(values.qsm_display1);
            const rendernode = values.qsm_rendernode || DEFAULT_RENDER_NODE;
            if ((shouldEnable && (!validVmid(vmid) || !validRenderNode(rendernode))) ||
                !edit || !edit.vmconfig) {
                qsunshineDisplayConfigurationError();
            }
            const clean = {
                type: shouldEnable ? 'virtio-gl' : values.type,
                memory: values.memory,
            };
            const printed = PVE.Parser.printPropertyString(clean, 'type');
            const result = printed === '' ? { delete: 'vga' } : { vga: printed };
            const changedArgs = updateQsunshineDisplayArgument(
                edit.vmconfig.args,
                vmid,
                rendernode,
                shouldEnable,
            );
            if (changedArgs !== (edit.vmconfig.args || '')) {
                result.args = changedArgs;
            }
            return result;
        },
    });

    // The VM wizard has no dedicated Display editor. It receives the same
    // explicit advanced page and normalises its values before PVE's normal
    // create request. A VMID is required rather than guessing a path for an
    // automatically assigned ID.
    Ext.define('PVE.qsunshine.CreateWizardOverlay', {
        override: 'PVE.qemu.CreateWizard',

        initComponent: function () {
            const me = this;
            const items = (me.items || []).slice();
            const confirmIndex = items.findIndex((item) => item && item.title === gettext('Confirm'));
            if (confirmIndex >= 0) {
                items.splice(confirmIndex, 0, {
                    xtype: 'inputpanel',
                    itemId: 'q-sunshine-create-display',
                    title: gettext('Display (Advanced)'),
                    onlineHelp: 'qm_display',
                    items: qsunshineDisplayFields(true),
                });
                me.items = items;
            }
            me.callParent();
        },

        getValues: function () {
            const me = this;
            return qsunshineCreateValues(me.callParent(arguments));
        },
    });

    // DisplayEdit performs its own asynchronous config load. Chain that
    // callback so the Advanced fields reflect only the one exact argument we
    // manage; hand-written D-Bus arguments are never claimed as managed.
    Ext.define('PVE.qsunshine.DisplayEditOverlay', {
        override: 'PVE.qemu.DisplayEdit',

        initComponent: function () {
            const me = this;
            const stockLoad = me.load;
            me.load = function (options) {
                const chained = Ext.apply({}, options);
                const stockSuccess = chained.success;
                chained.success = function (response) {
                    if (stockSuccess) {
                        stockSuccess.apply(this, arguments);
                    }
                    const data = response && response.result && response.result.data;
                    const state = qsunshineDisplayState(
                        data && data.args,
                        windowVmid(me),
                    );
                    me.setValues({
                        qsm_display1: state.managed ? 1 : 0,
                        qsm_rendernode: state.rendernode,
                    });
                    const renderNode = me.down('[name=qsm_rendernode]');
                    if (renderNode) {
                        renderNode.setDisabled(!state.managed);
                    }
                };
                return stockLoad.call(me, chained);
            };
            me.callParent();
            me.load = stockLoad;
        },
    });

    Ext.define('PVE.qsunshine.ConsoleButtonOverlay', {
        override: 'PVE.button.ConsoleButton',

        qsunshineLaunch: async function () {
            const me = this;
            const node = me.nodename;
            const vmid = Number(me.vmid);
            if (me.consoleType !== 'kvm' || !validNode(node) || !validVmid(vmid)) {
                launchError();
                return;
            }
            try {
                const launchFile = await requestLaunchFile(me, node, vmid);
                downloadLaunchFile(launchFile, node, vmid);
            } catch (_error) {
                // PVE ACL failures and terminal-side details must not reach a
                // browser console, Ext message, task log, or URL.
                launchError();
            }
        },

        initComponent: function () {
            const me = this;
            if (me.consoleType === 'kvm' && validNode(me.nodename) && validVmid(Number(me.vmid))) {
                // The stock class stores menu items on its prototype. Copy the
                // array and item configs so a QEMU button cannot alter LXC,
                // node-shell, or later button instances.
                me.menu = (me.menu || []).map((item) => Ext.apply({}, item));
                me.menu.push({
                    xtype: 'menuitem',
                    itemId: 'q-sunshine',
                    text: 'q-sunshine',
                    iconCls: 'fa fa-desktop',
                    handler: () => me.qsunshineLaunch(),
                });
            }
            me.callParent();
        },
    });
}());
