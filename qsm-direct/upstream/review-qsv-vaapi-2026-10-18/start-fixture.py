import os,pwd
account=pwd.getpwnam('qsm-display')
env=dict(os.environ,QT_QPA_PLATFORM='wayland',WAYLAND_DISPLAY='wayland-1',XDG_RUNTIME_DIR='/run/qsm-display-1')
os.initgroups(account.pw_name,account.pw_gid);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
os.execve('/usr/bin/python3',['python3','/tmp/qsm-video-motion.py'],env)
