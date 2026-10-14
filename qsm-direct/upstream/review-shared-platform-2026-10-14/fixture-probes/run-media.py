from pathlib import Path
import subprocess, shlex, threading
ssh=['ssh','-o','BatchMode=yes','-o','UserKnownHostsFile=/tmp/qsm-shared-platform/lab-known-hosts','-p','12222','root@127.0.0.1']
cmd=shlex.join(['ssh','-i','/root/.ssh/qsm-browser-gate','root@192.168.76.2', 'node /opt/qsm-browser/probe-media.cjs'])
p=subprocess.Popen(ssh+[cmd],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
guest=['ssh','-o','BatchMode=yes','-o','UserKnownHostsFile=/tmp/qsm-shared-platform/lab-known-hosts','-o','ProxyCommand=ssh -o BatchMode=yes -o UserKnownHostsFile=/tmp/qsm-shared-platform/lab-known-hosts -p 12222 root@127.0.0.1 -W %h:%p','root@192.168.76.6']
record=None
with open('/tmp/qsm-shared-platform/browser-media.log','w') as log:
 for line in p.stdout:
  log.write(line);log.flush();print(line.strip(),flush=True)
  if line.startswith('CLIPBOARD_COPY_WAITING'):
   subprocess.run(guest+['DISPLAY=:99 xclip -selection clipboard -in >/dev/null 2>&1 <<<shared-guest-clipboard-copy'],check=True)
  if line.startswith('MICROPHONE_ENABLED'):
   g='DISPLAY=:99 xclip -selection clipboard -in >/dev/null 2>&1 <<<shared-guest-clipboard; aplay -q -D hw:0,0 /tmp/shared-tone.wav >/tmp/shared-aplay.log 2>&1 & arecord -q -D hw:0,0 -f S16_LE -r 48000 -c2 -d 5 /tmp/shared-mic.wav'
   record=subprocess.Popen(guest+[g],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
 if record:
  output=record.communicate(timeout=10)[0];print('GUEST_RECORD_EXIT',record.returncode,flush=True)
 print('BROWSER_EXIT',p.wait(),flush=True)
 r=subprocess.run(guest+['DISPLAY=:99 timeout 10 xclip -out -selection clipboard'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 print('GUEST_PASTE_MATCH',r.returncode==0 and r.stdout.strip()=='shared-browser-clipboard',flush=True)
