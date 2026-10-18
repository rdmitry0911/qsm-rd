from pathlib import Path
s=Path('qsm-direct/tools/qsm_direct_media_worker.cpp').read_text();cls='class LibavVideoEncoder final {'+s.split('class LibavVideoEncoder final {',1)[1].split('\n#endif',1)[0]
old=Path('qsm-direct/upstream/review-adaptive-bitrate-2026-10-17/encoder-probe.cpp').read_text()
headers=old.split('class LibavVideoEncoder final {')[0].replace('#include <string>','#include <string>\n#include <string_view>\n#include <unistd.h>\n#include <grp.h>').replace('#include <libavutil/opt.h>','#include <libavutil/opt.h>\n#include <libavutil/hwcontext.h>')
main='int main'+old.split('};int main',1)[1]
main=main.replace(' LibavVideoEncoder encoder(1280,800,60,name,bitrate);',''' const std::string device=name.ends_with("_vaapi") ? "/dev/dri/renderD128" : "";
 auto* shared=LibavVideoEncoder::create_device(name,device);
 if(argc>3 && std::string(argv[3])=="nobody") {
  if(setgroups(0,nullptr)||setgid(65534)||setuid(65534))return 9;
 }
 LibavVideoEncoder encoder(1280,800,60,name,bitrate,device,shared);
 av_buffer_unref(&shared);''')
main=main.replace('name=="hevc_nvenc"','name.starts_with("hevc_")')
main=main.replace('n==300','n==301').replace('n==600','n==607').replace('n==900','n==913')
# Measure codec reopen time as well as ordinary frame latency.
main=main.replace('  if(n==301)', '  const auto step_start=std::chrono::steady_clock::now();\n  if(n==301)')
main=main.replace('std::chrono::steady_clock::now()-start','std::chrono::steady_clock::now()-step_start')
Path('/tmp/qsm-intel-amd/encoder-probe.cpp').write_text(headers+cls+main)
