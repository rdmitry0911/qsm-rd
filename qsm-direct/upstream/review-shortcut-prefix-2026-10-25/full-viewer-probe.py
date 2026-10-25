import asyncio,json,os,subprocess,sys,hashlib
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from threading import Thread
from urllib.parse import parse_qs
from playwright.async_api import async_playwright
baseline='--baseline' in sys.argv
root=Path('/tmp/qsm-shortcut-prefix-20261025')/('baseline' if baseline else 'fixed');root.mkdir(exist_ok=True)
source=subprocess.check_output(['git','show','v0.4.0-qsm-direct-20261025-stock4.10:qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js'],text=True) if baseline else Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
source_hash=hashlib.sha256(source.encode()).hexdigest()
# Observe production state; do not replace handlers, negotiate input policy or
# inject input capabilities. SDP transport/auth are isolated lab substitutes.
source=source.replace('const settings = readConsoleSettings(browserStorage);','const settings = readConsoleSettings(browserStorage); window.__settings = settings;')
source=source.replace('const acceptInputPolicy = (message) => {','const acceptInputPolicy = (message) => { window.__policy = message;')
source=source.replace("control = peer.createDataChannel('qsm-control', { ordered: true });","control = peer.createDataChannel('qsm-control', { ordered: true }); window.__control = control;")
source=source.replace("    if (VIEWER) {\n        Ext.onReady", "    window.__qsmTestOpenConsole = openConsole;\n    if (VIEWER) {\n        Ext.onReady")
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/javascript; charset=utf-8' if self.path=='/viewer.js' else 'text/html; charset=utf-8');self.end_headers()
  if self.path=='/viewer.js':self.wfile.write(source.encode());return
  html='''<body><script>window.gettext=s=>s;window.Ext={define(){},onReady(fn){fn();},util:{Cookies:{get(){return 'test'}}},Msg:{alert(...a){console.log(...a)}}};window.Proxmox={UserName:'lab@pam',Utils:{API2Request(o){o.success({result:{data:{status:'running'}}})}}};</script>'''
  if self.path=='/embedded':
   html+='''<iframe id="console" allow="autoplay; fullscreen" allowfullscreen style="width:96vw;height:90vh"></iframe><script>window.QSM_DIRECT_ENDPOINT={public_url:location.origin};</script><script src="/viewer.js"></script><script>window.__qsmTestOpenConsole(null,'lab',108,document.querySelector('#console'),'lxc');</script>'''
  else:html+='''<script>window.QSM_WEBRTC_VIEWER={node:'lab',vmid:108,csrf:'test'};</script><script src="/viewer.js"></script>'''
  self.wfile.write(html.encode())
 def log_message(self,*a):pass
