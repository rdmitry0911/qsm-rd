#!/usr/bin/env python3
"""SSH-authenticated component fixture; never restarts the production service.

Compares the installed shared21 worker with an unpacked shared22 worker,
using the same candidate bridge and CT108's disposable desktop in private sockets. JSON SDP travels
only in the authenticated SSH pipes, never in saved logs. No HTTP listener.
"""
import asyncio,json,os,subprocess,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from bridge import BrowserWebRtcBridge,AdaptiveVideoRate

async def main():
    encoder=sys.argv[1]
    assert encoder in ('h264_vaapi',)
    init=int(subprocess.check_output(['lxc-info','-n','147','-pH'],text=True).strip())
    with tempfile.TemporaryDirectory(prefix='qsm-stability-') as directory:
        applied=[]
        def apply(target):
            bridge.input.set_video_bitrate(target)
            applied.append(target)
        rate=AdaptiveVideoRate(8000,apply,nvenc=encoder=='h264_nvenc')
        bridge=BrowserWebRtcBridge(Path(directory),fps=60,expected_producer_uid=0,video_rate=rate)
        video,audio=bridge.start_taps()
        worker=await asyncio.create_subprocess_exec('/root/qsm-adaptive-20261018/worker23/usr/lib/qsm-pve-direct/bin/qsm-direct-media-worker',
            '--wlroots-root',f'/proc/{init}/root','--wlroots-dir','run/qsm-display-1',
            '--drop-privileges','nobody','--video-socket',video,'--audio-socket',audio,
            '--input-socket',bridge.input_context,'--codec','h264','--encoder',encoder,
            '--vaapi-device','/dev/dri/renderD128','--fps','60','--video-bitrate-kbps','8000','--initial-size','1280x800',
            stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL)
        try:
            while True:
                line=await asyncio.to_thread(sys.stdin.readline)
                if not line:break
                command=json.loads(line)
                op=command['op']
                if op=='offer':
                    result=await bridge.answer_offer(command['sdp'])
                elif op=='idr':
                    sender=next(s for s in bridge._pc.getSenders() if s.track and s.track.kind=='video')
                    from aiortc.rtp import RtcpPsfbPacket, RTCP_PSFB_PLI
                    await sender._handle_rtcp_packet(RtcpPsfbPacket(fmt=RTCP_PSFB_PLI,ssrc=999,media_ssrc=sender._ssrc))
                    result={}
                elif op=='stats':
                    result=dict(target=rate.current_kbps,maximum=rate.maximum_kbps,
                        overflows=bridge.video_track.overflows,drops=bridge.video_track.dropped_units,
                        changes=len(applied),targets=applied,
                        demand=[round(getattr(s,"sending_kbps",0)) for s in rate._receivers.values()],
                        state=bridge.connection_state)
                elif op in ('static','motion'):
                    subprocess.run(['pct','exec','147','--',*(('touch','/tmp/qsm-video-static') if op=='static' else ('rm','-f','/tmp/qsm-video-static'))],check=True,stdout=subprocess.DEVNULL)
                    result={}
                elif op=='quit':break
                else:raise ValueError('unknown fixture operation')
                print(json.dumps(result),flush=True)
        finally:
            await bridge.close()
            if worker.returncode is None:
                worker.terminate()
                try:await asyncio.wait_for(worker.wait(),5)
                except asyncio.TimeoutError:worker.kill();await worker.wait()
asyncio.run(main())
