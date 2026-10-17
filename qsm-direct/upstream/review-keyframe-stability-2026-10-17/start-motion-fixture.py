import os,pwd,subprocess
pid=int(subprocess.check_output(['pgrep','-u','1000','-x','plasmashell']).decode().split()[0])
env=dict(x.split('=',1) for x in open(f'/proc/{pid}/environ','rb').read().decode().split('\0') if '=' in x)
env['QT_QPA_PLATFORM']='xcb'
p=pwd.getpwuid(1000)
os.initgroups(p.pw_name,p.pw_gid);os.setgid(p.pw_gid);os.setuid(p.pw_uid)
os.execve('/usr/bin/python3',['python3','/tmp/qsm-video-motion.py'],env)
