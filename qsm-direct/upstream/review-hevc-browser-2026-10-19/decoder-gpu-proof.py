from pathlib import Path
import time,json
for sample in range(2):
 for p in Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:
   cmd=(p/'cmdline').read_bytes()
   if b'/opt/qsm-chrome/chrome' not in cmd:continue
   mappings=(p/'maps').read_bytes()
   if b'iHD_drv_video.so' not in mappings:continue
   print('CHROME_IHD_PROCESS',p.name,'gpu' if b'--type=gpu-process' in cmd else 'utility')
   for fd in (p/'fd').iterdir():
    try:
     path=str(fd.resolve())
     if path.startswith('/dev/dri/'):print(path,(p/'fdinfo'/fd.name).read_text())
    except OSError:pass
  except (OSError,ProcessLookupError):pass
 time.sleep(2)
