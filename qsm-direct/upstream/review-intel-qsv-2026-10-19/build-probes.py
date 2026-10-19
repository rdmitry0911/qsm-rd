"""Generate hardware probes using the exact current production encoder class."""
from pathlib import Path
import os
repo = Path(__file__).resolve().parents[2]
out = Path(os.environ.get('QSM_PROBE_OUTPUT', '/tmp/qsm-intel-lab'))
out.mkdir(parents=True, exist_ok=True)
src = (repo/'tools/qsm_direct_media_worker.cpp').read_text()
cls = 'class LibavVideoEncoder final {' + src.split('class LibavVideoEncoder final {', 1)[1].split('\n#endif', 1)[0]
old = (repo/'upstream/review-qsv-vaapi-2026-10-18/encoder-probe.cpp').read_text()
headers = old.split('class LibavVideoEncoder final {', 1)[0] + '#include <cstdlib>\n'
dynamic = 'int main' + old.split('};int main', 1)[1]
dynamic = dynamic.replace('"/dev/dri/renderD128" : ""', '(std::getenv("QSM_PROBE_VAAPI_DEVICE") ? std::getenv("QSM_PROBE_VAAPI_DEVICE") : "/dev/dri/renderD128") : ""')
dynamic = dynamic.replace(' const std::string name=', ' std::cout<<std::unitbuf;\n const std::string name=')
dynamic = dynamic.replace('encoder.set_bitrate(2000);segment=0;', 'encoder.set_bitrate(2000);').replace('encoder.set_bitrate(12000);segment=0;', 'encoder.set_bitrate(12000);').replace('encoder.set_bitrate(500);segment=0;', 'encoder.set_bitrate(500);')
dynamic = dynamic.replace(' if(n%300==299) std::cout<<"segment="<<n/300<<" mbps="<<segment*8/5e6<<" decoded="<<decoded<<"\\n";', ''' if(n==300 || n==606 || n==912 || n==1199) {
   const int count=n==300?301:n==1199?287:306;
   const int target=n==300?8000:n==606?2000:n==912?12000:500;
   std::cout<<"phase_target_kbps="<<target<<" frames="<<count<<" actual_mbps="<<segment*8*60/(count*1e6)<<" decoded="<<decoded<<"\\n";
   segment=0;
  }''')
dynamic = dynamic.replace('return decoded==1200 ? 0 : 5;', 'return decoded==1200 && keys==7 ? 0 : 5;')
static_old = (repo/'upstream/review-qsv-vaapi-2026-10-18/static-probe.cpp').read_text()
static = static_old.split('};\n#include <fstream>', 1)[1]
static = '#include <fstream>' + static
static = static.replace(' auto* shared=LibavVideoEncoder::create_device(name,"/dev/dri/renderD128");', ' const std::string device=name.ends_with("_vaapi") ? (std::getenv("QSM_PROBE_VAAPI_DEVICE") ? std::getenv("QSM_PROBE_VAAPI_DEVICE") : "/dev/dri/renderD128") : "";\n auto* shared=LibavVideoEncoder::create_device(name,device);')
static = static.replace('name,8000,"/dev/dri/renderD128",shared', 'name,8000,device,shared')
static = static.replace('return decoded==600?0:3;', 'return decoded==600 && keys==2 && flashes==0?0:3;')
(out/'encoder-probe.cpp').write_text(headers + cls + dynamic)
(out/'static-probe.cpp').write_text(headers + cls + '\n' + static)
