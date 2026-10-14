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
            page.on('response',lambda r:print('QSM_SIGNAL_HTTP',r.status,flush=True) if ':8007/' in r.url and 'qsm-direct' in r.url else None)
            await page.goto('https://192.168.64.20:8006/',wait_until='domcontentloaded')
            await page.wait_for_function("window.PVE && window.Ext && Ext.ComponentQuery.query('pveResourceTree')[0]",timeout=30000)
            notice=page.locator('.x-message-box:visible')
            if await notice.count():
                await notice.locator('.x-btn').filter(has_text='OK').last.click()
            await page.wait_for_function("Ext.ComponentQuery.query('pveResourceTree')[0].getStore().getRootNode().findChild('id','lxc/105',true)",timeout=30000)
            await page.evaluate("Ext.ComponentQuery.query('pveResourceTree')[0].selectById('lxc/105')")
            button_id=await page.wait_for_function("""()=>Ext.ComponentQuery.query('pveConsoleButton').find(b=>b.consoleType==='lxc'&&Number(b.vmid)===105&&b.rendered&&b.enableQsmDirect)?.getEl().dom.id""",timeout=30000)
            selector='#'+await button_id.json_value()
            box=await page.locator(selector).bounding_box()
            await page.mouse.click(box['x']+box['width']-6,box['y']+box['height']/2)
            item=page.locator('.x-menu:visible .x-menu-item').filter(has_text='QSM Direct')
            async with page.expect_popup() as event:
                await item.click()
            popup=await event.value
            await popup.wait_for_function("document.querySelector('video')?.videoWidth>0",timeout=40000)
            await popup.wait_for_timeout(3000)
            await popup.locator('video').focus()
            await popup.mouse.move(150,150)
            await popup.keyboard.press('ShiftLeft')
            print('LXC105_SAFE_POINTER_SHIFT_SENT',flush=True)
            for i in range(5):
                await popup.wait_for_timeout(4000)
                media=await popup.locator('video').evaluate('(v)=>({width:v.videoWidth,height:v.videoHeight,ready:v.readyState})')
                peers=await page.evaluate("""async()=>{let out=[];for(const pc of window.__peers){let video={};for(const r of (await pc.getStats()).values())if(r.type==='inbound-rtp'&&r.kind==='video')video={packets:r.packetsReceived,decoded:r.framesDecoded};out.push({state:pc.connectionState,ice:pc.iceConnectionState,video});}return out}""")
                print('FIREFOX_LXC105_VIDEO',json.dumps({'media':media,'peers':peers}),flush=True)
            await popup.close()
        finally:await browser.close()
asyncio.run(main())
