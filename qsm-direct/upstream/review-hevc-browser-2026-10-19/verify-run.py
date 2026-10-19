#!/usr/bin/env python3
import json,re,sys
from pathlib import Path
root=Path(sys.argv[1]);raw_rows=[json.loads(x) for x in (root/'artifacts/receiver-stats.jsonl').read_text().splitlines()];log=(root/'browser.log').read_text()
# A reused artifact directory can retain rows of a discarded fixture run.
# The current browser log defines this run; require its exact raw-log suffix.
rows=[json.loads(x.split(' ',1)[1]) for x in log.splitlines() if x.startswith('RECEIVER_STATS ')]
assert raw_rows[-len(rows):]==rows
assert len(rows)>=25
assert all(r['mimeType']=='video/H265' and r['connection']=='connected' for r in rows)
assert len({(r['id'],r['ssrc'],r['trackIdentifier']) for r in rows})==1
assert rows[-1]['framesDecoded']-rows[0]['framesDecoded']>5000
assert rows[-1]['framesDropped']==0
assert rows[-1]['videoRateStatus']=='Adaptive video: 8.00 / 8.00 Mbit/s'
rates=[float(re.search(r': ([0-9.]+) /',r['videoRateStatus']).group(1)) for r in rows]
assert min(rates)<3
motions=[json.loads(x.split(' ',1)[1]) for x in log.splitlines() if x.startswith('NATIVE_MOTION_PASS ')]
assert len(motions)==2 and all(x['changed']>=16 for x in motions)
assert 'NO_PORT_8007_REQUESTS' in log
assert 'after a crash' not in log and 'BROWSER_ERROR' not in log
assert any(int(v)>0 for v in re.findall(r'dropped (\d+)',(root/'congestion-qdisc.txt').read_text()))
proof=(root/'decoder-gpu-proof.txt').read_text();assert 'CHROME_IHD_PROCESS' in proof
video_times=[int(x) for x in re.findall(r'drm-engine-video:\s+(\d+) ns',proof)]
assert len(set(x for x in video_times if x>0))>=2
print(json.dumps({'pass':True,'prior_attempt_rows_in_raw_log':len(raw_rows)-len(rows),'final':{k:rows[-1].get(k) for k in ['mimeType','framesDecoded','framesReceived','framesDropped','packetsLost','freezeCount','totalFreezesDuration','keyFramesDecoded','pliCount','nackCount','videoRateStatus']},'minimum_target_mbps':min(rates),'target_sequence':list(dict.fromkeys(rates)),'motion':motions,'same_inbound_stream':True,'chrome_gpu_crashes':0,'hardware_decode_video_engine_ns_samples':video_times},indent=2))
