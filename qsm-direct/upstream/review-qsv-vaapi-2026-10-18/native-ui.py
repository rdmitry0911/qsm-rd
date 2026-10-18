#!/usr/bin/env python3
"""Interactive native PVE/SDDM qualification driver for isolated CT108.

Requires Playwright and SSH root access to the lab node. Ephemeral
account passwords stay in the selected CT's /root and are read
only into memory. No credentials are written to traces or screenshots.
See qualification.txt for the public JSON commands used.
"""
import asyncio,json,subprocess,sys,os,re
from pathlib import Path
from urllib.parse import quote
from playwright.async_api import async_playwright
HOST='172.16.16.17'
VMID=int(os.environ.get('QSM_LAB_VMID','108'))
assert VMID == 147
def ssh(command):
    return subprocess.run(['ssh','-o','BatchMode=yes','root@'+HOST,command],check=True,text=True,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout
async def main():
    Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login')).mkdir(parents=True,exist_ok=True)
    ticket=ssh("perl -MPVE::AccessControl -MPVE::Cluster -e 'PVE::Cluster::cfs_update(); print scalar PVE::AccessControl::assemble_ticket(q{root@pam})'")
    account=os.environ.get('QSM_TEST_ACCOUNT','qsm-test-a')
    assert account in ('qsm-test-a','qsm-test-b','qsm-layout-check','none')
    secret=ssh(f'pct exec {VMID} -- cat /root/'+account+'.password').strip() if account!='none' else None
    async with async_playwright() as p:
        if os.environ.get('QSM_LAB_BROWSER')=='firefox':
            browser=await p.firefox.launch(headless=True,env=dict(os.environ),firefox_user_prefs={
                'media.gmp-manager.updateEnabled':True,'media.gmp-provider.enabled':True,
                'media.gmp-gmpopenh264.enabled':True,'media.peerconnection.video.h264_enabled':True})
        else:
            browser=await p.chromium.launch(executable_path=os.environ.get('QSM_CHROMIUM_EXECUTABLE',p.chromium.executable_path),headless=True,args=['--autoplay-policy=no-user-gesture-required'])
        try:
            context=await browser.new_context(ignore_https_errors=True,viewport={'width':1280,'height':800})
            await context.add_init_script("""
                window.qsmLabPeers = [];
                window.qsmLabChannels = [];
                const NativePeer = window.RTCPeerConnection;
                window.RTCPeerConnection = class extends NativePeer {
                    constructor(...args) { super(...args); window.qsmLabPeers.push(this); }
                    createDataChannel(...args) {
                        const channel=super.createDataChannel(...args);
                        channel.qsmMessages=0;
                        channel.addEventListener('message',()=>{channel.qsmMessages++;});
                        window.qsmLabChannels.push(channel);
                        return channel;
                    }
                };
            """)
            legacy_requests=[]
            context.on('request', lambda request: legacy_requests.append(True) if ':8007/' in request.url else None)
            await context.route('**:8007/**', lambda route: route.abort())
            try:await context.grant_permissions(['local-network-access'],origin='https://'+HOST+':8006')
            except Exception:pass
            page=await context.new_page()
            page.on('pageerror',lambda e:print('EARLY_PAGE_ERROR',str(e).replace(ticket,'[ticket]'),flush=True))
            print('STAGE_PVE_LOGIN',flush=True)
            await page.goto('https://'+HOST+':8006/',wait_until='domcontentloaded')
            await page.wait_for_function("window.Ext && window.Proxmox && Ext.ComponentQuery.query('viewport').some(v=>typeof v.updateLoginData==='function')",timeout=30000)
            auth_result=await page.evaluate("""async(ticket)=>{
                const response=await fetch('/api2/json/access/ticket',{method:'POST',body:new URLSearchParams({username:'root@pam',password:ticket})});
                if(response.status!==200)throw Error('test login failed, HTTP '+response.status);
                const data=(await response.json()).data;
                const workspace=Ext.ComponentQuery.query('viewport').find(v=>typeof v.updateLoginData==='function');
                if(workspace.login){const login=workspace.login;workspace.login=null;login.close();}
                workspace.updateLoginData(data);
                return {status:response.status,capGroups:Object.keys(data.cap||{})};
            }""",ticket)
            print('AUTHENTICATED_PVE',json.dumps(auth_result),flush=True)
            await page.wait_for_function("Ext.ComponentQuery.query('pveResourceTree')[0]",timeout=30000)
            print('STAGE_PVE_TREE',flush=True)
            notices=page.locator('.x-message-box:visible')
            if await notices.count():
                await notices.locator('.x-btn').filter(has_text='OK').last.click()
            await page.wait_for_function("(vmid)=>Ext.ComponentQuery.query('pveResourceTree')[0]?.getStore().getRootNode().findChild('id','lxc/'+vmid,true)",arg=VMID,timeout=30000)
            await page.evaluate("(vmid)=>{Ext.ComponentQuery.query('pveResourceTree')[0].selectById('lxc/'+vmid);}",VMID)
            print('STAGE_PVE_GUEST_SELECTED',flush=True)
            await page.wait_for_timeout(2500)
            await page.screenshot(path='/tmp/qsm-intel-amd/native-ui/private-pve.png')
            print('BUTTONS',json.dumps(await page.evaluate("()=>Ext.ComponentQuery.query('pveConsoleButton').map(b=>({vmid:b.vmid,type:b.consoleType,rendered:b.rendered,qsm:b.enableQsmDirect,webrtc:b.enableWebRTC,disabled:b.disabled}))")),flush=True)
            print('STATUS_API',json.dumps(await page.evaluate("async()=>{const r=await fetch('/api2/json/nodes/pve-asrock/lxc/147/webrtcstatus',{credentials:'same-origin'});return {status:r.status,...await r.json()}}")),flush=True)
            identity=await page.wait_for_function("(vmid)=>Ext.ComponentQuery.query('pveConsoleButton').find(b=>b.consoleType==='lxc'&&Number(b.vmid)===vmid&&b.rendered&&(b.enableQsmDirect||b.enableWebRTC))?.getEl().dom.id",arg=VMID,timeout=30000)
            await page.evaluate('(id)=>{Ext.getCmp(id).showMenu();}',await identity.json_value())
            async with page.expect_popup() as event:
                menu=page.locator('.x-menu:visible')
                label='QSM Direct' if await menu.get_by_text('QSM Direct',exact=True).count() else 'WebRTC'
                await menu.get_by_text(label,exact=True).click()
            popup=await event.value
            print('STAGE_NATIVE_VIDEO',flush=True)
            popup.on('close',lambda:print('POPUP_CLOSED',flush=True))
            page.on('pageerror',lambda e:print('PAGE_ERROR',e.name,flush=True))
            await popup.wait_for_function("document.querySelector('video')?.videoWidth>0",timeout=40000)
            await popup.wait_for_timeout(1000)
            print('NATIVE_VIDEO',await popup.locator('video').evaluate('(v)=>({width:v.videoWidth,height:v.videoHeight})'),flush=True)
            while True:
                line=await asyncio.to_thread(sys.stdin.readline)
                if not line:break
                command=json.loads(line)
                op=command['op']
                if op=='snapshot':
                    name=command['name'];assert re.fullmatch('[a-z0-9-]+',name)
                    await popup.locator('video').screenshot(path=str(Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login'))/(name+'.png')))
                    print('SNAPSHOT',name,flush=True)
                elif op=='click':
                    box=await popup.locator('video').bounding_box()
                    await popup.mouse.click(box['x']+command['x'],box['y']+command['y'])
                    print('CLICK_OK',flush=True)
                elif op=='measure_dispatch':
                    assert VMID==147
                    from dispatch_probe import measure_dispatch
                    name=command['name'];assert re.fullmatch('[a-z0-9-]+',name)
                    await measure_dispatch(popup,Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login'))/(name+'.json'))
                elif op=='vm_policy_api':
                    result=await page.evaluate("""async () => {
                        const api=async(method,params)=>{
                            const response=await fetch('/api2/json/nodes/Vilnius/qemu/103/webrtcsettings',{
                                method,credentials:'same-origin',headers:{CSRFPreventionToken:Proxmox.CSRFPreventionToken},
                                ...(method==='GET'?{}:{body:new URLSearchParams(params)})});
                            const result=await response.json();
                            return {status:response.status,data:result.data};
                        };
                        const before=await api('GET');
                        if(before.status!==200) throw Error('VM policy read failed');
                        try {
                            const saved=await api('PUT',{...before.data,bitrate:20000});
                            const read=await api('GET');
                            const rejected=await api('PUT',{...before.data,bitrate:100001});
                            if(saved.status!==200||read.data.bitrate!==20000||rejected.status!==400)
                                throw Error('VM policy API qualification failed');
                            return {read:read.data,rejected:rejected.status};
                        } finally {
                            const restored=await api('PUT',before.data);
                            if(restored.status!==200) throw Error('VM policy restore failed');
                        }
                    }""")
                    print('VM_POLICY_API',json.dumps(result),flush=True)
                elif op=='media_policy':
                    # Exercise the real Settings UI and its authenticated PVE API.
                    assert VMID==147
                    await popup.mouse.move(1,100)
                    await popup.wait_for_timeout(900)
                    await popup.get_by_role('button',name='Console settings',exact=True).click()
                    field=popup.locator('#qsm-direct-video-bitrate')
                    await field.wait_for(state='visible')
                    await popup.wait_for_function("!document.querySelector('#qsm-direct-video-bitrate').disabled")
                    before=await field.input_value()
                    if 'mbps' in command:
                        await field.fill(str(command['mbps']))
                        async with page.expect_response(lambda r:r.request.method=='PUT' and '/lxc/147/webrtcsettings' in r.url) as response:
                            await popup.get_by_role('button',name='Save guest media policy',exact=True).click()
                        reply=await response.value
                        assert reply.status==200, 'media policy write failed'
                        value=(await reply.json())['data']
                        assert value['bitrate']==round(float(command['mbps'])*1000)
                        print('MEDIA_POLICY_SAVED',json.dumps(value),flush=True)
                    else:print('MEDIA_POLICY_READ',before,flush=True)
                    await popup.get_by_role('button',name='Close',exact=True).click()
                elif op=='receiver_stats':
                    stats=await page.evaluate("""async () => {
                        const peer=window.qsmLabPeers.at(-1);
                        const receiver=peer?.getReceivers().find(r=>r.track.kind==='video');
                        const report=receiver ? Array.from((await receiver.getStats()).values()).find(s=>s.type==='inbound-rtp'&&s.kind==='video') : null;
                        return {measuredAtMs:performance.now(),jitterBufferTarget:receiver?.jitterBufferTarget,playoutDelayHint:receiver?.playoutDelayHint,
                            jitterBufferDelay:report?.jitterBufferDelay,jitterBufferEmittedCount:report?.jitterBufferEmittedCount,
                            framesDecoded:report?.framesDecoded,framesDropped:report?.framesDropped,
                            framesReceived:report?.framesReceived,packetsLost:report?.packetsLost,
                            freezeCount:report?.freezeCount,totalFreezesDuration:report?.totalFreezesDuration,
                            pliCount:report?.pliCount,nackCount:report?.nackCount,
                            bytesReceived:report?.bytesReceived};
                    }""")
                    stats['channels']=await page.evaluate('()=>window.qsmLabChannels.map(c=>({label:c.label,state:c.readyState,buffered:c.bufferedAmount,messages:c.qsmMessages}))')
                    rate_status=popup.locator('#qsm-direct-adaptive-rate')
                    if await rate_status.count():
                        stats['videoRateStatus']=await rate_status.text_content()
                    print('RECEIVER_STATS',json.dumps(stats),flush=True)
                    with (Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login'))/'receiver-stats.jsonl').open('a') as log:
                        log.write(json.dumps(stats)+'\n')
                elif op in ('measure_fresh_frames','measure_cursor_shapes'):
                    assert VMID==147
                    from repaint_probe import measure_fresh_frames,measure_cursor_shapes
                    name=command['name'];assert re.fullmatch('[a-z0-9-]+',name)
                    function=measure_fresh_frames if op=='measure_fresh_frames' else measure_cursor_shapes
                    await function(popup,Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login'))/(name+'.json'))
                elif op=='measure_repaint':
                    assert VMID==147
                    from repaint_probe import measure_repaint
                    name=command['name'];assert re.fullmatch('[a-z0-9-]+',name)
                    await measure_repaint(popup,Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login'))/(name+'.json'))
                elif op=='measure_local_cursor':
                    if VMID==105:
                        sessions=ssh('pct exec 105 -- loginctl list-sessions --no-legend')
                        assert account=='none' and all(line.split()[2]=='sddm' for line in sessions.splitlines() if 'seatqsm' in line), 'CT105 probe requires greeters only'
                    else:assert VMID==147
                    from local_cursor_probe import measure_local_cursor
                    name=command['name'];assert re.fullmatch('[a-z0-9-]+',name)
                    await measure_local_cursor(popup,Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login'))/(name+'.json'))
                elif op=='measure_pointer':
                    assert VMID==147, 'pointer probing is restricted to disposable CT108'
                    from pointer_probe import measure_pointer
                    name=command['name'];assert re.fullmatch('[a-z0-9-]+',name)
                    output=Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login'))/(name+'.json')
                    await measure_pointer(popup,output)
                elif op=='native_motion':
                    focus_code = "from pathlib import Path; import subprocess,json; p=next(Path('/run/qsm-display-1').glob('sway-ipc*.sock')); r=subprocess.check_output(['/opt/qsm-native-sway/bin/swaymsg','-s',str(p),'[title=\"QSM continuous video transport fixture\"] focus'],text=True); assert all(x.get('success') for x in json.loads(r)),r"
                    import shlex
                    ssh('pct exec 147 -- python3 -c '+shlex.quote(focus_code))
                    await popup.wait_for_timeout(500)
                    result=await popup.evaluate("""async()=>{
                        const v=document.querySelector('video'),c=document.createElement('canvas');c.width=160;c.height=100;
                        const ctx=c.getContext('2d',{willReadFrequently:true});let old=null,changed=0;
                        for(let n=0;n<20;n++) {ctx.drawImage(v,0,0,160,100);const a=ctx.getImageData(0,0,160,100).data;
                            if(old){let delta=0;for(let i=0;i<a.length;i+=4)delta+=Math.abs(a[i]-old[i])+Math.abs(a[i+1]-old[i+1])+Math.abs(a[i+2]-old[i+2]);
                                if(delta/(160*100*3)>1)changed++;}old=a;await new Promise(r=>setTimeout(r,100));}
                        return {changed,total:19};
                    }""")
                    assert result['changed']>=16,result
                    print('NATIVE_MOTION_PASS',json.dumps(result),flush=True)
                    (Path(os.environ['QSM_LAB_ARTIFACTS'])/'motion.json').write_text(json.dumps(result)+'\n')
                elif op=='policy_get':
                    result=await page.evaluate("""async()=>{
                        const r=await fetch('/api2/json/nodes/pve-asrock/lxc/147/webrtcsettings',{credentials:'same-origin'});
                        return {status:r.status,...await r.json()};
                    }""")
                    print('POLICY_GET',json.dumps(result),flush=True)
                elif op=='wheel':
                    await popup.mouse.wheel(0,command['dy']);print('WHEEL_OK',flush=True)
                elif op=='focus':
                    await popup.locator('video').focus();print('FOCUS_OK',flush=True)
                elif op=='keys':
                    for key in command['keys']:await popup.keyboard.press(key)
                    print('KEYS_OK',flush=True)
                elif op=='type':
                    assert command['value'] in ['qsm-test-a','qsm-test-b','invalid-qsm-test','qsm-layout-check']
                    await popup.keyboard.type(command['value'],delay=30);print('TEXT_OK',flush=True)
                elif op=='password':
                    assert secret is not None, 'greeter-only mode has no credentials'
                    for ch in secret:
                        if ch.isupper(): key='Shift+Key'+ch
                        elif ch.islower(): key='Key'+ch.upper()
                        elif ch.isdigit(): key='Digit'+ch
                        elif ch=='_': key='Shift+Minus'
                        elif ch=='-': key='Minus'
                        else: raise ValueError('unexpected test password character')
                        await popup.keyboard.press(key)
                    print('PASSWORD_TYPED',flush=True)
                elif op=='wait':await popup.wait_for_timeout(1500);print('WAIT_OK',flush=True)
                elif op=='quit':
                    assert not legacy_requests, 'Unexpected legacy signalling request'
                    print('NO_PORT_8007_REQUESTS',flush=True)
                    break
        except Exception as error:
            message=str(error).replace(ticket,'[ticket]')
            if secret:message=message.replace(secret,'[password]')
            print('BROWSER_ERROR',message,flush=True)
            raise RuntimeError('browser native-login qualification failed') from None
        finally:await browser.close()
asyncio.run(main())
