import asyncio,json,os,subprocess,shlex,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
from playwright.async_api import async_playwright
ROOT=Path('/tmp/qsm-focus-20261025');source=Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
def part(a,b):
 i=source.index(a);return source[i:source.index(b,i)]
base='''(()=>{
const popup=window,document=window.document,gettext=s=>s;
window.fullscreenRequests=0;const realFullscreen=document.documentElement.requestFullscreen;document.documentElement.requestFullscreen=function(options){fullscreenRequests++;return realFullscreen.call(this,options);};
const video=document.querySelector('video'),kbInput=document.querySelector('#kb'),status=document.querySelector('#status');
const captureKeyboard=document.querySelector('#capture'),captureKeyboardSettings=document.querySelector('#capture-settings'),keyboardCaptureStatus=document.querySelector('#capture-status'),fullscreen=document.querySelector('#full'),settingsButton=document.querySelector('#settings');
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
base+=part('let keyboardLockGeneration =','const bytesToB64 =')
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
window.captureState=()=>({full:!!document.fullscreenElement,status:keyboardCaptureStatus.textContent,pressed:captureKeyboard.getAttribute('aria-pressed'),active:document.activeElement.id,keyboardLock:!!navigator.keyboard?.lock,sent:sent.length,requests:fullscreenRequests});
window.setClient=()=>{settings.keyboardPriority='client-first';updateGuestKeyboardLock();};
window.finish=()=>{releaseHeldInput();closed=true;releaseGuestKeyboardLock();clearInterval(inputStateTimer);};
})();'''
HTML=b'''<body><button id="baseline" onclick="document.documentElement.requestFullscreen()">Baseline full screen</button><button id="capture">Capture keyboard</button><button id="capture-settings">Capture from settings</button><button id="full">Full screen</button><button id="settings">Settings</button><span id="capture-status"></span><span id="status"></span><input id="kb" style="display:none"><video id="guest" tabindex="0" autoplay muted playsinline style="display:block;width:90vw;height:70vh;background:black"></video>'''
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
 display=':219';env={**os.environ,'DISPLAY':display}
 xvfb=subprocess.Popen(['Xvfb',display,'-screen','0','1280x900x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);wm=None
 def native(key):subprocess.run(['xdotool','key','--clearmodifiers',key],env=env,check=True)
 try:
  await asyncio.sleep(.3);assert xvfb.poll() is None,'Owned Xvfb did not start'
  wm=subprocess.Popen(['openbox','--sm-disable'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);await asyncio.sleep(.3)
  async with async_playwright() as p:
   browser=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=False,env={**env,'MOZ_GMP_PATH':'/tmp/qsm-rate-bands-20261023/gmp-gmpopenh264/2.6.0'},firefox_user_prefs={'focusmanager.testmode':False,'full-screen-api.ignore-widgets':False,'media.autoplay.default':0,'media.gmp-gmpopenh264.enabled':True,'media.gmp-gmpopenh264.version':'2.6.0','media.gmp-gmpopenh264.autoupdate':False})
   try:
    for mode in ['top','embedded']:
     log=(ROOT/f'{mode}-bridge.log').open('wb');server=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@192.168.64.20','python3 /root/qsm-keycapture25/isolated-input-bridge.py new',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=log)
     async def rpc(op,**kw):
      server.stdin.write((json.dumps(dict(op=op,**kw))+'\n').encode());await server.stdin.drain();line=await asyncio.wait_for(server.stdout.readline(),45);assert line,'bridge exited';return json.loads(line)
     ctx=await browser.new_context(viewport={'width':1100,'height':750});page=await ctx.new_page();page.on('pageerror',lambda e:print('PAGE_ERROR',str(e),flush=True))
     try:
      await page.goto(f'http://127.0.0.1:{web.server_port}/'+('embedded' if mode=='embedded' else 'console'))
      f=page.frame_locator('#console') if mode=='embedded' else page
      frame=page.frames[-1] if mode=='embedded' else page.main_frame
      offer=await frame.evaluate('''async()=>{window.peer=new RTCPeerConnection();const stream=new MediaStream();document.querySelector('video').srcObject=stream;peer.ontrack=e=>{stream.addTrack(e.track);document.querySelector('video').play().catch(()=>{})};peer.addTransceiver('video',{direction:'recvonly'});peer.addTransceiver('audio',{direction:'recvonly'});window.control=peer.createDataChannel('qsm-control',{ordered:true});peer.createDataChannel('qsm-pointer',{ordered:false,maxRetransmits:0});await peer.setLocalDescription(await peer.createOffer());while(peer.iceGatheringState!=='complete')await new Promise(r=>setTimeout(r,20));return peer.localDescription.sdp}''')
      lines=offer.split('\r\n')
      for i,line in enumerate(lines):
       if line.startswith('a=candidate:'):
        fields=line.split()
        if fields[4].endswith('.local'):fields[4]='192.168.64.25';lines[i]=' '.join(fields)
      answer=await rpc('offer',sdp='\r\n'.join(lines));await frame.evaluate('a=>peer.setRemoteDescription(a)',answer)
      await frame.wait_for_function("document.querySelector('video').videoWidth>0 && control.readyState==='open'",timeout=45000)
      await frame.evaluate(base);await fixture(True);await f.locator('#guest').focus();native('a');await asyncio.sleep(.2)
      value=await fixture();assert any(x[0]=='press' and x[1]==65 for x in value['events']),('native preflight',value)
      if mode=='top':
       await fixture(True);await f.locator('#baseline').click();await frame.wait_for_function('!!document.fullscreenElement');await f.locator('#guest').focus();native('Escape');await asyncio.sleep(.3)
       state=await frame.evaluate('captureState()');value=await fixture();assert not state['full'] and not any(x[1]==16777216 for x in value['events']),(state,value)
       results.append({'mode':mode,'case':'BASELINE_ESC_EXITS','browser':state,'guest':value});print(mode,'BASELINE_ESC_EXITS',json.dumps(state),flush=True)
      await fixture(True);await f.locator('#capture').click();await frame.wait_for_function("captureState().status.includes('capture active')");await asyncio.sleep(.4)
      for key in ['Escape','ctrl+w','ctrl+t','ctrl+l','ctrl+Tab','F5','ctrl+r','ctrl+f']:native(key);await asyncio.sleep(.12)
      await asyncio.sleep(.2);state=await frame.evaluate('captureState()');value=await fixture();keys={x[1] for x in value['events'] if x[0]=='press'}
      expected={16777216,87,84,76,16777217,16777268,82,70};assert state['full'] and expected<=keys,(state,value,expected-keys)
      results.append({'mode':mode,'case':'NATIVE_ESC_AND_BROWSER_CHORDS','browser':state,'guest':value,'user_agent':await frame.evaluate('navigator.userAgent')});print(mode,'NATIVE_ESC_AND_BROWSER_CHORDS',json.dumps(state),json.dumps(value),flush=True)
      await fixture(True);before=await frame.evaluate('captureState()')
      subprocess.run(['xdotool','keydown','Control_L'],env=env,check=True);await asyncio.sleep(.1)
      await frame.evaluate('window.dispatchEvent(new Event("blur"))');await asyncio.sleep(.1)
      paused=await frame.evaluate('captureState()');assert paused['full'] and 'paused' in paused['status'],paused
      await frame.evaluate('window.dispatchEvent(new Event("focus"))');await asyncio.sleep(.1)
      resumed=await frame.evaluate('captureState()');assert resumed['full'] and resumed['requests']==before['requests'] and 'capture active' in resumed['status'],resumed
      subprocess.run(['xdotool','keyup','Control_L'],env=env,check=True);native('a');await asyncio.sleep(.15)
      value=await fixture();ctrl=[x for x in value['events'] if x[1]==16777249];letter=[x for x in value['events'] if x[0]=='press' and x[1]==65]
      assert [x[0] for x in ctrl]==['press','release'] and len(letter)==1 and letter[0][2]==0,value
      results.append({'mode':mode,'case':'BLUR_RESUME_PRESERVES_FULLSCREEN_AND_RELEASES_MODIFIER','before':before,'paused':paused,'resumed':resumed,'guest':value});print(mode,'BLUR_RESUME_PASS',json.dumps(value),flush=True)
      await f.locator('#settings').click();await frame.wait_for_function('!document.fullscreenElement');await asyncio.sleep(.1);state=await frame.evaluate('captureState()');assert state['active']=='settings',state
      results.append({'mode':mode,'case':'SETTINGS_RELEASE','browser':state});print(mode,'SETTINGS_RELEASE',json.dumps(state),flush=True)
      await f.locator('#capture-settings').click();await frame.wait_for_function("captureState().status.includes('capture active')");await f.locator('#capture').click();await frame.wait_for_function('!document.fullscreenElement');results.append({'mode':mode,'case':'MOUSE_RELEASE_BUTTON','browser':await frame.evaluate('captureState()')})
      await f.locator('#capture').click();await frame.wait_for_function("captureState().status.includes('capture active')");native('ctrl+alt+shift+Escape');await frame.wait_for_function('!document.fullscreenElement');results.append({'mode':mode,'case':'LOCAL_EXIT_CHORD','browser':await frame.evaluate('captureState()')})
      await f.locator('#capture').click();await frame.wait_for_function("captureState().status.includes('capture active')")
      subprocess.run(['xdotool','keydown','Escape'],env=env,check=True);await asyncio.sleep(2.5);subprocess.run(['xdotool','keyup','Escape'],env=env,check=True);await frame.wait_for_function('!document.fullscreenElement');results.append({'mode':mode,'case':'LONG_ESCAPE_EXIT','browser':await frame.evaluate('captureState()')});print(mode,'LONG_ESCAPE_EXIT',flush=True)
     finally:
      if not page.is_closed():
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
  (ROOT/'real-capture-result.json').write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
