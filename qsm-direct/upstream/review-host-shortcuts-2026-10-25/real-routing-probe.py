import asyncio,json,os,subprocess,shlex,time,sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
from playwright.async_api import async_playwright
BASELINE='--baseline' in sys.argv
ROOT=Path('/tmp/qsm-switch-routing-20261025')/('baseline' if BASELINE else 'fixed');ROOT.mkdir(exist_ok=True)
source=subprocess.check_output(['git','show','v0.4.0-qsm-direct-20261025-stock4.9:qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js'],text=True) if BASELINE else Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
def part(a,b):
 i=source.index(a);return source[i:source.index(b,i)]
base='''(()=>{
const popup=window,document=window.document,gettext=s=>s;
const video=document.querySelector('video'),kbInput=document.querySelector('#kb'),status=document.querySelector('#status');
const captureKeyboard=document.querySelector('#capture'),captureKeyboardSettings=document.querySelector('#capture-settings'),keyboardCaptureStatus=document.querySelector('#capture-status'),fullscreen=document.querySelector('#full'),settingsButton=document.querySelector('#settings');
const releaseKeyboardSettings=document.querySelector('#release-settings');
const keyboardPriorityInput={value:'guest-first'},KEYBOARD_PRIORITY={guest:'guest-first',client:'client-first',raw:'raw-events'},KEY_INPUT_MODE={typing:'client-repeat',physical:'guest-repeat'};
const settings={keyboardPriority:'guest-first',keyInputMode:'client-repeat'},keyInputModeHint={},mouseInputModeHint={};
const heldKeys=new Set(),physicalHeldKeys=new Set(),heldMouseButtons=new Set();
let inputStateEnabled=false,inputStateTimer=null,inputTapsEnabled=false,inputMouseActionsEnabled=false,fullscreenEscapePending=false,closed=false,settingsPanelOpen=false;
let releaseHeldInput=()=>{},restoreClientFocusAfterFullscreen=false;
const noteGuestInput=()=>{},setFullscreenToggle=()=>{},resizeConsole=()=>{},cancelPendingMouse=()=>{},sendMouseButton=()=>{};
let diagKeySent=0,diagLastInput='';
const send=m=>{control.send(JSON.stringify(m));window.sent.push(m);return true;};window.sent=[];
const applyConsoleSettings=()=>{};
const setSettingsPanelOpen=open=>{settingsPanelOpen=open;updateGuestKeyboardLock();};
const guestCaptureExitShortcut=e=>e.code==='Escape'&&e.ctrlKey&&e.altKey&&e.shiftKey&&!e.metaKey;
const clientFirstShortcut=()=>false,pasteFromBrowser=()=>Promise.resolve(),guestSelectionToBrowser=()=>Promise.resolve();
'''
base+=part('const scanCodes =','// A guest-first console')
base+=part('const deferredModifiers =','let fullscreenEscapePending =')
base+=part('let keyboardLockGeneration =','const bytesToB64 =')
base+=part('const inputModifiers =','const sendMouseAction =')
base+=part('const sendGuestChord =','// Ctrl+C, or Ctrl+Shift+C')
base+=part('const sendKeyboard =','// Map a printable character')
base+=part('releaseHeldInput = () => {','const close = () => {')
base+=part("document.addEventListener('fullscreenchange'","const copy = document.createElement")
base+=part("for (const name of ['keydown', 'keyup']) {\n                video.addEventListener","peer.addTransceiver('video'")
base+='''
control.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.op==='qsm_input_policy')acceptInputPolicy(m);});
acceptInputPolicy({version:1,lease_ms:200,snapshot_ms:50,key_taps:true,mouse_actions:true});
settingsButton.onclick=()=>setSettingsPanelOpen(true);
fullscreen.onclick=()=>{if(document.fullscreenElement)exitConsoleFullscreen();else enterConsoleFullscreen(true);};
popup.addEventListener('blur',releaseHeldInput);
window.captureState=()=>({full:!!document.fullscreenElement,status:keyboardCaptureStatus.textContent,pressed:captureKeyboard.getAttribute('aria-pressed'),active:document.activeElement.id,keyboardLock:!!navigator.keyboard?.lock,sent:sent.length});
window.setRules=r=>{settings.hostShortcuts=normalizeShortcutRules(r);};window.resetRules=()=>{settings.hostShortcuts=defaultShortcutRules();};window.sendOverride=s=>{const c=shortcutGuestChord(s);sendGuestChord(c.modifiers,c.key);};
window.setMode=m=>{settings.keyInputMode=m;};window.setPriority=p=>{settings.keyboardPriority=p;};
window.setClient=()=>{settings.keyboardPriority='client-first';updateGuestKeyboardLock();};
window.finish=()=>{releaseHeldInput();closed=true;releaseGuestKeyboardLock();clearInterval(inputStateTimer);};
})();'''
HTML=b'''<body><button id="release-settings">Release</button><button id="baseline" onclick="document.documentElement.requestFullscreen()">Baseline full screen</button><button id="capture">Capture keyboard</button><button id="capture-settings">Capture from settings</button><button id="full">Full screen</button><button id="settings">Settings</button><span id="capture-status"></span><span id="status"></span><input id="kb" style="display:none"><video id="guest" tabindex="0" autoplay muted playsinline style="display:block;width:90vw;height:70vh;background:black"></video>'''
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
  self.wfile.write(b'<iframe id="console" src="/console" allow="autoplay; fullscreen; clipboard-read; clipboard-write" allowfullscreen style="width:96vw;height:90vh"></iframe>' if self.path=='/embedded' else HTML)
 def log_message(self,*args):pass
