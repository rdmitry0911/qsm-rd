import assert from 'node:assert/strict';
import fs from 'node:fs';
const source=fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js',import.meta.url),'utf8');
const part=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const make=new Function('env',`
const {video,document,popup,send,settings}=env;
const MOUSE_INPUT_MODE={exact:'exact-clicks',physical:'physical-buttons'},KEYBOARD_PRIORITY={raw:'raw'};
let inputMouseActionsEnabled=true,mouseFenceSequence=null,pointerSequence=0,pointerTimer=null,pendingPointer=null,latestPointer=null;
const heldMouseButtons=new Set(),physicalHeldKeys=new Set();
${part('const deferredMetaKeys =','let fullscreenEscapePending =')}
let diagButtonSent=0,diagLastInput='',usingTouchInput=false;
const noteGuestInput=()=>{},ensurePlaying=()=>{},observeToolbarZone=()=>{},placeGuestCursor=()=>{};
const pointerForMouseEvent=e=>({op:'mouse_position',x:e.clientX,y:e.clientY,width:1280,height:720});
const queuePointer=p=>{latestPointer={...p,sequence:pointerSequence++};};
const flushPointer=()=>{};
let pointerInToolbarZone=false,pointerNearToolbar=false;
const cancelToolbarReveal=()=>{},hideToolbarSoon=()=>{};
${part('const pendingMousePresses =','const physicalHeldKeys =')}
${part('const exactMouseClicks =','const sendKeyboard =')}
const releaseHeldInput=()=>{cancelPendingMouse();for(const n of [...heldMouseButtons])sendMouseButton(n,false);};
${part('const buttonMask =','// Touch and pen input')}
document.addEventListener('mouseup',endMouse);
${part('let wheelX =',"video.addEventListener('paste'")}
return {heldMouseButtons,deferredMetaKeys,pendingMousePresses,releaseHeldInput,sendMouseClick,physicalHeldKeys,
    fence:()=>mouseFenceSequence,ack:seq=>{if(seq===mouseFenceSequence)mouseFenceSequence=null;},
    oldServer:()=>{inputMouseActionsEnabled=false;}};
`);
const target=()=>{const listeners=new Map();return {focus(){},addEventListener(n,fn){if(!listeners.has(n))listeners.set(n,[]);listeners.get(n).push(fn);},emit(n,e){for(const fn of listeners.get(n)||[])fn(e);}};};
function harness(){
 let time=0,id=0,open=true;const timers=new Map(),messages=[],video=target(),document=target();
 const popup={navigator:{platform:'MacIntel'},innerHeight:720,setTimeout(fn,ms){timers.set(++id,{fn,at:time+ms});return id;},clearTimeout:n=>timers.delete(n)};
 const settings={mouseInputMode:'exact-clicks',keyboardPriority:'guest'};
 const state=make({video,document,popup,settings,send:m=>{if(!open)return false;messages.push(m);return true;}});
 return {...state,messages,settings,video,document,open:v=>{open=v;},
 tick(ms){time+=ms;for(const [n,t] of [...timers])if(t.at<=time&&timers.has(n)){timers.delete(n);t.fn();}},
 event(name,x=100,y=100,extra={},outside=false){const e={target:outside?document:video,clientX:x,clientY:y,button:0,buttons:name==='mouseup'?0:1,
    ctrlKey:false,shiftKey:false,altKey:false,metaKey:false,preventDefault(){},stopPropagation(){},...extra};
    (outside?document:video).emit(name,e);if(!outside)document.emit(name,e);}};
}
{
 const h=harness();h.event('mousedown');h.tick(100);
 assert.equal(h.messages.length,0,'a short physical press creates no remote held state');
 h.event('mouseup');assert.deepEqual(h.messages.map(m=>[m.op,m.button,m.action]),[['mouse_action',1,2]]);
 h.tick(900);assert.equal(h.messages.length,1,'delaying the complete packet cannot stretch a click');
 assert.equal(h.heldMouseButtons.size,0);
 const seq=h.fence();h.ack(seq-1);assert.equal(h.fence(),seq);h.ack(seq);assert.equal(h.fence(),null);
}
{
 const h=harness();for(let i=0;i<2;i++){h.event('mousedown');h.tick(20);h.event('mouseup');h.tick(50);}
 assert.deepEqual(h.messages.map(m=>m.action),[2,2],'a double tap produces exactly two clicks');
 h.event('mousedown',150,170,{button:2,buttons:2});h.event('mouseup',150,170,{button:2});
 assert.equal(h.messages.at(-1).button,3,'trackpad secondary click is one right click');
}
{
 const h=harness();h.event('mousedown',100,100);h.event('mousemove',103,100);assert.equal(h.messages.length,0);
 h.event('mousemove',120,140);assert.deepEqual([h.messages[0].action,h.messages[0].x,h.messages[0].y],[1,100,100],'drag starts at its original anchor');
 assert.ok(h.heldMouseButtons.has(1));h.event('mouseup',200,250,{},true);
 assert.deepEqual(h.messages.map(m=>m.action),[1,0]);assert.equal(h.messages[1].x,200);assert.equal(h.heldMouseButtons.size,0);
}
{
 const h=harness();h.event('mousedown');h.tick(250);assert.ok(h.heldMouseButtons.has(1),'an intentional stationary hold is supported');
 h.releaseHeldInput();h.tick(1000);assert.deepEqual(h.messages.map(m=>m.action),[1,0]);
}
for(const cancel of ['blur','missed-up']){
 const h=harness();h.event('mousedown');
 if(cancel==='blur')h.releaseHeldInput();else h.event('mousemove',110,110,{buttons:0});
 h.tick(1000);h.event('mouseup');assert.equal(h.messages.length,0,`${cancel} cancels an unfinished click without inventing one`);
}
{
 const h=harness();h.event('mousedown',100,100,{shiftKey:true});h.event('mouseup',100,100,{shiftKey:true});
 assert.deepEqual(h.messages[0].modifiers,[42],'shift-click carries its modifiers with the action');
 h.physicalHeldKeys.add(29);h.event('mousedown');h.event('mouseup');assert.deepEqual(h.messages.at(-1).modifiers,[],'a stale modifier is repaired');
}
for(const mode of ['raw','old','physical']){
 const h=harness();if(mode==='old')h.oldServer();else if(mode==='raw')h.settings.keyboardPriority='raw';else h.settings.mouseInputMode='physical-buttons';
 h.event('mousedown');assert.equal(h.messages[0].op,'mouse_button');assert.equal(h.messages[0].down,true);
 h.event('mouseup');assert.equal(h.messages[1].down,false);
}
{
 const h=harness();h.open(false);h.event('mousedown');h.event('mouseup');assert.equal(h.heldMouseButtons.size,0);assert.equal(h.fence(),null);
}
{
 const h=harness();for(let i=0;i<5;i++)h.event('wheel',0,0,{deltaX:.25,deltaY:.25,deltaMode:0});
 assert.equal(h.messages.length,1);assert.deepEqual(h.messages[0],{op:'scroll',vertical:1,horizontal:1},'fractional trackpad movement survives truncation');
 h.event('wheel',0,0,{deltaX:0,deltaY:1,deltaMode:1});assert.equal(h.messages.at(-1).vertical,16);
}
{
 const h=harness();h.deferredMetaKeys.set(0xdb,false);h.event('mousedown',100,100,{metaKey:true});assert.equal(h.deferredMetaKeys.get(0xdb),true,'mouse down consumes Command before keyup or click classification');h.event('mouseup',100,100,{metaKey:true});
 assert.deepEqual(h.messages[0].modifiers,[0xdb]);assert.equal(h.deferredMetaKeys.get(0xdb),true,'exact mouse click consumes the deferred Command modifier');
}
{
 const h=harness();h.settings.mouseInputMode='physical-buttons';h.deferredMetaKeys.set(0xdb,false);h.event('mousedown',100,100,{metaKey:true});h.event('mouseup',100,100,{metaKey:true});
 assert.equal(h.messages[0].op,'keyboard');assert.equal(h.messages[0].key,0xdb);assert.equal(h.messages[1].op,'mouse_button');assert.equal(h.deferredMetaKeys.size,0,'physical dragging flushes its modifier before the button edge');
}
{
 const h=harness();h.deferredMetaKeys.set(0xdb,false);h.event('wheel',0,0,{deltaX:0,deltaY:1,deltaMode:0});
 assert.deepEqual(h.messages.map(m=>m.op),['keyboard','scroll'],'modifier-wheel flushes Command before scrolling');
 assert.equal(h.messages[0].key,0xdb);assert.equal(h.physicalHeldKeys.has(0xdb),true);
}
console.log('QSM_MOUSE_TRACKPAD_STATE_OK');
