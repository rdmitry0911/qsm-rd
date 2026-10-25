import asyncio,json,os,re,sys,subprocess
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from threading import Thread
from playwright.async_api import async_playwright
root=Path('/tmp/qsm-modifier-chords-20261025');source=Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
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
 browser_name=sys.argv[1];web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start();results=[]
 try:
  async with async_playwright() as p:
   if browser_name=='firefox':b=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=True)
   else:b=await p.chromium.launch(executable_path='/home/dima/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome',headless=True,args=['--no-sandbox'])
   try:
    for mode in ['top','embedded']:
     page=await b.new_page(viewport={'width':1100,'height':800});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
     await page.add_init_script("Object.defineProperty(navigator,'platform',{get:()=> 'MacIntel'});")
     if browser_name=='chromium':
      cdp=await page.context.new_cdp_session(page);await cdp.send('Browser.grantPermissions',{'origin':f'http://127.0.0.1:{web.server_port}','permissions':['keyboardLock']})
     async def locate():
      await page.goto(f'http://127.0.0.1:{web.server_port}/'+mode)
      frame=page.frames[-1] if mode=='embedded' else page.main_frame
      await frame.get_by_role('button',name='Console settings',exact=True).click()
      return frame,frame.get_by_role('complementary',name='Console settings',exact=True)
     frame,panel=await locate();editor=panel.locator('section[aria-label="Host shortcuts"]');rows=editor.locator('li')
     assert await rows.count()==19
     def row(text):return rows.filter(has=frame.get_by_text(text,exact=True))
     assert await row('Alt+Tab — Host / browser').count()==1
     field=editor.get_by_role('textbox',name='Shortcut combination',exact=True);action=editor.get_by_role('combobox',name='Shortcut action',exact=True)
     await field.fill('Ctrl+Shift+F');await action.select_option('toolbar');await editor.get_by_role('button',name='Add shortcut',exact=True).click()
     assert await row('Ctrl+Shift+F — Show / hide controls').count()==1
     await field.fill('Control+Shift+f');await editor.get_by_role('button',name='Add shortcut',exact=True).click()
     await editor.get_by_role('alert').filter(has_text='already has a rule').wait_for(state='visible');assert await rows.count()==20
     await row('Ctrl+Shift+F — Show / hide controls').get_by_role('button',name='Edit',exact=True).click()
     await field.fill('Cmd+Shift+F');await action.select_option('fullscreen');await editor.get_by_role('button',name='Save shortcut',exact=True).click()
     assert await row('Shift+Meta+F — Toggle full screen').count()==1
     await editor.get_by_role('button',name='Record shortcut',exact=True).click()
     await field.dispatch_event('keydown',{'key':'y','code':'KeyY','ctrlKey':True,'altKey':True,'bubbles':True,'cancelable':True})
     assert await field.input_value()=='Ctrl+Alt+KeyY';await action.select_option('host');await editor.get_by_role('button',name='Add shortcut',exact=True).click()
     assert await row('Ctrl+Alt+Y — Host / browser').count()==1
     await row('Alt+Tab — Host / browser').get_by_role('button',name='Remove',exact=True).click();assert await rows.count()==20
     frame,panel=await locate();editor=panel.locator('section[aria-label="Host shortcuts"]');rows=editor.locator('li')
     assert await rows.count()==20 and await row('Alt+Tab — Host / browser').count()==0,'edits did not survive reopen'
     await panel.locator('#qsm-direct-keyboard-priority').select_option('client-first');assert await rows.count()>60
     assert await row('Ctrl+Alt+Y — Host / browser').count()==0,'Guest first and Client first lists must be independent'
     await panel.locator('#qsm-direct-keyboard-priority').select_option('raw-events');assert await editor.get_by_role('textbox').is_disabled()
     await panel.locator('#qsm-direct-keyboard-priority').select_option('guest-first');assert await row('Ctrl+Alt+Y — Host / browser').count()==1
     await editor.get_by_role('button',name='Reset shortcut defaults',exact=True).click();assert await rows.count()==19
     await panel.get_by_role('button',name='Capture keyboard (full screen)',exact=True).click();await frame.wait_for_function('!!document.fullscreenElement')
     await frame.get_by_role('button',name='Console settings',exact=True).click();assert await frame.evaluate('!!document.fullscreenElement')
     await editor.get_by_role('textbox',name='Shortcut combination',exact=True).fill('Ctrl+Shift+X');await editor.get_by_role('button',name='Add shortcut',exact=True).click();assert await frame.evaluate('!!document.fullscreenElement'),'editing shortcuts exited fullscreen'
     # Modifier-only rules support aliases, Guest overrides, recording and
     # persistent removal through the actual production editor.
     field=editor.get_by_role('textbox',name='Shortcut combination',exact=True);action=editor.get_by_role('combobox',name='Shortcut action',exact=True)
     await row('Alt+Shift — Host / browser').get_by_role('button',name='Edit',exact=True).click()
     await action.select_option('guest');await editor.get_by_role('button',name='Save shortcut',exact=True).click()
     assert await row('Alt+Shift — Guest').count()==1
     await row('Ctrl+Shift — Host / browser').get_by_role('button',name='Remove',exact=True).click()
     await editor.get_by_role('button',name='Record shortcut',exact=True).click()
     await field.dispatch_event('keydown',{'key':'Meta','code':'AltLeft','metaKey':True,'bubbles':True,'cancelable':True})
     assert await editor.get_by_role('button',name='Press a shortcut…',exact=True).count()==1
     await field.dispatch_event('keydown',{'key':'Shift','code':'ShiftRight','metaKey':True,'shiftKey':True,'bubbles':True,'cancelable':True})
     await field.dispatch_event('keyup',{'key':'Shift','code':'ShiftRight','metaKey':True,'bubbles':True,'cancelable':True})
     assert await field.input_value()=='Shift+Meta'
     assert await editor.get_by_role('button',name='Record shortcut',exact=True).count()==1
     await page.screenshot(path=str(root/f'{browser_name}-{mode}-shortcuts.png'))
     await panel.get_by_role('button',name='Close',exact=True).click();assert await frame.evaluate('document.activeElement.tagName')=='VIDEO'
     await frame.get_by_label('Console controls',exact=True).get_by_role('button',name='Exit Full Screen',exact=True).click();await frame.wait_for_function('!document.fullscreenElement')
     frame,panel=await locate();editor=panel.locator('section[aria-label="Host shortcuts"]');rows=editor.locator('li')
     assert await row('Alt+Shift — Guest').count()==1
     assert await row('Ctrl+Shift — Host / browser').count()==0,'deleted modifier rule was restored by migration'
     assert not errors,errors
     results.append({'mode':mode,'complete_production_JS':True,'default_host_rules':19,'add_edit_remove_record':True,'duplicate_validation':True,'persistent_reopen':True,'separate_priority_lists':True,'raw_bypasses_and_disables_editor':True,'reset_defaults':True,'editing_preserves_fullscreen':True,'close_refocuses_guest':True,'modifier_only_guest_edit_record_and_persistent_remove':True,'page_errors':errors});print(browser_name,mode,'SHORTCUT_EDITOR_PASS',flush=True)
     await page.close()
   finally:await b.close()
 finally:web.shutdown();(root/f'{browser_name}-ui-result.json').write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
