#include <array>
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
class LibavVideoEncoder final {
public:
    struct Packet {
        std::vector<std::uint8_t> bytes;
        bool keyframe {};
    };

    LibavVideoEncoder(std::uint32_t width, std::uint32_t height, std::uint32_t fps,
                     const std::string &encoder, std::uint32_t bitrate_kbps = 8000U) {
        if (width == 0U || height == 0U || width > static_cast<std::uint32_t>(INT_MAX) ||
            height > static_cast<std::uint32_t>(INT_MAX) || fps == 0U ||
            fps > static_cast<std::uint32_t>(INT_MAX)) {
            throw WorkerError("invalid in-process H.264 dimensions");
        }
        const auto *codec = avcodec_find_encoder_by_name(encoder.c_str());
        if (codec == nullptr) {
            throw WorkerError("libavcodec has no " + encoder + " encoder");
        }
        context_ = avcodec_alloc_context3(codec);
        frame_ = av_frame_alloc();
        packet_ = av_packet_alloc();
        if (context_ == nullptr || frame_ == nullptr || packet_ == nullptr) {
            release();
            throw WorkerError("cannot allocate in-process H.264 encoder");
        }
        context_->width = static_cast<int>(width);
        context_->height = static_cast<int>(height);
        context_->pix_fmt = AV_PIX_FMT_YUV420P;
        context_->time_base = AVRational {1, static_cast<int>(fps)};
        context_->framerate = AVRational {static_cast<int>(fps), 1};
        context_->gop_size = 30;
        context_->max_b_frames = 0;
        context_->bit_rate = static_cast<std::int64_t>(bitrate_kbps) * 1000;
        context_->rc_max_rate = context_->bit_rate;
        context_->rc_buffer_size = static_cast<int>(context_->bit_rate / fps);
        context_->flags |= AV_CODEC_FLAG_LOW_DELAY;
        // Two slice threads give useful
        // parallelism without frame-thread reordering latency.
        context_->thread_type = FF_THREAD_SLICE;
        context_->thread_count = 2;

        AVDictionary *options = nullptr;
        (void) av_dict_set(&options, "aud", "1", 0);
        if (encoder == "h264_nvenc" || encoder == "hevc_nvenc") {
            (void) av_dict_set(&options, "preset", "p1", 0);
            (void) av_dict_set(&options, "tune", "ll", 0);
            (void) av_dict_set(&options, "forced-idr", "1", 0);
            (void) av_dict_set(&options, "zerolatency", "1", 0);
            (void) av_dict_set(&options, "delay", "0", 0);
            (void) av_dict_set(&options, "rc-lookahead", "0", 0);
            (void) av_dict_set(&options, "rc", "vbr", 0);
        } else {
            (void) av_dict_set(&options, "preset", "ultrafast", 0);
            (void) av_dict_set(&options, "tune", "zerolatency", 0);
            (void) av_dict_set(&options, "x264-params",
                           "aud=1:keyint=30:min-keyint=30:scenecut=0:bframes=0:"
                           "repeat-headers=1:sliced-threads=1:sync-lookahead=0:rc-lookahead=0",
                           0);
        }
        const int opened = avcodec_open2(context_, codec, &options);
        av_dict_free(&options);
        if (opened < 0) {
            release();
            throw WorkerError("cannot initialise in-process " + encoder + ": " + error_string(opened));
        }

        frame_->format = context_->pix_fmt;
        frame_->width = context_->width;
        frame_->height = context_->height;
        const int allocated = av_frame_get_buffer(frame_, 32);
        if (allocated < 0) {
            release();
            throw WorkerError("cannot allocate in-process H.264 frame: " + error_string(allocated));
        }
        scale_ = sws_getContext(context_->width, context_->height, AV_PIX_FMT_BGRA,
                                context_->width, context_->height, context_->pix_fmt,
                                SWS_FAST_BILINEAR, nullptr, nullptr, nullptr);
        if (scale_ == nullptr) {
            release();
            throw WorkerError("cannot initialise BGRA to H.264 conversion");
        }
    }

    ~LibavVideoEncoder() { release(); }

    LibavVideoEncoder(const LibavVideoEncoder &) = delete;
    LibavVideoEncoder &operator=(const LibavVideoEncoder &) = delete;

    // Called only on the video thread. FFmpeg's libx264/NVENC wrappers
    // reconfigure rate control on the next submitted frame without reopening
    // the codec, resetting timestamps or breaking the prediction chain.
    void set_bitrate(std::uint32_t bitrate_kbps) {
        if (bitrate_kbps < 500U || bitrate_kbps > 100'000U) {
            throw WorkerError("invalid live video bitrate");
        }
        context_->bit_rate = static_cast<std::int64_t>(bitrate_kbps) * 1000;
        context_->rc_max_rate = context_->bit_rate;
        context_->rc_buffer_size = static_cast<int>(context_->bit_rate / context_->framerate.num);
    }

