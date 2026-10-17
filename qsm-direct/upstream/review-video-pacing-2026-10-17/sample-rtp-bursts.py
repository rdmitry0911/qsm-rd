import socket,subprocess,time,struct,json,collections,statistics
pid=subprocess.check_output(['systemctl','show','qsm-pve-direct-terminal','--property=MainPID','--value'],text=True).strip()
ports=set()
for line in subprocess.check_output(['ss','-uanp'],text=True).splitlines():
 if f'pid={pid},' in line:
  try:p=int(line.split()[3].rsplit(':',1)[1])
  except ValueError:continue
  if p!=5353:ports.add(p)
s=socket.socket(socket.AF_PACKET,socket.SOCK_DGRAM,socket.htons(3));s.bind(('vmbr0',0));s.settimeout(.5)
start=time.monotonic();end=start+15;frames={};bins=collections.Counter();sizes=[]
while time.monotonic()<end:
 try:data,address=s.recvfrom(65535)
 except TimeoutError:continue
 if len(data)<40 or data[0]>>4!=4 or data[9]!=17:continue
 off=(data[0]&15)*4
 if struct.unpack_from('!H',data,off)[0] not in ports:continue
 rtp=data[off+8:]
 if len(rtp)<12 or rtp[0]>>6!=2 or rtp[1]&127!=105:continue
 now=time.monotonic();ts=struct.unpack_from('!I',rtp,4)[0];size=len(data);sizes.append(size);bins[int((now-start)*1000)]+=size
 entry=frames.setdefault(ts,{'first':now,'last':now,'bytes':0,'packets':0});entry['last']=now;entry['bytes']+=size;entry['packets']+=1
largest=sorted(frames.values(),key=lambda x:x['bytes'],reverse=True)[:6]
print(json.dumps({'duration_s':15,'packets':len(sizes),'mbps':sum(sizes)*8/15e6,'largest_frames':[{'bytes':f['bytes'],'packets':f['packets'],'send_span_ms':round((f['last']-f['first'])*1000,3)} for f in largest],'max_bytes_per_1ms':max(bins.values(),default=0)}))
