import asyncio,json,os,subprocess,time,shlex
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
from playwright.async_api import async_playwright
ROOT=Path('/tmp/qsm-keyboard-20261024')
source=(Path(__file__).resolve().parents[2] / 'integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
def section(a,b):
 i=source.index(a);return source[i:source.index(b,i)]
base="""
(()=>{
const document=window.document,popup=window;
const video=document.querySelector('video'),kbInput=document.querySelector('input');
const heldKeys=new Set(),heldMouseButtons=new Set();
let inputStateEnabled=false,inputStateTimer=null,inputTapsEnabled=false;const physicalHeldKeys=new Set(),keyInputModeHint={};let diagKeySent=0,diagLastInput='',fullscreenEscapePending=false;
const noteGuestInput=()=>{},settings={keyboardPriority:'guest-first',keyInputMode:'guest-repeat'},KEY_INPUT_MODE={typing:'client-repeat',physical:'guest-repeat'},KEYBOARD_PRIORITY={client:'client-first',raw:'raw-events'};
const clientFirstShortcut=()=>false,guestCaptureExitShortcut=()=>false,leaveGuestKeyboardCapture=()=>{},releaseHeldInput=()=>{};
const pasteFromBrowser=()=>Promise.resolve(),guestSelectionToBrowser=()=>Promise.resolve(),status={},gettext=t=>t;
"""
base+=section('const scanCodes =','// A guest-first console')
base+=section('const send =','let keyboardLockGeneration')
base+=section('const sendGuestChord =','// Ctrl+C, or Ctrl+Shift+C')
base+=section('const sendKeyboard =','// Map a printable character')
base+=section("for (const name of ['keydown', 'keyup']) {\n                video.addEventListener", "peer.addTransceiver('video'")
base+="""
control.addEventListener('message',event=>{const value=JSON.parse(event.data);if(value.op==='qsm_input_policy'){window.inputPolicy=value;acceptInputPolicy(value);}});
window.inputMode=(mode,raw=false)=>{settings.keyInputMode=mode;settings.keyboardPriority=raw?'raw-events':'guest-first';};
window.sendChord=sendGuestChord;
window.injectInputGap=(after=0)=>{
 const native=control.send.bind(control);let queued=[],paused=false,used=false;
 const pause=()=>{paused=true;setTimeout(()=>{paused=false;for(const packet of queued)native(packet);window.delayedPackets=queued.length;queued=[];},900);};
 if(after>0){used=true;setTimeout(pause,after);}
 control.send=value=>{const msg=JSON.parse(value);
  if(!used&&((msg.op==='keyboard'&&msg.down)||msg.op==='key_tap')&&msg.key===30){used=true;native(value);pause();}
  else if(paused)queued.push(value);else native(value);
 };
};
"""
base+='})();'
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<video tabindex="0" autoplay muted playsinline style="width:1280px;height:720px"></video><input style="display:none">')
 def log_message(self,*args):pass
async def node(cmd):
 p=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@192.168.64.20',cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE);o,e=await p.communicate();assert p.returncode==0,e.decode();return o.decode()
async def fixture(reset=False):
 if reset:
  await node('pct exec 108 -- touch /run/qsm-keyboard24/reset')
  code="import glob,subprocess; p=glob.glob('/run/qsm-display-1/sway-ipc.*.sock')[0]; subprocess.run(['swaymsg','-s',p,'[title=\"QSM isolated keyboard timing fixture\"] focus'],check=True)"
  await node(shlex.join(['pct','exec','108','--','/usr/bin/python3','-c',code]));await asyncio.sleep(.2)
 return json.loads(await node('pct exec 108 -- cat /run/qsm-keyboard24/result.json'))
async def main():
 web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start()
 results=[]
 try:
  async with async_playwright() as p:
   browser=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=True,env={**os.environ,'MOZ_GMP_PATH':'/tmp/qsm-rate-bands-20261023/gmp-gmpopenh264/2.6.0'},firefox_user_prefs={'media.autoplay.default':0,'media.gmp-gmpopenh264.enabled':True,'media.gmp-gmpopenh264.version':'2.6.0','media.gmp-gmpopenh264.autoupdate':False})
   try:
    for mode in ['old','new']:
     log=(ROOT/f'private-{mode}.log').open('wb')
     server=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@192.168.64.20',f'python3 /root/qsm-keyboard24/isolated-input-bridge.py {mode}',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=log)
     async def rpc(op,**kw):
      server.stdin.write((json.dumps(dict(op=op,**kw))+'\n').encode());await server.stdin.drain();line=await asyncio.wait_for(server.stdout.readline(),45);assert line,'bridge exited';return json.loads(line)
     page=await browser.new_page(viewport={'width':1280,'height':720})
     page.on('pageerror',lambda error:print('PAGE_ERROR',str(error),flush=True))
     try:
      await page.goto(f'http://127.0.0.1:{web.server_port}')
      offer=await page.evaluate('''async()=>{window.peer=new RTCPeerConnection();const stream=new MediaStream();document.querySelector('video').srcObject=stream;peer.ontrack=e=>{stream.addTrack(e.track);document.querySelector('video').play().catch(()=>{})};peer.addTransceiver('video',{direction:'recvonly'});peer.addTransceiver('audio',{direction:'recvonly'});window.control=peer.createDataChannel('qsm-control',{ordered:true});peer.createDataChannel('qsm-pointer',{ordered:false,maxRetransmits:0});await peer.setLocalDescription(await peer.createOffer());while(peer.iceGatheringState!=='complete')await new Promise(r=>setTimeout(r,20));return peer.localDescription.sdp}''')
      await page.add_script_tag(content=base)
      lines=offer.split('\r\n')
      for n,line in enumerate(lines):
       if line.startswith('a=candidate:'):
        fields=line.split()
        if fields[4].endswith('.local'):fields[4]='192.168.64.25';lines[n]=' '.join(fields)
      answer=await rpc('offer',sdp='\r\n'.join(lines));await page.evaluate('(a)=>peer.setRemoteDescription(a)',answer)
      await page.wait_for_function("document.querySelector('video').videoWidth>0 && control.readyState==='open'",timeout=45000)
      await asyncio.sleep(.5)
      cleared=await fixture(True);assert cleared['length']==0 and cleared['focus'],cleared
      await page.locator('video').focus()
      async def delayed_tap(label,hold=.1,after=0,typing=False):
       await fixture(True)
       await page.evaluate('(typing)=>inputMode(typing?"client-repeat":"guest-repeat")',typing)
       await page.evaluate('()=>{control.send(JSON.stringify({op:"mouse_position",x:150,y:180,width:1280,height:720}));control.send(JSON.stringify({op:"mouse_button",button:1,down:true}));control.send(JSON.stringify({op:"mouse_button",button:1,down:false}));}')
       await asyncio.sleep(.1)
       await page.evaluate('(after)=>injectInputGap(after)',after)
       await page.keyboard.down('a');await asyncio.sleep(hold);await page.keyboard.up('a');await asyncio.sleep(1.2)
       value=await fixture();state=await rpc('stats')
       print(mode,label,json.dumps(value),json.dumps(state),flush=True)
       results.append({'mode':mode,'case':label,'fixture':value,'server':state})
       return value,state
      if mode=='old':
       value,_=await delayed_tap('LEGACY_TAP');assert value['length']>1,value
      else:
       value,_=await delayed_tap('GUARDED_TAP');assert value['length']==1,value
       value,_=await delayed_tap('GUARDED_LATE_GAP',hold=.55,after=510);assert value['length']>1,value
       value,state=await delayed_tap('EXACT_TAP',typing=True);assert value['length']==1,value
       value,state=await delayed_tap('EXACT_LATE_GAP',hold=.55,after=510,typing=True);assert value['length']==1,value
       await fixture(True);await page.evaluate('inputMode("client-repeat")')
       before=await rpc('stats');await page.evaluate('injectInputGap()')
       await page.keyboard.down('a');await asyncio.sleep(.6)
       for _ in range(5):await page.keyboard.down('a');await asyncio.sleep(.04)
       await page.keyboard.up('a');await asyncio.sleep(1.1)
       repeated=await fixture();state=await rpc('stats')
       assert repeated['length']==6,repeated
       assert state['taps']-before['taps']==6,state
       print(mode,'SIX_CONFIRMED_EVENTS',json.dumps(repeated),json.dumps(state),flush=True)
       results.append({'mode':mode,'case':'SIX_CONFIRMED_EVENTS','fixture':repeated,'server':state})
       await fixture(True);await page.evaluate('inputMode("client-repeat",true)');before=await rpc('stats')
       await page.keyboard.down('a');await asyncio.sleep(1);await page.keyboard.up('a');await asyncio.sleep(.2)
       hold=await fixture();state=await rpc('stats')
       assert hold['length']>1 and state['expired']==before['expired'] and state['taps']==before['taps'],(hold,state)
       print(mode,'RAW_HOLD',json.dumps(hold),json.dumps(state),flush=True)
       results.append({'mode':mode,'case':'RAW_HOLD','fixture':hold,'server':state})
       await fixture(True);await page.evaluate('inputMode("client-repeat")');await page.keyboard.press('Shift+a');await asyncio.sleep(.2)
       shifted=await fixture();assert shifted['length']==1 and shifted['uppercase'],shifted
       await page.evaluate('sendChord([42],0xcb)');await asyncio.sleep(.2)
       chord=await fixture();assert chord['selected']==1,chord
       print(mode,'MODIFIER_AND_ATOMIC_CHORD',json.dumps(chord),flush=True)
       results.append({'mode':mode,'case':'MODIFIER_AND_ATOMIC_CHORD','fixture':chord})
     finally:
      await page.close()
      if server.returncode is None:
       try:await rpc('quit')
       except (AssertionError,BrokenPipeError,ConnectionResetError):pass
       try:await asyncio.wait_for(server.wait(),10)
       except asyncio.TimeoutError:server.terminate();await server.wait()
      log.close()
   finally:await browser.close()
 finally:web.shutdown()
 (ROOT/'real-input-result.json').write_text(json.dumps({'browser':'Firefox 157','fixture':'CT108 Sway/Qt text input','method':'production JS keyboard handlers; real SCTP/worker/virtual keyboard; synthetic 900 ms browser-to-server control gap only','results':results},indent=2)+'\n')
 print('PASS',flush=True)
asyncio.run(main())
