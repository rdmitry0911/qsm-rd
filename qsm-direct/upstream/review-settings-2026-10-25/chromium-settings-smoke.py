import asyncio,json,os,re
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from threading import Thread
from playwright.async_api import async_playwright
root=Path('/tmp/qsm-settings-20261025');source=Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
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
 web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start();results=[]
 try:
  async with async_playwright() as p:
   b=await p.chromium.launch(executable_path='/home/dima/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome',headless=True,args=['--no-sandbox'])
   try:
    for mode in ['top','embedded']:
     page=await b.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
     cdp=await page.context.new_cdp_session(page)
     await cdp.send('Browser.grantPermissions',{'origin':f'http://127.0.0.1:{web.server_port}','permissions':['keyboardLock']})
     await page.add_init_script("Object.defineProperty(navigator,'platform',{get:()=> 'MacIntel'});")
     await page.goto(f'http://127.0.0.1:{web.server_port}/'+mode)
     frame=page.frames[-1] if mode=='embedded' else page.main_frame
     toolbar=frame.get_by_label('Console controls',exact=True)
     capture=toolbar.get_by_role('button',name='Capture keyboard (full screen)',exact=True)
     await capture.wait_for();assert await capture.inner_text()=='Capture keyboard'
     await frame.wait_for_function("""document.querySelector('button[title="Show console controls"]').getBoundingClientRect().left>30""")
     overlap=await frame.evaluate('''()=>{const h=document.querySelector('button[title="Show console controls"]').getBoundingClientRect();return [...document.querySelector('[aria-label="Console controls"]').querySelectorAll('button')].some(b=>{const r=b.getBoundingClientRect();return Math.min(h.right,r.right)>Math.max(h.left,r.left)&&Math.min(h.bottom,r.bottom)>Math.max(h.top,r.top);});}''')
     assert not overlap,'reveal handle overlaps a console control'
     await frame.get_by_role('button',name='Console settings',exact=True).click()
     panel=frame.get_by_role('complementary',name='Console settings',exact=True)
     panel_capture=panel.get_by_role('button',name='Capture keyboard (full screen)',exact=True)
     box=await panel_capture.bounding_box();bounds=await panel.bounding_box()
     assert box and bounds and box['y']>=bounds['y'] and box['y']+box['height']<bounds['y']+bounds['height'],(box,bounds)
     assert await panel.evaluate('(p)=>p.scrollTop')==0
     await panel_capture.click();await frame.wait_for_function('!!document.fullscreenElement')
     await frame.get_by_text(re.compile('Keyboard capture active')).wait_for(state='attached')
     await frame.get_by_role('button',name='Console settings',exact=True).click()
     assert await frame.evaluate('!!document.fullscreenElement'),'Settings exited fullscreen'
     await panel.get_by_text(re.compile('Settings is open')).wait_for(state='visible')
     assert await panel.evaluate('(p)=>p.scrollTop')==0
     await panel.locator('#qsm-direct-setting-toolbarHotZonePx').fill('14')
     await panel.locator('#qsm-direct-setting-toolbarHotZonePx').press('Tab')
     assert await frame.evaluate('!!document.fullscreenElement'),'editing Settings exited fullscreen'
     await panel.get_by_role('button',name='Close',exact=True).click()
     await frame.get_by_text(re.compile('Keyboard capture active')).wait_for(state='attached')
     assert await frame.evaluate('document.activeElement.tagName')=='VIDEO'
     assert await frame.evaluate('!!document.fullscreenElement'),'closing Settings exited fullscreen'
     await frame.get_by_role('button',name='Console settings',exact=True).click()
     assert await panel.evaluate('(p)=>p.scrollTop')==0,'reopening must expose capture at the top'
     await page.screenshot(path=str(root/f"chromium-{mode}-settings.png"))
     await panel.get_by_role('button',name='Close',exact=True).click()
     await frame.evaluate('window.dispatchEvent(new Event("blur"))')
     assert await frame.evaluate('!!document.fullscreenElement'),'blur exited fullscreen'
     await frame.get_by_text('Keyboard input paused while the console is unfocused.',exact=False).wait_for(state='attached')
     await frame.evaluate('window.dispatchEvent(new Event("focus"))')
     await frame.get_by_text(re.compile('Keyboard capture active')).wait_for(state='attached')
     await toolbar.get_by_role('button',name='Release keyboard and exit full screen',exact=True).click()
     await frame.wait_for_function('!document.fullscreenElement')
     assert not errors,errors
     results.append({'mode':mode,'complete_production_JS':True,'text_capture_button':True,'reveal_handle_does_not_overlap_controls':True,'settings_capture_visible_without_scrolling':True,'capture_from_settings':True,'settings_preserves_fullscreen':True,'settings_edit_preserves_fullscreen':True,'close_settings_refocuses_guest_and_resumes':True,'blur_focus_preserves_capture':True,'explicit_release_exits':True,'page_errors':errors});print(mode,'FULL_VIEWER_SETTINGS_PASS',flush=True)
     await page.close()
   finally:await b.close()
 finally:web.shutdown();(root/'chromium-settings-result.json').write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
