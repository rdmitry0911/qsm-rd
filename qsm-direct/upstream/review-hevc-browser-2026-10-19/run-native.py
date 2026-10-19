import subprocess,json,time,sys,shlex,os,re
from pathlib import Path
BASE=Path('/tmp/qsm-hevc-browser');encoder=sys.argv[1];assert encoder in ('hevc_qsv','hevc_vaapi');label=sys.argv[2] if len(sys.argv)>2 else encoder;assert label.replace('-','').replace('_','').isalnum();out=BASE/label;out.mkdir(exist_ok=True)
ssh=['/tmp/qsm-intel-lab/ssh-pve']
def remote(cmd):return subprocess.check_output(ssh+[cmd],stderr=subprocess.PIPE,text=True)
def script(s):return subprocess.run(ssh+['python3 -'],input=s,stderr=subprocess.PIPE,stdout=subprocess.PIPE,text=True,check=True).stdout
policy='QSM_DIRECT_CODEC=hevc\nQSM_DIRECT_ENCODER_MODE=hardware\nQSM_DIRECT_ENCODER='+encoder+'\nQSM_DIRECT_BITRATE_KBPS=8000\n'+('QSM_DIRECT_VAAPI_RENDER_NODE=/dev/dri/renderD128\n' if encoder.endswith('_vaapi') else '')
script("from pathlib import Path\np=Path('/etc/qsm-pve-direct/containers.d/147.conf');original=Path('/root/qsm-hevc-browser/original-147.conf').read_text();p.write_text(original+"+repr(policy)+");p.chmod(0o600)\n")
remote('systemctl restart qsm-pve-direct-terminal')
ticket=remote("perl -MPVE::AccessControl -MPVE::Cluster -e 'PVE::Cluster::cfs_update(); print scalar PVE::AccessControl::assemble_ticket(q{root@pam})'").strip()
artifacts='/root/qsm-hevc-browser/'+label
command='env DISPLAY=:91 DEBUG=pw:browser QSM_CHROME_ANGLE='+shlex.quote(os.environ.get('QSM_CHROME_ANGLE','gl'))+' QSM_CHROME_EXTRA='+shlex.quote(os.environ.get('QSM_CHROME_EXTRA','[]'))+' QSM_TEST_ARTIFACTS='+artifacts+' /root/qsm-hevc-browser-venv/bin/python /root/native-hevc.py'
log=(out/'browser.log').open('w');proc=subprocess.Popen(ssh+[command],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,text=True)
def send(c):proc.stdin.write(json.dumps(c)+'\n');proc.stdin.flush()
try:
 proc.stdin.write(ticket+'\n');proc.stdin.flush();ticket=None
 for _ in range(75):
  time.sleep(1);s=(out/'browser.log').read_text()
  if 'NATIVE_VIDEO' in s:break
  if proc.poll() is not None:raise RuntimeError('Browser exited before video')
 else:raise RuntimeError('Native video timeout')
 print('NATIVE_VIDEO_READY',encoder,flush=True)
 send({'op':'sdp'})
 journal=remote('journalctl -u qsm-pve-direct-terminal -n 100 --no-pager');slots=re.findall(r'container media transport started vmid=147 console=([1-8]) codec=hevc encoder='+encoder,journal);assert slots,journal;slot=int(slots[-1]);print('SOURCE_CONSOLE',slot,flush=True)
 remote('pct exec 147 -- systemctl stop qsm-video-fixture || true')
 remote('pct exec 147 -- systemd-run --unit=qsm-video-fixture --collect --setenv=QSM_LAB_SLOT='+str(slot)+' /usr/bin/python3 /opt/qsm-lab/start-fixture.py')
 time.sleep(2)
 focus_code="from pathlib import Path; import subprocess,json; p=next(Path('/run/qsm-display-1').glob('sway-ipc*.sock')); r=subprocess.check_output(['/opt/qsm-native-sway/bin/swaymsg','-s',str(p),'[title=\"QSM continuous video transport fixture\"] focus'],text=True); assert all(x.get('success') for x in json.loads(r)),r"
 focus_code=focus_code.replace('/run/qsm-display-1','/run/qsm-display-'+str(slot))
 remote('pct exec 147 -- python3 -c '+shlex.quote(focus_code))
 remote('tc qdisc replace dev lo root handle 1: prio bands 3; tc qdisc add dev lo parent 1:3 handle 30: tbf rate 20mbit burst 12kb latency 100ms; tc filter add dev lo parent 1: protocol ip prio 10 u32 match ip protocol 17 0xff flowid 1:3; tc filter add dev lo parent 1: protocol ipv6 prio 11 u32 match ip6 protocol 17 0xff flowid 1:3')
 time.sleep(8);send({'op':'stats'});send({'op':'motion'})
 for _ in range(20):
  time.sleep(.5)
  if 'NATIVE_MOTION_PASS' in (out/'browser.log').read_text():break
 else:raise RuntimeError('Initial motion test failed')
 print('3 Mbps congestion',flush=True)
 remote('tc qdisc change dev lo parent 1:3 handle 30: tbf rate 3mbit burst 12kb latency 100ms')
 for _ in range(5):time.sleep(5);send({'op':'stats'})
 (out/'congestion-qdisc.txt').write_text(remote('tc -s qdisc show dev lo'))
 remote('tc qdisc change dev lo parent 1:3 handle 30: tbf rate 20mbit burst 12kb latency 100ms')
 print('20 Mbps recovery',flush=True)
 for _ in range(24):time.sleep(5);send({'op':'stats'})
 send({'op':'motion'});send({'op':'snapshot','name':encoder.replace('_','-')+'-recovered'});send({'op':'quit'});proc.stdin.close();code=proc.wait(timeout=25);assert code==0,code
 (out/'server.log').write_text(remote('journalctl -u qsm-pve-direct-terminal --since "5 minutes ago" --no-pager'))
 (out/'final-qdisc.txt').write_text(remote('tc -s qdisc show dev lo'))
 print('Native HEVC network test complete',encoder,flush=True)
finally:
 try:remote('tc qdisc del dev lo root')
 except Exception:pass
 if proc.poll() is None:
  try:send({'op':'quit'});proc.stdin.close();proc.wait(timeout=15)
  except Exception:proc.terminate()
