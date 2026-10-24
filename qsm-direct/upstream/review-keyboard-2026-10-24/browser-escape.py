import asyncio,os,json,subprocess,time
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
from playwright.async_api import async_playwright
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<button id="full">full</button><video tabindex="0" style="display:block;width:400px;height:300px"></video><script>window.esc=0;window.keyEvents=[];document.addEventListener("keydown",e=>keyEvents.push([e.code,e.target.tagName]),true);document.querySelector("video").addEventListener("keydown",e=>{if(e.code==="Escape")esc++;e.preventDefault();});document.querySelector("#full").onclick=()=>document.documentElement.requestFullscreen();</script>')
 def log_message(self,*args):pass
async def main():
 web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start()
 xvfb=subprocess.Popen(['Xvfb',':218','-screen','0','1280x800x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 result={}
 wm=None
 def native_key(key):
  subprocess.run(['xdotool','key','--clearmodifiers',key],env={**os.environ,'DISPLAY':':218'},check=True)
 try:
  await asyncio.sleep(.3)
  assert xvfb.poll() is None, 'owned Xvfb failed to start'
  wm=subprocess.Popen(['openbox','--sm-disable'],env={**os.environ,'DISPLAY':':218'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  await asyncio.sleep(.3)
  async with async_playwright() as p:
   b=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=False,env={**os.environ,'DISPLAY':':218'},firefox_user_prefs={'full-screen-api.ignore-widgets':False})
   try:
    page=await b.new_page();await page.goto(f'http://127.0.0.1:{web.server_port}')
    await page.locator('#full').click();await page.wait_for_function('document.fullscreenElement!==null');await page.locator('video').focus();native_key('Escape');await asyncio.sleep(.2)
    result['firefox_page_fullscreen']=await page.evaluate('({window:window.fullScreen,page:!!document.fullscreenElement,esc,keyboardLock:!!navigator.keyboard?.lock})')
    await page.locator('#full').focus();native_key('F11');await asyncio.sleep(.4);result['firefox_browser_f11_before_escape']=await page.evaluate('({window:window.fullScreen,page:!!document.fullscreenElement})');await page.locator('video').focus();await page.evaluate('esc=0');native_key('Escape');await asyncio.sleep(.2)
    result['firefox_browser_f11']=await page.evaluate('({window:window.fullScreen,page:!!document.fullscreenElement,esc})')
   finally:await b.close()
   b=await p.chromium.launch(executable_path='/home/dima/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome',headless=False,env={**os.environ,'DISPLAY':':218'},args=['--no-sandbox'])
   try:
    ctx=await b.new_context();page=await ctx.new_page();origin=f'http://127.0.0.1:{web.server_port}'
    cdp=await ctx.new_cdp_session(page)
    await cdp.send('Browser.grantPermissions',{'origin':origin,'permissions':['keyboardLock']})
    await page.goto(origin);await page.locator('#full').click();await page.wait_for_function('document.fullscreenElement!==null')
    await asyncio.sleep(.3)
    active=subprocess.check_output(['xdotool','getactivewindow','getwindowname'],env={**os.environ,'DISPLAY':':218'},text=True).strip();result['chromium_active_window']=active
    locked=await page.evaluate('async()=>{try{await navigator.keyboard.lock();return true;}catch(e){return e.name;}}');await page.locator('video').focus();await asyncio.sleep(.4);native_key('Escape');await asyncio.sleep(.2)
    result['chromium_native_xtest']=await page.evaluate('({page:!!document.fullscreenElement,esc,active:document.activeElement.tagName,keyEvents})');result['chromium_native_xtest']['lock_result']=locked
    await page.keyboard.press('Escape');await asyncio.sleep(.1)
    result['chromium_cdp_escape']=await page.evaluate('({page:!!document.fullscreenElement,esc,active:document.activeElement.tagName,keyEvents})')
    result['chromium_cdp_escape']['lock_result']=locked
   finally:await b.close()
 finally:
  web.shutdown()
  if wm:wm.terminate();wm.wait(timeout=3)
  xvfb.terminate();xvfb.wait(timeout=3)
 print(json.dumps(result,indent=2))
 Path('/tmp/qsm-keyboard-20261024/browser-escape-result.json').write_text(json.dumps(result,indent=2)+'\n')
asyncio.run(main())
