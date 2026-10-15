import asyncio,json,subprocess
from urllib.parse import quote
from playwright.async_api import async_playwright

async def main():
    cmd="perl -MPVE::AccessControl -MPVE::Cluster -MJSON -e 'PVE::Cluster::cfs_update(); print encode_json({ticket=>scalar PVE::AccessControl::assemble_ticket(q{root@pam}),csrf=>scalar PVE::AccessControl::assemble_csrf_prevention_token(q{root@pam})})'"
    result=subprocess.run(['ssh','-o','BatchMode=yes','root@172.16.16.17',cmd],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
    auth=json.loads(result.stdout)
    async with async_playwright() as p:
        browser=await p.firefox.launch(headless=True,env={**__import__("os").environ,"MOZ_GMP_PATH":"/tmp/qsm-openh264/gmp-gmpopenh264/2.6.0"},firefox_user_prefs={"media.gmp-manager.updateEnabled":True,"media.gmp-provider.enabled":True,"media.gmp-gmpopenh264.enabled":True,"media.peerconnection.video.h264_enabled":True})
        try:
            ctx=await browser.new_context(ignore_https_errors=True)
            try:
                await ctx.grant_permissions(['local-network-access'],origin='https://172.16.16.17:8006')
                print('LOCAL_NETWORK_PERMISSION_GRANTED',flush=True)
            except Exception as error:
                print('LOCAL_NETWORK_PERMISSION_UNAVAILABLE',type(error).__name__,flush=True)
            await ctx.add_cookies([dict(name='PVEAuthCookie',value=quote(auth['ticket'],safe=''),url='https://172.16.16.17:8006')])
            await ctx.add_init_script("""window.__peers=[];window.__events=[];const Native=RTCPeerConnection;window.RTCPeerConnection=class extends Native{constructor(...args){super(...args);window.__peers.push(this);for(const event of ['connectionstatechange','iceconnectionstatechange'])this.addEventListener(event,()=>window.__events.push([event,this.connectionState,this.iceConnectionState]));}setRemoteDescription(desc){window.__events.push(['remote-start',desc?.type,(desc?.sdp?.match(/a=candidate:/g)||[]).length]);return super.setRemoteDescription(desc).then(()=>window.__events.push(['remote-end']));}};""")
            legacy_requests = []
            ctx.on('request', lambda r: legacy_requests.append(r.method) if ':8007/' in r.url else None)
            await ctx.route('**:8007/**', lambda route: route.abort('failed'))
            page=await ctx.new_page()
            page.on('response',lambda r:print('STATE_HTTP',r.status,flush=True) if 'webrtcstatus' in r.url else None)
            page.on('pageerror',lambda e:print('PAGE_ERROR',e.name,flush=True))
            await page.goto('https://172.16.16.17:8006/',wait_until='domcontentloaded')
            await page.wait_for_function("window.PVE && window.Ext && Ext.ComponentQuery.query('pveResourceTree')[0]",timeout=30000)
            notice=page.locator('.x-message-box:visible')
            if await notice.count():
                await notice.locator('.x-btn').filter(has_text='OK').last.click()
            await page.wait_for_function("Ext.ComponentQuery.query('pveResourceTree')[0].getStore().getRootNode().findChild('id','qemu/145',true)",timeout=30000)
            await page.evaluate("Ext.ComponentQuery.query('pveResourceTree')[0].selectById('qemu/145')")
            await page.wait_for_function("Ext.ComponentQuery.query('pveConsoleButton').find(b=>b.consoleType==='kvm'&&Number(b.vmid)===145)?.enableWebRTC===true",timeout=30000)
            result=await page.evaluate("""async()=>{const out={};for(const action of ['webrtcstatus','webrtcsettings']){
                const r=await fetch('/api2/json/nodes/pve-asrock/qemu/145/'+action,{credentials:'same-origin'});
                out[action]={status:r.status,data:(await r.json()).data};}return out;}""")
            assert result['webrtcstatus']['status']==200 and result['webrtcstatus']['data']['status']=='stopped'
            assert result['webrtcsettings']['status']==200
            print('ASROCK_NATIVE_API',json.dumps(result),flush=True)
            print('ASROCK_WEBRTC_MENU_AVAILABLE',flush=True)
            assert not legacy_requests
            print('NO_PORT_8007_REQUESTS',flush=True)
        finally:await browser.close()
asyncio.run(main())
