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
            
            def summarize(sdp):
                result=[];media=None;bundle=[]
                for line in sdp.splitlines():
                    if line.startswith('a=group:BUNDLE'):bundle=line.split()[1:]
                    if line.startswith('m='):
                        bits=line[2:].split();media={'kind':bits[0],'port':bits[1],'candidates':0};result.append(media)
                    if media is not None:
                        if line.startswith('a=mid:'):media['mid']=line[6:]
                        if line.startswith('a=candidate:'):media['candidates']+=1
                        if line=='a=bundle-only':media['bundle_only']=True
                return {'bundle':bundle,'media':result}
            def offer_metadata(req):
                from urllib.parse import parse_qs
                if 'webrtcproxy' in req.url:
                    values=parse_qs(req.post_data or '')
                    print('OFFER_LAYOUT',summarize(values.get('sdp',[''])[0]),flush=True)
            async def answer_metadata(resp):
                if 'webrtcproxy' in resp.url and resp.status==200:
                    result=await resp.json();print('ANSWER_LAYOUT',summarize(result.get('data',{}).get('sdp','')),flush=True)
            page=await ctx.new_page()
            page.on('request',offer_metadata)
            page.on('response',answer_metadata)
            page.on('response',lambda r:print('STATE_HTTP',r.status,flush=True) if 'webrtcstatus' in r.url else None)
            page.on('pageerror',lambda e:print('PAGE_ERROR',e.name,flush=True))
            page.on('response',lambda r:print('OFFER_HTTP',r.status,flush=True) if 'webrtcproxy' in r.url else None)
            await page.goto('https://192.168.64.20:8006/?console=kvm&webrtc=1&vmid=103&node=Vilnius',wait_until='domcontentloaded')
            for i in range(8):
                await page.wait_for_timeout(4000)
                stats=await page.evaluate("""async()=>{const out={videoWidth:document.querySelector('video')?.videoWidth,events:window.__events,peers:[]};for(const pc of window.__peers){const reports=[];for(const r of (await pc.getStats()).values()){if(r.type==='candidate-pair')reports.push({type:r.type,state:r.state,nominated:r.nominated,requestsSent:r.requestsSent,responsesReceived:r.responsesReceived});if(r.type==='inbound-rtp'&&r.kind==='video')reports.push({type:r.type,packets:r.packetsReceived,decoded:r.framesDecoded});}out.peers.push({state:pc.connectionState,ice:pc.iceConnectionState,signaling:pc.signalingState,remote:!!pc.remoteDescription,localCandidates:(pc.localDescription?.sdp.match(/a=candidate:/g)||[]).length,reports});}return out}""")
                print('BROWSER_PROBE',json.dumps(stats),flush=True)
        finally:await browser.close()
asyncio.run(main())
