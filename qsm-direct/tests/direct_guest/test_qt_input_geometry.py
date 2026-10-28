#!/usr/bin/python3
"""Geometry-only schema and real private D-Bus owner/lifetime checks."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'guest'))
from qsm_qt_input_geometry import parse_geometry, valid_publisher


def geometry():
    return {'version': 1, 'sequence': 1, 'window': 'a'*32, 'enabled': True,
            'size': [500, 400], 'rect': [11, 70, 1, 18], 'clip': [0,0,500,400]}


class GeometrySchemaTest(unittest.TestCase):
    def test_bounded_geometry_and_disabled_state(self):
        value = geometry()
        self.assertEqual(parse_geometry(json.dumps(value)), value)
        value.update(enabled=False, window='', size=[0, 0], rect=[0, 0, 0, 0], clip=[0,0,0,0])
        self.assertEqual(parse_geometry(json.dumps(value)), value)

    def test_no_text_unknown_fields_duplicates_or_unbounded_values(self):
        for key, bad in [('text', 'private'), ('title', 'private'), ('version', True),
                         ('sequence', 2**53), ('sequence', 0), ('window', 'not-a-window'),
                         ('size', [False, 400]), ('rect', [-1, 0, 1, 18]),
                         ('rect', [499, 0, 2, 18]), ('rect', [0, 0, 1, float('nan')]),
                         ('rect', [0, 0, 0, 18]), ('clip', [12,70,10,18]), ('clip', [0,0,501,400])]:
            value = geometry(); value[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                parse_geometry(json.dumps(value))
        with self.assertRaises(ValueError):
            parse_geometry('{"version":1,"version":1}')
        with self.assertRaises(ValueError):
            parse_geometry(' '*1025)

    def test_service_name_is_not_process_authentication(self):
        self.assertTrue(valid_publisher({'ProcessID': 42, 'UnixUserID': 1000}, 42, 1000))
        for credentials in ({'ProcessID': 43, 'UnixUserID': 1000},
                            {'ProcessID': 42, 'UnixUserID': 0},
                            {'ProcessID': True, 'UnixUserID': 1000}, {}, None):
            self.assertFalse(valid_publisher(credentials, 42, 1000))


class GeometryBusTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('dbus-run-session') and importlib.util.find_spec('gi'), 'Gio and private D-Bus required')
    def test_real_owner_bounds_generations_lock_expiry_and_late_reply(self):
        code = r'''
import copy, importlib.util, json, os, sys, time
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root/'guest'))
import gi
gi.require_version('Gio','2.0')
from gi.repository import Gio,GLib
from qsm_qt_input_geometry import QtInputGeometry,INTERFACE,OBJECT
spec=importlib.util.spec_from_file_location('bridge',root/'guest/qsm_desktop_metadata.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
pid=os.getpid();name='org.qsm.InputGeometry.p'+str(pid)
def name_call(method,name):return bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus',method,GLib.Variant('(s)',(name,)),None,Gio.DBusCallFlags.NONE,1000,None)
def own(name):bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','RequestName',GLib.Variant('(su)',(name,4)),None,Gio.DBusCallFlags.NONE,1000,None)
own(name)
body={'version':1,'sequence':1,'window':'a'*32,'enabled':True,'size':[500,400],'rect':[11,70,1,18],'clip':[0,0,500,400]}
layout={'desktop':{'rect':[0,0,1280,800],'scale':1},'windows':[{'id':'first','pid':pid,'rect':[20,30,500,400],'client_rect':[20,30,500,400],'visible':True,'opacity':1}],'focus':'first','available':True,'locked':False}
state=m.DesktopState();state.set_layout(layout);clock=[1.];changes=[];defer=[False]
def move(dx):
 value=copy.deepcopy(state.layout);value['windows'][0]['rect'][0]+=dx;value['windows'][0]['client_rect'][0]+=dx;state.set_layout(value)
def method(connection,sender,path,interface,method,parameters,invocation):
 try:
  raw=json.dumps(body)
  if defer[0]:
   defer[0]=False
   def late():invocation.return_value(GLib.Variant('(s)',(raw,)));return False
   GLib.timeout_add(120,late)
   GLib.timeout_add(10,lambda:(move(30),False)[1])
  else:invocation.return_value(GLib.Variant('(s)',(raw,)))
 finally:
  if getattr(bus.register_object,'get_symbol',lambda:None)()=='g_dbus_connection_register_object_with_closures':invocation._unref()
xml='<node><interface name="'+INTERFACE+'"><method name="Snapshot"><arg type="s" direction="out"/></method><signal name="Changed"><arg type="s"/></signal></interface></node>'
bus.register_object(OBJECT,Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0],method,None,None)
provider=QtInputGeometry(bus,state,lambda urgent:changes.append(urgent),clock=lambda:clock[0])
context=GLib.MainContext.default()
def pump(seconds):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  while context.pending():context.iteration(False)
  time.sleep(.002)
def wait(test):
 end=time.monotonic()+2
 while time.monotonic()<end:
  pump(.005)
  if test():return
 raise AssertionError({'state':state.snapshot(),'owner':provider.owner,'binding':provider.binding})
def emit(value):bus.emit_signal(None,OBJECT,INTERFACE,'Changed',GLib.Variant('(s)',(json.dumps(value),)))
wait(lambda:state.caret is not None);assert state.caret['rect']==[31,100,1,18]
bad=dict(body,text='not allowed');emit(bad);pump(.05);assert state.caret['rect']==[31,100,1,18]
old=dict(body,rect=[40,70,1,18]);emit(old);pump(.05);assert state.caret['rect']==[31,100,1,18]
body.update(sequence=2,rect=[24,70,1,18]);emit(body);wait(lambda:state.caret['rect'][0]==44)
move(40);assert state.caret is None;wait(lambda:state.caret is not None);assert state.caret['rect']==[84,100,1,18]
body.update(sequence=3,size=[501,400]);emit(body);wait(lambda:state.caret is None)
body.update(sequence=4,size=[500,400]);emit(body);wait(lambda:state.caret is not None)
state.set_locked(True);assert state.caret is None;emit(body);pump(.05);assert state.caret is None
state.set_locked(False);wait(lambda:state.caret is not None)
clock[0]+=3;provider.expire();assert state.caret is None
emit(body);wait(lambda:state.caret is not None)
# A reply from a superseded layout must not restore the old position.
defer[0]=True;move(20);wait(lambda:state.caret is not None and state.caret['rect'][0]==134);pump(.2);assert state.caret['rect'][0]==134
name_call('ReleaseName',name);wait(lambda:state.caret is None)
own(name);wait(lambda:state.caret is not None)
# Owning another PID's spelling of the well-known name is insufficient.
wrong='org.qsm.InputGeometry.p'+str(pid+1);own(wrong)
value=copy.deepcopy(state.layout);value['windows'][0]['pid']=pid+1;state.set_layout(value);pump(.15);assert state.caret is None and not provider.owner
provider.close();emit(body);pump(.05);assert state.caret is None
print('QT_GEOMETRY_DBUS_OK')
'''
        result = subprocess.run(['dbus-run-session', '--', sys.executable, '-c', code, str(ROOT)],
            capture_output=True, text=True, timeout=15, env={**os.environ, 'G_DEBUG': 'fatal-criticals'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('QT_GEOMETRY_DBUS_OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
