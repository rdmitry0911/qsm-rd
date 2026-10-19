import json,socket,time
q=socket.socket(socket.AF_UNIX);q.connect('/var/tmp/qsm-intel-lab/qmp.sock');f=q.makefile('rwb',buffering=0)
def command(name, arguments=None):
    f.write((json.dumps({'execute':name,**({'arguments':arguments} if arguments else {})})+'\r\n').encode())
    while True:
        r=json.loads(f.readline())
        if 'return' in r:return r['return']
        if 'error' in r:raise RuntimeError(r['error'])
json.loads(f.readline());command('qmp_capabilities')
reply=command('human-monitor-command',{'command-line':'hostfwd_add udp:127.0.0.1:18010-:50000'})
assert not reply,reply
try:
    u=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    for _ in range(5):u.sendto(b'QSM owned firewall negative test',('127.0.0.1',18010));time.sleep(.05)
    ssh=socket.create_connection(('127.0.0.1',12222),timeout=3);ssh.settimeout(3)
    try:
        value=ssh.recv(256)
        assert not value,'new SSH unexpectedly passed firewall'
    except TimeoutError:pass
    ssh.close();print(json.dumps({'udp_probes':5,'new_ssh_blocked':True}))
finally:
    reply=command('human-monitor-command',{'command-line':'hostfwd_remove udp:127.0.0.1:18010'});assert 'removed' in reply,reply
    q.close()
