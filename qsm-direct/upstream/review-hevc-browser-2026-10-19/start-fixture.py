import os,pwd
from pathlib import Path
account=pwd.getpwnam('qsm-display')
slot=int(os.environ.get('QSM_LAB_SLOT','1'));assert 1<=slot<=8
runtime=Path('/run/qsm-display-'+str(slot))
sockets=[p for p in runtime.glob('wayland-*') if p.is_socket()]
assert len(sockets)==1,sockets
env=dict(os.environ,QT_QPA_PLATFORM='wayland',WAYLAND_DISPLAY=sockets[0].name,XDG_RUNTIME_DIR=str(runtime))
os.initgroups(account.pw_name,account.pw_gid);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
os.execve('/usr/bin/python3',['python3','/opt/qsm-lab/qsm-video-motion.py'],env)
