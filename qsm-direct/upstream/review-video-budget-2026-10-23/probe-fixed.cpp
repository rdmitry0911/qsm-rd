#include <array>
#include <vector>
#include <string>
#include <string_view>
#include <unistd.h>
#include <grp.h>
#include <span>
#include <cstdint>
#include <climits>
#include <stdexcept>
#include <iostream>
#include <chrono>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
#include <libavutil/hwcontext.h>
#include <libswscale/swscale.h>
}
using WorkerError = std::runtime_error;
#include <cstdlib>
class LibavVideoEncoder final {
public:
    struct Packet {
        std::vector<std::uint8_t> bytes;
        bool keyframe {};
    };

    static bool supports(std::string_view encoder) {
        return encoder == "libx264" || encoder == "h264_nvenc" || encoder == "hevc_nvenc" ||
               encoder == "h264_qsv" || encoder == "hevc_qsv" ||
               encoder == "h264_vaapi" || encoder == "hevc_vaapi";
    }

    // Open trusted host hardware before an LXC worker drops its privileges.
    // Codec resets and resizes retain this device; no render-group grant or
    // root privilege is needed while reading the container's compositor.
    static AVBufferRef *create_device(const std::string &encoder, const std::string &device) {
        const bool vaapi = std::string_view(encoder).ends_with("_vaapi");
        if (!vaapi && !std::string_view(encoder).ends_with("_qsv")) {
            return nullptr;
        }
        AVBufferRef *result = nullptr;
        const int created = av_hwdevice_ctx_create(&result,
            vaapi ? AV_HWDEVICE_TYPE_VAAPI : AV_HWDEVICE_TYPE_QSV,
            device.empty() ? nullptr : device.c_str(), nullptr, 0);
        if (created < 0) {
            av_buffer_unref(&result);
            throw WorkerError("cannot initialise " + encoder + " device: " + error_string(created));
        }
        return result;
    }

    LibavVideoEncoder(std::uint32_t width, std::uint32_t height, std::uint32_t fps,
                     const std::string &encoder, std::uint32_t bitrate_kbps = 8000U,
                     const std::string &device = {}, AVBufferRef *shared_device = nullptr)
        : width_(width), height_(height), fps_(fps), encoder_(encoder),
          vaapi_(std::string_view(encoder).ends_with("_vaapi")),
          qsv_(std::string_view(encoder).ends_with("_qsv")) {
        if (width == 0U || height == 0U || width > static_cast<std::uint32_t>(INT_MAX) ||
            height > static_cast<std::uint32_t>(INT_MAX) || fps == 0U ||
            fps > static_cast<std::uint32_t>(INT_MAX) || !supports(encoder)) {
            throw WorkerError("invalid in-process video encoder configuration");
        }
        codec_ = avcodec_find_encoder_by_name(encoder.c_str());
        if (codec_ == nullptr) {
            throw WorkerError("libavcodec has no " + encoder + " encoder");
        }
        try {
            if (vaapi_ || qsv_) {
                device_ = shared_device != nullptr ? av_buffer_ref(shared_device) : create_device(encoder, device);
                if (device_ == nullptr) {
                    throw WorkerError("cannot reference video encoder device");
                }
            }
            if (vaapi_) {
                frames_ = av_hwframe_ctx_alloc(device_);
                if (frames_ == nullptr) {
                    throw WorkerError("cannot allocate VA-API frame pool");
                }
                auto *pool = reinterpret_cast<AVHWFramesContext *>(frames_->data);
                pool->format = AV_PIX_FMT_VAAPI;
                pool->sw_format = AV_PIX_FMT_NV12;
                pool->width = static_cast<int>(width);
                pool->height = static_cast<int>(height);
                pool->initial_pool_size = 20;
                const int initialized = av_hwframe_ctx_init(frames_);
                if (initialized < 0) {
                    throw WorkerError("cannot initialise VA-API frame pool: " + error_string(initialized));
                }
                upload_ = av_frame_alloc();
                if (upload_ == nullptr) {
                    throw WorkerError("cannot allocate VA-API upload frame");
                }
            }
            open_codec(bitrate_kbps);
            frame_ = av_frame_alloc();
            packet_ = av_packet_alloc();
            if (frame_ == nullptr || packet_ == nullptr) {
                throw WorkerError("cannot allocate in-process video frame/packet");
            }
            const auto software_format = vaapi_ || qsv_ ? AV_PIX_FMT_NV12 : AV_PIX_FMT_YUV420P;
            frame_->format = software_format;
            frame_->width = static_cast<int>(width);
            frame_->height = static_cast<int>(height);
            const int allocated = av_frame_get_buffer(frame_, 32);
            if (allocated < 0) {
                throw WorkerError("cannot allocate video pixels: " + error_string(allocated));
            }
            scale_ = sws_getContext(static_cast<int>(width), static_cast<int>(height), AV_PIX_FMT_BGRA,
                                   static_cast<int>(width), static_cast<int>(height), software_format,
                                   SWS_FAST_BILINEAR, nullptr, nullptr, nullptr);
            if (scale_ == nullptr) {
                throw WorkerError("cannot initialise BGRA to video conversion");
            }
        } catch (...) {
            release();
            throw;
        }
    }

