import asyncio,json,os,subprocess,sys,hashlib
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from threading import Thread
from urllib.parse import parse_qs
from playwright.async_api import async_playwright
baseline='--baseline' in sys.argv
root=Path('/tmp/qsm-modifier-chords-20261025')/('baseline' if baseline else 'fixed');root.mkdir(exist_ok=True)
source=subprocess.check_output(['git','show','v0.4.0-qsm-direct-20261025-stock4.11:qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js'],text=True) if baseline else Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
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
 env={**os.environ,'DISPLAY':':223'}
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
      names=['Ctrl','Alt','Shift','Meta'];scans={'Ctrl':29,'Alt':56,'Shift':42,'Meta':219};flags={'Ctrl':'ctrlKey','Alt':'altKey','Shift':'shiftKey','Meta':'metaKey'};codes={'Ctrl':'ControlLeft','Alt':'MetaLeft','Shift':'ShiftLeft','Meta':'AltLeft'};keys={'Ctrl':'Control','Alt':'Alt','Shift':'Shift','Meta':'Meta'}
      def events_for(chord):
       active=set();events=[]
       for name in chord:
        active.add(name);events.append(key('keydown',codes[name],keys[name],**{flags[n]:n in active for n in names}))
       for name in reversed(chord):
        active.remove(name);events.append(key('keyup',codes[name],keys[name],**{flags[n]:n in active for n in names}))
       return events
      for physical in [False,True]:
       await page.evaluate('p=>window.__settings.keyInputMode=p?"guest-repeat":"client-repeat"',physical)
       for shortcut in ['Alt+Shift','Ctrl+Shift','Shift+Meta','Ctrl+Alt+Shift+Meta']:
        chord=shortcut.split('+');events=events_for(chord)
        if not baseline or shortcut!='Ctrl+Alt+Shift+Meta':await inject('DEFAULT_HOST_'+shortcut+'_'+str(physical),events,[])
        # Remove only the exact modifier-only rule, leaving every longer
        # host shortcut intact. This is the old prefix-shadowing regression.
        await page.evaluate('s=>{window.__savedRules=window.__settings.hostShortcuts;window.__settings.hostShortcuts=(window.__settings.hostShortcuts||[]).filter(r=>r.shortcut!==s)}',shortcut)
        if baseline:
         # stock4.11 has no editor rule at all; all three named pairs are
         # still swallowed as prefixes of longer default shortcuts.
         if shortcut!='Ctrl+Alt+Shift+Meta':await inject('REMOVED_STILL_BLOCKED_'+shortcut+'_'+str(physical),events,[])
        else:
         want=[{'op':'key_tap','key':scans[chord[-1]],'modifiers':[scans[n] for n in chord[:-1]]}]
         await inject('REMOVED_GOES_TO_GUEST_'+shortcut+'_'+str(physical),events,want)
         await page.evaluate('s=>window.__settings.hostShortcuts.push({shortcut:s,action:"guest"})',shortcut)
         await inject('EXPLICIT_GUEST_'+shortcut+'_'+str(physical),events,want)
        await page.evaluate('window.__settings.hostShortcuts=window.__savedRules')
      await page.evaluate('window.__settings.keyInputMode="client-repeat"')
      down=key('keydown','AltLeft','Alt',altKey=True);up=key('keyup','AltLeft','Alt',altKey=False)
      await inject('BARE_ALT_NO_TAB',[down,up],[])
      await inject('FULL_ALT_TAB',[down,key('keydown','Tab','Tab',altKey=True),key('keyup','Tab','Tab',altKey=True),up],[])
      if not baseline:
       # Native toolbar auto-hide is expected after the input loop. Reveal
       # it through the actual handle before using its Settings button.
       handle=frame.locator('button[aria-controls="qsm-direct-toolbar"]')
       if await handle.get_attribute('aria-expanded')=='false':await handle.click()
       # Change the real editor instead of mutating test state, then route
       # its keyboard combination through the installed bridge/worker.
       await frame.get_by_role('button',name='Console settings',exact=True).click()
       panel=frame.get_by_role('complementary',name='Console settings',exact=True)
       editor=panel.locator('section[aria-label="Host shortcuts"]')
       row=editor.locator('li').filter(has=frame.get_by_text('Ctrl+Shift — Host / browser',exact=True))
       await row.get_by_role('button',name='Edit',exact=True).click()
       await editor.get_by_role('combobox',name='Shortcut action',exact=True).select_option('guest')
       await editor.get_by_role('button',name='Save shortcut',exact=True).click()
       await panel.get_by_role('button',name='Close',exact=True).click()
       await inject('ACTUAL_EDITOR_GUEST_CTRL_SHIFT',events_for(['Ctrl','Shift']),[{'op':'key_tap','key':42,'modifiers':[29]}])
       await frame.get_by_role('button',name='Console settings',exact=True).click()
       row=editor.locator('li').filter(has=frame.get_by_text('Alt+Shift — Host / browser',exact=True))
       await rpc('clear_inputs');await row.get_by_role('button',name='Send to guest',exact=True).click();await asyncio.sleep(.15)
       wire=(await rpc('stats'))['input_trace'];assert wire==[{'op':'key_tap','key':42,'modifiers':[56]}],wire
       results.append({'mode':mode,'case':'MODIFIER_RULE_SEND_BYPASS','wire':wire})
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
