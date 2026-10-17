import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js',import.meta.url),'utf8');
const begin=source.indexOf('            const flushPointer =');
const end=source.indexOf('            const pointerForMouseEvent =',begin);
let now=0, timer=null, sequence=0, blocked=false;
const motion=[],ordered=[];
const context=vm.createContext({
    closed:false,pointerTimer:null,pendingPointer:null,latestPointer:null,nextPointerSendAt:0,pointerSequence:sequence,
    pointer:{readyState:'open'}, noteGuestInput(){},
    send:value=>ordered.push(value), sendPointer:value=>{if(blocked)return false;motion.push(value);return true;},
    popup:{performance:{now:()=>now},setTimeout(fn,delay){timer={fn,delay};return 1;},clearTimeout(){timer=null;}},
});
vm.runInContext(source.slice(begin,end)+'\nglobalThis.queue=queuePointer;globalThis.flush=flushPointer;',context);
context.queue({x:1});assert.equal(motion.length,1,'first movement is immediate');
now=1;context.queue({x:2});context.queue({x:3});
assert.equal(motion.length,1);assert.equal(timer.delay,7);
now=8;timer.fn();assert.equal(motion.at(-1).x,3,'a burst retains only its latest coordinate');
now=9;context.queue({x:4});context.flush(true);
assert.equal(ordered.at(-1).x,4,'button fence uses the ordered channel with its current position');
assert.equal(timer,null);
blocked=true;now=20;context.queue({x:5});context.queue({x:6});
assert.equal(motion.length,2);assert.equal(context.pendingPointer.x,6);
blocked=false;now=28;timer.fn();assert.equal(motion.at(-1).x,6);
now=29;context.queue({x:7});assert.ok(timer);
now=100;context.queue({x:8});
assert.equal(motion.at(-1).x,8,'fresh foreground input must bypass a throttled background timer');
assert.equal(timer,null);
context.closed=true;now=110;context.queue({x:9});assert.equal(motion.length,4);
console.log('QSM_POINTER_PACING_OK');
