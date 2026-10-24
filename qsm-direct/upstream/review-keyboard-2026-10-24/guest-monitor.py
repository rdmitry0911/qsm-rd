import os,fcntl,struct,select,time,json,glob
fds={}; info=[]; held={}; durations=[]; repeats=press=release=unknown_up=0
for path in glob.glob('/dev/input/event*'):
 fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
 buf=bytearray(256)
 try:fcntl.ioctl(fd,0x81004506,buf);name=bytes(buf).split(bytes([0]))[0].decode(errors='replace')
 except OSError:name='unknown'
 fds[fd]=name;info.append(name)
 # Only count held keys; do not persist any identities or text.
 bits=bytearray(96)
 try:fcntl.ioctl(fd,0x80604518,bits);initial=sum(int(x).bit_count() for x in bits)
 except OSError:initial=-1
 info[-1]={'device':name,'initial_held':initial}
os.umask(0o077)
end=time.monotonic()+600;fmt=struct.Struct('llHHi')
while time.monotonic()<end:
 ready,_,_=select.select(list(fds),[],[],min(1,end-time.monotonic()))
 for fd in ready:
  try:data=os.read(fd,fmt.size*128)
  except BlockingIOError:continue
  for i in range(0,len(data)-fmt.size+1,fmt.size):
   sec,usec,typ,code,value=fmt.unpack_from(data,i)
   if typ!=1 or code>=256:continue
   ident=(fd,code);t=sec+usec/1e6
   if value==1:press+=1;held[ident]=t
   elif value==0:
    release+=1
    if ident in held:durations.append(round((t-held.pop(ident))*1000,2))
    else:unknown_up+=1
   elif value==2:repeats+=1
 snapshot={'scope':'evdev aggregates; no key identities or text','press':press,'release':release,'repeat_events':repeats,'still_held':len(held),'completed_hold_ms':sorted(durations),'captured_at':time.time()}
 with open('/run/qsm-keyboard-diag-20261024.json.new','w') as f:json.dump(snapshot,f)
 os.replace('/run/qsm-keyboard-diag-20261024.json.new','/run/qsm-keyboard-diag-20261024.json')
for fd in fds:os.close(fd)
print(json.dumps({'devices':info,'duration_seconds':600,'press':press,'release':release,'repeat_events':repeats,'unmatched_release':unknown_up,'still_held':len(held),'completed_hold_ms':sorted(durations)},indent=2))
