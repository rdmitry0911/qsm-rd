import asyncio,json,os,subprocess,hashlib
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
from threading import Thread
from urllib.parse import parse_qs
from playwright.async_api import async_playwright
from guest_exec import user_bus,guest_exec
import layout_signals
root=Path('/tmp/qsm-layout-switch-20261025');source=Path('/home/dima/Projects/q-sunshine/qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js').read_text();source_hash=hashlib.sha256(source.encode()).hexdigest()
source=source.replace('const send = (value) => {',"const send = (value) => { if(value.op==='resize'){return true;} if(['keyboard','key_tap'].includes(value.op)){(window.__sent ||= []).push(value);}")
source=source.replace('const acceptInputPolicy = (message) => {','const acceptInputPolicy = (message) => { window.__policy=message;')
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/javascript; charset=utf-8' if self.path=='/viewer.js' else 'text/html; charset=utf-8');self.end_headers()
  self.wfile.write(source.encode() if self.path=='/viewer.js' else b'''<body><script>window.gettext=s=>s;window.Ext={define(){},onReady(fn){fn();},Msg:{alert(...a){console.log(...a)}}};window.Proxmox={Utils:{API2Request(o){o.success({result:{data:{status:'running'}}})}}};window.QSM_WEBRTC_VIEWER={node:'Vilnius',vmid:103,csrf:'test'};</script><script src="/viewer.js"></script>''')
 def log_message(self,*a):pass
async def node_json(code,data):
 p=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@192.168.64.20','python3','-c',__import__('shlex').quote(code),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
 o,e=await p.communicate(json.dumps(data).encode());assert p.returncode==0,e.decode();return json.loads(o)
async def layout():return int((await asyncio.to_thread(user_bus,['busctl','--user','call','org.kde.keyboard','/Layouts','org.kde.KeyboardLayouts','getLayout'])).split()[1])
async def select(n):await asyncio.to_thread(user_bus,['busctl','--user','call','org.kde.keyboard','/Layouts','org.kde.KeyboardLayouts','setLayout','u',str(n)])
async def start_modifier_watch(combo):
 import uuid
 base='/run/qsm-layout-probe-'+uuid.uuid4().hex
 targets=[42,125 if combo=='Meta' else 56]
 code="""import os,struct,select,time,json
from pathlib import Path
base=BASE_PLACEHOLDER;targets=set(TARGET_PLACEHOLDER);events=[];expected={(k,v) for k in targets for v in (0,1)};seen=set()
fd=os.open('/dev/input/event1',os.O_RDONLY|os.O_NONBLOCK);Path(base+'.ready').write_text('ready');deadline=time.monotonic()+4
try:
 while time.monotonic()<deadline and not expected.issubset(seen):
  r,_,_=select.select([fd],[],[],max(0,deadline-time.monotonic()))
  if not r:break
  data=os.read(fd,4096);size=struct.calcsize('llHHI')
  for i in range(0,len(data),size):
   sec,usec,kind,key,value=struct.unpack('llHHI',data[i:i+size])
   if kind==1 and key in targets and value in (0,1):events.append([key,value]);seen.add((key,value))
finally:
 os.close(fd);Path(base+'.json').write_text(json.dumps({'modifier_events':events,'complete':expected.issubset(seen),'text_keys_recorded':False}))
""".replace('BASE_PLACEHOLDER',repr(base)).replace('TARGET_PLACEHOLDER',repr(targets))
 launch="import subprocess; p=subprocess.Popen(['/usr/bin/python3','-c',%r],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print(p.pid)"%code
 await asyncio.to_thread(guest_exec,['/usr/bin/python3','-c',launch]);return base
async def read_modifier_watch(base):
 code="import json,time;from pathlib import Path;p=Path(%r+'.json');deadline=time.monotonic()+4\nwhile not p.exists() and time.monotonic()<deadline:time.sleep(.02)\nr=json.loads(p.read_text());p.unlink();Path(%r+'.ready').unlink();print(json.dumps(r))"%(base,base)
 return json.loads(await asyncio.to_thread(guest_exec,['/usr/bin/python3','-c',code]))
