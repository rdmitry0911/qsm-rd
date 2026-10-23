#!/usr/bin/env python3
"""Firefox receiver with manual stock4 network-target transitions."""
import asyncio,json,os,re,sys,time,subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
from playwright.async_api import async_playwright
ROOT=Path(os.environ['QSM_NVENC_ARTIFACTS']);ROOT.mkdir(parents=True,exist_ok=True)
ENCODER=sys.argv[1];assert ENCODER in ('hevc_nvenc','h264_nvenc');MIME='video/H265' if ENCODER=='hevc_nvenc' else 'video/H264';STATIC_ONLY='--static-only' in sys.argv
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<video autoplay muted playsinline style="width:1280px;height:800px"></video>')
 def log_message(self,*a):pass
async def shape(rate):
 return
async def node(cmd):
 p=await asyncio.create_subprocess_exec('ssh','-o','ControlMaster=no','-o','ControlPath=none','-o','ControlPersist=no','-o','BatchMode=yes','root@192.168.64.20',cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE);out,err=await p.communicate();assert p.returncode==0,err.decode();return out.decode()
async def main():
 await shape(100 if STATIC_ONLY else 20)
 if STATIC_ONLY:await node('pct exec 108 -- touch /run/qsm-bands23-static-unused')
 serverlog=(ROOT/'server.log').open('wb');server=await asyncio.create_subprocess_exec('ssh','-o','ControlMaster=no','-o','ControlPath=none','-o','ControlPersist=no','-o','BatchMode=yes','root@192.168.64.20','python3 /root/qsm-rate-bands-20261023/server/isolated-bridge.py '+ENCODER+'',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=serverlog)
 async def rpc(op,**kw):
  server.stdin.write((json.dumps(dict(op=op,**kw))+'\n').encode());await server.stdin.drain();line=await asyncio.wait_for(server.stdout.readline(),40);assert line,'bridge exited';return json.loads(line)
 browser=None;web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start()
 try:
  async with async_playwright() as p:
   browser=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=True,env={**os.environ,'MOZ_GMP_PATH':'/tmp/qsm-rate-bands-20261023/gmp-gmpopenh264/2.6.0'},firefox_user_prefs={'media.autoplay.default':0,'media.gmp-gmpopenh264.enabled':True,'media.gmp-gmpopenh264.version':'2.6.0','media.gmp-gmpopenh264.autoupdate':False})
   page=await browser.new_page(viewport={'width':1280,'height':800});await page.goto('http://127.0.0.1:'+str(web.server_port))
   print('RECEIVER_FIREFOX',browser.version,flush=True)
   offer=await page.evaluate('''async()=>{window.peer=new RTCPeerConnection();const media=new MediaStream();const video=document.querySelector('video');video.srcObject=media;peer.ontrack=e=>{media.addTrack(e.track);video.play().catch(()=>{})};peer.addTransceiver('video',{direction:'recvonly'});peer.addTransceiver('audio',{direction:'recvonly'});window.control=peer.createDataChannel('qsm-control',{ordered:true});peer.createDataChannel('qsm-pointer',{ordered:false,maxRetransmits:0});await peer.setLocalDescription(await peer.createOffer());const deadline=Date.now()+12000;while(peer.iceGatheringState!=='complete'&&Date.now()<deadline)await new Promise(r=>setTimeout(r,20));return peer.localDescription.sdp}''')
   # Only the known test namespace's obfuscated host address is resolved here.
   # SDP/ICE credentials travel directly through SSH pipes and are never saved.
   lines=offer.split('\r\n');rewrites=0
   for n,line in enumerate(lines):
    if not line.startswith('a=candidate:'):continue
    fields=line.split()
    if len(fields)>5 and fields[4].endswith('.local'):fields[4]='192.168.64.25';lines[n]=' '.join(fields);rewrites+=1
   answer=await rpc('offer',sdp='\r\n'.join(lines));print('NEGOTIATION',json.dumps({'mdns_test_address_resolutions':rewrites,'answer_codecs':[l for l in answer['sdp'].split('\r\n') if l.startswith(('a=rtpmap:','a=fmtp:'))]}),flush=True)
   await page.evaluate('(a)=>peer.setRemoteDescription(a)',answer);await page.wait_for_function("document.querySelector('video').videoWidth>0",timeout=45000)
   async def sample(stage):
    data=await page.evaluate('''async()=>{const r=await peer.getStats();const v=Array.from(r.values()).find(s=>s.type==='inbound-rtp'&&s.kind==='video');const c=r.get(v?.codecId);return {connection:peer.connectionState,mimeType:c?.mimeType,...v}}''');assert data['mimeType']==MIME,data;assert data['framesDecoded']>0,data
    data={'stage':stage,'browser':data,'server':await rpc('stats')};print('SAMPLE',json.dumps(data),flush=True)
    with (ROOT/'samples.jsonl').open('a') as f:f.write(json.dumps(data)+'\n')
    return data
   async def motion():
    d=await page.evaluate('''async()=>{const v=document.querySelector('video'),c=document.createElement('canvas');c.width=160;c.height=100;const ctx=c.getContext('2d',{willReadFrequently:true});let old=null,changed=0;for(let n=0;n<20;n++){ctx.drawImage(v,0,0,160,100);const a=ctx.getImageData(0,0,160,100).data;if(old){let delta=0;for(let i=0;i<a.length;i+=4)delta+=Math.abs(a[i]-old[i])+Math.abs(a[i+1]-old[i+1])+Math.abs(a[i+2]-old[i+2]);if(delta/(160*100*3)>.1)changed++}old=a;await new Promise(r=>setTimeout(r,100))}return {changed,total:19}}''');assert d['changed']>=16,d;print('MOTION_PASS',json.dumps(d),flush=True)
   await asyncio.sleep(4)
   await node('pct exec 108 -- touch /run/qsm-bands23-motion')
   if not STATIC_ONLY:await motion()
   initial=await sample('initial')
   await page.evaluate("""()=>{const v=document.querySelector('video'),c=document.createElement('canvas');c.width=150;c.height=100;const ctx=c.getContext('2d',{willReadFrequently:true});window.probe={count:0,maxMeanDelta:0,stop:false};let old=null;function tick(){if(probe.stop)return;ctx.drawImage(v,0,0);const a=ctx.getImageData(0,0,150,100).data;if(old){let d=0;for(let n=0;n<a.length;n+=4)d+=Math.abs(a[n]-old[n])+Math.abs(a[n+1]-old[n+1])+Math.abs(a[n+2]-old[n+2]);probe.maxMeanDelta=Math.max(probe.maxMeanDelta,d/(150*100*3));probe.count++}old=a;v.requestVideoFrameCallback(tick)}v.requestVideoFrameCallback(tick)}""")
   for rate,duration,stage in ([] if STATIC_ONLY else [(8000,8,'unlimited'),(2000,15,'congestion'),(8000,15,'recovery')]):
    await rpc('rate',target=rate);print('STAGE',stage,rate,flush=True)
    for _ in range((duration+4)//5):await asyncio.sleep(5);await sample(stage)
   quality=await page.evaluate('()=>{probe.stop=true;return probe}')
   print('STATIC_DURING_TARGET_CHANGES',json.dumps(quality),flush=True)
   assert quality['count']>100 and quality['maxMeanDelta']<.15,quality
   if not STATIC_ONLY:
    await motion();before=await sample('before-pli');await rpc('idr');await asyncio.sleep(3);after=await sample('after-pli')
    assert after['browser']['framesDecoded']>before['browser']['framesDecoded']+60
    assert after['browser']['keyFramesDecoded']>before['browser']['keyFramesDecoded'];print('PLI_RECOVERY_PASS',flush=True)
    await page.locator('video').screenshot(path=str(ROOT/'recovered.png'))
   await node('pct exec 108 -- rm -f /run/qsm-bands23-motion');await asyncio.sleep(5)
   stable=await page.evaluate('''async()=>{const v=document.querySelector('video'),c=document.createElement('canvas');c.width=600;c.height=760;const ctx=c.getContext('2d',{willReadFrequently:true});let old=null,max=0,sum=0;for(let n=0;n<200;n++){ctx.drawImage(v,0,0);const a=ctx.getImageData(0,0,600,760).data;if(old){let delta=0;for(let i=0;i<a.length;i+=4)delta+=Math.abs(a[i]-old[i])+Math.abs(a[i+1]-old[i+1])+Math.abs(a[i+2]-old[i+2]);delta/=600*760*3;sum+=delta;max=Math.max(max,delta)}old=a;await new Promise(r=>setTimeout(r,100))}return {samples:199,maxMeanDelta:max,averageMeanDelta:sum/199}}''')
   print('STATIC_TEXT',json.dumps(stable),flush=True)
   await page.locator('video').screenshot(path=str(ROOT/'static.png'));final=await sample('final')
   assert stable['maxMeanDelta']<.15,stable
   rows=[json.loads(l) for l in (ROOT/'samples.jsonl').read_text().splitlines()]
   assert final['server']['target']>=4000
   assert len({x['browser']['ssrc'] for x in rows})==1 and final['browser']['framesDropped']==0
   if STATIC_ONLY:
    assert final['browser']['framesDecoded']>initial['browser']['framesDecoded']+600
    assert final['browser']['keyFramesDecoded']==initial['browser']['keyFramesDecoded']
    assert not final['server']['targets']
   else:
    assert final['browser']['framesDecoded']>initial['browser']['framesDecoded']+1000
    assert final['browser']['framesDecoded']>after['browser']['framesDecoded']+600
    assert min(x['server']['target'] for x in rows)<3000
   (ROOT/'result.json').write_text(json.dumps({'pass':True,'encoder':ENCODER,'initial':initial,'final':final,'static':stable,'minimum_target':min(x['server']['target'] for x in rows),'same_stream':True,'pli_recovery':not STATIC_ONLY,'static_only':STATIC_ONLY},indent=2)+'\n')
   print('PASS',ENCODER,flush=True)
 finally:
  if browser:await browser.close()
  if server.returncode is None:
   try:server.stdin.write(b'{"op":"quit"}\n');await server.stdin.drain();await asyncio.wait_for(server.wait(),10)
   except (BrokenPipeError,ConnectionResetError,asyncio.TimeoutError):server.terminate();await server.wait()
  serverlog.close();web.shutdown()
  await node('pct exec 108 -- rm -f /run/qsm-bands23-motion /run/qsm-bands23-static-unused')
  await shape(20)
asyncio.run(main())
