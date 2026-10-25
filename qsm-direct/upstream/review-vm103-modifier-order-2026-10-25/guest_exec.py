import subprocess,shlex,json

def guest_exec(args):
 p=subprocess.run(['ssh','-o','BatchMode=yes','root@192.168.64.20',shlex.join(['qm','guest','exec','103','--',*args])],check=True,capture_output=True,text=True)
 r=json.loads(p.stdout)
 if not r.get('exited') or r.get('exitcode')!=0:raise RuntimeError(r)
 return r.get('out-data','')

def user_bus(args):
 return guest_exec(['runuser','-u','dima','--','env','XDG_RUNTIME_DIR=/run/user/1000','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',*args])
if __name__=='__main__':
 print(user_bus(['busctl','--user','introspect','org.kde.keyboard','/Layouts']))
