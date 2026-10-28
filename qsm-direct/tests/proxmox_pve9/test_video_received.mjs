import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js',import.meta.url),'utf8');
const begin=source.indexOf('    const createVideoReceiveFeedback =');
const end=source.indexOf('    const applyReceiverPlayoutDelay =',begin);
assert.ok(begin>0&&end>begin);
const context=vm.createContext({});
vm.runInContext(source.slice(begin,end)+'\nglobalThis.create=createVideoReceiveFeedback;',context);

function harness(){
 const workers=[],messages=[],timers=new Map(),urls=new Map();let now=0,serial=0;
 const scope={performance:{now:()=>now},
  Blob:class{constructor(parts){this.source=parts.join('');}},
  URL:{createObjectURL(blob){const id='blob:'+ ++serial;urls.set(id,blob);return id;},revokeObjectURL(id){urls.delete(id);}},
  Worker:class{constructor(url){this.source=urls.get(url).source;this.sent=[];workers.push(this);}postMessage(v){this.sent.push(v);}terminate(){this.terminated=true;}emit(v){this.onmessage({data:v});}},
  RTCRtpScriptTransform:class{constructor(worker){this.worker=worker;}},
  setTimeout(fn){const id=++serial;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);}};
 const channel={readyState:'open',bufferedAmount:0,send(v){messages.push(JSON.parse(v));}};
 const receiver={transform:null};const feedback=context.create(scope,()=>channel);
 return{scope,receiver,feedback,workers,messages,timers,urls,channel,time:v=>{now=v;}};
}

{
 const h=harness();h.feedback.attach(h.receiver);
 assert.equal(h.workers.length,0,'legacy server does not install a transform');
 h.feedback.allow(true);const w=h.workers[0];
 assert.equal(h.receiver.transform,null,'wait for worker readiness before touching media');
 w.emit({op:'ready'});assert.equal(h.receiver.transform.worker,w);assert.equal(h.feedback.active,true);
 assert.equal(h.urls.size,0);assert.equal(h.timers.size,0);
 w.emit({op:'received',rtp:1});h.time(60);w.emit({op:'received',rtp:1});
 assert.deepEqual(h.messages,[{op:'qsm_video_received',rtp:1}]);
 for(const rtp of [-1,0x100000000,1.5,true])w.emit({op:'received',rtp});
 h.channel.bufferedAmount=1025;w.emit({op:'received',rtp:2});
 h.channel.bufferedAmount=0;w.emit({op:'received',rtp:2});
 assert.deepEqual(h.messages.at(-1),{op:'qsm_video_received',rtp:2});
 h.time(120);w.emit({op:'received',rtp:0xffffffff});h.time(180);w.emit({op:'received',rtp:0});
 assert.deepEqual(h.messages.slice(-2),[{op:'qsm_video_received',rtp:0xffffffff},{op:'qsm_video_received',rtp:0}]);
 assert.equal(h.feedback.reports,4);assert.ok(w.sent.every(v=>v==='next'));
 h.feedback.close();assert.equal(h.receiver.transform,null);assert.ok(w.terminated);
 w.emit({op:'ready'});h.feedback.allow(true);assert.equal(h.workers.length,1,'closed viewer cannot reactivate');
}
{
 const h=harness();h.feedback.attach(h.receiver);h.feedback.allow(true);const old=h.workers[0];
 const staleTimer=[...h.timers.values()][0];h.feedback.allow(false);h.feedback.allow(true);const current=h.workers[1];
 old.emit({op:'ready'});old.onerror();staleTimer();current.emit({op:'ready'});
 assert.equal(h.receiver.transform.worker,current,'late old callbacks cannot disable a new worker');
 current.emit({op:'failed'});assert.equal(h.receiver.transform,null);assert.ok(current.terminated);
 h.feedback.allow(true);assert.equal(h.workers.length,2,'failed optional feature is not retried every health tick');
}
{
 const h=harness();h.feedback.attach(h.receiver);h.feedback.allow(true);
 [...h.timers.values()][0]();assert.equal(h.receiver.transform,null);assert.ok(h.workers[0].terminated);
 const absent=harness();delete absent.scope.RTCRtpScriptTransform;absent.feedback.attach(absent.receiver);absent.feedback.allow(true);
 assert.equal(absent.workers.length,0);
 const owned=harness();owned.receiver.transform={other:true};owned.feedback.attach(owned.receiver);owned.feedback.allow(true);
 assert.equal(owned.workers.length,0,'an existing media transform is never replaced');
}

// Execute the actual worker source with native Web Streams. It must forward
// the same frames in order without reading or replacing their encoded bytes.
{
 const h=harness();h.feedback.attach(h.receiver);h.feedback.allow(true);
 const posts=[],frames=[];let now=0,input,finish;
 const done=new Promise(resolve=>{finish=resolve;});
 const self={postMessage:value=>posts.push(value)};
 vm.runInNewContext(h.workers[0].source,{self,TransformStream,performance:{now:()=>now}});
 assert.equal(posts.shift().op,'ready');
 const readable=new ReadableStream({start(controller){input=controller;}});
 const writable=new WritableStream({write(frame){frames.push(frame);},close(){finish();}});
 self.onrtctransform({transformer:{readable,writable}});
 const frame=rtp=>({getMetadata(){return{rtpTimestamp:rtp};},get data(){throw Error('encoded payload was read');}});
 const wait=async n=>{for(let i=0;frames.length<n&&i<100;i++)await new Promise(setImmediate);assert.equal(frames.length,n);};
 const first=frame(10),second=frame(11);input.enqueue(first);await wait(1);
 now=60;input.enqueue(second);await wait(2);
 assert.equal(posts.length,1,'at most one unconsumed report');assert.equal(frames[0],first);assert.equal(frames[1],second);
 self.onmessage({data:'next'});now=120;input.enqueue(frame(12));await wait(3);
 assert.equal(posts.length,2);assert.equal(posts.at(-1).rtp,12);
 self.onmessage({data:'next'});now=180;input.enqueue({getMetadata(){return{};},get data(){throw Error('payload was read');}});await wait(4);
 assert.equal(posts.at(-1).op,'unsupported');input.close();await done;
 h.feedback.close();
}
console.log('QSM_VIDEO_RECEIVED_OK');
