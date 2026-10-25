import asyncio,json,sys,subprocess,tempfile
from pathlib import Path
sys.path.insert(0,'/root/qsm-keycapture25/'+sys.argv[1])
from qsm_browser_bridge import BrowserWebRtcBridge
async def main():
 init=int(subprocess.check_output(['lxc-info','-n','108','-pH'],text=True).strip())
 with tempfile.TemporaryDirectory(prefix='qsm-keycapture25-') as directory:
  bridge=BrowserWebRtcBridge(Path(directory),fps=15,expected_producer_uid=0,video_bitrate_kbps=2000)
  video,audio=bridge.start_taps()
  worker_path='/usr/lib/qsm-pve-direct/bin/qsm-direct-media-worker'
  worker=await asyncio.create_subprocess_exec(worker_path,'--wlroots-root',f'/proc/{init}/root','--wlroots-dir','run/qsm-display-1','--drop-privileges','nobody','--video-socket',video,'--audio-socket',audio,'--input-socket',bridge.input_context,'--codec','h264','--encoder','h264_nvenc','--fps','15','--video-bitrate-kbps','2000','--initial-size','1280x720',stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL)
  try:
   while True:
    line=await asyncio.to_thread(sys.stdin.readline)
    if not line:break
    c=json.loads(line);op=c['op']
    if op=='offer':r=await bridge.answer_offer(c['sdp'])
    elif op=='stats':
     r={'worker':worker.pid,'connection':bridge.connection_state}
     if hasattr(bridge,'_input_state'):
      state=bridge._input_state;r.update(leased=state.leased,snapshots=state.snapshots,expired=state.expired_keys,taps=state.taps,max_client_ms=state.max_client_hold_ms,max_hold_ms=state.max_hold_ms,clicks=state.clicks,expired_buttons=state.expired_buttons)
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
