import subprocess,json, pathlib
root=pathlib.Path('/root/qsm-native-seat-lab/video-pacing');root.mkdir(exist_ok=True)
base=['ffmpeg','-hide_banner','-loglevel','error','-filter_threads','1','-f','lavfi','-i','testsrc2=size=1782x1118:rate=60','-frames:v','240','-an','-c:v','h264_nvenc','-preset','p1','-tune','ll','-zerolatency','1','-delay','0','-rc-lookahead','0','-rc','vbr','-g','30','-bf','0','-aud','1','-pix_fmt','yuv420p']
for name, options in [('cq19',['-cq','19','-b:v','8M','-maxrate','20M','-bufsize','333k']),('vbv8',['-b:v','8M','-maxrate','8M','-bufsize','133333']),('vbv4',['-b:v','4M','-maxrate','4M','-bufsize','66667'])]:
 path=root/(name+'.h264');subprocess.run(base+options+['-y',str(path)],check=True)
 result=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_packets','-select_streams','v','-show_entries','packet=size,flags','-of','json',str(path)]))['packets']
 sizes=[int(p['size']) for p in result];keys=[int(p['size']) for p in result if 'K' in p['flags']]
 print(json.dumps(dict(name=name,frames=len(sizes),mbps=sum(sizes)*8/4e6,max_frame_bytes=max(sizes),keyframe_bytes=keys)),flush=True)