    ~LibavVideoEncoder() { release(); }
    LibavVideoEncoder(const LibavVideoEncoder &) = delete;
    LibavVideoEncoder &operator=(const LibavVideoEncoder &) = delete;

    // All codec access stays on the video thread. libx264/NVENC read live
    // rate settings. VA-API caches them at open; QSV cannot reset its HRD
    // buffer size on all drivers. Reopen those codecs on a rate change,
    // retaining the device, upload pool and increasing source timestamps.
    void set_bitrate(std::uint32_t bitrate_kbps) {
        if (bitrate_kbps < 500U || bitrate_kbps > 100'000U) {
            throw WorkerError("invalid live video bitrate");
        }
        const auto bits = static_cast<std::int64_t>(bitrate_kbps) * 1000;
        if (context_->bit_rate == bits) {
            return;
        }
        if (vaapi_ || qsv_) {
            const int flushed = avcodec_send_frame(context_, nullptr);
            if (flushed < 0 && flushed != AVERROR_EOF) {
                throw WorkerError("cannot flush " + encoder_ + " rate boundary: " + error_string(flushed));
            }
            receive_packets(pending_packets_);
            if (upload_ != nullptr) {
                av_frame_unref(upload_);
            }
            avcodec_free_context(&context_);
            open_codec(bitrate_kbps);
        } else {
            context_->bit_rate = bits;
            context_->rc_max_rate = bits;
            context_->rc_buffer_size = static_cast<int>(bits / fps_);
        }
    }

