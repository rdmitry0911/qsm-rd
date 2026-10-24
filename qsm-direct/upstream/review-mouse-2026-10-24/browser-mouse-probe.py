import asyncio,json,os,subprocess,time,shlex
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
from playwright.async_api import async_playwright
ROOT=Path('/tmp/qsm-mouse-20261024')
source=Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
def part(a,b):
 i=source.index(a);return source[i:source.index(b,i)]
base='''(()=>{
const document=window.document,popup=window,video=document.querySelector('video');
const MOUSE_INPUT_MODE={exact:'exact-clicks',physical:'physical-buttons'},KEYBOARD_PRIORITY={raw:'raw-events'};
const settings={mouseInputMode:'exact-clicks',keyboardPriority:'guest-first'};
let inputMouseActionsEnabled=true,mouseFenceSequence=null,pointerSequence=0,pointerTimer=null,pendingPointer=null,latestPointer=null,nextPointerSendAt=0;
const heldMouseButtons=new Set(),physicalHeldKeys=new Set();
let diagButtonSent=0,diagLastInput='',usingTouchInput=false,closed=false;
const noteGuestInput=()=>{},ensurePlaying=()=>{},observeToolbarZone=()=>{},placeGuestCursor=()=>{};
const pointerForMouseEvent=e=>({op:'mouse_position',x:e.clientX,y:e.clientY,width:1280,height:720});
let pointerInToolbarZone=false,pointerNearToolbar=false;
const cancelToolbarReveal=()=>{},hideToolbarSoon=()=>{};
const pointer=window.pointer;
const sendPointer=v=>{pointer.send(JSON.stringify(v));return true;};
let blocked=false,queued=[],heartbeat=true;
const send=v=>{if(blocked)queued.push(v);else control.send(JSON.stringify(v));return true;};
window.gap=()=>{blocked=true;setTimeout(()=>{blocked=false;for(const v of queued)control.send(JSON.stringify(v));queued=[];},900);};
control.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.op==='qsm_mouse_ack'&&m.sequence===mouseFenceSequence)mouseFenceSequence=null;});
setInterval(()=>{if(heartbeat)send({op:'input_state',keys:[],buttons:[...heldMouseButtons]});},50);
'''
base+=part('const pendingMousePresses =','const physicalHeldKeys =')
base+=part('const exactMouseClicks =','const sendKeyboard =')
base+=part('const flushPointer =','const pointerForMouseEvent =')
base+='''const releaseHeldInput=()=>{cancelPendingMouse();for(const n of [...heldMouseButtons])sendMouseButton(n,false);};
window.mode=(name)=>{releaseHeldInput();settings.mouseInputMode=name==='legacy'?'physical-buttons':'exact-clicks';heartbeat=true;};
'''
base+=part('const buttonMask =','// Touch and pen input')
base+="document.addEventListener('mouseup',endMouse);"
base+=part('let wheelX =',"video.addEventListener('paste'")
base+='})();'
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'<body style="margin:0"><video tabindex="0" autoplay muted playsinline style="width:1280px;height:720px"></video>')
 def log_message(self,*args):pass
async def node(cmd):
 p=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@192.168.64.20',cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE);o,e=await p.communicate();assert p.returncode==0,e.decode();return o.decode()
async def fixture(reset=False):
 if reset:
  await node('pct exec 108 -- touch /run/qsm-mouse24/reset')
  code="import glob,subprocess; p=glob.glob('/run/qsm-display-1/sway-ipc.*.sock')[0]; subprocess.run(['swaymsg','-s',p,'[title=\"QSM isolated mouse timing fixture\"] focus'],check=True)"
  await node(shlex.join(['pct','exec','108','--','python3','-c',code]));await asyncio.sleep(.15)
 return json.loads(await node('pct exec 108 -- cat /run/qsm-mouse24/result.json'))
