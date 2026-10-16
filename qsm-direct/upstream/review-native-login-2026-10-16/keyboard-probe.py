#!/usr/bin/env python3
"""CT108-only native layout upload/access probe; see layout-and-ct105.txt."""
import json, os, socket, subprocess, sys, time
sys.path.insert(0,'/usr/lib/qsm-console')
from qsm_native_keyboard import NativeKeyboard, sway

def keyboards():
    return [x for x in sway(1,100) if x.get('type')=='keyboard']

first=NativeKeyboard(1)
assert first.selected()=='ru'
assert NativeKeyboard(2).selected()=='us'
process=subprocess.Popen(['/usr/lib/qsm-console/qsm-seat-keeper','/run/qsm-display-1'],
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
    deadline=time.monotonic()+5
    while True:
        entries=keyboards()
        if len(entries)==3 and all(k.get('xkb_layout_names') for k in entries): break
        assert time.monotonic()<deadline, 'new virtual input failed to appear'
        time.sleep(.05)
    assert any(k['xkb_layout_names']==['English (US)'] for k in entries)
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as connection:
        connection.settimeout(10)
        connection.connect('/run/qsm-login/1/control')
        connection.sendall(b'{"op":"input_ready"}\n')
        assert json.loads(connection.recv(4096))['ok'] is True
    assert all(k['xkb_layout_names']==['Russian'] for k in keyboards())
    assert first.selected()=='ru' and NativeKeyboard(2).selected()=='us'
    print('PASS: uploaded US map restored to Russian on all three keyboards; second seat stays US')
finally:
    process.terminate();process.wait(timeout=3)
child=os.fork()
if child==0:
    os.setgroups([]);os.setgid(1000);os.setuid(1000)
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.connect('/run/qsm-native/keyboard-1.sock')
    except PermissionError:os._exit(0)
    os._exit(1)
assert os.waitpid(child,0)[1]==0
print('PASS: normal desktop UID denied keyboard control socket')