async def main():
 web=HTTPServer(('127.0.0.1',0),Handler);Thread(target=web.serve_forever,daemon=True).start();results={'source_sha256':source_hash,'vmid':103,'actual_installed_shared_terminal_bridge_worker':True,'resize_suppressed_for_production_VM':True,'cases':[]};original=await layout();results['original_layout']=original
 env={**os.environ,'DISPLAY':':225'};xvfb=subprocess.Popen(['Xvfb',env['DISPLAY'],'-screen','0','1280x900x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);wm=None
 try:
  await asyncio.sleep(.3);assert xvfb.poll() is None;wm=subprocess.Popen(['openbox','--sm-disable'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);await asyncio.sleep(.3)
  async with async_playwright() as p:
   browser=await p.firefox.launch(executable_path='/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox',headless=False,env={**env,'MOZ_GMP_PATH':'/tmp/qsm-rate-bands-20261023/gmp-gmpopenh264/2.6.0'},firefox_user_prefs={'media.autoplay.default':0,'media.gmp-gmpopenh264.enabled':True,'media.gmp-gmpopenh264.version':'2.6.0','media.gmp-gmpopenh264.autoupdate':False})
   try:
    page=await browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    async def offer(route):
     data=parse_qs(route.request.post_data);sdp=data['sdp'][0];lines=sdp.split('\r\n')
     for i,line in enumerate(lines):
      if line.startswith('a=candidate:'):
       fields=line.split()
       if fields[4].endswith('.local'):fields[4]='192.168.64.25';lines[i]=' '.join(fields)
     code="""import socket,json,sys
request={'version':2,'vmid':103,'user':'root@pam','offer':{'sdp':json.load(sys.stdin)['sdp'],'width':2560,'height':1440,'fps':60}}
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(40);s.connect('/run/webrtc-console/offer.sock');s.sendall(json.dumps(request).encode()+b'\\n');line=s.makefile('rb').readline();r=json.loads(line);assert r['ok'],r;print(json.dumps(r['answer']));s.close()
"""
     answer=await node_json(code,{'sdp':'\r\n'.join(lines)});await route.fulfill(json={'data':answer})
    await page.route('**/webrtcproxy',offer);await page.route('**/webrtcsettings',lambda route:route.fulfill(json={'data':{'codec':'h264','encoder':'h264_nvenc','maximum_kbps':8000}}))
    await page.goto(f'http://127.0.0.1:{web.server_port}/')
    await page.wait_for_function('window.__policy && document.querySelector("video").videoWidth>0',timeout=45000);results['policy']=await page.evaluate('window.__policy')
    async def guest_rule(text):
     h=page.locator('button[aria-controls="qsm-direct-toolbar"]');
     if await h.get_attribute('aria-expanded')=='false':await h.click()
     await page.get_by_role('button',name='Console settings',exact=True).click();panel=page.get_by_role('complementary',name='Console settings',exact=True);editor=panel.locator('section[aria-label="Host shortcuts"]')
     row=editor.locator('li').filter(has=page.get_by_text(text+' — Host / browser',exact=True));await row.get_by_role('button',name='Edit',exact=True).click();await editor.get_by_role('combobox',name='Shortcut action',exact=True).select_option('guest');await editor.get_by_role('button',name='Save shortcut',exact=True).click();await panel.get_by_role('button',name='Close',exact=True).click()
    for combo,shortcut in [('Meta','Shift+Meta'),('Alt','Alt+Shift')]:
     await guest_rule(shortcut)
     for release_first in ['Shift',combo]:
      await select(original);before=await layout();await page.locator('video').focus();await page.evaluate('window.__sent=[]');watch=await start_modifier_watch(combo);signals=await layout_signals.start()
      await page.keyboard.down(combo);await page.keyboard.down('Shift');await page.keyboard.up(release_first);await page.keyboard.up(combo if release_first=='Shift' else 'Shift');
      guest_events=await read_modifier_watch(watch);assert guest_events['complete'],guest_events;gui_events=await layout_signals.finish(signals);after=await layout();sent=await page.evaluate('window.__sent');r={'combo':combo+'+Shift','released_first':release_first,'before':before,'after':after,'sent':sent,'guest_evdev':guest_events,'guest_gui':gui_events,'keyboard_events':'Playwright Firefox actual keyboard dispatch, no modelled DOM events'};results['cases'].append(r);print(json.dumps(r),flush=True)
      assert len(sent)==1 and sent[0]['op']=='key_tap',r
      expected=[125 if combo=='Meta' else 56,42];assert [x[0] for x in guest_events['modifier_events'] if x[1]==1]==expected,r
      # Final activation is checked after collecting all four samples.
    assert not errors,errors;results['page_errors']=errors;await page.close()
   finally:await browser.close()
 finally:
  await select(original);results['restored_layout']=await layout();web.shutdown()
  if wm:wm.terminate();wm.wait(timeout=3)
  xvfb.terminate();xvfb.wait(timeout=3)
  (root/'after-binding-live-result.json').write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
