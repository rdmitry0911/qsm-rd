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
    const {video, kbInput, document, send, settings, actions, popup} = env;
    const heldKeys = new Set(), heldMouseButtons = new Set(), physicalHeldKeys = new Set();
    let inputStateEnabled = false, inputStateTimer = null, inputTapsEnabled = false, inputMouseActionsEnabled = false;
    const mouseInputModeHint={};
    const KEY_INPUT_MODE={typing:'client-repeat',physical:'guest-repeat'},keyInputModeHint={};
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
    return {heldKeys, sendKeyboard, acceptInputPolicy, publishInputState};
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
const harness = (keyInputMode='guest-repeat', keyboardPriority='guest') => {
    const video = target(), kbInput = target(), document = target(), messages = [], actions = [];
    let open = true;
    const timers = new Map(); let nextTimer = 0;
    const popup = {performance:{now:()=>1100},
        setInterval:callback => {timers.set(++nextTimer, callback); return nextTimer;},
        clearInterval:id => timers.delete(id)};
    const state = make({video, kbInput, document, messages, actions, popup,
        settings:{keyboardPriority,keyInputMode}, send:message => {
            if (!open) { return false; }
            messages.push(message); return true;
        }});
    return {...state, video, kbInput, document, messages, actions,
        tick:() => {for (const callback of timers.values()) {callback();}},
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
{
    const h = harness(); h.tick();
    assert.equal(h.messages.length, 0, 'old servers receive no unknown input operations');
    h.acceptInputPolicy({version:1, lease_ms:200, snapshot_ms:50});
    h.key('keydown', 'KeyA', {timeStamp:1000}); h.tick();
    assert.deepEqual(h.messages.at(-1), {op:'input_state',keys:[30],buttons:[]});
    const down = h.messages.find(message => message.op === 'keyboard');
    assert.equal(down.client_time_ms, 1000); assert.equal(down.event_delay_ms, 100);
    h.key('keyup', 'KeyA', {timeStamp:1100}); h.tick();
    assert.deepEqual(h.messages.at(-1), {op:'input_state',keys:[],buttons:[]});
}
{
    const h = harness('client-repeat');
    h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
    h.key('keydown', 'KeyA', {repeat:true});
    assert.equal(h.messages.filter(m=>m.op==='key_tap').length,0,'foreign repeats remain local');
    h.key('keydown');
    for(let n=0;n<3;++n){h.key('keydown','KeyA',{repeat:true});}
    h.tick();
    assert.deepEqual(h.messages.at(-1),{op:'input_state',keys:[],buttons:[]},'typing never leases a held character');
    h.open(false); h.key('keyup'); h.open(true);
    assert.equal(h.messages.filter(m=>m.op==='key_tap').length,4,'only four source events generate four actions');
    assert.equal(h.messages.filter(m=>m.op==='keyboard').length,0,'no network-separated down/up pair for text');
    h.key('keydown','ControlLeft'); h.key('keydown','KeyA',{ctrlKey:true});
    assert.deepEqual(h.messages.at(-1),{op:'key_tap',key:30,modifiers:[29]});
    h.key('keyup','KeyA'); h.key('keyup','ControlLeft');
    assert.deepEqual(h.messages.at(-1),{op:'keyboard',key:29,down:false,modifiers:0});
    h.key('keydown','KeyA',{shiftKey:true});
    assert.deepEqual(h.messages.at(-1),{op:'key_tap',key:30,modifiers:[42]},'unseen physical modifiers accompany the action');
}
{
    const h=harness('client-repeat','raw');
    h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
    h.key('keydown'); h.key('keydown','KeyA',{repeat:true}); h.key('keyup');
    assert.deepEqual(edges(h).filter(([key])=>key!==undefined),[[30,true],[30,false]],'Raw always preserves physical holds');
}
{
    const fragment=source.slice(source.indexOf('const sendGuestChord ='),source.indexOf('// Ctrl+C, or Ctrl+Shift+C'));
    const makeChord=new Function('inputTapsEnabled','send',fragment+'return sendGuestChord;');
    const messages=[];makeChord(true,message=>messages.push(message))([29,42],46);
    assert.deepEqual(messages,[{op:'key_tap',key:46,modifiers:[29,42]}],'clipboard/special chords cross as one action');
    const defaults=source.slice(source.indexOf('const defaultConsoleSettings ='),source.indexOf('// Standard receiver target'));
    const getDefaults=new Function('CONSOLE_SETTING_SCHEMA','KEYBOARD_PRIORITY','KEY_INPUT_MODE','MOUSE_INPUT_MODE',defaults+'return defaultConsoleSettings();');
    assert.equal(getDefaults({}, {guest:'guest'}, {typing:'typing',physical:'physical'},{exact:'exact'}).keyInputMode,'typing','exact typing must be default');
}
console.log('PASS: guest keyboard edges, focus changes, exact typing, raw holds, chords and clipboard repeat');