async def main():
 web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start();results=[]
 async with async_playwright() as p:
  browser=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=True,env={**os.environ,'MOZ_GMP_PATH':'/tmp/qsm-rate-bands-20261023/gmp-gmpopenh264/2.6.0'},firefox_user_prefs={'media.autoplay.default':0,'media.gmp-gmpopenh264.enabled':True,'media.gmp-gmpopenh264.version':'2.6.0','media.gmp-gmpopenh264.autoupdate':False})
  log=(ROOT/'private-bridge.log').open('wb')
  server=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@192.168.64.20','python3 /root/qsm-mouse24/isolated-input-bridge.py new',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=log)
  async def rpc(op,**kw):
   server.stdin.write((json.dumps(dict(op=op,**kw))+'\n').encode());await server.stdin.drain();line=await asyncio.wait_for(server.stdout.readline(),45);assert line,'bridge exited';return json.loads(line)
  page=await browser.new_page(viewport={'width':1280,'height':720});page.on('pageerror',lambda e:print('PAGE_ERROR',e,flush=True))
  async def record(name):
   state=await fixture();r={'case':name,'fixture':state,'server':await rpc('stats')};results.append(r);print(name,json.dumps(r),flush=True);return state
  try:
   await page.goto(f'http://127.0.0.1:{web.server_port}')
   offer=await page.evaluate('''async()=>{window.peer=new RTCPeerConnection();const stream=new MediaStream();document.querySelector('video').srcObject=stream;peer.ontrack=e=>{stream.addTrack(e.track);document.querySelector('video').play().catch(()=>{})};peer.addTransceiver('video',{direction:'recvonly'});peer.addTransceiver('audio',{direction:'recvonly'});window.control=peer.createDataChannel('qsm-control',{ordered:true});window.pointer=peer.createDataChannel('qsm-pointer',{ordered:false,maxRetransmits:0});await peer.setLocalDescription(await peer.createOffer());while(peer.iceGatheringState!=='complete')await new Promise(r=>setTimeout(r,20));return peer.localDescription.sdp}''')
   lines=offer.split('\r\n')
   for n,line in enumerate(lines):
    if line.startswith('a=candidate:'):
     f=line.split()
     if f[4].endswith('.local'):f[4]='192.168.64.25';lines[n]=' '.join(f)
   answer=await rpc('offer',sdp='\r\n'.join(lines));await page.evaluate('(a)=>peer.setRemoteDescription(a)',answer)
   await page.wait_for_function("document.querySelector('video').videoWidth===1280 && control.readyState==='open'",timeout=45000)
   await page.add_script_tag(content=base);await asyncio.sleep(.2)
   await page.evaluate('mode("legacy")');await fixture(True);await page.mouse.move(150,150);await page.mouse.down();await asyncio.sleep(.15);await page.evaluate('gap()');await asyncio.sleep(.05);await page.mouse.up();await asyncio.sleep(1.2)
   state=await record('LEGACY_DELAYED_RELEASE');assert state['clicks']>1,state
   await page.evaluate('mode("exact")');await fixture(True);await page.mouse.move(150,150);await page.mouse.down();await asyncio.sleep(.15);await page.evaluate('gap()');await asyncio.sleep(.05);await page.mouse.up();await page.mouse.move(650,500);await asyncio.sleep(1.2)
   state=await record('EXACT_DELAYED_CLICK_AND_LATER_MOTION');assert state['clicks']==1 and len(state['press'])==1 and state['press'][0][:2]==[150,150] and max(state['holds'])<100,state
   await fixture(True);await page.mouse.click(450,350,click_count=2,delay=50);await asyncio.sleep(.2)
   state=await record('DOUBLE_CLICK');assert len(state['press'])==2 and len(state['release'])==2 and state['double']==1,state
   await fixture(True);await page.mouse.click(450,350,button='right');await asyncio.sleep(.2)
   state=await record('TRACKPAD_SECONDARY_CLICK');assert len(state['press'])==1 and state['press'][0][2]==2,state
   await fixture(True);await page.mouse.move(400,400);await page.mouse.down();await page.mouse.move(450,400,steps=5);await asyncio.sleep(.12);await page.evaluate('gap()');await page.mouse.up();await page.mouse.move(750,550,steps=8);await asyncio.sleep(1.2)
   state=await record('DRAG_DELAYED_RELEASE_AND_LATER_MOTION');assert len(state['press'])==len(state['release'])==1 and state['drag'] and all(x<=450 for x,y,b in state['drag']),state
   await fixture(True);await page.mouse.move(450,350)
   await page.evaluate("()=>{const v=document.querySelector('video');for(let i=0;i<5;i++)v.dispatchEvent(new WheelEvent('wheel',{deltaX:.25,deltaY:.25,bubbles:true,cancelable:true}));}")
   await asyncio.sleep(.2);state=await record('FRACTIONAL_TWO_AXIS_TRACKPAD_SCROLL');assert any(x for x,y in state['wheel']) and any(y for x,y in state['wheel']),state
   await fixture(True);await page.mouse.move(150,150);await page.mouse.down();await asyncio.sleep(.9);await page.mouse.up();await asyncio.sleep(.2)
   state=await record('INTENTIONAL_LONG_HOLD');assert state['clicks']>1,state
  finally:
   await page.close()
   if server.returncode is None:
    try:await rpc('quit')
    except (BrokenPipeError,AssertionError):pass
    await asyncio.wait_for(server.wait(),10)
   log.close();await browser.close();web.shutdown();(ROOT/'real-mouse-result.json').write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