    [[nodiscard]] std::vector<Packet> encode(std::span<const std::uint8_t> bgra,
                                            bool force_idr) {
        if (bgra.size() != static_cast<std::size_t>(width_) * height_ * 4U) {
            throw WorkerError("in-process video input frame has an invalid size");
        }
        const int writable = av_frame_make_writable(frame_);
        if (writable < 0) {
            throw WorkerError("cannot reuse video frame: " + error_string(writable));
        }
        const std::array<const std::uint8_t *, 4U> source {bgra.data(), nullptr, nullptr, nullptr};
        const std::array<int, 4U> stride {static_cast<int>(width_) * 4, 0, 0, 0};
        (void) sws_scale(scale_, source.data(), stride.data(), 0, static_cast<int>(height_),
                         frame_->data, frame_->linesize);
        frame_->pts = next_pts_++;
        frame_->pict_type = force_idr ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
        AVFrame *input = frame_;
        if (vaapi_) {
            av_frame_unref(upload_);
            const int allocated = av_hwframe_get_buffer(frames_, upload_, 0);
            if (allocated < 0) {
                throw WorkerError("cannot obtain VA-API input surface: " + error_string(allocated));
            }
            const int uploaded = av_hwframe_transfer_data(upload_, frame_, 0);
            if (uploaded < 0) {
                throw WorkerError("cannot upload VA-API input surface: " + error_string(uploaded));
            }
            upload_->pts = frame_->pts;
            upload_->pict_type = frame_->pict_type;
            input = upload_;
        }
        const int submitted = avcodec_send_frame(context_, input);
        if (submitted < 0) {
            throw WorkerError("video encoder rejected a frame: " + error_string(submitted));
        }
        std::vector<Packet> result;
        result.swap(pending_packets_);
        receive_packets(result);
        return result;
    }

private:
    void open_codec(std::uint32_t bitrate_kbps) {
        context_ = avcodec_alloc_context3(codec_);
        if (context_ == nullptr) {
            throw WorkerError("cannot allocate in-process video encoder");
        }
        context_->width = static_cast<int>(width_);
        context_->height = static_cast<int>(height_);
        context_->pix_fmt = vaapi_ ? AV_PIX_FMT_VAAPI : qsv_ ? AV_PIX_FMT_NV12 : AV_PIX_FMT_YUV420P;
        context_->time_base = AVRational {1, static_cast<int>(fps_)};
        context_->framerate = AVRational {static_cast<int>(fps_), 1};
        // QSV's GopPicSize is a 16-bit field; 65535 is its infinite-GOP value.
        // Recovery uses explicit IDRs rather than full refreshes every 0.5s.
        context_->gop_size = qsv_ ? 65535 : INT_MAX;
        context_->max_b_frames = 0;
        context_->bit_rate = static_cast<std::int64_t>(bitrate_kbps) * 1000;
        context_->rc_max_rate = context_->bit_rate;
        context_->rc_buffer_size = static_cast<int>(context_->bit_rate / fps_);
        if (encoder_ == "hevc_qsv") {
            // Intel HEVC CBR can exhaust FFmpeg's output buffer on static
            // pictures. VBR enables LowDelayBRC/MaxFrameSize; one extra kbit/s
            // selects it without materially raising the target. A two-frame
            // VBV leaves room for headers and small frame-size overshoots.
            context_->rc_max_rate += 1000;
            context_->rc_buffer_size *= 2;
        }
        context_->flags |= AV_CODEC_FLAG_LOW_DELAY;
        context_->thread_type = FF_THREAD_SLICE;
        context_->thread_count = 2;
        if (device_ != nullptr) {
            context_->hw_device_ctx = av_buffer_ref(device_);
            if (context_->hw_device_ctx == nullptr) {
                throw WorkerError("cannot reference video device");
            }
        }
        if (frames_ != nullptr) {
            context_->hw_frames_ctx = av_buffer_ref(frames_);
            if (context_->hw_frames_ctx == nullptr) {
                throw WorkerError("cannot reference VA-API frames");
            }
        }
        AVDictionary *options = nullptr;
        // Packets already delimit access units. Intel's HEVC VA-API path
        // escapes an inserted AUD's Annex B start code a second time.
        (void) av_dict_set(&options, "aud", encoder_ == "hevc_vaapi" ? "0" : "1", 0);
        if (encoder_ == "h264_nvenc" || encoder_ == "hevc_nvenc") {
            (void) av_dict_set(&options, "preset", "p1", 0);
            (void) av_dict_set(&options, "tune", "ll", 0);
            (void) av_dict_set(&options, "forced-idr", "1", 0);
            (void) av_dict_set(&options, "zerolatency", "1", 0);
            (void) av_dict_set(&options, "delay", "0", 0);
            (void) av_dict_set(&options, "rc-lookahead", "0", 0);
            (void) av_dict_set(&options, "rc", "vbr", 0);
        } else if (qsv_) {
            (void) av_dict_set(&options, "preset", "veryfast", 0);
            (void) av_dict_set(&options, "async_depth", "1", 0);
            (void) av_dict_set(&options, "look_ahead", "0", 0);
            (void) av_dict_set(&options, "forced_idr", "1", 0);
            if (encoder_ == "hevc_qsv") {
                (void) av_dict_set(&options, "low_delay_brc", "1", 0);
                (void) av_dict_set_int(&options, "max_frame_size", context_->bit_rate / fps_ / 8, 0);
            }
        } else if (vaapi_) {
            (void) av_dict_set(&options, "async_depth", "1", 0);
            (void) av_dict_set(&options, "rc_mode", "CBR", 0);
        } else {
            (void) av_dict_set(&options, "preset", "ultrafast", 0);
            (void) av_dict_set(&options, "tune", "zerolatency", 0);
            (void) av_dict_set(&options, "x264-params",
                           "aud=1:keyint=infinite:scenecut=0:bframes=0:"
                           "repeat-headers=1:sliced-threads=1:sync-lookahead=0:rc-lookahead=0", 0);
        }
        const int opened = avcodec_open2(context_, codec_, &options);
        av_dict_free(&options);
        if (opened < 0) {
            throw WorkerError("cannot initialise in-process " + encoder_ + ": " + error_string(opened));
        }
    }

