import asyncio,json,os,re
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from threading import Thread
from playwright.async_api import async_playwright
root=Path('/tmp/qsm-focus-20261025');source=Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text()
source=source.replace('    if (VIEWER) {\n        Ext.onReady', '    window.__qsmTestOpenConsole=openConsole;\n    if (VIEWER) {\n        Ext.onReady')
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/javascript' if self.path=='/viewer.js' else 'text/html');self.end_headers()
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
   b=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=True)
   try:
    for mode in ['top','embedded']:
     page=await b.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
     await page.goto(f'http://127.0.0.1:{web.server_port}/'+mode)
     frame=page.frames[-1] if mode=='embedded' else page.main_frame
     await frame.get_by_role('button',name='Capture keyboard (full screen)',exact=True).wait_for()
     await frame.get_by_role('button',name='Capture keyboard (full screen)',exact=True).click()
     await frame.wait_for_function('!!document.fullscreenElement')
     await frame.get_by_text('Keyboard capture active.',exact=False).wait_for(state='attached')
     await frame.get_by_role('button',name='Release keyboard and exit full screen',exact=True).first.click()
     await frame.wait_for_function('!document.fullscreenElement')
     await frame.get_by_role('button',name='Capture keyboard (full screen)',exact=True).first.click()
     await frame.wait_for_function('!!document.fullscreenElement')
     await frame.evaluate('window.dispatchEvent(new Event("blur"))')
     assert await frame.evaluate('!!document.fullscreenElement'),'blur exited fullscreen'
     await frame.get_by_text('Keyboard input paused while the console is unfocused.',exact=False).wait_for(state='attached')
     await frame.evaluate('window.dispatchEvent(new Event("focus"))')
     await frame.get_by_text(re.compile('Keyboard capture active')).wait_for(state='attached')
     assert await frame.evaluate('!!document.fullscreenElement'),'resume lost fullscreen'
     await frame.get_by_role('button',name='Release keyboard and exit full screen',exact=True).first.click()
     await frame.wait_for_function('!document.fullscreenElement')
     assert not errors,errors
     results.append({'mode':mode,'complete_production_JS':True,'capture_and_release':True,'blur_preserves_fullscreen':True,'focus_resumes_capture':True,'page_errors':errors});print(mode,'FULL_VIEWER_PASS',flush=True)
     await page.close()
   finally:await b.close()
 finally:web.shutdown();(root/'firefox-focus-result.json').write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
