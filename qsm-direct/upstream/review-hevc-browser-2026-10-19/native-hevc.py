#!/usr/bin/env python3
"""Disposable CT147 native UI codec/receiver qualification; ticket via stdin."""
import asyncio,json,os,re,sys
from pathlib import Path
from playwright.async_api import async_playwright
BASE=os.environ.get('QSM_TEST_ORIGIN','https://127.0.0.1:8006')
OUT=Path(os.environ['QSM_TEST_ARTIFACTS']);OUT.mkdir(parents=True,exist_ok=True)
async def main():
 ticket=sys.stdin.readline().strip();assert ticket.startswith('PVE:')
 browser_name=os.environ.get('QSM_TEST_BROWSER','chrome')
 expected=os.environ.get('QSM_TEST_CODEC','video/H265')
 async with async_playwright() as p:
  if browser_name=='firefox':
   browser=await p.firefox.launch(headless=True,env=dict(os.environ),firefox_user_prefs={'media.peerconnection.ice.obfuscate_host_addresses':False,'media.gmp-gmpopenh264.enabled':True,'media.peerconnection.video.h264_enabled':True})
  else:
   browser=await p.chromium.launch(executable_path=os.environ.get('QSM_CHROME','/opt/qsm-chrome/chrome'),headless=False,args=['--no-sandbox','--autoplay-policy=no-user-gesture-required','--use-gl=angle','--use-angle='+os.environ.get('QSM_CHROME_ANGLE','gl'),'--enable-features=AcceleratedVideoDecodeLinuxGL,PlatformHEVCDecoderSupport,WebRtcAllowH265Receive','--ignore-gpu-blocklist']+json.loads(os.environ.get('QSM_CHROME_EXTRA','[]')))
  try:
   context=await browser.new_context(ignore_https_errors=True,viewport={'width':1280,'height':800})
   await context.add_init_script('''window.qsmTestPeers=[];const Original=window.RTCPeerConnection;
    window.RTCPeerConnection=class extends Original {constructor(...a){super(...a);window.qsmTestPeers.push(this)}};''')
   negotiation_errors=[]
   async def record_response(response):
    if '/lxc/147/webrtcproxy' in response.url and response.status>=400:
     try: payload=await response.json()
     except Exception: payload={'message':'non-JSON error'}
     negotiation_errors.append({'status':response.status,'message':payload.get('message'),'errors':payload.get('errors')})
   context.on('response',record_response)
   legacy=[];context.on('request',lambda r:legacy.append(True) if ':8007/' in r.url else None)
   await context.route('**:8007/**',lambda r:r.abort())
   page=await context.new_page();await page.goto(BASE+'/',wait_until='domcontentloaded')
   await page.wait_for_function("window.Ext&&window.Proxmox&&Ext.ComponentQuery.query('viewport').some(v=>typeof v.updateLoginData==='function')")
   print('BROWSER',browser_name,await browser.version() if callable(browser.version) else browser.version,flush=True)
   caps=await page.evaluate("()=>RTCRtpReceiver.getCapabilities('video')")
   print('RECEIVER_CAPABILITIES',json.dumps(caps),flush=True)
   if browser_name!='firefox':
    cdp=await browser.new_browser_cdp_session();gpu=(await cdp.send('SystemInfo.getInfo'))['gpu'];print('BROWSER_GPU',json.dumps({'devices':gpu['devices'],'featureStatus':gpu['featureStatus'],'renderer':gpu['auxAttributes'].get('glRenderer')}),flush=True)
   await page.evaluate('''async ticket=>{const r=await fetch('/api2/json/access/ticket',{method:'POST',body:new URLSearchParams({username:'root@pam',password:ticket})});if(r.status!==200)throw Error('auth failed');const d=(await r.json()).data;const w=Ext.ComponentQuery.query('viewport').find(v=>typeof v.updateLoginData==='function');if(w.login){const l=w.login;w.login=null;l.close()}w.updateLoginData(d)}''',ticket)
   await page.wait_for_function("Ext.ComponentQuery.query('pveResourceTree')[0]?.getStore().getRootNode().findChild('id','lxc/147',true)")
   notices=page.locator('.x-message-box:visible')
   if await notices.count():await notices.locator('.x-btn').filter(has_text='OK').last.click()
   await page.evaluate("()=>{Ext.ComponentQuery.query('pveResourceTree')[0].selectById('lxc/147');}")
   identity=await page.wait_for_function("Ext.ComponentQuery.query('pveConsoleButton').find(b=>b.consoleType==='lxc'&&Number(b.vmid)===147&&b.rendered&&(b.enableQsmDirect||b.enableWebRTC))?.getEl().dom.id")
   await page.evaluate('(id)=>{Ext.getCmp(id).showMenu();}',await identity.json_value())
   async with page.expect_popup() as event:
    menu=page.locator('.x-menu:visible');label='QSM Direct' if await menu.get_by_text('QSM Direct',exact=True).count() else 'WebRTC';await menu.get_by_text(label,exact=True).click()
   popup=await event.value
   if os.environ.get('QSM_EXPECT_REJECT')=='1':
    for _ in range(60):
     if negotiation_errors:break
     await page.wait_for_timeout(500)
    assert negotiation_errors and all(x['status']>=400 for x in negotiation_errors),negotiation_errors
    assert not any(c['mimeType'].lower()=='video/h265' for c in caps['codecs'])
    assert not legacy
    print('EXPECTED_HEVC_REJECT',json.dumps(negotiation_errors),flush=True);return
   await popup.wait_for_function("document.querySelector('video')?.videoWidth>0",timeout=45000)
   async def stats():
    d=await page.evaluate('''async()=>{const p=window.qsmTestPeers.at(-1);const report=await p.getStats();const v=Array.from(report.values()).find(s=>s.type==='inbound-rtp'&&s.kind==='video');const c=report.get(v?.codecId);return {connection:p.connectionState,codec:c,mimeType:c?.mimeType,...v}}''')
    status=popup.locator('#qsm-direct-adaptive-rate');d['videoRateStatus']=await status.text_content() if await status.count() else None
    print('RECEIVER_STATS',json.dumps(d),flush=True)
    with (OUT/'receiver-stats.jsonl').open('a') as f:f.write(json.dumps(d)+'\n')
    assert d['mimeType']==expected,d['mimeType'];assert d['framesDecoded']>0,d
    return d
   await stats();print('NATIVE_VIDEO',flush=True)
   while True:
    line=await asyncio.to_thread(sys.stdin.readline)
    if not line:break
    cmd=json.loads(line);op=cmd['op']
    if op=='stats':await stats()
    elif op=='motion':
     result=await popup.evaluate('''async()=>{const v=document.querySelector('video'),c=document.createElement('canvas');c.width=160;c.height=100;const ctx=c.getContext('2d',{willReadFrequently:true});let old=null,changed=0;for(let n=0;n<20;n++){ctx.drawImage(v,0,0,160,100);const a=ctx.getImageData(0,0,160,100).data;if(old){let delta=0;for(let i=0;i<a.length;i+=4)delta+=Math.abs(a[i]-old[i])+Math.abs(a[i+1]-old[i+1])+Math.abs(a[i+2]-old[i+2]);if(delta/(160*100*3)>1)changed++}old=a;await new Promise(r=>setTimeout(r,100))}return {changed,total:19}}''')
     assert result['changed']>=16,result;print('NATIVE_MOTION_PASS',json.dumps(result),flush=True)
    elif op=='snapshot':
     assert re.fullmatch('[a-z0-9-]+',cmd['name']);await popup.locator('video').screenshot(path=str(OUT/(cmd['name']+'.png')))
    elif op=='sdp':
     result=await page.evaluate("()=>{const p=window.qsmTestPeers.at(-1);return {offer:p.localDescription.sdp.split('\\r\\n').filter(l=>/^a=(rtpmap|fmtp):/.test(l)),answer:p.remoteDescription.sdp.split('\\r\\n').filter(l=>/^a=(rtpmap|fmtp):/.test(l))}}")
     print('NEGOTIATED_SDP',json.dumps(result),flush=True)
    elif op=='quit':
     assert not legacy;print('NO_PORT_8007_REQUESTS',flush=True);break
    else:raise ValueError(op)
  except Exception as e:
   print('BROWSER_ERROR',str(e).replace(ticket,'[ticket]'),flush=True);raise RuntimeError('native HEVC check failed') from None
  finally:await browser.close()
asyncio.run(main())