async def main():
 web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start();results=[]
 env={**os.environ,'DISPLAY':':221'}
 xvfb=subprocess.Popen(['Xvfb',env['DISPLAY'],'-screen','0','1280x900x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);wm=None
 try:
  await asyncio.sleep(.3);assert xvfb.poll() is None;wm=subprocess.Popen(['openbox','--sm-disable'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);await asyncio.sleep(.3)
  async with async_playwright() as p:
   b=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=False,env={**env,'MOZ_GMP_PATH':'/tmp/qsm-rate-bands-20261023/gmp-gmpopenh264/2.6.0'},firefox_user_prefs={'media.autoplay.default':0,'media.gmp-gmpopenh264.enabled':True,'media.gmp-gmpopenh264.version':'2.6.0','media.gmp-gmpopenh264.autoupdate':False})
   try:
    for mode in ['top','embedded']:
     log=(root/f'{mode}-bridge.log').open('wb');server=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@192.168.64.20','python3 /root/qsm-shortcut-rules25/isolated-input-bridge.py new',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=log)
     async def rpc(op,**kw):
      server.stdin.write((json.dumps({'op':op,**kw})+'\n').encode());await server.stdin.drain();line=await asyncio.wait_for(server.stdout.readline(),20);assert line,log.name;return json.loads(line)
     ctx=await b.new_context();page=await ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
     try:
      async def offer(route):
       data=parse_qs(route.request.post_data) if route.request.url.endswith('webrtcproxy') else json.loads(route.request.post_data)
       sdp=data['sdp'][0] if isinstance(data['sdp'],list) else data['sdp'];lines=sdp.split('\r\n')
       for i,line in enumerate(lines):
        if line.startswith('a=candidate:'):
         fields=line.split()
         if fields[4].endswith('.local'):fields[4]='192.168.64.25';lines[i]=' '.join(fields)
       answer=await rpc('offer',sdp='\r\n'.join(lines));await route.fulfill(json={'data':answer})
      await page.route('**/webrtcproxy',offer);await page.route('**/nodes/lab/lxc/108/qsm-direct',offer)
      await page.route('**/qsm-direct-settings',lambda route:route.fulfill(json={'data':{'codec':'h264','encoder':'h264_nvenc','maximum_kbps':8000}}))
      await page.add_init_script("Object.defineProperty(navigator,'platform',{get:()=> 'MacIntel'});")
      await page.goto(f'http://127.0.0.1:{web.server_port}/'+mode)
      frame=page.frames[-1] if mode=='embedded' else page.main_frame
      await page.wait_for_function("window.__policy && window.__control?.readyState==='open'",timeout=25000)
      await frame.wait_for_function("document.querySelector('video').videoWidth>0",timeout=25000)
      policy=await page.evaluate('window.__policy');assert policy['key_taps'] is True,policy
      v=frame.locator('video');await v.focus()
      async def inject(case,events,want):
       await rpc('clear_inputs');await frame.evaluate("events=>{for(const e of events)document.querySelector('video').dispatchEvent(new KeyboardEvent(e.type,{bubbles:true,cancelable:true,...e}));}",events);await asyncio.sleep(.15)
       wire=(await rpc('stats'))['input_trace'];assert wire==want,(mode,case,wire,want)
       results.append({'mode':mode,'case':case,'wire':wire,'hasFocus_after_events':await frame.evaluate('document.hasFocus()'),'DOM_Tab':any(e['code']=='Tab' for e in events),'blur_injected':False});print(mode,case,'PASS',flush=True)
      def key(t,code,k,**kw):return {'type':t,'code':code,'key':k,**kw}
      for physical in [False,True]:
       await page.evaluate('p=>window.__settings.keyInputMode=p?"guest-repeat":"client-repeat"',physical)
       for code,k,flag,scan in [('AltLeft','Alt','altKey',56),('AltRight','Alt','altKey',184),('AltLeft','Meta','metaKey',219),('MetaLeft','Alt','altKey',56)]:
        await inject(f'NO_TAB_KEYUP_BEFORE_BLUR_{code}_{k}_{physical}',[key('keydown',code,k,**{flag:True}),key('keyup',code,k,**{flag:False})],[{'op':'key_tap','key':scan,'modifiers':[]}] if baseline else [])
      await page.evaluate('window.__settings.keyInputMode="client-repeat"')
      down=key('keydown','AltLeft','Alt',altKey=True);up=key('keyup','AltLeft','Alt',altKey=False)
      await inject('FULL_ALT_TAB',[down,key('keydown','Tab','Tab',altKey=True),key('keyup','Tab','Tab',altKey=True),up],[])
      await inject('GUEST_ALT_F',[down,key('keydown','KeyF','f',altKey=True),key('keyup','KeyF','f',altKey=True),up],[{'op':'key_tap','key':33,'modifiers':[56]}])
      if not baseline:
       await page.evaluate('window.__settings.hostShortcuts=[]')
       await inject('EMPTY_RULES_ALLOWS_SOLO_ALT',[down,up],[{'op':'key_tap','key':56,'modifiers':[]}])
       await page.evaluate('window.__settings.hostShortcuts=[{shortcut:"Alt+Tab",action:"host"}]')
       await frame.get_by_role('button',name='Console settings',exact=True).click()
       panel=frame.get_by_role('complementary',name='Console settings',exact=True)
       for label,scan,mods in [('Alt',56,[]),('Alt+Shift',56,[42]),('Super / Windows',219,[])]:
        if not await panel.is_visible():await frame.get_by_role('button',name='Console settings',exact=True).click()
        await rpc('clear_inputs');await panel.get_by_role('button',name=label,exact=True).click();await asyncio.sleep(.15)
        wire=(await rpc('stats'))['input_trace'];assert wire==[{'op':'key_tap','key':scan,'modifiers':mods}],wire
        results.append({'mode':mode,'case':'EXPLICIT_BUTTON_'+label,'wire':wire})
       await page.evaluate('window.__settings.keyboardPriority="raw-events"')
       await inject('RAW_SOLO_ALT',[down,up],[{'op':'keyboard','key':56,'down':True},{'op':'keyboard','key':56,'down':False}])
      assert not errors,errors
      results.append({'mode':mode,'complete_production_JS':True,'source_sha256':source_hash,'forced_policy':False,'negotiated_policy':policy,'page_errors':errors})
     finally:
      await page.close();await ctx.close()
      if server.returncode is None:
       try:await rpc('quit')
       except Exception:pass
       await server.wait()
      log.close()
   finally:await b.close()
 finally:
  web.shutdown()
  if wm:wm.terminate();wm.wait(timeout=3)
  xvfb.terminate();xvfb.wait(timeout=3)
  (root/'result.json').write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