async def node(cmd):
 p=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@192.168.64.20',cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE);o,e=await p.communicate();assert p.returncode==0,e.decode();return o.decode()
async def fixture(reset=False):
 if reset:
  await node('pct exec 108 -- touch /run/qsm-keycapture25/reset')
  code="import glob,subprocess;p=glob.glob('/run/qsm-display-1/sway-ipc.*.sock')[0];subprocess.run(['swaymsg','-s',p,'[title=\"QSM isolated fullscreen keyboard fixture\"] focus'],check=True)"
  await node(shlex.join(['pct','exec','108','--','python3','-c',code]));await asyncio.sleep(.15)
 return json.loads(await node('pct exec 108 -- cat /run/qsm-keycapture25/result.json'))
async def main():
 web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start();results=[]
 env={**os.environ,'DISPLAY':':219'};xvfb=subprocess.Popen(['Xvfb',env['DISPLAY'],'-screen','0','1280x900x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);wm=None
 try:
  await asyncio.sleep(.3);assert xvfb.poll() is None;wm=subprocess.Popen(['openbox','--sm-disable'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);await asyncio.sleep(.3)
  async with async_playwright() as p:
   browser=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=False,env={**env,'MOZ_GMP_PATH':'/tmp/qsm-rate-bands-20261023/gmp-gmpopenh264/2.6.0'},firefox_user_prefs={'media.autoplay.default':0,'media.gmp-gmpopenh264.enabled':True,'media.gmp-gmpopenh264.version':'2.6.0','media.gmp-gmpopenh264.autoupdate':False})
   try:
    for mode in ['top','embedded']:
     log=(ROOT/f'{mode}-bridge.log').open('wb');server=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@192.168.64.20','python3 /root/qsm-shortcut-rules25/isolated-input-bridge.py new',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=log)
     async def rpc(op,**kw):
      server.stdin.write((json.dumps({'op':op,**kw})+'\n').encode());await server.stdin.drain();line=await asyncio.wait_for(server.stdout.readline(),15);assert line,(server.returncode,log.name);return json.loads(line)
     ctx=await browser.new_context();page=await ctx.new_page();frame=None
     try:
      await page.goto(f'http://127.0.0.1:{web.server_port}/'+('embedded' if mode=='embedded' else 'console'));f=page.frame_locator('#console') if mode=='embedded' else page;frame=page.frames[-1] if mode=='embedded' else page.main_frame
      offer=await frame.evaluate("""async()=>{window.peer=new RTCPeerConnection();const stream=new MediaStream();document.querySelector('video').srcObject=stream;peer.ontrack=e=>{stream.addTrack(e.track);document.querySelector('video').play().catch(()=>{})};peer.addTransceiver('video',{direction:'recvonly'});peer.addTransceiver('audio',{direction:'recvonly'});window.control=peer.createDataChannel('qsm-control',{ordered:true});peer.createDataChannel('qsm-pointer',{ordered:false,maxRetransmits:0});await peer.setLocalDescription(await peer.createOffer());while(peer.iceGatheringState!=='complete')await new Promise(r=>setTimeout(r,20));return peer.localDescription.sdp}""")
      lines=offer.split('\r\n')
      for i,line in enumerate(lines):
       if line.startswith('a=candidate:'):
        fields=line.split()
        if fields[4].endswith('.local'):fields[4]='192.168.64.25';lines[i]=' '.join(fields)
      answer=await rpc('offer',sdp='\r\n'.join(lines));await frame.evaluate('a=>peer.setRemoteDescription(a)',answer);await frame.wait_for_function("document.querySelector('video').videoWidth>0 && control.readyState==='open'",timeout=45000)
      await frame.evaluate("Object.defineProperty(navigator,'platform',{get:()=> 'MacIntel'});");await frame.evaluate(base);await f.locator('#guest').focus()
      async def inject(events):
       await fixture(True);await rpc('clear_inputs');await frame.evaluate("events=>{const v=document.querySelector('video');for(const e of events)v.dispatchEvent(new KeyboardEvent(e.type,{bubbles:true,cancelable:true,...e}));}",events);await asyncio.sleep(.2);return (await rpc('stats'))['input_trace']
      def chord(code='Tab',reverse=False,release_alt_first=False):
       events=[]
       if reverse:events.append({'type':'keydown','code':'ShiftLeft','key':'Shift','shiftKey':True})
       events += [{'type':'keydown','code':'MetaLeft','key':'Alt','altKey':True,'shiftKey':reverse},{'type':'keydown','code':code,'key':code,'altKey':True,'shiftKey':reverse}]
       release=[{'type':'keyup','code':code,'key':code,'altKey':True,'shiftKey':reverse},{'type':'keyup','code':'MetaLeft','key':'Alt','altKey':False,'shiftKey':reverse}]
       if release_alt_first:release=[release[1],{**release[0],'altKey':False}]
       events+=release
       if reverse:events.append({'type':'keyup','code':'ShiftLeft','key':'Shift','shiftKey':False})
       return events
      for case,events in [('FULL_ALT_TAB',chord()),('FULL_REVERSE_ALT_TAB',chord(reverse=True)),('ALT_RELEASE_BEFORE_TAB',chord(release_alt_first=True))]:
       wire=await inject(events);guest=await fixture()
       if BASELINE:assert any(m['op']=='key_tap' and m['key']==15 and 56 in m['modifiers'] for m in wire),wire
       else:assert wire==[] and guest['events']==[],(wire,guest)
       results.append({'mode':mode,'case':case,'all_DOM_events_delivered_before_blur':True,'wire':wire,'guest':guest});print(mode,case,'BASELINE_GUEST_CHORD' if BASELINE else 'ZERO_GUEST_PACKETS',flush=True)
      if not BASELINE:
       await frame.evaluate("setRules([{shortcut:'Ctrl+KeyK',action:'host'}])")
       events=[{'type':'keydown','code':'ControlLeft','key':'Control','ctrlKey':True},{'type':'keydown','code':'KeyK','key':'k','ctrlKey':True},{'type':'keyup','code':'ControlLeft','key':'Control','ctrlKey':False},{'type':'keyup','code':'KeyK','key':'k','ctrlKey':False}]
       wire=await inject(events);assert wire==[],wire;results.append({'mode':mode,'case':'CUSTOM_HOST_RULE','wire':wire})
       wire=await inject(chord());assert wire==[{'op':'key_tap','key':15,'modifiers':[56]}],wire;results.append({'mode':mode,'case':'REMOVED_RULE_GOES_TO_GUEST','wire':wire})
       await frame.evaluate('resetRules()');await fixture(True);await rpc('clear_inputs');await frame.evaluate("sendOverride('Alt+Tab')");await asyncio.sleep(.2);wire=(await rpc('stats'))['input_trace'];assert wire==[{'op':'key_tap','key':15,'modifiers':[56]}],wire;guest=await fixture()
       results.append({'mode':mode,'case':'EXPLICIT_SEND_TO_GUEST_BYPASS','wire':wire,'guest':guest})
       await frame.evaluate("setPriority('raw-events')");events=chord();events[0]['code']='AltLeft';events[-1]['code']='AltLeft';wire=await inject(events);assert [(x['key'],x.get('down'))for x in wire]==[(56,True),(15,True),(15,False),(56,False)],wire;results.append({'mode':mode,'case':'RAW_BYPASS','wire':wire});print(mode,'EDITABLE_RULES_AND_SEND_OVERRIDE_PASS',flush=True)
     finally:
      if frame:
       try:await frame.evaluate('finish()')
       except Exception:pass
      await page.close()
      if server.returncode is None:
       try:await rpc('quit')
       except Exception:pass
       await asyncio.wait_for(server.wait(),10)
      log.close();await ctx.close()
   finally:await browser.close()
 finally:
  web.shutdown()
  if wm:wm.terminate();wm.wait(timeout=3)
  xvfb.terminate();xvfb.wait(timeout=3)
  (ROOT/'result.json').write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
