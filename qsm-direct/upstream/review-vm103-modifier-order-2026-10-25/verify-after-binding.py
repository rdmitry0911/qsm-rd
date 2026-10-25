from pathlib import Path
import json,sys
from guest_exec import guest_exec,user_bus
root=Path('/tmp/qsm-layout-switch-20261025');r=json.loads((root/'after-binding-live-result.json').read_text());assert len(r['cases'])==4
for c in r['cases']:
 assert c['after']!=c['before'] and c['guest_evdev']['complete']
 events=c['guest_gui']['events'];assert len([x for x in events if x['event']=='shortcut'])==1
 changes=[x for x in events if x['event']=='layoutChanged'];assert len(changes)==1 and changes[0]['index']==c['after']
assert r['restored_layout']==r['original_layout'];current=int(user_bus(['busctl','--user','call','org.kde.keyboard','/Layouts','org.kde.KeyboardLayouts','getLayout']).split()[1]);assert current==r['original_layout']
code="""import glob,json,configparser
c=configparser.ConfigParser(interpolation=None,strict=False);c.optionxform=str;c.read('/home/dima/.config/kglobalshortcutsrc');s=c['KDE Keyboard Layout Switcher']['Switch to Next Keyboard Layout'];print(json.dumps({'saved_layout_switch_shortcuts':s,'remaining_owned_files':glob.glob('/run/qsm-layout-probe-*')+glob.glob('/run/user/1000/qsm-layout-signals-*')}))
"""
p=json.loads(guest_exec(['/usr/bin/python3','-c',code]));assert 'Alt+Shift' in p['saved_layout_switch_shortcuts'] and 'Meta+Shift' in p['saved_layout_switch_shortcuts'];assert not p['remaining_owned_files'];(root/'guest-binding-persisted.json').write_text(json.dumps(p,indent=2)+'\n')
print(json.dumps({'all_four_cases_switch_layout_once':True,'guest_keyboard_and_layout_signals_verified':True,'both_bindings_saved':True,'original_layout_restored':current,'temporary_observers_cleaned':True},indent=2))
