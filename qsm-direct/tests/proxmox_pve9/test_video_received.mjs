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
 const w=h.workers[0];
 assert.equal(h.receiver.transform.worker,w,'attach before Chromium can bypass encoded streams');
 w.emit({op:'received',rtp:99});assert.equal(h.messages.length,0,'server must authorize receipt reports');
 h.feedback.allow(true);
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
 assert.equal(h.feedback.reports,4);
 const attached=h.receiver.transform;h.feedback.allow(false);
 assert.equal(h.receiver.transform,attached,'revoking reports keeps the passthrough media pipe');
 assert.ok(!w.terminated);h.time(240);w.emit({op:'received',rtp:5});assert.equal(h.feedback.reports,4);
 h.feedback.allow(true);w.emit({op:'received',rtp:6});assert.equal(h.feedback.reports,5);
 h.feedback.close();assert.equal(h.receiver.transform,null);assert.ok(w.terminated);
 w.emit({op:'ready'});h.feedback.allow(true);assert.equal(h.workers.length,1,'closed viewer cannot reactivate');
}
{
 const h=harness();h.feedback.attach(h.receiver);h.feedback.allow(true);const w=h.workers[0];w.emit({op:'ready'});
 for(let n=0;n<=64;n++){h.time(n);w.emit({op:'received',rtp:n});}
 assert.deepEqual(h.messages.map(v=>v.rtp),[0,16,32,48,64],
  'receipt reports must expose a path change within one60fps frame, with bounded frequency');
 h.feedback.close();
}
{
 const h=harness();h.feedback.attach(h.receiver);h.feedback.allow(true);const old=h.workers[0];
 const staleTimer=[...h.timers.values()][0];const next={transform:null};h.feedback.attach(next);const current=h.workers[1];
 old.emit({op:'ready'});old.onerror();staleTimer();current.emit({op:'ready'});
 assert.equal(next.transform.worker,current,'late old callbacks cannot disable a new worker');
 current.emit({op:'failed'});assert.equal(next.transform,null);assert.ok(current.terminated);
 h.feedback.allow(true);assert.equal(h.workers.length,2,'failed optional feature is not retried every health tick');
}
{
 const h=harness();h.feedback.attach(h.receiver);h.feedback.allow(true);const w=h.workers[0];w.emit({op:'ready'});
 const attached=h.receiver.transform;w.emit({op:'unsupported'});
 h.feedback.allow(false);h.feedback.allow(true);w.emit({op:'received',rtp:9});
 assert.equal(h.receiver.transform,attached,'missing optional metadata preserves unchanged encoded frames');
 assert.ok(!w.terminated);assert.equal(h.messages.length,0);assert.equal(h.workers.length,1);
 h.feedback.close();assert.ok(w.terminated);
}
{
 const begin=source.indexOf("const incomingVideo = peer.addTransceiver('video'");
 const end=source.indexOf('micTransceiver =',begin);
 assert.ok(begin>0&&end>begin);
 const h=harness();let creationTask=true;
 const receiver={get transform(){return this.value??null;},set transform(value){if(value!==null)assert.ok(creationTask,'receiver already bypassed');this.value=value;}};
 vm.runInNewContext(source.slice(begin,end),{peer:{addTransceiver(){return{receiver};}},receiveFeedback:h.feedback});
 creationTask=false;
 assert.equal(receiver.transform.worker,h.workers[0],'actual console flow attaches within receiver creation task');
 h.workers[0].emit({op:'ready'});h.feedback.close();
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
 const legacy=frame(9);input.enqueue(legacy);await wait(1);assert.equal(posts.length,0,'legacy server media passes without receipts');
 self.onmessage({data:'allow'});
 const first=frame(10),second=frame(11);input.enqueue(first);await wait(2);
 now=60;input.enqueue(second);await wait(3);
 assert.equal(posts.length,1,'at most one unconsumed report');assert.equal(frames[0],legacy);assert.equal(frames[1],first);assert.equal(frames[2],second);
 self.onmessage({data:'next'});now=120;input.enqueue(frame(12));await wait(4);
 assert.equal(posts.length,2);assert.equal(posts.at(-1).rtp,12);
 for(let n=1;n<=64;n++){
  self.onmessage({data:'next'});now=120+n;input.enqueue(frame(100+n));await wait(4+n);
 }
 assert.deepEqual(posts.slice(2).map(v=>v.rtp),[116,132,148,164],
  'the actual worker reports at16ms intervals while forwarding every original frame');
 self.onmessage({data:'next'});self.onmessage({data:'deny'});now=200;input.enqueue(frame(200));await wait(69);
 assert.equal(posts.length,6,'revoked permission suppresses worker reports without stopping frames');
 self.onmessage({data:'allow'});now=220;input.enqueue({getMetadata(){return{};},get data(){throw Error('payload was read');}});await wait(70);
 assert.equal(posts.at(-1).op,'unsupported');const count=posts.length;
 now=250;input.enqueue(frame(250));await wait(71);assert.equal(posts.length,count,'unsupported metadata permanently disables only reports');
 input.close();await done;
 h.feedback.close();
}
console.log('QSM_VIDEO_RECEIVED_OK');
