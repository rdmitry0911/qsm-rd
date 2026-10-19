#!/usr/bin/env python3
"""Owned stock-PVE lab only; credentials arrive over stdin and are not saved."""
import asyncio
import json
import os
from pathlib import Path
import sys
import subprocess
from playwright.async_api import async_playwright

async def main():
    parameters = json.loads(sys.stdin.readline())
    kind, vmid, port = parameters['kind'], parameters['vmid'], parameters['port']
    assert (kind, vmid) in (('lxc', 147), ('qemu', 148))
    output = Path('/root/qsm-stock1-lab/browser-' + kind)
    output.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path='/opt/qsm-chrome/chrome',
            headless=True, args=['--autoplay-policy=no-user-gesture-required', '--disable-gpu'])
        try:
            context = await browser.new_context(ignore_https_errors=True,
                                                viewport={'width':1280, 'height':800})
            await context.add_init_script('''window.qsmPeers=[];const Original=window.RTCPeerConnection;
                window.RTCPeerConnection=class extends Original {constructor(...a){super(...a);window.qsmPeers.push(this)}};''')
            urls=[]
            context.on('request',lambda request:urls.append(request.url.split('?')[0])
                       if '/qsm-direct' in request.url or ':8007/' in request.url else None)
            page=await context.new_page()
            await page.goto('https://127.0.0.1:8006/', wait_until='domcontentloaded')
            await page.wait_for_function("window.Ext&&window.Proxmox&&Ext.ComponentQuery.query('viewport').some(v=>typeof v.updateLoginData==='function')")
            config=await page.evaluate("()=>({native:typeof PVE.Utils.openWebRTCViewer==='function',endpoint:window.QSM_DIRECT_ENDPOINT})")
            assert config['native'] is False, config
            assert config['endpoint']['port']==port,config
            print('STOCK_UI',json.dumps(config),flush=True)
            await page.evaluate('''async ticket=>{
                const r=await fetch('/api2/json/access/ticket',{method:'POST',body:new URLSearchParams({username:'root@pam',password:ticket})});
                if(r.status!==200)throw Error('PVE test login failed');const d=(await r.json()).data;
                const w=Ext.ComponentQuery.query('viewport').find(v=>typeof v.updateLoginData==='function');
                if(w.login){const l=w.login;w.login=null;l.close()}w.updateLoginData(d);
            }''',parameters['ticket'])
            rootid=f'{kind}/{vmid}'
            await page.wait_for_function("id=>Ext.ComponentQuery.query('pveResourceTree')[0]?.getStore().getRootNode().findChild('id',id,true)",arg=rootid)
            notices=page.locator('.x-message-box:visible')
            if await notices.count():
                await notices.locator('.x-btn').filter(has_text='OK').last.click()
            await page.evaluate("id=>{Ext.ComponentQuery.query('pveResourceTree')[0].selectById(id);}",rootid)
            identity=await page.wait_for_function("vmid=>Ext.ComponentQuery.query('pveConsoleButton').find(b=>Number(b.vmid)===vmid&&b.rendered&&b.enableQsmDirect)?.getEl().dom.id",arg=vmid)
            button=await identity.json_value()
            async def open_console():
                await page.evaluate('id=>{Ext.getCmp(id).showMenu();}',button)
                async with page.expect_popup() as event:
                    await page.locator('.x-menu:visible').get_by_text('QSM Direct',exact=True).click()
                popup=await event.value
                await popup.wait_for_function("document.querySelector('video')?.videoWidth>0",timeout=45000)
                await page.wait_for_function("async()=>{const p=window.qsmPeers.at(-1);if(!p||p.connectionState!=='connected')return false;for(const s of (await p.getStats()).values()){if(s.type==='inbound-rtp'&&s.kind==='video'&&s.framesDecoded>30)return true}return false}",timeout=45000)
                return popup
            popup=await open_console()
            if kind == 'lxc':
                fixture_focus = "from pathlib import Path;import subprocess,json;p=next(Path('/run/qsm-display-1').glob('sway-ipc*.sock'));r=subprocess.check_output(['/opt/qsm-native-sway/bin/swaymsg','-s',str(p),'[title=\"QSM continuous video transport fixture\"] focus'],text=True);assert all(x.get('success') for x in json.loads(r)),r"
                subprocess.run(['pct','exec','147','--','python3','-c',fixture_focus],check=True,capture_output=True)
                await popup.wait_for_timeout(1000)
            async def stats():
                return await page.evaluate('''async()=>{const p=window.qsmPeers.at(-1),r=await p.getStats();
                    const v=Array.from(r.values()).find(s=>s.type==='inbound-rtp'&&s.kind==='video');
                    return {state:p.connectionState,codec:r.get(v.codecId)?.mimeType,
                        decoded:v.framesDecoded,dropped:v.framesDropped,keys:v.keyFramesDecoded}}''')
            before=await stats();assert before['codec']=='video/H264',before
            motion=await popup.evaluate('''async()=>{const v=document.querySelector('video'),c=document.createElement('canvas');c.width=160;c.height=100;
                const ctx=c.getContext('2d',{willReadFrequently:true});let old=null,changed=0;
                for(let n=0;n<20;n++){ctx.drawImage(v,0,0,160,100);const a=ctx.getImageData(0,0,160,100).data;
                    if(old){let d=0;for(let i=0;i<a.length;i+=4)d+=Math.abs(a[i]-old[i])+Math.abs(a[i+1]-old[i+1])+Math.abs(a[i+2]-old[i+2]);
                        if(d/(160*100*3)>1)changed++}old=a;await new Promise(r=>setTimeout(r,100))}return {changed,total:19}}''')
            assert motion['changed']>=12,motion
            await popup.locator('video').screenshot(path=str(output/'video.png'))
            after=await stats();assert after['decoded']>before['decoded'],(before,after)
            print('VIDEO_MOTION',json.dumps({'before':before,'after':after,'motion':motion}),flush=True)
            await popup.close()
            await page.wait_for_function("()=>window.qsmPeers.at(-1).connectionState==='closed'",timeout=20000)
            popup=await open_console()
            reconnected=await stats()
            await popup.close()
            path=f'/nodes/qsm-intel-pve/{kind}/{vmid}/qsm-direct-settings'
            origin=config['endpoint']['public_url'] or f'https://127.0.0.1:{port}'
            async def policy(method,user,ticket,body=None):
                return await page.evaluate('''async p=>{const o={method:p.method,credentials:'omit',headers:{Authorization:'Bearer '+p.ticket,'X-QSM-User':p.user}};
                    if(p.method!=='GET'){o.headers['Content-Type']='application/json';o.body=JSON.stringify(p.body)}
                    const r=await fetch(p.url,o);return {status:r.status,data:(await r.json()).data}}''',
                    {'method':method,'user':user,'ticket':ticket,'body':body,'url':origin+path})
            saved=await policy('GET','root@pam',parameters['ticket']);assert saved['status']==200,saved
            updated=await policy('PUT','root@pam',parameters['ticket'],{**saved['data'],'bitrate':10000})
            assert updated['status']==200 and updated['data']['bitrate']==10000,updated
            restored=await policy('PUT','root@pam',parameters['ticket'],saved['data']);assert restored==saved,(restored,saved)
            readonly=await policy('GET','qsm-stock-viewer@pve',parameters['viewer_ticket']);assert readonly['status']==200,readonly
            denied=await policy('PUT','qsm-stock-viewer@pve',parameters['viewer_ticket'],saved['data']);assert denied['status']==401,denied
            invalid=await policy('GET','qsm-stock-viewer@pve','invalid-fixture-ticket');assert invalid['status']==401,invalid
            assert not any(':8007/' in url for url in urls),urls
            assert any(f':{port}/nodes/' in url for url in urls),urls
            result={'kind':kind,'vmid':vmid,'browser':browser.version,'config':config,
                    'initial':before,'final':after,'motion':motion,'reconnect':reconnected,
                    'root_settings_write':updated['status'],'viewer_read':readonly['status'],
                    'viewer_write_denied':denied['status'],'invalid_ticket_denied':invalid['status'],
                    'port_8007_requests':0}
            (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
            print('STOCK_BROWSER_PASS',json.dumps(result),flush=True)
        finally:
            await browser.close()

asyncio.run(main())
