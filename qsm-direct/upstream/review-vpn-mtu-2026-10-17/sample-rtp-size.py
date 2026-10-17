import socket,subprocess,time,struct,json,re
pid=subprocess.check_output(['systemctl','show','qsm-pve-direct-terminal','--property=MainPID','--value'],text=True).strip()
ss=subprocess.check_output(['ss','-uanp'],text=True)
ports=set()
for line in ss.splitlines():
 if f'pid={pid},' in line:
  local=line.split()[3]
  try:port=int(local.rsplit(':',1)[1])
  except ValueError:continue
  if port!=5353:ports.add(port)
s=socket.socket(socket.AF_PACKET,socket.SOCK_DGRAM,socket.htons(3));s.bind(('vmbr0',0));s.settimeout(.5)
end=time.monotonic()+10;lengths=[];fragments=0
while time.monotonic()<end:
 try:data,address=s.recvfrom(65535)
 except TimeoutError:continue
 if len(data)<28 or data[0]>>4!=4 or data[9]!=17:continue
 offset=(data[0]&15)*4
 frag=struct.unpack_from('!H',data,6)[0]
 if frag&0x1fff:continue
 source_port=struct.unpack_from('!H',data,offset)[0]
 if source_port not in ports:continue
 lengths.append(struct.unpack_from('!H',data,2)[0]);fragments+=bool(frag&0x2000)
print(json.dumps({'packets':len(lengths),'max_ip_packet_bytes':max(lengths,default=0),'above_1300':sum(x>1300 for x in lengths),'fragmented_first_packets':fragments,'duration_seconds':10}))
