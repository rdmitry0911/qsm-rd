import asyncio,json,os,re,sys,subprocess
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from threading import Thread
from playwright.async_api import async_playwright
root=Path('/tmp/qsm-controls-20261025');source=Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
source=source.replace('    if (VIEWER) {\n        Ext.onReady', '    window.__qsmTestOpenConsole=openConsole;\n    if (VIEWER) {\n        Ext.onReady')
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/javascript; charset=utf-8' if self.path=='/viewer.js' else 'text/html; charset=utf-8');self.end_headers()
  if self.path=='/viewer.js':self.wfile.write(source.encode());return
  mode=self.path=='/embedded'
  html='''<body><script>window.gettext=s=>s;window.Ext={define(){},onReady(fn){fn();}};window.Proxmox={Utils:{API2Request(){}}};</script>'''
  if not mode:html+='<script>window.QSM_WEBRTC_VIEWER={node:"lab",vmid:108,csrf:"test"};</script>'
  if mode:html+='<iframe id="console" allow="autoplay; fullscreen" allowfullscreen style="width:95vw;height:90vh"></iframe>'
  html+='<script src="/viewer.js"></script>'
  if mode:html+='''<script>window.__qsmTestOpenConsole(null,'lab',108,document.querySelector('#console'),'lxc');</script>'''
  self.wfile.write(html.encode())
 def log_message(self,*args):pass
