import asyncio,json,os,time
from pathlib import Path
async def main():
    root=Path(os.environ.get('QSM_ADAPTIVE_ARTIFACTS','/tmp/qsm-adaptive/network-final-software'));root.mkdir(exist_ok=True,parents=True)
    env=dict(os.environ,MOZ_GMP_PATH='/tmp/qsm-openh264/gmp-gmpopenh264/2.6.0',QSM_LAB_VMID='108',QSM_TEST_ACCOUNT='qsm-test-a',QSM_LAB_BROWSER='firefox',QSM_LAB_ARTIFACTS=str(root))
    cmd=['sudo','-n','ip','netns','exec','qsm-pacing','runuser','-u','dima','--','env']+[f'{k}={env[k]}' for k in ('MOZ_GMP_PATH','QSM_LAB_VMID','QSM_TEST_ACCOUNT','QSM_LAB_BROWSER','QSM_LAB_ARTIFACTS')]+['/tmp/takt-roi-check-venv/bin/python','qsm-direct/upstream/review-native-login-2026-10-16/browser-lab.py']
    child=await asyncio.create_subprocess_exec(*cmd,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    ready=asyncio.Event()
    measurements=[]
    async def reader():
        with (root/'browser.log').open('w') as log:
            async for line in child.stdout:
                text=line.decode();log.write(text);log.flush();print(text,end='',flush=True)
                if text.startswith('NATIVE_VIDEO'):ready.set()
                if text.startswith('RECEIVER_STATS '):measurements.append(json.loads(text.split(' ',1)[1]))
    task=asyncio.create_task(reader())
    async def send(op,**kw):
        child.stdin.write((json.dumps(dict(op=op,**kw))+'\n').encode());await child.stdin.drain()
    try:
        await asyncio.wait_for(ready.wait(),70)
        await send('keys',keys=['Shift'])
        await send('snapshot',name='motion-start')
        for rate,duration,limit in ((20,15,150),(4,35,50),(20,60,150)):
            p=await asyncio.create_subprocess_exec('sudo','-n','tc','qdisc','change','dev','qsm-pace-host','parent','1:3','handle','30:','netem','delay','40ms','rate',str(rate)+'mbit','limit',str(limit));assert await p.wait()==0
            marker=dict(stage_mbit=rate,seconds=duration,unix=time.time())
            with (root/'stages.jsonl').open('a') as log:log.write(json.dumps(marker)+'\n')
            print('STAGE',json.dumps(marker),flush=True)
            for n in range(duration//5):
                await send('receiver_stats');await send('keys',keys=['Shift'])
                await asyncio.sleep(5)
        await send('receiver_stats')
        await send('snapshot',name='motion-final')
        await send('quit')
        await asyncio.wait_for(child.wait(),20)
        await task
        import re
        rates=[float(re.search(r': ([0-9.]+) /',m['videoRateStatus']).group(1)) for m in measurements if 'videoRateStatus' in m]
        assert rates and min(rates)<4 and rates[-1]>=7, f'adaptation/recovery failed: {rates}'
        assert measurements[-1]['framesDecoded']>6000
        result=dict(minimum_target_mbps=min(rates),final_target_mbps=rates[-1],
                    freezes=measurements[-1]['freezeCount'],freeze_seconds=measurements[-1]['totalFreezesDuration'])
        (root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print('QUALIFIED',json.dumps(result),flush=True)
    finally:
        if child.returncode is None:child.terminate();await child.wait()
        await task
asyncio.run(main())
