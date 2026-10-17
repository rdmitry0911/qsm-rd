from pathlib import Path
source=Path('qsm-direct/tools/qsm_direct_media_worker.cpp').read_text()
encoder=source.split('class LibavH264Encoder final {',1)[1].split('\n#endif',1)[0]
headers='''#include <array>
#include <vector>
#include <string>
#include <span>
#include <cstdint>
#include <climits>
#include <stdexcept>
#include <iostream>
#include <chrono>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}
using WorkerError = std::runtime_error;
'''
main=r'''
int main(int argc, char **argv) {
 const std::string name=argc>1 ? argv[1] : "h264_nvenc";
 LibavH264Encoder encoder(1280,800,60,name);
 std::vector<std::uint8_t> pixels(1280*800*4);
 auto *codec=avcodec_find_decoder(AV_CODEC_ID_H264);
 auto *decoder=avcodec_alloc_context3(codec);auto *frame=av_frame_alloc();
 if(avcodec_open2(decoder,codec,nullptr)<0) return 2;
 std::size_t total=0,max_size=0,keys=0,decoded=0;
 double max_ms=0;
 for(int n=0;n<120;++n) {
  for(std::size_t i=0;i<pixels.size();i+=4) {
   pixels[i]=static_cast<std::uint8_t>((i/4+n*13)%256);
   pixels[i+1]=static_cast<std::uint8_t>((i/5120+n*7)%256);
   pixels[i+2]=static_cast<std::uint8_t>((i/81920+n*3)%256);pixels[i+3]=255;
  }
  const bool force=(n==7||n==17||n==49);
  const auto start=std::chrono::steady_clock::now();
  auto packets=encoder.encode(pixels,force);
  const auto ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count();
  max_ms=std::max(max_ms,ms);
  if(packets.size()!=1 || (force&&!packets[0].keyframe)) return 3;
  for(auto &p:packets) {
   total+=p.bytes.size();max_size=std::max(max_size,p.bytes.size());keys+=p.keyframe;
   AVPacket packet {};packet.data=p.bytes.data();packet.size=static_cast<int>(p.bytes.size());
   if(avcodec_send_packet(decoder,&packet)<0) return 4;
   while(avcodec_receive_frame(decoder,frame)==0) {++decoded;av_frame_unref(frame);}
  }
 }
 av_frame_free(&frame);avcodec_free_context(&decoder);
 std::cout << "{\"encoder\":\""<<name<<"\",\"frames\":120,\"decoded\":"<<decoded
  <<",\"forced_idrs\":3,\"keys\":"<<keys<<",\"max_frame_bytes\":"<<max_size
  <<",\"mbps\":"<<total*8/2e6<<",\"max_encode_ms\":"<<max_ms<<"}\n";
 return decoded==120 ? 0 : 5;
}
'''
Path('/tmp/qsm-video-pacing/encoder-probe.cpp').write_text(headers+'class LibavH264Encoder final {'+encoder+main)