async def main():
 browser_name=sys.argv[1];native='--native' in sys.argv
 web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start();results=[];xvfb=None;wm=None
 try:
  env=dict(os.environ)
  if native:
   env['DISPLAY']=':221';xvfb=subprocess.Popen(['Xvfb',env['DISPLAY'],'-screen','0','1280x900x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);await asyncio.sleep(.3);assert xvfb.poll() is None
   wm=subprocess.Popen(['openbox','--sm-disable'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);await asyncio.sleep(.3)
  async with async_playwright() as p:
   if browser_name=='firefox':
    b=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=not native,env=env,firefox_user_prefs={'focusmanager.testmode':False,'full-screen-api.ignore-widgets':False})
   else:
    b=await p.chromium.launch(executable_path='/home/dima/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome',headless=True,args=['--no-sandbox'])
   try:
    for mode in ['top','embedded']:
     for width,height in ([(1280,800)] if native else [(1280,800),(640,480)]):
      page=await b.new_page(viewport={'width':width,'height':height});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
      if browser_name=='chromium':
       cdp=await page.context.new_cdp_session(page);await cdp.send('Browser.grantPermissions',{'origin':f'http://127.0.0.1:{web.server_port}','permissions':['keyboardLock']})
      await page.add_init_script("Object.defineProperty(navigator,'platform',{get:()=> 'MacIntel'});")
      await page.goto(f'http://127.0.0.1:{web.server_port}/'+mode)
      frame=page.frames[-1] if mode=='embedded' else page.main_frame
      toolbar=frame.locator('#qsm-direct-toolbar');handle=frame.locator('button[aria-controls="qsm-direct-toolbar"]')
      capture=toolbar.get_by_role('button',name='Capture keyboard (full screen)',exact=True)
      await capture.wait_for();assert await capture.inner_text()=='Capture keyboard'
      await frame.evaluate("""()=>{
       window.__exits=0;window.__entries=0;window.__esc=0;
       const exit=document.exitFullscreen.bind(document);document.exitFullscreen=(...args)=>{window.__exits++;return exit(...args);};
       const enter=document.documentElement.requestFullscreen.bind(document.documentElement);document.documentElement.requestFullscreen=(...args)=>{window.__entries++;return enter(...args);};
       document.addEventListener('keydown',e=>{if(e.code==='Escape'){window.__esc++;e.preventDefault();}},true);
       const plain=document.createElement('button');plain.id='plain-full';plain.textContent='Test ordinary fullscreen';plain.style='position:fixed;right:0;bottom:0;z-index:90';plain.onclick=()=>document.documentElement.requestFullscreen({keyboardLock:'none'});document.body.append(plain);
      }""")
      for expected in [False,True,False,True]:
       await handle.click();assert await handle.get_attribute('aria-expanded')==str(expected).lower()
       assert await handle.evaluate('(e)=>e.getBoundingClientRect().left')==0,'handle moved away from left edge'
       assert await toolbar.evaluate('(e)=>e.style.opacity')==('1' if expected else '0')
       await frame.wait_for_function("Math.abs(document.querySelector('#qsm-direct-toolbar').getBoundingClientRect().left-32)<.1" if expected else "document.querySelector('#qsm-direct-toolbar').getBoundingClientRect().right<.1")
      overlap=await frame.evaluate("""()=>{const h=document.querySelector('button[aria-controls="qsm-direct-toolbar"]').getBoundingClientRect();return [...document.querySelector('#qsm-direct-toolbar').querySelectorAll('button')].some(b=>{const r=b.getBoundingClientRect();return Math.min(h.right,r.right)>Math.max(h.left,r.left)&&Math.min(h.bottom,r.bottom)>Math.max(h.top,r.top);});}""")
      assert not overlap,'fixed handle overlaps a console control'
      if native:
       await frame.locator('#plain-full').click();await frame.wait_for_function('!!document.fullscreenElement')
       subprocess.run(['xdotool','key','--clearmodifiers','Escape'],env=env,check=True);await frame.wait_for_function('!document.fullscreenElement')
      await frame.locator('#plain-full').click();await frame.wait_for_function('!!document.fullscreenElement')
      exits=await frame.evaluate('__exits');await capture.click();await frame.get_by_text(re.compile('Keyboard capture active')).wait_for(state='attached')
      assert await frame.evaluate('!!document.fullscreenElement');assert await frame.evaluate('__exits')==exits,'Capture exited ordinary fullscreen'
      if native:
       before=await frame.evaluate('__esc');subprocess.run(['xdotool','key','--clearmodifiers','Escape'],env=env,check=True);await asyncio.sleep(.2)
       assert await frame.evaluate('!!document.fullscreenElement'),'native Esc escaped upgraded capture';assert await frame.evaluate('__esc')>before,'native Esc not delivered after upgrade'
      entries=await frame.evaluate('__entries')
      for _ in range(3):await capture.click()
      assert await frame.evaluate('!!document.fullscreenElement');assert await frame.evaluate('__exits')==exits,'repeated Capture exited fullscreen'
      assert await frame.evaluate('__entries')==entries,'repeated Capture requested fullscreen again'
      await frame.get_by_role('button',name='Console settings',exact=True).click()
      panel=frame.get_by_role('complementary',name='Console settings',exact=True)
      panel_capture=panel.get_by_role('button',name='Capture keyboard (full screen)',exact=True)
      box=await panel_capture.bounding_box();bounds=await panel.bounding_box();assert box and bounds and box['y']>=bounds['y'] and box['y']+box['height']<bounds['y']+bounds['height']
      await panel_capture.click();await panel.wait_for(state='hidden')
      assert await frame.evaluate('!!document.fullscreenElement');assert await frame.evaluate('__exits')==exits,'Capture from active Settings exited fullscreen'
      assert await frame.evaluate('__entries')==entries;assert await frame.evaluate('document.activeElement.tagName')=='VIDEO'
      await frame.get_by_role('button',name='Console settings',exact=True).click()
      await panel.locator('#qsm-direct-setting-toolbarHotZonePx').fill('14');await panel.locator('#qsm-direct-setting-toolbarHotZonePx').press('Tab')
      await panel.get_by_role('button',name='Close',exact=True).click();assert await frame.evaluate('!!document.fullscreenElement')
      await page.screenshot(path=str(root/f'{browser_name}-{mode}-{width}-captured.png'))
      await toolbar.get_by_role('button',name='Exit Full Screen',exact=True).click();await frame.wait_for_function('!document.fullscreenElement')
      await capture.click();await frame.wait_for_function('!!document.fullscreenElement')
      await frame.get_by_role('button',name='Console settings',exact=True).click()
      await panel.get_by_role('button',name='Release keyboard and exit full screen',exact=True).click();await frame.wait_for_function('!document.fullscreenElement')
      if mode=='embedded':
       await panel.get_by_role('button',name='Close',exact=True).click()
       await toolbar.get_by_role('button',name='Enter Full Screen',exact=True).click()
       assert not await frame.evaluate('!!document.fullscreenElement'),'in-page maximize changed native fullscreen'
       assert await page.locator('#console').evaluate('(e)=>e.style.position')=='fixed'
       await capture.click();await frame.wait_for_function('!!document.fullscreenElement')
       exits=await frame.evaluate('__exits');await capture.click();assert await frame.evaluate('__exits')==exits
       await toolbar.get_by_role('button',name='Exit Full Screen',exact=True).click();await frame.wait_for_function('!document.fullscreenElement')
       assert await page.locator('#console').evaluate('(e)=>e.style.position')=='fixed','native exit lost in-page maximize'
       await toolbar.get_by_role('button',name='Exit Full Screen',exact=True).click()
       assert await page.locator('#console').evaluate('(e)=>e.style.position')!='fixed'
      assert not errors,errors
      results.append({'mode':mode,'viewport':[width,height],'complete_production_JS':True,'fixed_left_handle_repeated_toggle':True,'no_handle_control_overlap':True,'capture_upgrades_ordinary_fullscreen_without_exit':True,'repeated_capture_keeps_fullscreen_without_new_request':True,'capture_from_active_settings_resumes_without_exit':True,'settings_edit_preserves_fullscreen':True,'explicit_fullscreen_exit':True,'distinct_release_exits':True,'in_page_maximize_capture_roundtrip':mode=='embedded','native_XTEST_Esc_after_upgrade':native,'page_errors':errors});print(browser_name,mode,width,'CONTROLS_PASS',flush=True)
      await page.close()
   finally:await b.close()
 finally:
  web.shutdown()
  if wm:wm.terminate();wm.wait(timeout=3)
  if xvfb:xvfb.terminate();xvfb.wait(timeout=3)
  (root/f"{browser_name}-{'native-' if native else ''}result.json").write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
