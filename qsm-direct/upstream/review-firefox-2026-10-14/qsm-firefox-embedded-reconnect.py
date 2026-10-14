import asyncio,json,subprocess
from urllib.parse import quote
from playwright.async_api import async_playwright

async def main():
    cmd="perl -MPVE::AccessControl -MPVE::Cluster -MJSON -e 'PVE::Cluster::cfs_update(); print encode_json({ticket=>scalar PVE::AccessControl::assemble_ticket(q{root@pam}),csrf=>scalar PVE::AccessControl::assemble_csrf_prevention_token(q{root@pam})})'"
    result=subprocess.run(['ssh','-o','BatchMode=yes','root@192.168.64.20',cmd],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
    auth=json.loads(result.stdout)
    async with async_playwright() as p:
        browser=await p.firefox.launch(headless=True,env={**__import__("os").environ,"MOZ_GMP_PATH":"/tmp/qsm-openh264/gmp-gmpopenh264/2.6.0"},firefox_user_prefs={"media.gmp-manager.updateEnabled":True,"media.gmp-provider.enabled":True,"media.gmp-gmpopenh264.enabled":True,"media.peerconnection.video.h264_enabled":True})
        try:
            ctx=await browser.new_context(ignore_https_errors=True)
            try:
                await ctx.grant_permissions(['local-network-access'],origin='https://192.168.64.20:8006')
                print('LOCAL_NETWORK_PERMISSION_GRANTED',flush=True)
            except Exception as error:
                print('LOCAL_NETWORK_PERMISSION_UNAVAILABLE',type(error).__name__,flush=True)
            await ctx.add_cookies([dict(name='PVEAuthCookie',value=quote(auth['ticket'],safe=''),url='https://192.168.64.20:8006')])
            await ctx.add_init_script("""window.__peers=[];window.__events=[];const Native=RTCPeerConnection;window.RTCPeerConnection=class extends Native{constructor(...args){super(...args);window.__peers.push(this);for(const event of ['connectionstatechange','iceconnectionstatechange'])this.addEventListener(event,()=>window.__events.push([event,this.connectionState,this.iceConnectionState]));}setRemoteDescription(desc){window.__events.push(['remote-start',desc?.type,(desc?.sdp?.match(/a=candidate:/g)||[]).length]);return super.setRemoteDescription(desc).then(()=>window.__events.push(['remote-end']));}};""")
            async def relay(route):
                req=route.request
                from urllib.parse import urlparse,parse_qs
                endpoint=urlparse(req.url).path.rsplit('/',1)[-1]
                if endpoint not in ['webrtcstatus','webrtcproxy','webrtcsettings']:
                    response=await ctx.request.fetch(req)
                    await route.fulfill(response=response);return
                command=['ssh','-o','BatchMode=yes','root@192.168.64.20']
                import shlex
                argv=['pvesh','create' if req.method=='POST' else 'get','/nodes/Vilnius/qemu/103/'+endpoint,'--output-format','json']
                if req.method=='POST':
                    values=parse_qs(req.post_data or '')
                    for key in ['sdp','width','height','fps']:
                        if key in values:argv.extend(['--'+key,values[key][0]])
                proc=await asyncio.create_subprocess_exec(*command,shlex.join(argv),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                out,err=await proc.communicate()
                await route.fulfill(status=200 if proc.returncode==0 else 500,content_type='application/json',body=json.dumps({'success':proc.returncode==0,'data':json.loads(out) if proc.returncode==0 else None}))
            
            page=await ctx.new_page()
            page.on('response',lambda r:print('STATE_HTTP',r.status,flush=True) if 'webrtcstatus' in r.url else None)
            page.on('pageerror',lambda e:print('PAGE_ERROR',e.name,flush=True))
            page.on('response',lambda r:print('OFFER_HTTP',r.status,flush=True) if 'webrtcproxy' in r.url else None)
            await page.goto('https://192.168.64.20:8006/',wait_until='domcontentloaded')
            await page.wait_for_function("window.PVE && window.Ext && Ext.ComponentQuery.query('pveResourceTree')[0]",timeout=30000)
            notice=page.locator('.x-message-box:visible')
            if await notice.count():
                await notice.locator('.x-btn').filter(has_text='OK').last.click()
            await page.wait_for_function("Ext.ComponentQuery.query('pveResourceTree')[0].getStore().getRootNode().findChild('id','qemu/103',true)",timeout=30000)
            await page.evaluate("Ext.ComponentQuery.query('pveResourceTree')[0].selectById('qemu/103')")
            await page.locator('.x-treelist-item-text').filter(has_text='Console').click()
            await page.wait_for_function("[...document.querySelectorAll('iframe')].some(f=>f.src.includes('webrtc=1'))",timeout=30000)
            async with asyncio.timeout(15):
                while not any('webrtc=1' in f.url for f in page.frames):
                    await asyncio.sleep(0.1)
            frame=next(f for f in page.frames if 'webrtc=1' in f.url)
            await frame.wait_for_function("document.querySelector('video')?.videoWidth>0",timeout=30000)
            print('FIREFOX_NATIVE_EMBEDDED_VIDEO_OK',await frame.locator('video').evaluate('(v)=>({width:v.videoWidth,height:v.videoHeight})'),flush=True)
            await frame.evaluate("window.__peers[0].close()")
            retry=frame.get_by_role('button',name='Reconnect',exact=True)
            await retry.wait_for(state='visible',timeout=10000)
            await retry.click(timeout=5000)
            await frame.wait_for_function("document.querySelector('video')?.videoWidth>0",timeout=30000)
            await frame.wait_for_timeout(12000)
            print('FIREFOX_EMBEDDED_RECONNECT_AND_VIDEO_OK',await frame.evaluate("({state:window.__peers[0].connectionState,width:document.querySelector('video').videoWidth})"),flush=True)
        finally:await browser.close()
asyncio.run(main())
