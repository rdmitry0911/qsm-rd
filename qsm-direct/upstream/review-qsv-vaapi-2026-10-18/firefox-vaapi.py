#!/usr/bin/env python3
"""Isolated Firefox comparison of shared21 and unpacked shared22 workers.

Run inside the dedicated qsm-pacing namespace, after CT108 fixture login.
No production API/service is replaced; root SSH authorizes this lab peer.
"""
import asyncio,json,os,sys,time
from pathlib import Path
from playwright.async_api import async_playwright

async def shape(rate, delay=40, limit=300):
    p=await asyncio.create_subprocess_exec('sudo','-n','nsenter','--net=/proc/1/ns/net',
        'tc','qdisc','change','dev','qsm-pace-host','parent','1:3','handle','30:',
        'netem','delay',str(delay)+'ms','rate',str(rate)+'mbit','limit',str(limit))
    assert await p.wait()==0

async def check(encoder, version, profile, root, playwright):
    root.mkdir(parents=True,exist_ok=True)
    await shape(20)
    stderr=(root/'server.log').open('wb')
    server=await asyncio.create_subprocess_exec('ssh','-o','BatchMode=yes','root@172.16.16.17',
        'python3','/root/qsm-adaptive-20261018/isolated-bridge.py',encoder,version,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=stderr)
    async def rpc(op,**kw):
        server.stdin.write((json.dumps(dict(op=op,**kw))+'\n').encode());await server.stdin.drain()
        line=await asyncio.wait_for(server.stdout.readline(),30)
        if not line:raise RuntimeError('isolated fixture exited; inspect server.log')
        return json.loads(line)
    browser=None
    try:
        await rpc('static' if profile=='static' else 'motion')
        browser=await playwright.firefox.launch(headless=True,env=dict(os.environ),firefox_user_prefs={
            'media.gmp-manager.updateEnabled':True,'media.gmp-provider.enabled':True,
            'media.gmp-gmpopenh264.enabled':True,'media.peerconnection.video.h264_enabled':True,
            'media.autoplay.default':0,'media.peerconnection.ice.obfuscate_host_addresses':False})
        page=await browser.new_page(viewport={'width':1280,'height':800})
        await page.set_content('<video autoplay muted playsinline></video>')
        offer=await page.evaluate('''async()=>{
            window.peer=new RTCPeerConnection();
            window.media=new MediaStream();
            const video=document.querySelector('video');video.srcObject=media;
            peer.ontrack=e=>{media.addTrack(e.track);video.play().catch(()=>{});};
            peer.addTransceiver('video',{direction:'recvonly'});
            peer.addTransceiver('audio',{direction:'recvonly'});
            window.control=peer.createDataChannel('qsm-control',{ordered:true});
            peer.createDataChannel('qsm-pointer',{ordered:false,maxRetransmits:0});
            await peer.setLocalDescription(await peer.createOffer());
            while(peer.iceGatheringState!=='complete') await new Promise(r=>setTimeout(r,20));
            window.canvas=document.createElement('canvas');canvas.width=600;canvas.height=800;
            window.ctx=canvas.getContext('2d',{willReadFrequently:true});
            window.visual={samples:0,changed:0,maxMeanDelta:0};window.previous=null;
            window.visualTimer=setInterval(()=>{
                if(video.readyState<2)return;ctx.drawImage(video,0,0,600,800,0,0,600,800);
                const data=ctx.getImageData(0,0,600,800).data;
                if(previous){let delta=0;for(let i=0;i<data.length;i+=4)
                    delta+=Math.abs(data[i]-previous[i])+Math.abs(data[i+1]-previous[i+1])+Math.abs(data[i+2]-previous[i+2]);
                    delta/=600*800*3;visual.samples++;visual.maxMeanDelta=Math.max(visual.maxMeanDelta,delta);
                    if(delta>1)visual.changed++;}previous=data;
            },100);
            return peer.localDescription.sdp;
        }''')
        answer=await rpc('offer',sdp=offer)
        await page.evaluate('(answer)=>peer.setRemoteDescription(answer)',answer)
        await page.wait_for_function("document.querySelector('video').videoWidth>0",timeout=40000)
        await asyncio.sleep(5)
        await page.evaluate('()=>{visual={samples:0,changed:0,maxMeanDelta:0};previous=null;}')
        stages=[(20,40,20)] if profile=='static' else [(20,40,15),(4,40,25),(20,40,110)]
        samples=[]
        for rate,delay,duration in stages:
            await shape(rate,delay,50 if rate==4 else 300)
            print('STAGE',encoder,version,profile,rate,delay,flush=True)
            for _ in range(duration//5):
                await page.evaluate('''()=>{if(control.readyState==='open'){
                    control.send(JSON.stringify({op:'keyboard',key:42,down:true,modifiers:0}));
                    control.send(JSON.stringify({op:'keyboard',key:42,down:false,modifiers:0}));
                }}''')
                stats=await rpc('stats')
                stats['browser']=await page.evaluate('''async()=>{
                    const stats=[...(await peer.getStats()).values()];
                    const video=stats.find(s=>s.type==='inbound-rtp'&&s.kind==='video');
                    return {frames:video?.framesDecoded,lost:video?.packetsLost,pli:video?.pliCount,
                        freezes:video?.freezeCount,freezeSeconds:video?.totalFreezesDuration,
                        dropped:video?.framesDropped,visual:{...visual}};
                }''')
                stats.update(rate=rate,delay=delay,time=time.monotonic())
                samples.append(stats)
                with (root/'samples.jsonl').open('a') as out:out.write(json.dumps(stats)+'\n')
                print('SAMPLE',encoder,version,profile,json.dumps(stats),flush=True)
                await asyncio.sleep(5)
        final=await rpc('stats')
        await page.locator('video').screenshot(path=str(root/'final.png'))
        before=samples[-1]['browser']['frames']
        await rpc('idr')
        await asyncio.sleep(2)
        after=await page.evaluate("async()=>[...(await peer.getStats()).values()].find(s=>s.type==='inbound-rtp'&&s.kind==='video').framesDecoded")
        assert after>before+60, (before,after)
        print('PLI_RECOVERY_PASS',encoder,version,before,after,flush=True)
        if profile=='static':
            if version=='baseline':assert samples[-1]['browser']['visual']['maxMeanDelta']>2,samples[-1]
            else:
                assert final['changes']==0,final
                assert samples[-1]['browser']['visual']['maxMeanDelta']<.15,samples[-1]
            assert samples[-1]['browser']['lost']==0,samples[-1]
        else:
            assert min(s['target'] for s in samples)<4000,samples
            assert final['target']==8000,final
            assert samples[-1]['browser']['frames']>5000,samples[-1]
            first,last=samples[-6]['browser']['visual'],samples[-1]['browser']['visual']
            assert last['changed']-first['changed'] >= .8*(last['samples']-first['samples']), 'moving fixture became static/locked'
        (root/'result.json').write_text(json.dumps(final,indent=2)+'\n')
        print('PASS',encoder,version,profile,json.dumps(final),flush=True)
    finally:
        if browser:await browser.close()
        if server.returncode is None:
            try:server.stdin.write(b'{"op":"quit"}\n');await server.stdin.drain()
            except (BrokenPipeError,ConnectionResetError):pass
            try:await asyncio.wait_for(server.wait(),10)
            except asyncio.TimeoutError:server.terminate();await server.wait()
        stderr.close()

async def main():
    root=Path('/tmp/qsm-intel-amd/browser')
    async with async_playwright() as p:
        cases=[('h264_vaapi','new','static'),('h264_vaapi','new','motion')]
        if '--cpu-only' in sys.argv:cases=cases[-1:]
        for encoder,version,profile in cases:
            await check(encoder,version,profile,root/f'{encoder}-{version}-{profile}',p)
asyncio.run(main())