    [[nodiscard]] std::vector<Packet> encode(std::span<const std::uint8_t> bgra,
                                               bool force_idr) {
        const auto required = static_cast<std::size_t>(context_->width) *
                              static_cast<std::size_t>(context_->height) * 4U;
        if (bgra.size() != required) {
            throw WorkerError("in-process H.264 input frame has an invalid size");
        }
        const int writable = av_frame_make_writable(frame_);
        if (writable < 0) {
            throw WorkerError("cannot reuse in-process H.264 frame: " + error_string(writable));
        }
        const std::array<const std::uint8_t *, 4U> source {bgra.data(), nullptr, nullptr, nullptr};
        const std::array<int, 4U> stride {context_->width * 4, 0, 0, 0};
        (void) sws_scale(scale_, source.data(), stride.data(), 0, context_->height,
                         frame_->data, frame_->linesize);
        frame_->pts = next_pts_++;
        frame_->pict_type = force_idr ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
        const int submitted = avcodec_send_frame(context_, frame_);
        if (submitted < 0) {
            throw WorkerError("in-process H.264 encoder rejected a frame: " + error_string(submitted));
        }
        std::vector<Packet> result;
        while (true) {
            const int received = avcodec_receive_packet(context_, packet_);
            if (received == AVERROR(EAGAIN) || received == AVERROR_EOF) {
                break;
            }
            if (received < 0) {
                throw WorkerError("in-process H.264 encoder did not produce H.264: " + error_string(received));
            }
            result.push_back({
                .bytes = {packet_->data, packet_->data + packet_->size},
                .keyframe = (packet_->flags & AV_PKT_FLAG_KEY) != 0,
            });
            av_packet_unref(packet_);
        }
        return result;
    }

private:
    void release() noexcept {
        if (packet_ != nullptr) { av_packet_free(&packet_); }
        if (frame_ != nullptr) { av_frame_free(&frame_); }
        if (context_ != nullptr) { avcodec_free_context(&context_); }
        if (scale_ != nullptr) { sws_freeContext(scale_); scale_ = nullptr; }
    }

    static std::string error_string(int error) {
        std::array<char, AV_ERROR_MAX_STRING_SIZE> message {};
        av_strerror(error, message.data(), message.size());
        return message.data();
    }

    AVCodecContext *context_ {};
    AVFrame *frame_ {};
    AVPacket *packet_ {};
    SwsContext *scale_ {};
    std::int64_t next_pts_ {};
};int main(int argc, char **argv) {
 const std::string name=argc>1 ? argv[1] : "h264_nvenc";
 const auto bitrate=argc>2 ? static_cast<std::uint32_t>(std::stoul(argv[2])) : 8000U;
 LibavVideoEncoder encoder(1280,800,60,name,bitrate);
 std::vector<std::uint8_t> pixels(1280*800*4);
 auto *codec=avcodec_find_decoder(name=="hevc_nvenc" ? AV_CODEC_ID_HEVC : AV_CODEC_ID_H264);
 auto *decoder=avcodec_alloc_context3(codec);auto *frame=av_frame_alloc();
 if(avcodec_open2(decoder,codec,nullptr)<0) return 2;
 std::size_t total=0,max_size=0,keys=0,decoded=0,segment=0;
 double max_ms=0;
 for(int n=0;n<1200;++n) {
  for(std::size_t i=0;i<pixels.size();i+=4) {
   pixels[i]=static_cast<std::uint8_t>((i/4+n*13)%256);
   pixels[i+1]=static_cast<std::uint8_t>((i/5120+n*7)%256);
   pixels[i+2]=static_cast<std::uint8_t>((i/81920+n*3)%256);pixels[i+3]=255;
  }
  if(n==300) {encoder.set_bitrate(2000);segment=0;}
  if(n==600) {encoder.set_bitrate(12000);segment=0;}
  if(n==900) {encoder.set_bitrate(500);segment=0;}
  const bool force=(n==7||n==17||n==49);
  const auto start=std::chrono::steady_clock::now();
  auto packets=encoder.encode(pixels,force);
  const auto ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count();
  max_ms=std::max(max_ms,ms);
  if(packets.size()!=1 || (force&&!packets[0].keyframe)) return 3;
  for(auto &p:packets) {
   segment+=p.bytes.size();total+=p.bytes.size();max_size=std::max(max_size,p.bytes.size());keys+=p.keyframe;
   AVPacket packet {};packet.data=p.bytes.data();packet.size=static_cast<int>(p.bytes.size());
   if(avcodec_send_packet(decoder,&packet)<0) return 4;
   while(avcodec_receive_frame(decoder,frame)==0) {++decoded;av_frame_unref(frame);}
  }
  if(n%300==299) std::cout<<"segment="<<n/300<<" mbps="<<segment*8/5e6<<" decoded="<<decoded<<"\n";
 }
 av_frame_free(&frame);avcodec_free_context(&decoder);
 std::cout << "{\"encoder\":\""<<name<<"\",\"bitrate_kbps\":"<<bitrate<<",\"frames\":1200,\"decoded\":"<<decoded
  <<",\"forced_idrs\":3,\"keys\":"<<keys<<",\"max_frame_bytes\":"<<max_size
  <<",\"mbps\":"<<total*8/20e6<<",\"max_encode_ms\":"<<max_ms<<"}\n";
 return decoded==1200 ? 0 : 5;
}
