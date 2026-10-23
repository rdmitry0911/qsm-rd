from pathlib import Path
import html,subprocess
out=Path('/tmp/qsm-rate-bands-20261023')
svg=['<svg xmlns="http://www.w3.org/2000/svg" width="2560" height="1440"><rect width="2560" height="1440" fill="#f2f2f2"/>']
for n,y in enumerate(range(24,1440,30)):
 text=html.escape(f'QSM static text 0123456789 bandwidth change / line {n:02d} ABCDEFGHIJKLMNOPQRSTUVWXYZ []{{}} <> +-= //')
 svg.append(f'<text x="12" y="{y}" font-family="DejaVu Sans Mono" font-size="20" fill="#0c0c0c">{text}</text>')
 for x in range(1600,2560,32):
  c=((x//32*17+n*3)%255,(n*21)%255,(x//32*41)%255)
  svg.append(f'<rect x="{x}" y="{y-16}" width="24" height="20" fill="rgb{c}"/>')
svg.append('</svg>');(out/'fixture.svg').write_text('\n'.join(svg))
draw=[]
for n,y in enumerate(range(24,1440,30)):
 draw.append(f"fill '#0c0c0c' text 12,{y} 'QSM static text 0123456789 bandwidth change / line {n:02d} ABCDEFGHIJKLMNOPQRSTUVWXYZ []{{}} +-= //'")
 for x in range(1600,2560,32):
  c=((x//32*17+n*3)%255,(n*21)%255,(x//32*41)%255)
  draw.append(f"fill 'rgb({c[0]},{c[1]},{c[2]})' rectangle {x},{y-16} {x+23},{y+4}")
subprocess.run(['convert','-size','2560x1440','xc:#f2f2f2','-font','/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf','-pointsize','20','-draw',' '.join(draw),'-depth','8','BGRA:'+str(out/'fixture.bgra')],check=True)

src=Path('qsm-direct/tools/qsm_direct_media_worker.cpp').read_text()
old=Path('qsm-direct/upstream/review-intel-qsv-2026-10-19/static-probe.cpp').read_text()
headers=old.split('class LibavVideoEncoder final {')[0]
cls='class LibavVideoEncoder final {'+src.split('class LibavVideoEncoder final {',1)[1].split('\n#endif',1)[0]
main=r'''
#include <fstream>
#include <cmath>
#include <algorithm>
int main(int argc,char**argv) {
 const std::string name=argc>1?argv[1]:"h264_nvenc";
 const int W=2560,H=1440;
 LibavVideoEncoder encoder(W,H,60,name,8000);
 std::vector<uint8_t> pixels(W*H*4);std::ifstream input("fixture.bgra",std::ios::binary);
 if(!input.read((char*)pixels.data(),pixels.size()))return 1;
 auto*codec=avcodec_find_decoder(name.starts_with("hevc_")?AV_CODEC_ID_HEVC:AV_CODEC_ID_H264);
 auto*ctx=avcodec_alloc_context3(codec);auto*f=av_frame_alloc();avcodec_open2(ctx,codec,nullptr);
 std::vector<uint8_t> old(W*H);size_t keys=0,total=0,decoded=0;double max_delta=0;int rate=8000;
 for(int n=0;n<600;++n) {
  if(n==120)rate=2000;if(n==240)rate=8000;if(n==360)rate=1500;if(n==480)rate=8000;
  if(n%120==0 && n)encoder.set_bitrate(rate);
  for(auto&p:encoder.encode(pixels,false)) {
   keys+=p.keyframe;total+=p.bytes.size();AVPacket pkt{};pkt.data=p.bytes.data();pkt.size=p.bytes.size();if(avcodec_send_packet(ctx,&pkt)<0)return 2;
   while(avcodec_receive_frame(ctx,f)==0) {
    ++decoded;double delta=0,maxrow=0;int bands=0;bool inband=false;
    for(int y=0;y<H;++y){double row=0;
     for(int x=0;x<W;++x){size_t i=y*W+x;int v=f->data[0][y*f->linesize[0]+x];int dif=std::abs(v-old[i]);row+=dif;old[i]=v;}
     row/=W;delta+=row;maxrow=std::max(maxrow,row);
     bool band=row>2.;if(band&&!inband)++bands;inband=band;
    }delta/=H;if(n>90)max_delta=std::max(max_delta,delta);
    std::cout<<"{\"frame\":"<<n<<",\"rate\":"<<rate<<",\"key\":"<<p.keyframe<<",\"bytes\":"<<p.bytes.size()<<",\"delta\":"<<delta<<",\"max_row_delta\":"<<maxrow<<",\"bands\":"<<bands<<"}\n";
    if(n>110 && (p.keyframe||delta>1.)) {
     std::ofstream pgm("frame-"+std::to_string(n)+".pgm",std::ios::binary);pgm<<"P5\n"<<W<<" "<<H<<"\n255\n";pgm.write((char*)old.data(),old.size());
    }
    av_frame_unref(f);
   }
  }
 }
 std::cerr<<"decoded="<<decoded<<" keys="<<keys<<" bytes="<<total<<" max_delta="<<max_delta<<"\n";
 av_frame_free(&f);avcodec_free_context(&ctx);return decoded==600?0:3;
}
'''
(out/'baseline/probe.cpp').write_text(headers+cls+main)
print('Generated real-production-class rate-transition probe and fixture.')
