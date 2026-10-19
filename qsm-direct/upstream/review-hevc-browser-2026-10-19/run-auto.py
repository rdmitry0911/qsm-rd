from pathlib import Path
import subprocess,time,json,sys,re,shlex
base=Path('/tmp/qsm-hevc-browser');kind=sys.argv[1];assert kind in ('chrome','firefox');label=kind+'-auto';out=base/label;out.mkdir(exist_ok=True);ssh=['/tmp/qsm-intel-lab/ssh-pve']
def remote(c):return subprocess.check_output(ssh+[c],text=True,stderr=subprocess.PIPE)
remote('cp /root/qsm-hevc-browser/original-147.conf /etc/qsm-pve-direct/containers.d/147.conf; systemctl restart qsm-pve-direct-terminal')
ticket=remote("perl -MPVE::AccessControl -MPVE::Cluster -e 'PVE::Cluster::cfs_update(); print scalar PVE::AccessControl::assemble_ticket(q{root@pam})'").strip()
if kind=='chrome':
 prefix=ssh;cmd='env DISPLAY=:91 QSM_CHROME_ANGLE=vulkan QSM_CHROME_EXTRA='+shlex.quote(json.dumps(['--enable-features=Vulkan,DefaultANGLEVulkan,VulkanFromANGLE,AcceleratedVideoDecodeLinuxGL,PlatformHEVCDecoderSupport,WebRtcAllowH265Receive']))+' QSM_TEST_ARTIFACTS=/root/qsm-hevc-browser/'+label+' /root/qsm-hevc-browser-venv/bin/python /root/native-hevc.py'
else:
 prefix=['ssh','-o','BatchMode=yes','dima@172.16.16.200'];cmd='env QSM_TEST_BROWSER=firefox QSM_TEST_ORIGIN=https://127.0.0.1:18006 QSM_TEST_CODEC=video/H264 QSM_TEST_ARTIFACTS=/var/tmp/qsm-intel-lab/hevc-firefox-auto PLAYWRIGHT_BROWSERS_PATH=/var/tmp/qsm-intel-lab/browsers MOZ_GMP_PATH=/var/tmp/qsm-intel-lab/gmp-gmpopenh264/2.6.0 /var/tmp/qsm-intel-lab/takt-roi-check-venv/bin/python /var/tmp/qsm-intel-lab/native-hevc.py'
log=(out/'browser.log').open('w');p=subprocess.Popen(prefix+[cmd],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,text=True)
def send(c):p.stdin.write(json.dumps(c)+'\n');p.stdin.flush()
try:
 p.stdin.write(ticket+'\n');p.stdin.flush();ticket=None
 for _ in range(75):
  time.sleep(1)
  if 'NATIVE_VIDEO' in (out/'browser.log').read_text():break
  if p.poll() is not None:raise RuntimeError('Browser exited before video')
 else:raise RuntimeError('Native video timeout')
 journal=remote('journalctl -u qsm-pve-direct-terminal -n 100 --no-pager');found=re.findall(r'container media transport started vmid=147 console=([1-8]) codec=(hevc|h264) encoder=(\w+)',journal);assert found,journal;slot,codec,encoder=found[-1];assert codec==('hevc' if kind=='chrome' else 'h264'),found
 print('AUTO_ENCODER',kind,slot,codec,encoder,flush=True)
 remote('pct exec 147 -- systemctl stop qsm-video-fixture || true');remote('pct exec 147 -- systemd-run --unit=qsm-video-fixture --collect --setenv=QSM_LAB_SLOT='+slot+' /usr/bin/python3 /opt/qsm-lab/start-fixture.py');time.sleep(2)
 focus="from pathlib import Path; import subprocess,json; p=next(Path('/run/qsm-display-"+slot+"').glob('sway-ipc*.sock')); r=subprocess.check_output(['/opt/qsm-native-sway/bin/swaymsg','-s',str(p),'[title=\"QSM continuous video transport fixture\"] focus'],text=True); assert all(x.get('success') for x in json.loads(r)),r"
 remote('pct exec 147 -- python3 -c '+shlex.quote(focus));time.sleep(10)
 for c in ({'op':'sdp'},{'op':'stats'},{'op':'motion'},{'op':'quit'}):send(c)
 p.stdin.close();code=p.wait(timeout=25);assert code==0,code;assert 'NATIVE_MOTION_PASS' in (out/'browser.log').read_text()
 (out/'server.log').write_text(remote('journalctl -u qsm-pve-direct-terminal --since "2 minutes ago" --no-pager'))
 print('Automatic codec test passed',kind,flush=True)
finally:
 if p.poll() is None:p.terminate()
