from pathlib import Path
import json,subprocess,re
original=json.loads(Path('/root/qsm-nvenc-browser24/original-output.json').read_text())
subprocess.run(['pct','exec','108','--','systemctl','stop','qsm-nvenc-fixture'],check=True)
subprocess.run(['pct','exec','108','--','rm','-f','/run/qsm-nvenc-browser24-static'],check=True)
for output in original:
 name=output['name'];mode=output['current_mode'];assert re.fullmatch('[A-Za-z0-9_-]+',name)
 command=f"output {name} mode {mode['width']}x{mode['height']}@{mode['refresh']/1000:.3f}Hz"
 p=subprocess.run(['pct','exec','108','--','/opt/qsm-native-sway/bin/swaymsg','-s','/run/qsm-display-1/sway-ipc.999.444366.sock',command],capture_output=True,text=True,check=True)
 assert all(x.get('success') for x in json.loads(p.stdout)),p.stdout
 print(command,': restored')
