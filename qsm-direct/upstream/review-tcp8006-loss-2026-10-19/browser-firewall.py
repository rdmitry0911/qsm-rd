#!/usr/bin/env python3
"""Actual PVE UI from outside the owned nested lab's firewall."""
import asyncio, json, os, sys
from pathlib import Path
from playwright.async_api import async_playwright

async def main():
    params = json.loads(sys.stdin.readline())
    kind,vmid=params.get('kind','lxc'),params.get('vmid',147)
    assert (kind,vmid) in (('lxc',147),('qemu',148))
    output = Path(params['output']); output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await (p.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True,
            args=['--autoplay-policy=no-user-gesture-required','--disable-gpu']) if params['browser']=='chrome'
            else p.firefox.launch(headless=True, env={**os.environ,'MOZ_GMP_PATH':'/var/tmp/qsm-intel-lab/gmp-gmpopenh264/2.6.0'}, firefox_user_prefs={'media.autoplay.default':0,'media.gmp-gmpopenh264.enabled':True,'media.gmp-gmpopenh264.version':'2.6.0','media.gmp-gmpopenh264.autoupdate':False}))
        try:
            context = await browser.new_context(ignore_https_errors=True, viewport={'width':1280,'height':800})
            await context.add_init_script('''window.qsmPeers=[];window.qsmRtcErrors=[];const Original=window.RTCPeerConnection;
                window.RTCPeerConnection=class extends Original {constructor(...a){super(...a);window.qsmPeers.push(this);this.addEventListener('icecandidateerror',e=>window.qsmRtcErrors.push({code:e.errorCode,text:e.errorText}));} async setRemoteDescription(...a){try{return await super.setRemoteDescription(...a)}catch(e){window.qsmRtcErrors.push(String(e));throw e}}};''')
            requests=[];errors=[];responses=[]
            context.on('response',lambda r:responses.append({'path':r.url.split('?')[0],'status':r.status}) if '/qsm-direct/' in r.url else None)
            context.on('request',lambda r:requests.append(r.url.split('?')[0]) if '/qsm-direct/' in r.url else None)
            page=await context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            await page.goto('https://172.31.252.1:18006/',wait_until='domcontentloaded')
            await page.wait_for_function("window.Ext&&window.Proxmox&&Ext.ComponentQuery.query('viewport').some(v=>typeof v.updateLoginData==='function')")
            config=await page.evaluate("()=>({native:typeof PVE.Utils.openWebRTCViewer==='function',endpoint:window.QSM_DIRECT_ENDPOINT})")
            assert config['native'] is False and config['endpoint']['gateway'] is True,config
            await page.evaluate('''async ticket=>{
                const r=await fetch('/api2/json/access/ticket',{method:'POST',body:new URLSearchParams({username:'root@pam',password:ticket})});
                if(!r.ok)throw Error('PVE test login failed');const d=(await r.json()).data;
                const w=Ext.ComponentQuery.query('viewport').find(v=>typeof v.updateLoginData==='function');
                if(w.login){const l=w.login;w.login=null;l.close()}w.updateLoginData(d);
            }''',params['ticket'])
            await page.wait_for_function("()=>Ext.ComponentQuery.query('pveResourceTree')[0]?.getStore().getRootNode().findChild('id','GUEST_ID',true)".replace('GUEST_ID',f'{kind}/{vmid}'))
            notices=page.locator('.x-message-box:visible')
            if await notices.count():await notices.locator('.x-btn').filter(has_text='OK').last.click()
            await page.evaluate("()=>{Ext.ComponentQuery.query('pveResourceTree')[0].selectById('GUEST_ID');}".replace('GUEST_ID',f'{kind}/{vmid}'))
            button=await (await page.wait_for_function("()=>Ext.ComponentQuery.query('pveConsoleButton').find(b=>Number(b.vmid)===GUEST_VMID&&b.rendered&&b.enableQsmDirect)?.getEl().dom.id".replace('GUEST_VMID',str(vmid)))).json_value()
            async def stats():
                return await page.evaluate('''async()=>{const p=window.qsmPeers.at(-1),r=await p.getStats();
                    const v=Array.from(r.values()).find(s=>s.type==='inbound-rtp'&&s.kind==='video');
                    const t=Array.from(r.values()).find(s=>s.type==='transport'&&s.selectedCandidatePairId);
                    const pair=(t&&r.get(t.selectedCandidatePairId))||Array.from(r.values()).find(s=>s.type==='candidate-pair'&&s.nominated&&s.state==='succeeded');
                    const local=pair&&r.get(pair.localCandidateId);
                    return {state:p.connectionState,codec:v&&r.get(v.codecId)?.mimeType,decoded:v?.framesDecoded,
                        dropped:v?.framesDropped,received:v?.bytesReceived,local:local&&{type:local.candidateType,protocol:local.protocol,relayProtocol:local.relayProtocol},
                        relayOnly:p.getConfiguration().iceTransportPolicy==='relay'} }''')
            async def open_console():
                await page.evaluate('id=>{Ext.getCmp(id).showMenu();}',button)
                async with page.expect_popup() as event:await page.locator('.x-menu:visible').get_by_text('QSM Direct',exact=True).click()
                popup=await event.value
                await popup.wait_for_function("document.querySelector('video')?.videoWidth>0",timeout=60000)
                await page.wait_for_function("async()=>{const p=window.qsmPeers.at(-1);if(!p||p.connectionState!=='connected')return false;return Array.from((await p.getStats()).values()).some(v=>v.type==='inbound-rtp'&&v.kind==='video'&&v.framesDecoded>30)}",timeout=60000)
                return popup
            popup=await open_console();before=await stats()
            assert before['relayOnly'] and before['local']['type']=='relay',before
            assert before['codec']=='video/H264',before
            motion=await popup.evaluate('''async()=>{const v=document.querySelector('video'),c=document.createElement('canvas');c.width=160;c.height=100;
                const x=c.getContext('2d',{willReadFrequently:true});let old=null,changed=0;
                for(let n=0;n<20;n++){x.drawImage(v,0,0,160,100);const a=x.getImageData(0,0,160,100).data;
                    if(old){let d=0;for(let i=0;i<a.length;i+=4)d+=Math.abs(a[i]-old[i])+Math.abs(a[i+1]-old[i+1])+Math.abs(a[i+2]-old[i+2]);if(d/(160*100*3)>1)changed++}old=a;await new Promise(r=>setTimeout(r,100))}return {changed,total:19}}''')
            assert motion['changed']>=12,motion
            await popup.locator('video').screenshot(path=str(output/'video.png'))
            await asyncio.sleep(8);after=await stats();assert after['decoded']>before['decoded']+60,(before,after)
            await popup.close();await page.wait_for_function("()=>window.qsmPeers.at(-1).connectionState==='closed'")
            popup=await open_console();reconnect=await stats();await popup.close()
            assert all(url.startswith('https://172.31.252.1:18006/qsm-direct/') for url in requests),requests
            result={'pass':True,'browser':browser.version,'configuration':config,'before':before,'after':after,'motion':motion,'reconnect':reconnect,'request_count':len(requests),'errors':errors}
            (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps(result),flush=True)
        except Exception:
            if not page.is_closed():
                print('HTTP',json.dumps(responses),flush=True)
                print('DIAGNOSTIC',json.dumps(await page.evaluate("()=>({errors:window.qsmRtcErrors,codecs:RTCRtpReceiver.getCapabilities('video').codecs.map(c=>c.mimeType),peers:window.qsmPeers.map(p=>({connection:p.connectionState,ice:p.iceConnectionState})),alerts:Array.from(document.querySelectorAll('.x-message-box')).map(e=>e.textContent)})")),flush=True)
            raise
        finally:
            await browser.close()
asyncio.run(main())
