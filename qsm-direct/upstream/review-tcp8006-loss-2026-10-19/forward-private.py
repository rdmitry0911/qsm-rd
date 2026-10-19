import json,socket,sys
q=socket.socket(socket.AF_UNIX);q.connect('/var/tmp/qsm-intel-lab/qmp.sock');f=q.makefile('rwb',buffering=0)
json.loads(f.readline())
def cmd(line):
    f.write((json.dumps(line)+'\r\n').encode())
    while True:
        r=json.loads(f.readline())
        if 'return' in r:return r['return']
        if 'error' in r:raise RuntimeError(r['error'])
cmd({'execute':'qmp_capabilities'})
text='hostfwd_add tcp:172.31.252.1:18006-:8006' if sys.argv[1]=='add' else 'hostfwd_remove tcp:172.31.252.1:18006'
r=cmd({'execute':'human-monitor-command','arguments':{'command-line':text}})
assert not r if sys.argv[1]=='add' else 'removed' in r,r
q.close();print('OWNED_PRIVATE_FORWARD_'+sys.argv[1].upper())
