import argparse,asyncio,json,subprocess
from urllib.parse import quote
from playwright.async_api import async_playwright

async def main():
    parser=argparse.ArgumentParser(description='Real touch gestures against a native QSM VM console')
    parser.add_argument('--host', required=True)
    parser.add_argument('--node', required=True)
    parser.add_argument('--vmid', type=int, required=True)
    parser.add_argument('--chromium', required=True)
    args=parser.parse_args()
    origin=f'https://{args.host}:8006'

    cmd="perl -MPVE::AccessControl -MPVE::Cluster -MJSON -e 'PVE::Cluster::cfs_update(); print encode_json({ticket=>scalar PVE::AccessControl::assemble_ticket(q{root@pam}),csrf=>scalar PVE::AccessControl::assemble_csrf_prevention_token(q{root@pam})})'"
    result=subprocess.run(['ssh','-o','BatchMode=yes',f'root@{args.host}',cmd],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
    auth=json.loads(result.stdout)
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=args.chromium,headless=True,args=['--autoplay-policy=no-user-gesture-required'])
        try:
            ctx=await browser.new_context(ignore_https_errors=True,has_touch=True,is_mobile=True,viewport={"width":1024,"height":768})
            try:
                await ctx.grant_permissions(['local-network-access'],origin=origin)
                print('LOCAL_NETWORK_PERMISSION_GRANTED',flush=True)
            except Exception as error:
                print('LOCAL_NETWORK_PERMISSION_UNAVAILABLE',type(error).__name__,flush=True)
            await ctx.add_cookies([dict(name='PVEAuthCookie',value=quote(auth['ticket'],safe=''),url=origin)])
            await ctx.add_init_script("""window.__peers=[];window.__events=[];const Native=RTCPeerConnection;window.RTCPeerConnection=class extends Native{constructor(...args){super(...args);window.__peers.push(this);for(const event of ['connectionstatechange','iceconnectionstatechange'])this.addEventListener(event,()=>window.__events.push([event,this.connectionState,this.iceConnectionState]));}setRemoteDescription(desc){window.__events.push(['remote-start',desc?.type,(desc?.sdp?.match(/a=candidate:/g)||[]).length]);return super.setRemoteDescription(desc).then(()=>window.__events.push(['remote-end']));}};""")
            await ctx.add_init_script("""window.__buttons=[];window.__positions=[];const send=RTCDataChannel.prototype.send;
                RTCDataChannel.prototype.send=function(value){if(typeof value==='string'){try{const m=JSON.parse(value);
                if(m.op==='mouse_button')window.__buttons.push({button:m.button,down:m.down});if(m.op==='mouse_position')window.__positions.push({x:m.x,y:m.y});}catch(e){}}
                return send.call(this,value);};
                localStorage.setItem('qsm-gestures',JSON.stringify({leftClick:'tap',rightClick:'longPress',middleClick:'tap3',drag:'drag1',scroll:'drag2',zoom:'pinch'}));""")
            page=await ctx.new_page()
            page.on('response',lambda r:print('STATE_HTTP',r.status,flush=True) if 'webrtcstatus' in r.url else None)
            page.on('pageerror',lambda e:print('PAGE_ERROR',e.name,e.message.replace(auth['ticket'],'[redacted]'),flush=True))
            page.on('response',lambda r:print('QSM_SIGNAL_HTTP',r.status,flush=True) if ':8007/' in r.url and 'qsm-direct' in r.url else None)
            await page.goto(origin+f'/?console=kvm&webrtc=1&vmid={args.vmid}&node={quote(args.node,safe="")}',wait_until='domcontentloaded')
            popup=page
            await popup.wait_for_function("document.querySelector('video')?.videoWidth>0",timeout=40000)
            cdp=await ctx.new_cdp_session(popup)
            async def touch(kind,x=100,y=100):
                await cdp.send('Input.dispatchTouchEvent',{'type':kind,'touchPoints':[] if kind in ('touchEnd','touchCancel') else [{'x':x,'y':y,'id':1}]})
            async def buttons():
                await page.wait_for_timeout(150)
                return await page.evaluate('window.__buttons.splice(0)')
            await buttons()
            await page.evaluate('window.__positions.splice(0)')
            await touch('touchStart',100,100)
            await touch('touchMove',140,120)
            await touch('touchMove',200,150)
            await touch('touchEnd')
            result=await buttons()
            assert result==[],result
            positions=await page.evaluate('window.__positions.splice(0)')
            assert positions and positions[0]!=positions[-1],positions
            print('REAL_TOUCH_SLIDE_NO_BUTTONS',flush=True)
            await touch('touchStart',100,100)
            await page.wait_for_timeout(650)
            assert await buttons()==[]
            await touch('touchMove',150,120)
            await touch('touchMove',200,130)
            await touch('touchEnd')
            result=await buttons()
            assert result==[{'button':1,'down':True},{'button':1,'down':False}],result
            print('REAL_TOUCH_HOLD_DRAG_SELECT_RELEASE',flush=True)
            await touch('touchStart',100,100)
            await page.wait_for_timeout(650)
            await touch('touchEnd')
            result=await buttons()
            assert result==[{'button':3,'down':True},{'button':3,'down':False}],result
            print('REAL_TOUCH_HOLD_LIFT_RIGHT_CLICK',flush=True)
            await touch('touchStart',100,100)
            await page.wait_for_timeout(650)
            await touch('touchMove',150,120)
            await touch('touchCancel')
            result=await buttons()
            assert result==[{'button':1,'down':True},{'button':1,'down':False}],result
            print('REAL_TOUCH_CANCEL_RELEASES_SELECTION',flush=True)
            media=await popup.locator('video').evaluate('(v)=>({width:v.videoWidth,height:v.videoHeight,ready:v.readyState})')
            print('TABLET_CONNECTED_VIDEO',json.dumps(media),flush=True)
            await popup.close()
        finally:await browser.close()
asyncio.run(main())
