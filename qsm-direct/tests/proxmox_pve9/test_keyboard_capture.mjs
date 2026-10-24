import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const section = (start, end) => {
    const a = source.indexOf(start), b = source.indexOf(end, a);
    assert.ok(a >= 0 && b > a); return source.slice(a, b);
};
const factory = new Function('env', `
    const {popup,document,video,kbInput,settings,keyboardCaptureStatus} = env;
    const KEYBOARD_PRIORITY={client:'client-first'},gettext=value=>value,closed=false;
    ${section('let keyboardLockGeneration =', 'const bytesToB64 =')}
    return {updateGuestKeyboardLock,releaseGuestKeyboardLock};
`);
const target = () => ({listeners:{}, addEventListener(name, callback) { this.listeners[name] = callback; }});
const tick = () => new Promise(resolve => setImmediate(resolve));
{
    const calls = [], video = target(), kbInput = target(), status = {};
    const keyboard = {lock:(...args)=>{calls.push(['lock',...args]);return Promise.resolve();}, unlock:()=>calls.push(['unlock'])};
    const document = {fullscreenElement:{}, activeElement:video}, settings={keyboardPriority:'guest-first'};
    const h=factory({popup:{navigator:{keyboard},top:{navigator:{keyboard}}},document,video,kbInput,settings,keyboardCaptureStatus:status});
    await tick();
    assert.deepEqual(calls,[['lock']], 'no argument captures every available key, not only Esc');
    assert.match(status.textContent,/capture active/);
    document.activeElement={}; video.listeners.blur();
    assert.equal(calls.at(-1)[0],'unlock','client fields must not retain guest capture');
    document.activeElement=video; settings.keyboardPriority='raw-events'; h.updateGuestKeyboardLock();
    assert.equal(calls.at(-1)[0],'lock','Raw input also captures available reserved keys');
    settings.keyboardPriority='client-first'; h.updateGuestKeyboardLock();
    assert.equal(calls.at(-1)[0],'unlock');
    h.releaseGuestKeyboardLock(); assert.equal(calls.at(-1)[0],'unlock','console close explicitly releases lock');
}
{
    const video=target(),kbInput=target(),status={}; let calls=0;
    factory({popup:{navigator:{keyboard:{lock:()=>{throw Error('iframe must not acquire lock');}}},
        top:{navigator:{keyboard:{lock:()=>{calls++;return Promise.reject(Error('denied'));},unlock(){}}}}},
        document:{fullscreenElement:{},activeElement:video},video,kbInput,settings:{},keyboardCaptureStatus:status});
    await tick(); assert.equal(calls,1); assert.match(status.textContent,/denied/,'denial must be visible');
}
{
    const video=target(),kbInput=target(),status={};
    factory({popup:{navigator:{},top:{navigator:{}}},document:{},video,kbInput,settings:{},keyboardCaptureStatus:status});
    assert.match(status.textContent,/Firefox.*Esc.*F11/,'Firefox users need a working escape alternative');
}
{
    const actions=[],buttons=[],video={focus:()=>actions.push('focus')};
    const document={createElement:()=>({style:{},addEventListener(_name, callback){this.click=callback;},append:button=>buttons.push(button)})};
    new Function('env', `
        const {document,video,buttons,actions}=env,control={readyState:'open'},gettext=t=>t;
        const releaseHeldInput=()=>actions.push('release'),setSettingsPanelOpen=()=>actions.push('close');
        const sendGuestChord=(mods,key)=>actions.push([mods,key]);
        ${section('const specialKeys =', 'const settingsForm =')}
    `)({document,video,buttons,actions});
    for(const button of buttons){button.click();}
    assert.deepEqual(actions.filter(Array.isArray),[[[],1],[[56],15],[[29,56],0xd3],[[],0xdb]]);
    assert.equal(buttons[0].textContent,'Esc');
    const maps = new Function(section('const scanCodes =', '// A guest-first console')+'return qemuKey;')();
    assert.equal(maps({code:'MetaLeft'}),0xdb); assert.equal(maps({code:'MetaRight'}),0xdc);
    for (const [code,key] of Object.entries({IntlBackslash:86,NumpadEnter:0x9c,NumpadDivide:0xb5,
        PrintScreen:0xb7,Pause:0xc6,ContextMenu:0xdd})) { assert.equal(maps({code}),key); }
}
console.log('PASS: all-key capture, unlock, iframe owner, denial, Firefox guidance and guest key buttons');
