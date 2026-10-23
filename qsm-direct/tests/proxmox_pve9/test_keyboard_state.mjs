import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const section = (start, end) => {
    const a = source.indexOf(start), b = source.indexOf(end, a);
    assert.ok(a >= 0 && b > a);
    return source.slice(a, b);
};
const keyboard = section('const sendKeyboard =', '// Map a printable character');
const listeners = section("for (const name of ['keydown', 'keyup']) {\n                video.addEventListener", "peer.addTransceiver('video'");
const make = new Function('env', `
    const {video, kbInput, document, send, settings, actions} = env;
    const heldKeys = new Set();
    let diagKeySent = 0, diagLastInput = '', fullscreenEscapePending = false;
    const noteGuestInput = () => {}, qemuKey = event => ({KeyA:30, ControlLeft:29, KeyV:47})[event.code] ?? null;
    const KEYBOARD_PRIORITY = {client:'client', raw:'raw'};
    const guestCaptureExitShortcut = () => false, clientFirstShortcut = () => false;
    const leaveGuestKeyboardCapture = () => {}, releaseHeldInput = () => releaseGuestKeys();
    const pasteFromBrowser = () => { actions.push('paste'); return Promise.resolve(); };
    const guestSelectionToBrowser = () => { actions.push('copy'); return Promise.resolve(); };
    const status = {}, gettext = value => value;
    ${keyboard}
    ${listeners}
    return {heldKeys, sendKeyboard};
`);
const target = () => {
    const listeners = new Map();
    return {addEventListener(name, callback) {
        if (!listeners.has(name)) { listeners.set(name, []); }
        listeners.get(name).push(callback);
    }, emit(name, event = {}) {
        for (const callback of listeners.get(name) || []) { callback(event); }
    }};
};
const harness = () => {
    const video = target(), kbInput = target(), document = target(), messages = [], actions = [];
    let open = true;
    const state = make({video, kbInput, document, messages, actions,
        settings:{keyboardPriority:'guest'}, send:message => {
            if (!open) { return false; }
            messages.push(message); return true;
        }});
    return {...state, video, kbInput, document, messages, actions,
        open:value => { open = value; },
        key(name, code = 'KeyA', extra = {}) {
            const event = {code, repeat:false, preventDefault() {}, ...extra};
            if (name === 'keyup') { document.emit(name, event); }
            video.emit(name, event);
        }};
};
const edges = h => h.messages.map(({key, down}) => [key, down]);

{
    const h = harness();
    h.key('keydown');
    for (let i = 0; i < 20; ++i) { h.key('keydown', 'KeyA', {repeat:true}); }
    assert.deepEqual(edges(h), [[30,true]], 'guest autorepeat needs only one press');
    h.key('keyup');
    assert.deepEqual(edges(h), [[30,true],[30,false]], 'capture and target must release exactly once');
}
for (const element of ['video', 'kbInput']) {
    const h = harness(); h.key('keydown'); h[element].emit('blur');
    assert.deepEqual(edges(h), [[30,true],[30,false]], `${element} focus loss stops guest repeat`);
    h.key('keydown', 'KeyA', {repeat:true});
    assert.equal(h.heldKeys.size, 0, 'returning focus while local autorepeat continues must not re-press');
    h.key('keyup'); h.key('keydown'); h.key('keyup');
    assert.deepEqual(edges(h), [[30,true],[30,false],[30,true],[30,false]], 'fresh typing resumes');
}
{
    const h = harness(); h.key('keydown');
    h.document.emit('keyup', {code:'KeyA'});
    assert.deepEqual(edges(h), [[30,true],[30,false]], 'keyup at another target releases guest');
    h.document.emit('keyup', {code:'KeyV'});
    assert.equal(h.messages.length, 2, 'unrelated local keyups never inject guest input');
}
{
    const h = harness(); h.key('keydown', 'KeyA', {repeat:true});
    assert.equal(h.messages.length, 0, 'hover must not adopt a press started outside console');
    h.open(false); h.key('keydown');
    assert.equal(h.heldKeys.size, 0, 'failed transport must not register a held key');
    h.open(true); h.key('keydown'); h.key('keyup');
    assert.deepEqual(edges(h), [[30,true],[30,false]]);
}
{
    const h = harness(); h.key('keydown', 'ControlLeft');
    h.key('keydown', 'KeyV', {ctrlKey:true});
    h.key('keydown', 'KeyV', {ctrlKey:true, repeat:true});
    assert.deepEqual(h.actions, ['paste'], 'host autorepeat must not paste clipboard repeatedly');
    assert.deepEqual(edges(h), [[29,true],[29,false]]);
}
console.log('PASS: guest keyboard edges, focus changes, foreign repeats and clipboard repeat');
