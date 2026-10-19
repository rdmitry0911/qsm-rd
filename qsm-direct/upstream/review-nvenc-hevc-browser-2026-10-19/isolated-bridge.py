#!/usr/bin/env python3
"""Shared24 component qualification, CT108 only, authenticated SSH stdio."""
import asyncio,json,sys,subprocess,tempfile
from pathlib import Path
sys.path.insert(0,'/root/qsm-nvenc-browser24/worker24/usr/lib/qsm-pve-direct/browser_bridge')
from qsm_browser_bridge import BrowserWebRtcBridge,AdaptiveVideoRate
async def main():
 encoder=sys.argv[1];assert encoder in ('hevc_nvenc','h264_nvenc');codec='hevc' if encoder.startswith('hevc') else 'h264'
 init=int(subprocess.check_output(['lxc-info','-n','108','-pH'],text=True).strip())
 with tempfile.TemporaryDirectory(prefix='qsm-nvenc24-') as directory:
  applied=[]
  def apply(target):bridge.input.set_video_bitrate(target);applied.append(target)
  rate=AdaptiveVideoRate(8000,apply,hardware=True,enabled="--fixed" not in sys.argv)
  bridge=BrowserWebRtcBridge(Path(directory),fps=60,expected_producer_uid=0,video_rate=rate,video_codec=codec)
  video,audio=bridge.start_taps()
  worker=await asyncio.create_subprocess_exec('/root/qsm-nvenc-browser24/worker24/usr/lib/qsm-pve-direct/bin/qsm-direct-media-worker','--wlroots-root',f'/proc/{init}/root','--wlroots-dir','run/qsm-display-1','--drop-privileges','nobody','--video-socket',video,'--audio-socket',audio,'--input-socket',bridge.input_context,'--codec',codec,'--encoder',encoder,'--fps','60','--video-bitrate-kbps','8000','--initial-size','1280x800',stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL)
  try:
   while True:
    line=await asyncio.to_thread(sys.stdin.readline)
    if not line:break
    c=json.loads(line);op=c['op']
    if op=='offer':r=await bridge.answer_offer(c['sdp'])
    elif op=='stats':r={'pid':worker.pid,'encoder':encoder,'target':rate.current_kbps,'maximum':rate.maximum_kbps,'targets':applied,'overflows':bridge.video_track.overflows,'drops':bridge.video_track.dropped_units,'state':bridge.connection_state}
    elif op=='idr':
     sender=next(s for s in bridge._pc.getSenders() if s.track and s.track.kind=='video')
     from aiortc.rtp import RtcpPsfbPacket,RTCP_PSFB_PLI
     await sender._handle_rtcp_packet(RtcpPsfbPacket(fmt=RTCP_PSFB_PLI,ssrc=999,media_ssrc=sender._ssrc));r={}
    elif op=='quit':break
    else:raise ValueError(op)
    print(json.dumps(r),flush=True)
  finally:
   await bridge.close()
   if worker.returncode is None:
    worker.terminate()
    try:await asyncio.wait_for(worker.wait(),5)
    except asyncio.TimeoutError:worker.kill();await worker.wait()
asyncio.run(main())
