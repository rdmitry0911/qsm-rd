import asyncio,json,uuid
from guest_exec import user_bus
async def start():
 base='/run/user/1000/qsm-layout-signals-'+uuid.uuid4().hex
 code='''import dbus,json,time
from pathlib import Path
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
base=BASE;DBusGMainLoop(set_as_default=True);bus=dbus.SessionBus();events=[]
def save(done=False):Path(base+'.json').write_text(json.dumps({'events':events,'done':done}))
def changed(n):events.append({'event':'layoutChanged','index':int(n)});save()
def shortcut(*args):events.append({'event':'shortcut','args':[str(x) for x in args]});save()
bus.add_signal_receiver(changed,dbus_interface='org.kde.KeyboardLayouts',signal_name='layoutChanged',path='/Layouts')
bus.add_signal_receiver(shortcut,dbus_interface='org.kde.kglobalaccel.Component',signal_name='globalShortcutPressed',path='/component/KDE_Keyboard_Layout_Switcher')
loop=GLib.MainLoop()
def stop():save(True);loop.quit();return False
save();Path(base+'.ready').write_text('ready');GLib.timeout_add(2500,stop);loop.run()
'''.replace('BASE',repr(base))
 launch="import subprocess,time;from pathlib import Path;p=subprocess.Popen(['/usr/bin/python3','-c',%r],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);deadline=time.monotonic()+2\nwhile not Path(%r+'.ready').exists() and time.monotonic()<deadline:time.sleep(.01)\nassert Path(%r+'.ready').exists();print(p.pid)"%(code,base,base)
 await asyncio.to_thread(user_bus,['/usr/bin/python3','-c',launch]);return base
async def finish(base):
 code="import json,time;from pathlib import Path;p=Path(%r+'.json');deadline=time.monotonic()+4\nwhile time.monotonic()<deadline:\n r=json.loads(p.read_text())\n if r['done']:break\n time.sleep(.02)\nassert r['done'];p.unlink();Path(%r+'.ready').unlink();print(json.dumps(r))"%(base,base)
 return json.loads(await asyncio.to_thread(user_bus,['/usr/bin/python3','-c',code]))
