// SPDX-License-Identifier: GPL-3.0-or-later
// Static/pure checks for the real Playwright lab gate. No browser, PVE URL,
// password file, HTTP mock, or network connection is used here.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const scriptPath = path.resolve(__dirname, '../../lab/proxmox9/qualify-pve-console-launch.cjs');
const source = fs.readFileSync(scriptPath, 'utf8');
const {
    QualificationError,
    StrictJsonParser,
    browserHandoffDiagnostic,
    isValidRouteHost,
    parseUser,
    observeLiveTraffic,
    responseStatusClass,
} = require(scriptPath);

const expectsCode = (code) => (error) => error instanceof QualificationError && error.code === code;

// The executable must observe actual browser traffic only. Its own comments
// may mention route mocking, but a route installation would defeat the gate.
assert.equal(source.includes('.route('), false);
assert.equal(source.includes('API2Request('), false);
assert.equal(source.includes('force: true'), false);

assert.deepEqual(parseUser('qsmconsole@pve'), {
    full: 'qsmconsole@pve',
    username: 'qsmconsole',
    realm: 'pve',
});
assert.throws(() => parseUser('qsmconsole'), expectsCode('INVALID_PVE_USER'));
assert.throws(() => parseUser('qsm@console@pve'), expectsCode('INVALID_PVE_USER'));

const parsedObject = new StrictJsonParser('{"a":1,"nested":{"b":true}}').parse();
assert.equal(Object.getPrototypeOf(parsedObject), null);
assert.equal(parsedObject.a, 1);
assert.equal(Object.getPrototypeOf(parsedObject.nested), null);
assert.equal(parsedObject.nested.b, true);
assert.throws(
    () => new StrictJsonParser('{"claim":"first","claim":"second"}').parse(),
    expectsCode('INVALID_LAUNCH_DESCRIPTOR'),
);
assert.throws(
    () => new StrictJsonParser('{"endpoint":{"host":"a","host":"b"}}').parse(),
    expectsCode('INVALID_LAUNCH_DESCRIPTOR'),
);

assert.equal(isValidRouteHost('terminal.example.test'), true);
assert.equal(isValidRouteHost('2001:db8::1'), true);
assert.equal(isValidRouteHost('terminal.example.test:48123/path'), false);

const observers = Object.create(null);
const page = {
    on: (event, handler) => {
        assert.equal(typeof handler, 'function');
        observers[event] = handler;
    },
};
const observed = observeLiveTraffic(page, {
    pveOrigin: 'https://pve.example.test:8006',
    node: 'qsm-pve9-lab',
    vmid: 100,
});
assert.deepEqual(Object.keys(observers).sort(), ['request', 'requestfailed', 'response']);
observers.request({
    method: () => 'POST',
    url: () => 'https://pve.example.test:8006/api2/extjs/nodes/qsm-pve9-lab/qemu/100/q-sunshine',
});
assert.equal(observed.qsunshine, 1);
assert.equal(observed.invalidRequest, false);
assert.equal(observed.legacyRoute, false);
observers.response({
    request: () => ({
        method: () => 'POST',
        url: () => 'https://pve.example.test:8006/api2/extjs/nodes/qsm-pve9-lab/qemu/100/q-sunshine',
    }),
    status: () => 503,
});
observers.requestfailed({
    method: () => 'POST',
    url: () => 'https://pve.example.test:8006/api2/extjs/nodes/qsm-pve9-lab/qemu/100/q-sunshine',
});
assert.equal(observed.qsunshineResponses, 1);
assert.deepEqual([...observed.qsunshineResponseStatusClasses], ['5xx']);
assert.equal(observed.qsunshineRequestFailures, 1);
assert.equal(responseStatusClass(204), '2xx');
assert.equal(responseStatusClass(999), 'other');
assert.equal(
    browserHandoffDiagnostic('REQUESTING_LAUNCH_FILE', observed),
    'QSM_BROWSER_HANDOFF_DIAGNOSTIC phase=REQUESTING_LAUNCH_FILE expected_post_count=1 ' +
        'expected_response_count=1 expected_response_status_class=5xx expected_requestfailed_count=1',
);
observers.request({
    method: () => 'POST',
    url: () => 'https://pve.example.test:8006/api2/extjs/nodes/qsm-pve9-lab/qemu/100/vncproxy',
});
assert.equal(observed.legacyRoute, true);

assert.equal(source.includes('validateBrokerRequest'), false);
assert.equal(source.includes('brokerSentCookie'), false);
assert.equal(source.includes('const qsunshinePath'), true);

console.log('PVE_BROWSER_QUALIFICATION_STATIC_OK');
