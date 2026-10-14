import subprocess,shlex
ssh=['ssh','-o','BatchMode=yes','-o','UserKnownHostsFile=/tmp/qsm-shared-platform/lab-known-hosts','-p','12222','root@127.0.0.1']
cmd=shlex.join(['ssh','-i','/root/.ssh/qsm-browser-gate','root@192.168.76.2','node /opt/qsm-browser/probe-gl-concurrent.cjs'])
p=subprocess.Popen(ssh+[cmd],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
probe=None
with open('/tmp/qsm-shared-platform/concurrent-browser.log','w') as log:
 for line in p.stdout:
  log.write(line);log.flush();print(line.strip(),flush=True)
  if line.startswith('BROWSER_VIDEO_READY'):
   out=open('/tmp/qsm-shared-platform/second-consumer.log','w')
   probe=subprocess.Popen(ssh+['/root/shared-display-probe --dbus-address unix:path=/run/qemu-server/102.dbusdisplay --destination org.qemu --duration-ms 4000 --fps 30 --no-audio --snapshot /root/shared-second-consumer.ppm'],stdout=out,stderr=subprocess.STDOUT)
 print('BROWSER_EXIT',p.wait(),flush=True)
 if probe:print('SECOND_CONSUMER_EXIT',probe.wait(),flush=True)