    void receive_packets(std::vector<Packet> &result) {
        while (true) {
            const int received = avcodec_receive_packet(context_, packet_);
            if (received == AVERROR(EAGAIN) || received == AVERROR_EOF) {
                break;
            }
            if (received < 0) {
                throw WorkerError("video encoder did not produce a packet: " + error_string(received));
            }
            result.push_back({.bytes = {packet_->data, packet_->data + packet_->size},
                              .keyframe = (packet_->flags & AV_PKT_FLAG_KEY) != 0});
            av_packet_unref(packet_);
        }
    }

    void release() noexcept {
        if (packet_ != nullptr) { av_packet_free(&packet_); }
        if (frame_ != nullptr) { av_frame_free(&frame_); }
        if (upload_ != nullptr) { av_frame_free(&upload_); }
        if (context_ != nullptr) { avcodec_free_context(&context_); }
        av_buffer_unref(&frames_);
        av_buffer_unref(&device_);
        if (scale_ != nullptr) { sws_freeContext(scale_); scale_ = nullptr; }
    }

    static std::string error_string(int error) {
        std::array<char, AV_ERROR_MAX_STRING_SIZE> message {};
        av_strerror(error, message.data(), message.size());
        return message.data();
    }

    std::uint32_t width_, height_, fps_;
    std::string encoder_;
    bool vaapi_, qsv_;
    const AVCodec *codec_ {};
    AVCodecContext *context_ {};
    AVFrame *frame_ {}, *upload_ {};
    AVPacket *packet_ {};
    AVBufferRef *device_ {}, *frames_ {};
    SwsContext *scale_ {};
    std::int64_t next_pts_ {};
    std::vector<Packet> pending_packets_;
};
#include "video_budget.hpp"
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
 qmdp::VideoBudget budget;
 for(int n=0;n<600;++n) {
  if(n==120)rate=2000;if(n==240)rate=8000;if(n==360)rate=1500;if(n==480)rate=8000;
  if(!budget.ready(qmdp::VideoBudget::Clock::time_point{}+std::chrono::microseconds(n*16667),rate))continue;
  for(auto&p:encoder.encode(pixels,false)) {
   budget.account(p.bytes.size());keys+=p.keyframe;total+=p.bytes.size();AVPacket pkt{};pkt.data=p.bytes.data();pkt.size=p.bytes.size();if(avcodec_send_packet(ctx,&pkt)<0)return 2;
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
 av_frame_free(&f);avcodec_free_context(&ctx);return decoded>100 && keys==1 && max_delta<0.15?0:3;
}
