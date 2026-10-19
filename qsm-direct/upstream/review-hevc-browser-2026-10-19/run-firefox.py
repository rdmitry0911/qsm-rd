from pathlib import Path
import subprocess,time,json,sys
base=Path('/tmp/qsm-hevc-browser');mode=sys.argv[1];assert mode in ('reject','auto');out=base/('firefox-'+mode);out.mkdir(exist_ok=True)
ssh=['/tmp/qsm-intel-lab/ssh-pve'];ticket=subprocess.check_output(ssh+["perl -MPVE::AccessControl -MPVE::Cluster -e 'PVE::Cluster::cfs_update(); print scalar PVE::AccessControl::assemble_ticket(q{root@pam})'"],text=True,stderr=subprocess.PIPE).strip()
cmd='env QSM_TEST_BROWSER=firefox QSM_TEST_ORIGIN=https://127.0.0.1:18006 QSM_TEST_CODEC=video/H264 QSM_EXPECT_REJECT='+('1' if mode=='reject' else '0')+' QSM_TEST_ARTIFACTS=/var/tmp/qsm-intel-lab/hevc-firefox-'+mode+' PLAYWRIGHT_BROWSERS_PATH=/var/tmp/qsm-intel-lab/browsers MOZ_GMP_PATH=/var/tmp/qsm-intel-lab/gmp-gmpopenh264/2.6.0 /var/tmp/qsm-intel-lab/takt-roi-check-venv/bin/python /var/tmp/qsm-intel-lab/native-hevc.py'
with (out/'browser.log').open('w') as log:
 p=subprocess.Popen(['ssh','-o','BatchMode=yes','dima@172.16.16.200',cmd],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,text=True)
 p.stdin.write(ticket+'\n');p.stdin.flush();ticket=None
 if mode=='reject':p.stdin.close();code=p.wait(timeout=65);assert code==0,code;assert 'EXPECTED_HEVC_REJECT' in (out/'browser.log').read_text()
 else:
  for _ in range(65):
   time.sleep(1)
   if 'NATIVE_VIDEO' in (out/'browser.log').read_text():break
   if p.poll() is not None:raise RuntimeError('Firefox exited before native video')
  else:raise RuntimeError('Firefox native timeout')
  time.sleep(10)
  for c in ({'op':'sdp'},{'op':'stats'},{'op':'motion'},{'op':'quit'}):p.stdin.write(json.dumps(c)+'\n')
  p.stdin.flush();p.stdin.close();code=p.wait(timeout=20);assert code==0,code;assert 'NATIVE_MOTION_PASS' in (out/'browser.log').read_text()
print('Firefox',mode,'passed')
