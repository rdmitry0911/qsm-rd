import json,subprocess,sys,urllib.request,urllib.error,ssl
sys.path.insert(0,'/usr/lib/qsm-pve-direct')
from signal_service.qsm_gateway import read_gateway

def run(*args):return subprocess.check_output(args,text=True).strip()
users=('qsm-8006-console@pve','qsm-8006-audit@pve')
roles=('QSM8006Console','QSM8006Audit')
# Owned names must be absent; never change an existing user's ACL.
existing=json.loads(run('pveum','user','list','--output-format','json'))
assert not set(users)&{x['userid'] for x in existing}
results={}
try:
 for user,role,priv in zip(users,roles,('VM.Console','VM.Audit')):
  run('pveum','role','add',role,'--privs',priv)
  run('pveum','user','add',user)
  run('pveum','acl','modify','/vms/148','--users',user,'--roles',role)
  ticket=run('perl','-MPVE::AccessControl','-e',f'print PVE::AccessControl::assemble_ticket("{user.replace("@",chr(92)+"@")}")')
  request=urllib.request.Request('https://'+read_gateway()['relay_ipv4']+':8006/qsm-direct/nodes/qsm-intel-pve/qemu/148/qsm-direct-ice',headers={'Authorization':'Bearer '+ticket,'X-QSM-User':user})
  try:
   with urllib.request.urlopen(request,context=ssl._create_unverified_context(),timeout=10) as response:
    status=response.status;data=json.load(response)['data'];assert set(data)=={'username','credential','ttl'}
  except urllib.error.HTTPError as e:status=e.code
  results[priv]=status
 assert results=={'VM.Console':200,'VM.Audit':401},results
 print(json.dumps(results))
finally:
 for user,role in zip(users,roles):
  subprocess.run(['pveum','user','delete',user],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  subprocess.run(['pveum','role','delete',role],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
