"""Seed the disposable Firefox test profile from its bundled codec manifest."""
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

browser = Path('/home/dima/.cache/ms-playwright/firefox-1543/firefox/omni.ja')
with zipfile.ZipFile(browser) as bundle:
    manifest = json.loads(bundle.read('chrome/toolkit/content/global/gmp-sources/openh264.json'))
vendor = manifest['vendors']['gmp-gmpopenh264']
artifact = vendor['platforms']['Linux_x86_64-gcc3']
blob = urllib.request.urlopen(artifact['fileUrl'], timeout=30).read()
assert hashlib.sha512(blob).hexdigest() == artifact['hashValue']
destination = Path('/tmp/qsm-openh264/gmp-gmpopenh264') / vendor['version']
destination.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(io.BytesIO(blob)) as archive:
    for name in archive.namelist():
        assert Path(name).name == name and not name.startswith('.')
        (destination / name).write_bytes(archive.read(name))
print('FIREFOX_TEST_CODEC_READY')
