import assert from 'node:assert/strict';
import fs from 'node:fs';
const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const section=(start,end)=>{const a=source.indexOf(start),b=source.indexOf(end,a);assert.ok(a>=0&&b>a);return source.slice(a,b);};
const factory=new Function('env',`
    const {popup,document,video,kbInput,settings,keyboardCaptureStatus,captureKeyboard,captureKeyboardSettings,releaseKeyboardSettings,
        keyboardPriorityInput,applyConsoleSettings,releaseHeldInput}=env;
    const KEYBOARD_PRIORITY={client:'client-first',guest:'guest-first'},gettext=value=>value;
    let closed=false,settingsPanelOpen=false,restoreClientFocusAfterFullscreen=false;
    const setSettingsPanelOpen=open=>{settingsPanelOpen=open;updateGuestKeyboardLock();};
    ${section('let keyboardLockGeneration =','const bytesToB64 =')}
    return {updateGuestKeyboardLock,releaseGuestKeyboardLock,enterConsoleFullscreen,captureGuestKeyboard,
        openSettings:open=>{settingsPanelOpen=open;updateGuestKeyboardLock();},close:()=>{closed=true;releaseGuestKeyboardLock();},
        restore:()=>restoreClientFocusAfterFullscreen};
`);
const target=()=>({listeners:{},style:{},attrs:{},addEventListener(name,fn){this.listeners[name]=fn;},setAttribute(n,v){this.attrs[n]=v;},focus(){}});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function harness({keyboard=null,topKeyboard=keyboard,option=true,reject=false,priority='guest-first',defer=false}={}){
 const calls=[],video=target(),kbInput=target(),captureKeyboard=target(),captureKeyboardSettings=target(),releaseKeyboardSettings=target(),status={},settings={keyboardPriority:priority};
 let resolve;
 const document={...target(),fullscreenElement:null,activeElement:video,documentElement:{},exitFullscreen(){calls.push('exit');this.fullscreenElement=null;return Promise.resolve();}};
 document.documentElement.requestFullscreen=options=>{
  calls.push(['fullscreen',option?options.keyboardLock:null]);
  if(reject)return Promise.reject(Error('denied'));
  if(defer)return new Promise(r=>{resolve=()=>{document.fullscreenElement=document.documentElement;r();};});
  document.fullscreenElement=document.documentElement;return Promise.resolve();
 };
 video.focus=()=>{calls.push('focus');document.activeElement=video;};
 const popup={...target(),navigator:{keyboard},top:{navigator:{keyboard:topKeyboard}}};
 const state=factory({popup:{...popup,navigator:{keyboard},top:{navigator:{keyboard:topKeyboard}}},document,video,kbInput,settings,keyboardCaptureStatus:status,
  captureKeyboard,captureKeyboardSettings,releaseKeyboardSettings,keyboardPriorityInput:{},applyConsoleSettings:()=>calls.push('persist'),releaseHeldInput:()=>calls.push('release')});
 return {...state,calls,popup,document,video,kbInput,settings,status,captureKeyboard,captureKeyboardSettings,releaseKeyboardSettings,resolve:()=>resolve()};
}
{
 const h=harness();h.captureKeyboard.listeners.click();await tick();
 assert.ok(h.calls.some(x=>Array.isArray(x)&&x[0]==='fullscreen'&&x[1]==='browser'),'Firefox/Safari receives keyboardLock browser');
 assert.match(h.status.textContent,/capture active/);assert.equal(h.captureKeyboard.attrs['aria-pressed'],'true');
 assert.equal(h.document.activeElement,h.video);assert.ok(!h.calls.includes('F11'),'no OS hotkey is used');
 h.calls.length=0;h.document.activeElement=h.captureKeyboardSettings;h.openSettings(true);
 assert.ok(h.calls.includes('release'),'opening Settings releases held guest input');
 assert.ok(h.document.fullscreenElement);assert.ok(!h.calls.includes('exit'),'Settings preserves Firefox/Safari fullscreen-bound capture');
 assert.equal(h.captureKeyboard.attrs['aria-pressed'],'true');assert.match(h.status.textContent,/Settings is open/);
 h.document.activeElement=h.video;h.openSettings(false);assert.match(h.status.textContent,/capture active/);
 assert.ok(!h.calls.some(v=>Array.isArray(v)&&v[0]==='fullscreen'),'closing Settings resumes the existing capture');
}
{
 const calls=[];const keyboard={lock:(...args)=>{calls.push(['lock',...args]);return Promise.resolve();},unlock:()=>calls.push(['unlock'])};
 const h=harness({keyboard,option:false});h.enterConsoleFullscreen();await tick();
 assert.deepEqual(calls.at(-1),['lock'],'Chromium still captures every available key with no list');
 h.document.activeElement={};h.video.listeners.blur();assert.equal(calls.at(-1)[0],'unlock','client fields release Chromium lock');
 h.document.activeElement=h.video;h.settings.keyboardPriority='raw-events';h.updateGuestKeyboardLock();await tick();assert.equal(calls.at(-1)[0],'lock');
 h.settings.keyboardPriority='client-first';h.updateGuestKeyboardLock();assert.equal(calls.at(-1)[0],'unlock');assert.ok(h.calls.includes('exit'));
}
{
 let calls=0;const keyboard={lock:()=>{throw Error('iframe must not own Chromium lock');}};
 const h=harness({option:false,keyboard,topKeyboard:{lock:()=>{calls++;return Promise.reject(Error('denied'));},unlock(){}}});
 h.enterConsoleFullscreen();await tick();assert.equal(calls,1);assert.match(h.status.textContent,/denied/);
}
{
 const h=harness({option:false});h.enterConsoleFullscreen();await tick();assert.match(h.status.textContent,/did not enable/,'unknown options cannot falsely imply capture');
}
{
 const h=harness({reject:true});h.enterConsoleFullscreen();await tick();assert.match(h.status.textContent,/denied/);assert.equal(h.captureKeyboard.attrs['aria-pressed'],'false');assert.equal(h.captureKeyboard.disabled,false);
}
{
 const h=harness();assert.equal(h.releaseKeyboardSettings.disabled,true);h.enterConsoleFullscreen();await tick();h.calls.length=0;
 h.captureKeyboard.listeners.click();h.captureKeyboard.listeners.click();h.openSettings(true);h.captureKeyboardSettings.listeners.click();await tick();
 assert.ok(h.document.fullscreenElement);assert.ok(!h.calls.includes('exit'),'Capture is enable/resume only, including repeated clicks and Settings');
 assert.ok(!h.calls.some(v=>Array.isArray(v)&&v[0]==='fullscreen'),'already active Capture does not request fullscreen again');
 assert.equal(h.captureKeyboard.textContent,'Capture keyboard');assert.equal(h.captureKeyboard.attrs['aria-label'],'Capture keyboard (full screen)');
 assert.equal(h.releaseKeyboardSettings.disabled,false);h.releaseKeyboardSettings.listeners.click();assert.ok(h.calls.includes('exit'),'the distinct Release action exits capture');
}
{
 const h=harness();h.document.fullscreenElement=h.document.documentElement;h.updateGuestKeyboardLock();h.calls.length=0;
 h.captureKeyboard.listeners.click();await tick();assert.ok(h.document.fullscreenElement);assert.ok(!h.calls.includes('exit'),'Capture upgrades ordinary fullscreen without exit');
 assert.match(h.status.textContent,/capture active/);assert.equal(h.calls.filter(v=>Array.isArray(v)&&v[0]==='fullscreen').length,1);
}
{
 const h=harness({defer:true});h.captureKeyboard.listeners.click();h.captureKeyboard.listeners.click();h.captureKeyboardSettings.listeners.click();
 assert.equal(h.calls.filter(v=>Array.isArray(v)&&v[0]==='fullscreen').length,1,'rapid Capture clicks coalesce a pending request');
 h.resolve();await tick();assert.ok(h.document.fullscreenElement);assert.ok(!h.calls.includes('exit'));
}
{
 const h=harness({priority:'client-first'});h.captureKeyboardSettings.listeners.click();await tick();assert.equal(h.settings.keyboardPriority,'guest-first');assert.ok(h.calls.includes('persist'),'explicit Capture switches/persists Guest first');
}
{
 const h=harness({defer:true});h.enterConsoleFullscreen();h.releaseGuestKeyboardLock();h.resolve();await tick();assert.ok(h.calls.includes('exit'));assert.doesNotMatch(h.status.textContent||'',/^Keyboard capture active/,'late fullscreen resolution cannot reacquire released capture');
}
{
 const h=harness();h.enterConsoleFullscreen();await tick();h.calls.length=0;
 h.popup.listeners.blur();assert.ok(h.calls.includes('release'),'blur releases guest-held input even before media connects');
 assert.ok(!h.calls.includes('exit'),'host Alt/Cmd+Tab must preserve fullscreen');
 assert.equal(h.captureKeyboard.attrs['aria-pressed'],'true');assert.match(h.status.textContent,/paused/);
 h.popup.listeners.focus();await tick();assert.match(h.status.textContent,/capture active/);
 assert.equal(h.calls.filter(v=>Array.isArray(v)&&v[0]==='fullscreen').length,0,'return does not request a new fullscreen');
}
{
 const h=harness();h.enterConsoleFullscreen();await tick();h.calls.length=0;
 h.document.hidden=true;h.document.listeners.visibilitychange();assert.ok(!h.calls.includes('exit'));
 h.document.hidden=false;h.document.listeners.visibilitychange();await tick();assert.match(h.status.textContent,/capture active/);
 assert.equal(h.calls.filter(v=>Array.isArray(v)&&v[0]==='fullscreen').length,0);
}
{
 const calls=[];const keyboard={lock:()=>{calls.push('lock');return Promise.resolve();},unlock:()=>calls.push('unlock')};
 const h=harness({keyboard,option:false});h.enterConsoleFullscreen();await tick();h.calls.length=0;calls.length=0;
 h.popup.listeners.blur();assert.equal(calls.at(-1),'unlock');assert.ok(!h.calls.includes('exit'));
 h.popup.listeners.focus();await tick();assert.equal(calls.at(-1),'lock');assert.match(h.status.textContent,/capture active/);
 assert.ok(!h.calls.some(v=>Array.isArray(v)&&v[0]==='fullscreen'));
}
{
 const h=harness({defer:true});h.enterConsoleFullscreen();const focusCalls=h.calls.filter(v=>v==='focus').length;
 h.popup.listeners.blur();h.resolve();await tick();assert.ok(h.document.fullscreenElement);assert.ok(!h.calls.includes('exit'));
 assert.equal(h.calls.filter(v=>v==='focus').length,focusCalls,'a late request may not focus the background guest');
 assert.match(h.status.textContent,/paused/);h.popup.listeners.focus();await tick();assert.match(h.status.textContent,/capture active/);
}
{
 const h=harness();h.enterConsoleFullscreen();await tick();h.document.fullscreenElement=null;h.updateGuestKeyboardLock();assert.equal(h.captureKeyboard.attrs['aria-pressed'],'false');
 h.enterConsoleFullscreen();await tick();h.close();assert.ok(h.calls.includes('exit'),'closing the console releases fullscreen capture');
}
{
 const calls=[];const keyboard={lock:()=>{calls.push('lock');return Promise.resolve();},unlock:()=>calls.push('unlock')};
 const h=harness({keyboard,option:false});h.enterConsoleFullscreen();await tick();h.calls.length=0;calls.length=0;
 h.document.activeElement=h.captureKeyboardSettings;h.openSettings(true);assert.equal(calls.at(-1),'unlock');
 assert.ok(h.document.fullscreenElement);assert.ok(!h.calls.includes('exit'));
 h.document.activeElement=h.video;h.openSettings(false);await tick();assert.equal(calls.at(-1),'lock');assert.match(h.status.textContent,/capture active/);
}
{
 const h=harness({defer:true});h.enterConsoleFullscreen();h.document.activeElement=h.captureKeyboardSettings;h.openSettings(true);
 const focuses=h.calls.filter(v=>v==='focus').length;h.resolve();await tick();assert.ok(h.document.fullscreenElement);
 assert.ok(!h.calls.includes('exit'),'opening Settings cannot retire a pending capture request');
 assert.equal(h.calls.filter(v=>v==='focus').length,focuses,'pending fullscreen must not steal focus from Settings');
 assert.match(h.status.textContent,/Settings is open/);h.document.activeElement=h.video;h.openSettings(false);assert.match(h.status.textContent,/capture active/);
}
{
 const h=harness();h.enterConsoleFullscreen();await tick();h.openSettings(true);h.settings.keyboardPriority='client-first';h.updateGuestKeyboardLock();
 assert.ok(h.calls.includes('exit'),'explicit Client first still exits capture from Settings');
}
{
 const actions=[],buttons=[],video={focus:()=>actions.push('focus')};
 const document={createElement:()=>({style:{},addEventListener(_name,callback){this.click=callback;},append:button=>buttons.push(button)})};
 new Function('env',`
  const {document,video,buttons,actions}=env,control={readyState:'open'},gettext=t=>t;
  const releaseHeldInput=()=>actions.push('release'),setSettingsPanelOpen=()=>actions.push('close');
  const sendGuestChord=(mods,key)=>actions.push([mods,key]);
  ${section('const specialKeys =','const settingsForm =')}
 `)({document,video,buttons,actions});
 for(const button of buttons)button.click();
 assert.deepEqual(actions.filter(Array.isArray),[[[],1],[[],56],[[56],42],[[56],15],[[29,56],0xd3],[[],0xdb]]);
 const maps=new Function(section('const scanCodes =','// A guest-first console')+'return qemuKey;')();
 for(const [code,key]of Object.entries({MetaLeft:0xdb,MetaRight:0xdc,IntlBackslash:86,NumpadEnter:0x9c,NumpadDivide:0xb5,PrintScreen:0xb7,Pause:0xc6,ContextMenu:0xdd}))assert.equal(maps({code}),key);
}
console.log('PASS: Firefox/Safari fullscreen capture, Chromium, release, denial, ownership, races and guest buttons');
