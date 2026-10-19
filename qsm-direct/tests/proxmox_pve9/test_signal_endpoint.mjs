// Exercise the real overlay's authenticated request and certificate-check URLs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
for (const { endpoint, origin, hostname, expected, prefix = '' } of [
    { endpoint: { port: 18007, gateway: true }, origin: 'https://pve-a:28006', hostname: 'pve-a', expected: 'https://pve-a:28006', prefix: '/qsm-direct' },
    { endpoint: undefined, origin: 'https://pve-a:8006', hostname: 'pve-a', expected: 'https://pve-a:8007' },
    { endpoint: { port: 18019, public_url: '' }, origin: 'https://pve-a:8006', hostname: 'pve-a', expected: 'https://pve-a:18019' },
    { endpoint: { port: 18019, public_url: '' }, origin: 'https://[2001:db8::1]:8006', hostname: '[2001:db8::1]', expected: 'https://[2001:db8::1]:18019' },
    { endpoint: { port: 18019, public_url: 'https://console.example.org:28019' }, origin: 'https://pve-a:8006', hostname: 'pve-a', expected: 'https://console.example.org:28019' },
]) {
    const definitions = new Map();
    const requests = [];
    const opens = [];
    const sandbox = {
        URL, AbortSignal, URLSearchParams, setTimeout, clearTimeout,
        gettext: text => text,
        window: { QSM_DIRECT_ENDPOINT: endpoint, Proxmox: { UserName: 'tester@pve' }, open: (...args) => opens.push(args) },
        location: { origin, hostname },
        Proxmox: { UserName: 'tester@pve' },
        PVE: { Parser: {} },
        Ext: { define: (name, body) => definitions.set(name, body), apply: Object.assign,
            util: { Cookies: { get: () => 'fixture-ticket' } } },
        fetch: async (url, options) => {
            requests.push({ url, options });
            return { ok: true, json: async () => ({ data: {} }) };
        },
    };
    vm.runInNewContext(source, sandbox);
    const view = { pveSelNode: { data: { node: 'pve-a', vmid: 105 } },
        callParent() {}, down() { return null; }, on() {} };
    definitions.get('PVE.qsmDirect.LxcConfigOverlay').initComponent.call(view);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, `${expected}${prefix}/nodes/pve-a/lxc/105/qsm-direct-settings`);
    assert.equal(requests[0].options.headers.Authorization, 'Bearer fixture-ticket');
    assert.equal(requests[0].options.credentials, 'omit');
    const button = { consoleType: 'lxc', nodename: 'pve-a', vmid: 105, callParent() {} };
    definitions.get('PVE.qsmDirect.ConsoleButtonOverlay').initComponent.call(button);
    button.menu.find(item => item.itemId === 'qsm-direct-connection').handler();
    assert.equal(opens[0][0], `${expected}/`);
}
console.log('QSM_SIGNAL_ENDPOINT_BROWSER_OK');
