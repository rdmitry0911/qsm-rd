#!/usr/bin/env python3
"""Extract exact encoder classes, create a static text fixture and decoder probes.

Run from repository root with Pillow and shared21 commit e690083 in git history.
Compile generated /tmp/qsm-keyframe-probes/{baseline,candidate,dynamic}.cpp
with C++20 and pkg-config --cflags --libs libavcodec libavutil libswscale.
Run from a directory containing fixture.bgra, passing the encoder name.
"""
from pathlib import Path
import subprocess
from PIL import Image,ImageDraw,ImageFont
p=Path('/tmp/qsm-keyframe-probes');p.mkdir(parents=True,exist_ok=True)
im=Image.new('RGB',(1970,1220),(31,34,42));d=ImageDraw.Draw(im);font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',15)
for row in range(45):
 y=40+row*25
 d.text((40,y),f'{row:03d} QSM desktop: Firefox / Settings / Hello World! 0123456789',font=font,fill=(220,220,225))
 for x in range(850,1900):d.line((x,y,x,y+20),fill=(x%256,(row*13)%256,(x*7+row)%256))
im.save(p/'fixture.png');p.joinpath('fixture.bgra').write_bytes(im.convert('RGBA').tobytes('raw','BGRA'))
s=Path('qsm-direct/tools/qsm_direct_media_worker.cpp').read_text();cls='class LibavVideoEncoder final {'+s.split('class LibavVideoEncoder final {',1)[1].split('\n#endif',1)[0]
headers=Path('qsm-direct/upstream/review-adaptive-bitrate-2026-10-17/encoder-probe.cpp').read_text().split('class LibavVideoEncoder final {')[0]
main=r'''
#include <fstream>
#include <cmath>
int main(int argc,char**argv) {
 std::string name=argc>1?argv[1]:"h264_nvenc";
 LibavVideoEncoder encoder(1970,1220,60,name,8000);
 std::vector<uint8_t> pixels(1970*1220*4);std::ifstream in("fixture.bgra",std::ios::binary);if(!in.read((char*)pixels.data(),pixels.size()))return 1;
 auto*codec=avcodec_find_decoder(name=="hevc_nvenc"?AV_CODEC_ID_HEVC:AV_CODEC_ID_H264);
 auto*ctx=avcodec_alloc_context3(codec);auto*f=av_frame_alloc();avcodec_open2(ctx,codec,nullptr);
 std::vector<uint8_t> old(1970*1220);size_t keys=0,total=0,decoded=0;double max_delta=0,acc=0,max_ms=0;int flashes=0;
 for(int n=0;n<600;++n) {
  auto start=std::chrono::steady_clock::now();auto packets=encoder.encode(pixels,n==347);
  max_ms=std::max(max_ms,std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count());
  for(auto&p:packets) {
   keys+=p.keyframe;total+=p.bytes.size();AVPacket pkt{};pkt.data=p.bytes.data();pkt.size=p.bytes.size();if(avcodec_send_packet(ctx,&pkt)<0)return 2;
   while(avcodec_receive_frame(ctx,f)==0) {
    ++decoded;double delta=0;size_t changed=0;
    for(int y=0;y<1220;++y)for(int x=0;x<1970;++x){size_t i=y*1970+x;int v=f->data[0][y*f->linesize[0]+x];int dif=std::abs(v-old[i]);if(x<800){delta+=dif;changed+=dif>=8;}old[i]=v;}
    delta/=800.0*1220;
    if(n>90 && (n<347 || n>437)){max_delta=std::max(max_delta,delta);acc+=delta;if(delta>.15)++flashes;}
    if(n>90&&(p.keyframe||delta>.2))std::cerr<<"frame="<<n<<" key="<<p.keyframe<<" bytes="<<p.bytes.size()<<" text_delta="<<delta<<" changed8="<<changed<<"\n";
    av_frame_unref(f);
   }
  }
 }
 std::cout<<"encoder="<<name<<" decoded="<<decoded<<" keys="<<keys<<" text_max_delta="<<max_delta<<" text_mean_delta="<<acc/418<<" flashes="<<flashes<<" mbps="<<total*8/1e7<<" max_encode_ms="<<max_ms<<"\n";
 av_frame_free(&f);avcodec_free_context(&ctx);return decoded==600?0:3;
}
'''
baseline=subprocess.check_output(['git','show','e690083:qsm-direct/tools/qsm_direct_media_worker.cpp'],text=True)
baseline='class LibavVideoEncoder final {'+baseline.split('class LibavVideoEncoder final {',1)[1].split('\n#endif',1)[0]
for name,encoder_class in [('baseline',baseline),('candidate',cls)]:
 p.joinpath(name+'.cpp').write_text(headers+encoder_class+main)
dynamic=Path('qsm-direct/upstream/review-adaptive-bitrate-2026-10-17/encoder-probe.cpp').read_text().split('};int main',1)[1]
dynamic='int main'+dynamic.split('int main',1)[1] if 'int main' in dynamic else 'int main'+dynamic
# Apply changes away from a periodic GOP boundary to exercise reconfiguration.
dynamic=dynamic.replace('n==300','n==301').replace('n==600','n==607').replace('n==900','n==913')
p.joinpath('dynamic.cpp').write_text(headers+cls+dynamic)
