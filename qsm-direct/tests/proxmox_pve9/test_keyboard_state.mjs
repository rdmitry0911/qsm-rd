import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const section = (start, end) => {
    const a = source.indexOf(start), b = source.indexOf(end, a);
    assert.ok(a >= 0 && b > a);
    return source.slice(a, b);
};
const keyboard = section('const sendKeyboard =', '// Map a printable character');
const maps = section('const scanCodes =','// A guest-first console');
const modifierState = section('const deferredModifiers =','let fullscreenEscapePending =');
const listeners = section("for (const name of ['keydown', 'keyup']) {\n                video.addEventListener", "peer.addTransceiver('video'");
const kbListeners=section("for (const name of ['keydown', 'keyup']) {\n            kbInput.addEventListener",'// beforeinput is the primary path');
const make = new Function('env', `
    const {video, kbInput, document, send, settings, actions, popup} = env;
    const heldKeys = new Set(), heldMouseButtons = new Set(), physicalHeldKeys = new Set();
    let inputStateEnabled = false, inputStateTimer = null, inputTapsEnabled = false, inputMouseActionsEnabled = false;
    const mouseInputModeHint={};
    const KEY_INPUT_MODE={typing:'client-repeat',physical:'guest-repeat'},keyInputModeHint={};
    let diagKeySent = 0, diagLastInput = '', fullscreenEscapePending = false;
    const noteGuestInput = () => {};
    ${maps}
    ${modifierState}
    let keyboardCaptureSuspended=false;
    const KEYBOARD_PRIORITY = {client:'client', raw:'raw'};
    const leaveGuestKeyboardCapture = () => actions.push('release'), releaseHeldInput = () => releaseGuestKeys();
    const fullscreen={click:()=>actions.push('fullscreen')},revealHandle={click:()=>actions.push('toolbar')};
    const pasteFromBrowser = () => { actions.push('paste'); return Promise.resolve(); };
    const guestSelectionToBrowser = () => { actions.push('copy'); return Promise.resolve(); };
    const status = {}, gettext = value => value;
    const pasteTextIntoGuest=text=>{actions.push('pasted:'+text);return Promise.resolve();};
    ${section('const inputModifiers =','const sendMouseAction =')}
    ${section('const sendGuestChord =','// Ctrl+C, or Ctrl+Shift+C')}
    ${keyboard}
    ${section("video.addEventListener('paste'","for (const name of ['keydown', 'keyup']) {\n                video.addEventListener")}
    ${listeners}
    ${kbListeners}
    return {settings, hostShortcutKeys, heldKeys, physicalHeldKeys, deferredModifiers, sendKeyboard, acceptInputPolicy, publishInputState,pause:v=>{keyboardCaptureSuspended=v;}};
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
const harness = (keyInputMode='guest-repeat', keyboardPriority='guest', platform='Linux') => {
    const video = target(), kbInput = target(), document = target(), messages = [], actions = [];
    let open = true;
    const timers = new Map(); let nextTimer = 0;
    const popup = {...target(),navigator:{platform},performance:{now:()=>1100},
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
            const event = {type:name, code, repeat:false, defaultPrevented:false,preventDefault() {this.defaultPrevented=true;},stopPropagation(){}, ...extra};
            if (name === 'keyup') { document.emit(name, event); }
            video.emit(name, event);return event;
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
    assert.deepEqual(h.messages.at(-1),{op:'key_tap',key:30,modifiers:[29]},'modifier release does not append a separate held edge after an atomic chord');
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
    const getDefaults=new Function('CONSOLE_SETTING_SCHEMA','KEYBOARD_PRIORITY','KEY_INPUT_MODE','MOUSE_INPUT_MODE','defaultShortcutRules',defaults+'return defaultConsoleSettings();');
    assert.equal(getDefaults({}, {guest:'guest'}, {typing:'typing',physical:'physical'},{exact:'exact'},()=>[]).keyInputMode,'typing','exact typing must be default');
}

const packets=h=>h.messages.filter(m=>m.op==='keyboard'||m.op==='key_tap');
{
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Meta',metaKey:true,repeat:true});h.key('keyup','AltLeft',{key:'Meta'});
 assert.equal(h.deferredModifiers.size,0);assert.deepEqual(packets(h),[],'a foreign repeating modifier cannot create a later solo Super tap');
}

for(const [physical,logical,scan,side]of [['AltLeft','Meta',0xdb,1],['AltRight','Meta',0xdc,2]]){
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown',physical,{key:logical,location:side,metaKey:true});
 assert.equal(packets(h).length,0,'remapped Command is deferred instead of pressing guest Alt');
 const tab=h.key('keydown','Tab',{key:'Tab',metaKey:true});
 h.key('keyup','Tab',{key:'Tab',metaKey:true});h.key('keyup',physical,{key:logical,location:side,metaKey:false});
 assert.equal(tab.defaultPrevented,false,'native host Command+Tab remains available');
 assert.deepEqual(packets(h),[],'host switch produces neither guest Tab nor a lone Super tap');
 assert.equal(h.heldKeys.size,0);assert.equal(h.deferredModifiers.size,0);
}
{
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','MetaLeft',{key:'Alt',location:1,altKey:true});
 const tab=h.key('keydown','Tab',{key:'Tab',altKey:true});h.key('keyup','Tab',{key:'Tab',altKey:true});h.key('keyup','MetaLeft',{key:'Alt',location:1,altKey:false});
 assert.equal(tab.defaultPrevented,false);assert.deepEqual(packets(h),[],'remapped Option+Tab is reserved for the host on macOS even when DOM receives Tab');
}
{
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Meta',metaKey:true});h.key('keydown','KeyA',{key:'a',metaKey:true});h.key('keyup','KeyA',{key:'a',metaKey:true});h.key('keyup','AltLeft',{key:'Meta',metaKey:false});
 assert.deepEqual(packets(h),[{op:'key_tap',key:30,modifiers:[0xdb]}],'a guest Command chord carries the logical modifier without extra menu taps');
}
{
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Meta',metaKey:true});h.key('keyup','AltLeft',{key:'Meta',metaKey:false});assert.deepEqual(packets(h),[{op:'key_tap',key:0xdb,modifiers:[]}],'a genuine solo logical Command tap remains available');
}
{
 const h=harness('guest-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Meta',metaKey:true});h.key('keydown','KeyA',{key:'a',metaKey:true});h.key('keyup','KeyA',{key:'a',metaKey:true});h.key('keyup','AltLeft',{key:'Meta',metaKey:false});
 assert.deepEqual(packets(h).map(m=>[m.key,m.down]),[[0xdb,true],[30,true],[30,false],[0xdb,false]],'physical main-key holds flush the logical modifier before the held key');
}
{
 const h=harness('client-repeat','raw','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Meta',metaKey:true});h.key('keydown','Tab',{key:'Tab',metaKey:true});h.key('keyup','Tab',{key:'Tab',metaKey:true});h.key('keyup','AltLeft',{key:'Meta',metaKey:false});
 assert.deepEqual(packets(h).map(m=>[m.key,m.down]),[[56,true],[15,true],[15,false],[56,false]],'Raw remains literal physical positions/edges');
}
{
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Meta',metaKey:true});h.video.emit('blur');h.key('keyup','AltLeft',{key:'Meta',metaKey:false});assert.deepEqual(packets(h),[],'focus cleanup must not invent a solo modifier tap');
 h.pause(true);h.key('keydown','KeyA',{key:'a'});h.key('keydown','ControlLeft',{key:'Control'});assert.deepEqual(packets(h),[],'late source presses while paused stay out of the guest');
 h.pause(false);h.key('keydown','KeyA',{key:'a',repeat:true});assert.deepEqual(packets(h),[],'foreign repeats after return do not revive an old press');
 h.key('keydown','KeyA',{key:'a'});h.key('keyup','KeyA',{key:'a'});assert.equal(packets(h).length,1);
}
{
 const maps=new Function(section('const scanCodes =','// A guest-first console')+'return qemuKey;')();
 for(const [code,key,want,raw]of [['AltLeft','Meta',0xdb,56],['MetaLeft','Alt',56,0xdb],['AltRight','Meta',0xdc,0xb8],['MetaRight','Alt',0xb8,0xdc],['ControlLeft','Shift',42,29],['KeyA','a',30,30]]){
  assert.equal(maps({code,key}),want);assert.equal(maps({code,key},true),raw);
 }
}

{
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 const emit=(name,code,key,metaKey)=>{const e={type:name,code,key,metaKey,repeat:false,preventDefault(){},stopPropagation(){}};if(name==='keyup')h.document.emit(name,e);h.kbInput.emit(name,e);};
 emit('keydown','AltLeft','Meta',true);emit('keydown','Tab','Tab',true);emit('keyup','Tab','Tab',true);emit('keyup','AltLeft','Meta',false);
 assert.deepEqual(packets(h),[],'the physical keyboard path behind the on-screen input applies host routing too');
}
// The host can consume Tab entirely. No DOM Tab is required for safe cleanup.
for(const platform of ['MacIntel','Linux'])for(const [code,key,scan,flag]of [
 ['AltLeft','Alt',56,'altKey'],['AltRight','Alt',0xb8,'altKey'],['MetaLeft','Meta',0xdb,'metaKey'],['MetaRight','Meta',0xdc,'metaKey'],
 ['ControlLeft','Control',29,'ctrlKey'],['ControlRight','Control',0x9d,'ctrlKey'],['ShiftLeft','Shift',42,'shiftKey'],['ShiftRight','Shift',54,'shiftKey']]){
 const h=harness('client-repeat','guest',platform);h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown',code,{key,[flag]:true});h.tick();assert.deepEqual(packets(h),[],`${platform} ${code}: no prefix press before a guest action`);
 assert.deepEqual(h.messages.at(-1),{op:'input_state',keys:[],buttons:[]},'pending modifiers are never renewed into the guest by heartbeat');
 h.document.hasFocus=()=>false;h.video.emit('blur');h.key('keyup',code,{key,[flag]:false});assert.deepEqual(packets(h),[],'host-swallowed Tab + blur produces no modifier or release tap');
 h.document.hasFocus=()=>true;h.key('keydown','KeyA',{key:'a'});h.key('keyup','KeyA',{key:'a'});assert.deepEqual(packets(h),[{op:'key_tap',key:30,modifiers:[]}],'return has no inherited modifier');
}
for(const state of ['unfocused','hidden','suspended']){
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Alt',altKey:true});
 if(state==='unfocused')h.document.hasFocus=()=>false;else if(state==='hidden')h.document.hidden=true;else h.pause(true);
 h.key('keyup','AltLeft',{key:'Alt',altKey:false});assert.deepEqual(packets(h),[],`${state} keyup preceding blur must not synthesize solo Alt`);
}
for(const releaseFirst of ['AltLeft','ShiftLeft']){
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','ShiftLeft',{key:'Shift',shiftKey:true});h.key('keydown','AltLeft',{key:'Alt',altKey:true,shiftKey:true});h.tick();assert.deepEqual(packets(h),[],'reverse host switching has no leaked Shift/Alt prefix');
 if(releaseFirst==='AltLeft'){
  h.key('keyup','AltLeft',{key:'Alt',altKey:false,shiftKey:true});h.key('keyup','ShiftLeft',{key:'Shift',altKey:false,shiftKey:false});
  assert.deepEqual(packets(h),[{op:'key_tap',key:56,modifiers:[42]}],'intentional modifier-only Alt+Shift completes once on release');
 }else{
  h.key('keyup','ShiftLeft',{key:'Shift',altKey:true,shiftKey:false});h.key('keyup','AltLeft',{key:'Alt',altKey:false,shiftKey:false});
  assert.deepEqual(packets(h),[{op:'key_tap',key:42,modifiers:[56]}],'modifier-only chord supports either release order');
 }
}
{
 const h=harness('client-repeat');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Alt',altKey:true});assert.deepEqual(packets(h),[]);h.key('keyup','AltLeft',{key:'Alt',altKey:false});
 assert.deepEqual(packets(h),[{op:'key_tap',key:56,modifiers:[]}],'a deliberate solo Alt menu tap works on release');
}
{
 const h=harness('client-repeat');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltRight',{key:'Alt',altKey:true,location:2});h.key('keydown','KeyF',{key:'f',altKey:true});h.key('keyup','KeyF',{key:'f',altKey:true});h.key('keyup','AltRight',{key:'Alt',altKey:false,location:2});
 assert.deepEqual(packets(h),[{op:'key_tap',key:33,modifiers:[0xb8]}],'right-side Alt remains in the complete guest action');
}
{
 const h=harness('client-repeat');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','ControlLeft',{key:'Control',ctrlKey:true});h.key('keydown','KeyV',{key:'v',ctrlKey:true});h.key('keyup','KeyV',{key:'v',ctrlKey:true});h.key('keyup','ControlLeft',{key:'Control',ctrlKey:false});
 assert.deepEqual(h.actions,['paste']);assert.deepEqual(packets(h),[],'local clipboard integration cannot leak its Ctrl prefix');
}
{
 const h=harness('client-repeat','client');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Alt',altKey:true});const e=h.key('keydown','ArrowLeft',{key:'ArrowLeft',altKey:true});h.key('keyup','AltLeft',{key:'Alt',altKey:false});
 assert.deepEqual(packets(h),[],'client-first browser shortcut discards the pending Alt');assert.equal(e.defaultPrevented,false);
}
{
 const h=harness('client-repeat');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltRight',{key:'AltGraph',location:2,altKey:false,getModifierState:n=>n==='AltGraph'});
 h.key('keydown','KeyE',{key:'€',altKey:false,ctrlKey:false,getModifierState:n=>n==='AltGraph'});
 h.key('keyup','KeyE',{key:'€',altKey:false,ctrlKey:false,getModifierState:n=>n==='AltGraph'});
 h.key('keyup','AltRight',{key:'AltGraph',location:2,altKey:false,getModifierState:()=>false});
 assert.deepEqual(packets(h),[{op:'key_tap',key:18,modifiers:[0xb8]}],'AltGraph is an independent modifier state and survives altKey=false');
}
// Full DOM sequence reported by the user: Alt down, Tab down/up, Alt up,
// with host focus loss only after all four events. Nothing may reach guest.
for(const mode of ['client-repeat','guest-repeat'])for(const side of ['Left','Right'])for(const reverse of [false,true]){
 const h=harness(mode,'guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 if(reverse)h.key('keydown','ShiftLeft',{key:'Shift',shiftKey:true});
 h.key('keydown','Meta'+side,{key:'Alt',location:side==='Right'?2:1,altKey:true,shiftKey:reverse});
 const tab=h.key('keydown','Tab',{key:'Tab',altKey:true,shiftKey:reverse});h.key('keyup','Tab',{key:'Tab',altKey:true,shiftKey:reverse});h.key('keyup','Meta'+side,{key:'Alt',altKey:false,shiftKey:reverse});
 if(reverse)h.key('keyup','ShiftLeft',{key:'Shift',shiftKey:false});
 h.tick();assert.deepEqual(packets(h),[],'complete DOM host chord must emit zero guest packets before blur');assert.equal(tab.defaultPrevented,false);assert.equal(h.heldKeys.size,0);
}
{
 const h=harness('client-repeat','guest','Linux');h.settings.hostShortcuts=[];h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','AltLeft',{key:'Alt',altKey:true});h.key('keydown','Tab',{key:'Tab',altKey:true});h.key('keyup','Tab',{key:'Tab',altKey:true});h.key('keyup','AltLeft',{key:'Alt',altKey:false});
 assert.deepEqual(packets(h),[{op:'key_tap',key:15,modifiers:[56]}],'removing the host rule allows the full guest Alt+Tab action');
}
{
 const h=harness('client-repeat','guest','MacIntel');h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 const emit=(name,code,key,flags)=>{const e={type:name,code,key,repeat:false,preventDefault(){},stopPropagation(){},...flags};if(name==='keyup')h.document.emit(name,e);h.kbInput.emit(name,e);};
 emit('keydown','AltLeft','Alt',{altKey:true});emit('keydown','Tab','Tab',{altKey:true});emit('keyup','Tab','Tab',{altKey:true});emit('keyup','AltLeft','Alt',{altKey:false});assert.deepEqual(packets(h),[],'on-screen input physical keyboard path reserves the complete host chord too');
}
for(const action of ['host','release','fullscreen','toolbar']){
 const h=harness('client-repeat');h.settings.hostShortcuts=[{shortcut:'Ctrl+KeyK',action}];h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','ControlLeft',{key:'Control',ctrlKey:true});const down=h.key('keydown','KeyK',{key:'k',ctrlKey:true});
 h.key('keydown','KeyK',{key:'k',ctrlKey:true,repeat:true});h.key('keyup','ControlLeft',{key:'Control',ctrlKey:false});const up=h.key('keyup','KeyK',{key:'k',ctrlKey:false});
 assert.deepEqual(packets(h),[],'host rules never forward their prefixes or main keys');assert.equal(h.hostShortcutKeys.size,0,'keyup is latched even after modifiers release');
 assert.equal(down.defaultPrevented,action!=='host');assert.equal(up.defaultPrevented,action!=='host');assert.deepEqual(h.actions,action==='host'?[]:[action],'local rule actions execute once, not on repeat or release');
}
{
 const h=harness('client-repeat');h.settings.hostShortcuts=[{shortcut:'Ctrl+KeyK',action:'host'}];h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','ControlLeft',{key:'Control',ctrlKey:true});h.key('keydown','KeyK',{key:'k',ctrlKey:true});h.document.emit('keyup',{type:'keyup',code:'KeyK',ctrlKey:false});
 assert.equal(h.hostShortcutKeys.size,0,'keyup at another target retires host ownership');assert.deepEqual(packets(h),[]);
}
{
 const h=harness('client-repeat');h.settings.hostShortcuts=[{shortcut:'Ctrl+KeyV',action:'host'}];h.acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true});
 h.key('keydown','ControlLeft',{key:'Control',ctrlKey:true});h.key('keydown','KeyV',{key:'v',ctrlKey:true});h.key('keyup','KeyV',{key:'v',ctrlKey:true});h.key('keyup','ControlLeft',{key:'Control',ctrlKey:false});
 const e={preventDefault(){this.prevented=true;},clipboardData:{getData:()=> 'host-only text'}};h.video.emit('paste',e);
 assert.equal(e.prevented,true);assert.deepEqual(h.actions,[],'native paste after keyup must not bypass the host shortcut rule');assert.deepEqual(packets(h),[]);
}
console.log('PASS: deferred modifier prefixes, host-swallowed Tab, focus cleanup, exact guest actions, raw holds and clipboard');
