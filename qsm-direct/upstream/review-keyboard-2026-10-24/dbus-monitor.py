import subprocess,selectors,time,re,json,os
os.umask(0o077)
p=subprocess.Popen(['dbus-monitor','--address','unix:path=/run/qemu-server/103.dbusdisplay',"type='method_call',interface='org.qemu.Display1.Keyboard'"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
sel=selectors.DefaultSelector();sel.register(p.stdout,selectors.EVENT_READ)
held={};durations=[];press=release=duplicate=unknown=0;pending=None;end=time.monotonic()+600; buffered=""
try:
 while time.monotonic()<end and p.poll() is None:
  if not sel.select(min(.5,end-time.monotonic())):continue
  chunk=os.read(p.stdout.fileno(),65536)
  if not chunk:break
  buffered+=chunk.decode()
  while '\n' in buffered:
   line,buffered=buffered.split('\n',1)
   m=re.search(r'method call time=([0-9.]+).*member=(Press|Release)',line)
   if m:pending=(float(m[1]),m[2]);continue
   m=re.search(r'uint32 (\d+)',line)
   if not m or pending is None:continue
   key=int(m[1]);t,op=pending;pending=None
   if op=='Press':
    press+=1
    if key in held:duplicate+=1
    held[key]=t
   else:
    release+=1
    if key in held:durations.append(round((t-held.pop(key))*1000,2))
    else:unknown+=1
  snapshot={'scope':'D-Bus aggregates; no key identities or text','press':press,'release':release,'duplicate_press':duplicate,'still_held':len(held),'completed_hold_ms':sorted(durations),'captured_at':time.time()}
  with open('/run/qsm-keyboard-diag-20261024.json.new','w') as f:json.dump(snapshot,f)
  os.replace('/run/qsm-keyboard-diag-20261024.json.new','/run/qsm-keyboard-diag-20261024.json')
finally:
 p.terminate()
 try:p.wait(timeout=2)
 except subprocess.TimeoutExpired:p.kill();p.wait()
print(json.dumps({'scope':'QEMU D-Bus input edges only; no key identities retained','duration_seconds':600,'press':press,'release':release,'duplicate_press':duplicate,'unmatched_release':unknown,'still_held':len(held),'hold_ms':sorted(durations),'monitor_stderr':p.stderr.read().decode()[:500]},indent=2))
