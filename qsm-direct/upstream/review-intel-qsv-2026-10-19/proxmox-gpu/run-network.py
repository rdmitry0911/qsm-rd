import subprocess,json,time,sys,shlex
from pathlib import Path
base=Path('/tmp/qsm-intel-lab');enc=sys.argv[1];assert enc in ('h264_qsv','h264_vaapi')
label=sys.argv[2] if len(sys.argv)>2 else enc;assert label.replace('-','').replace('_','').isalnum();out=base/'gpu'/label;out.mkdir(exist_ok=True)
ssh=[str(base/'ssh-pve')]
def remote(cmd):return subprocess.check_output(ssh+[cmd],stderr=subprocess.PIPE,text=True)
def phase(label):print(label,flush=True)
policy="from pathlib import Path; p=Path('/etc/qsm-pve-direct/containers.d/147.conf'); original=Path('/root/qsm-intel-gpu-20261019/original-147.conf').read_text(); p.write_text(original+'QSM_DIRECT_CODEC=h264\\nQSM_DIRECT_ENCODER_MODE=hardware\\nQSM_DIRECT_ENCODER="+enc+"\\nQSM_DIRECT_BITRATE_KBPS=8000\\n"+("QSM_DIRECT_VAAPI_RENDER_NODE=/dev/dri/renderD128\\n" if enc.endswith('_vaapi') else '')+"');p.chmod(0o600)"
remote('python3 -c '+shlex.quote(policy))
remote('systemctl restart qsm-pve-direct-terminal')
subprocess.run(ssh+['bash'],input=(base/'gpu/shape.sh').read_text(),text=True,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
ticket=remote("perl -MPVE::AccessControl -MPVE::Cluster -e 'PVE::Cluster::cfs_update(); print scalar PVE::AccessControl::assemble_ticket(q{root@pam})'").strip()
remote_artifacts='/var/tmp/qsm-intel-lab/browser-'+label
subprocess.run(['ssh','-o','BatchMode=yes','dima@172.16.16.200','mkdir -p '+remote_artifacts],check=True)
command='env QSM_LAB_VMID=147 QSM_TEST_ACCOUNT=none QSM_LAB_BROWSER=firefox QSM_LAB_ARTIFACTS='+remote_artifacts+' PLAYWRIGHT_BROWSERS_PATH=/var/tmp/qsm-intel-lab/browsers MOZ_GMP_PATH=/var/tmp/qsm-intel-lab/gmp-gmpopenh264/2.6.0 /var/tmp/qsm-intel-lab/takt-roi-check-venv/bin/python /var/tmp/qsm-intel-lab/native-ui.py'
log=(out/'browser.log').open('w');proc=subprocess.Popen(['ssh','-o','BatchMode=yes','dima@172.16.16.200',command],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,text=True)
def send(c):proc.stdin.write(json.dumps(c)+'\n');proc.stdin.flush()
try:
 proc.stdin.write(ticket+'\n');proc.stdin.flush();ticket=None
 for _ in range(60):
  time.sleep(1);s=(out/'browser.log').read_text()
  if 'NATIVE_VIDEO ' in s:break
  if proc.poll() is not None:raise RuntimeError('Browser exited before video')
 else:raise RuntimeError('Native video timeout')
 remote('pct exec 147 -- systemctl stop qsm-video-fixture || true')
 remote('pct exec 147 -- systemd-run --unit=qsm-video-fixture --collect /usr/bin/python3 /opt/qsm-lab/start-fixture.py')
 time.sleep(2)
 focus_code = "from pathlib import Path; import subprocess,json; p=next(Path('/run/qsm-display-1').glob('sway-ipc*.sock')); r=subprocess.check_output(['/opt/qsm-native-sway/bin/swaymsg','-s',str(p),'[title=\"QSM continuous video transport fixture\"] focus'],text=True); assert all(x.get('success') for x in json.loads(r)),r"
 remote('pct exec 147 -- python3 -c '+shlex.quote(focus_code))

 phase('20 Mbps path / 8 Mbps ceiling');time.sleep(8);send({'op':'receiver_stats'});send({'op':'native_motion'})
 for _ in range(20):
  time.sleep(.5)
  if 'NATIVE_MOTION_PASS' in (out/'browser.log').read_text():break
 else:raise RuntimeError('Initial motion test did not complete')
 remote('tc qdisc change dev eth0 parent 1:3 handle 30: tbf rate 3mbit burst 12kb latency 100ms')
 phase('3 Mbps congestion')
 for _ in range(5):time.sleep(5);send({'op':'receiver_stats'})
 remote('tc qdisc change dev eth0 parent 1:3 handle 30: tbf rate 20mbit burst 12kb latency 100ms')
 phase('20 Mbps recovery')
 for _ in range(24):time.sleep(5);send({'op':'receiver_stats'})
 send({'op':'native_motion'});send({'op':'snapshot','name':enc.replace('_','-')+'-recovered'});send({'op':'quit'});proc.stdin.close();code=proc.wait(timeout=20)
 assert code==0,code
 (out/'server.log').write_text(remote('journalctl -u qsm-pve-direct-terminal --since "5 minutes ago" --no-pager'))
 phase('Native shaped network test complete')
finally:
 try:remote('tc qdisc del dev eth0 root')
 except Exception:pass
 if proc.poll() is None:
  try:send({'op':'quit'});proc.stdin.close();proc.wait(timeout=15)
  except Exception:proc.terminate()
