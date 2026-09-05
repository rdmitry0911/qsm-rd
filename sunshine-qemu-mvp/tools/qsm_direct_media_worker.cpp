// SPDX-License-Identifier: GPL-3.0-or-later
//
// One VM-scoped direct QEMU Display1 -> WebRTC media worker.
//
// This executable has deliberately no TCP listener, HTTP parser, PVE
// credential, GameStream code, or desktop session dependency.  The PVE
// terminal broker has already checked VM.Console and starts it with three
// owner-private Unix sockets owned by the browser WebRTC bridge:
//
//   Display1 -> BGRA -> FFmpeg H.264 -> browser-video.sock
//   Display1 -> float PCM -> libopus -> browser-audio.sock
//   browser-input.sock -> bounded Display1 input calls
//
// The socket record format is shared with qsm_browser_bridge.py.  It
// keeps H.264 and Opus encoded end to end; aiortc only packetizes them as
// SRTP.  The worker is intentionally usable with NVENC, QSV, VA-API, or a
// software encoder selected by the node policy.

#include "core/pixel_format.hpp"
#include "core/resize_coalescer.hpp"
#include "interfaces/media_adapter.hpp"
#include "pipeline/desktop_session.hpp"
#include "qemu/qemu_dbus_display.hpp"

#include <opus/opus.h>

#if defined(QMDP_HAS_LIBAVCODEC)
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/error.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}
#endif

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <csignal>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fcntl.h>
#include <iostream>
#include <limits>
#include <mutex>
#include <memory>
#include <optional>
#include <poll.h>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <thread>
#include <unordered_set>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {

using namespace std::chrono_literals;

constexpr std::uint32_t packet_magic = 0x51534d50U; // QSMP
constexpr std::uint32_t packet_first = 0x00000001U;
constexpr std::uint32_t packet_end = 0x00000002U;
constexpr std::uint32_t packet_idr = 0x00000004U;
constexpr std::uint32_t packet_audio = 0x00000008U;
constexpr std::uint32_t packet_config = 0x00000010U;
// A Display1 cursor is state, not video. It travels on the existing private
// video socket but bypasses H.264 and video presentation latency.
constexpr std::uint32_t packet_cursor = 0x00000020U;
constexpr std::uint32_t input_magic = 0x51534d49U; // QSMI
constexpr std::uint8_t input_version = 1U;
constexpr std::uint8_t input_mouse_position = 1U;
constexpr std::uint8_t input_mouse_button = 2U;
constexpr std::uint8_t input_keyboard = 3U;
constexpr std::uint8_t input_scroll = 4U;
constexpr std::uint8_t input_resize = 5U;
// Private server-to-worker recovery request. It is emitted only after an
// authenticated WebRTC peer sends RTCP PLI, never from browser JSON input.
constexpr std::uint8_t input_keyframe_request = 6U;
constexpr std::size_t packet_header_size = 20U;
constexpr std::size_t input_header_size = 8U;
constexpr std::size_t max_fragment_bytes = 256U * 1024U;
constexpr std::size_t max_access_unit_bytes = 4U * 1024U * 1024U;
constexpr std::uint16_t max_cursor_edge = 64U;
constexpr std::size_t cursor_wire_header_size = 34U;
constexpr std::uint8_t cursor_wire_version = 1U;
constexpr std::uint8_t cursor_visible = 0x01U;
constexpr std::uint8_t cursor_has_shape = 0x02U;
constexpr std::uint32_t opus_sample_rate = 48'000U;
constexpr std::uint16_t opus_channels = 2U;
constexpr std::uint16_t opus_samples_per_frame = 960U; // 20 ms

// RFC 1982-style comparison for the browser's wrapping u32 pointer serial.
// Half the serial space is intentionally not considered newer, so one
// malformed/delayed packet can never turn a current pointer stream backwards.
bool pointer_sequence_is_newer(std::uint32_t candidate, std::uint32_t previous) noexcept {
    return candidate != previous && static_cast<std::uint32_t>(candidate - previous) < 0x8000'0000U;
}

// QEMU is permitted to announce the same cursor shape repeatedly.  Its D-Bus
// Display1 implementation creates a fresh backing object for every
// CursorDefine, so pointer identity is not a shape generation.  Treating that
// allocation as a new browser cursor sends another BGRA image over the ordered
// WebRTC control channel and can starve the much smaller MouseSet updates.
// Compare the actual, bounded Display1 value instead.
bool same_cursor_shape(const std::shared_ptr<const qmdp::CursorShape> &left,
                       const std::shared_ptr<const qmdp::CursorShape> &right) noexcept {
    if (left == right) {
        return true;
    }
    if (!left || !right || left->width != right->width || left->height != right->height ||
        left->hotspot_x != right->hotspot_x || left->hotspot_y != right->hotspot_y ||
        left->argb.size() != right->argb.size()) {
        return false;
    }
    return std::equal(left->argb.begin(), left->argb.end(), right->argb.begin());
}

class WorkerError final : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct Size {
    std::uint32_t width {};
    std::uint32_t height {};
};

struct Options {
    std::string dbus_address;
    std::string video_socket;
    std::string audio_socket;
    std::string input_socket;
    std::string encoder {"libx264"};
    std::optional<std::string> vaapi_device;
    std::uint32_t fps {60U};
    std::optional<Size> initial_size;
};

[[noreturn]] void usage(int status) {
    auto &stream = status == EXIT_SUCCESS ? std::cout : std::cerr;
    stream
        << "usage: qsm-direct-media-worker --dbus-address ADDRESS "
           "--video-socket unix:PATH --audio-socket unix:PATH "
           "--input-socket unix:PATH [options]\n\n"
        << "  --encoder NAME              FFmpeg H.264 encoder (default: libx264)\n"
        << "  --vaapi-device /dev/dri/renderD<N>  VA-API node for h264_vaapi\n"
        << "  --fps N                     10..240 (default: 60)\n"
        << "  --initial-size WIDTHxHEIGHT request initial guest scanout\n";
    std::exit(status);
}

template <typename Integer>
Integer parse_integer(std::string_view value, std::string_view name) {
    Integer parsed {};
    const auto result = std::from_chars(value.data(), value.data() + value.size(), parsed);
    if (result.ec != std::errc {} || result.ptr != value.data() + value.size()) {
        throw WorkerError("invalid " + std::string(name));
    }
    return parsed;
}

Size parse_size(std::string_view value) {
    const auto separator = value.find_first_of("xX");
    if (separator == std::string_view::npos) {
        throw WorkerError("invalid display size");
    }
    const auto width = parse_integer<std::uint32_t>(value.substr(0U, separator), "display width");
    const auto height = parse_integer<std::uint32_t>(value.substr(separator + 1U), "display height");
    if (width < 64U || height < 64U || width > 16'384U || height > 16'384U) {
        throw WorkerError("display size is outside 64..16384 pixels");
    }
    return {.width = width, .height = height};
}

std::string socket_path(std::string_view value, std::string_view name) {
    constexpr std::string_view prefix {"unix:"};
    if (!value.starts_with(prefix) || value.size() == prefix.size()) {
        throw WorkerError(std::string(name) + " must be a unix: socket path");
    }
    const std::string path(value.substr(prefix.size()));
    if (path.front() != '/' || path.find('\0') != std::string::npos || path.size() >= 104U) {
        throw WorkerError(std::string(name) + " is invalid");
    }
    return path;
}

Options parse_options(int argc, char **argv) {
    Options options;
    auto next = [&](int &index, std::string_view name) -> std::string_view {
        if (index + 1 >= argc) {
            throw WorkerError(std::string(name) + " requires a value");
        }
        return argv[++index];
    };
    for (int index = 1; index < argc; ++index) {
        const std::string_view argument(argv[index]);
        if (argument == "--dbus-address") {
            options.dbus_address = next(index, argument);
        } else if (argument == "--video-socket") {
            options.video_socket = socket_path(next(index, argument), argument);
        } else if (argument == "--audio-socket") {
            options.audio_socket = socket_path(next(index, argument), argument);
        } else if (argument == "--input-socket") {
            options.input_socket = socket_path(next(index, argument), argument);
        } else if (argument == "--encoder") {
            options.encoder = next(index, argument);
        } else if (argument == "--vaapi-device") {
            options.vaapi_device = std::string(next(index, argument));
        } else if (argument == "--fps") {
            options.fps = parse_integer<std::uint32_t>(next(index, argument), "frame rate");
        } else if (argument == "--initial-size") {
            options.initial_size = parse_size(next(index, argument));
        } else if (argument == "--help" || argument == "-h") {
            usage(EXIT_SUCCESS);
        } else {
            throw WorkerError("unknown option: " + std::string(argument));
        }
    }
    if (options.dbus_address.empty() || options.video_socket.empty() || options.audio_socket.empty() ||
        options.input_socket.empty() || options.encoder.empty() || options.fps < 10U || options.fps > 240U) {
        throw WorkerError("required worker options are missing or invalid");
    }
    if (options.encoder == "h264_vaapi" && (!options.vaapi_device ||
        !std::string_view(*options.vaapi_device).starts_with("/dev/dri/renderD"))) {
        throw WorkerError("h264_vaapi requires a DRM render node");
    }
    return options;
}

void append_u32(std::array<std::uint8_t, packet_header_size> &output,
                std::size_t offset, std::uint32_t value) {
    output[offset] = static_cast<std::uint8_t>(value >> 24U);
    output[offset + 1U] = static_cast<std::uint8_t>(value >> 16U);
    output[offset + 2U] = static_cast<std::uint8_t>(value >> 8U);
    output[offset + 3U] = static_cast<std::uint8_t>(value);
}

void append_u16(std::vector<std::uint8_t> &output, std::uint16_t value) {
    output.push_back(static_cast<std::uint8_t>(value >> 8U));
    output.push_back(static_cast<std::uint8_t>(value));
}

void append_u32(std::vector<std::uint8_t> &output, std::uint32_t value) {
    output.push_back(static_cast<std::uint8_t>(value >> 24U));
    output.push_back(static_cast<std::uint8_t>(value >> 16U));
    output.push_back(static_cast<std::uint8_t>(value >> 8U));
    output.push_back(static_cast<std::uint8_t>(value));
}

void append_u64(std::vector<std::uint8_t> &output, std::uint64_t value) {
    append_u32(output, static_cast<std::uint32_t>(value >> 32U));
    append_u32(output, static_cast<std::uint32_t>(value));
}

std::uint32_t read_u32(std::span<const std::uint8_t> input, std::size_t offset) {
    return (static_cast<std::uint32_t>(input[offset]) << 24U) |
           (static_cast<std::uint32_t>(input[offset + 1U]) << 16U) |
           (static_cast<std::uint32_t>(input[offset + 2U]) << 8U) |
           static_cast<std::uint32_t>(input[offset + 3U]);
}

std::uint16_t read_u16(std::span<const std::uint8_t> input, std::size_t offset) {
    return static_cast<std::uint16_t>((static_cast<std::uint16_t>(input[offset]) << 8U) |
                                      input[offset + 1U]);
}

std::int16_t read_i16(std::span<const std::uint8_t> input, std::size_t offset) {
    return static_cast<std::int16_t>(read_u16(input, offset));
}

class PacketSink {
public:
    struct Stats {
        std::uint64_t video_access_units {};
        std::uint64_t video_records {};
        std::uint64_t video_send_failures {};
        std::uint64_t cursor_records {};
        std::uint64_t cursor_shape_records {};
    };

    PacketSink(std::string video_path, std::string audio_path)
        : video_(connect(video_path)), audio_(connect(audio_path)) {}

    PacketSink(const PacketSink &) = delete;
    PacketSink &operator=(const PacketSink &) = delete;

    void send_video(bool keyframe, std::span<const std::uint8_t> data) noexcept {
        if (send(video_, ++video_number_, keyframe ? packet_idr : 0U, data)) {
            std::lock_guard lock(mutex_);
            ++video_access_units_;
        }
    }

    void send_audio_config() noexcept {
        const std::array<std::uint8_t, 0U> empty {};
        send(audio_, 0U, packet_audio | packet_config, empty, opus_samples_per_frame);
    }

    void send_audio(std::span<const std::uint8_t> data) noexcept {
        send(audio_, ++audio_number_, packet_audio, data);
    }

    void send_cursor(const qmdp::CursorState &cursor, bool include_shape,
                     std::uint64_t shape_id) noexcept {
        try {
            std::uint16_t width = 0U;
            std::uint16_t height = 0U;
            std::uint16_t hotspot_x = 0U;
            std::uint16_t hotspot_y = 0U;
            std::span<const std::uint8_t> pixels;
            bool usable_shape = false;
            if (cursor.shape) {
                const auto &shape = *cursor.shape;
                const auto expected = static_cast<std::size_t>(shape.width) * shape.height * 4U;
                usable_shape = shape.width > 0U && shape.height > 0U &&
                    shape.width <= max_cursor_edge && shape.height <= max_cursor_edge &&
                    shape.hotspot_x < shape.width && shape.hotspot_y < shape.height &&
                    shape.argb.size() == expected;
                if (usable_shape) {
                    width = static_cast<std::uint16_t>(shape.width);
                    height = static_cast<std::uint16_t>(shape.height);
                    hotspot_x = static_cast<std::uint16_t>(shape.hotspot_x);
                    hotspot_y = static_cast<std::uint16_t>(shape.hotspot_y);
                    if (include_shape) {
                        pixels = shape.argb;
                    }
                }
            }
            // A custom cursor beyond the browser's bounded wire format is
            // represented as hidden rather than risking a large payload in a
            // latency-sensitive local media queue.
            const bool visible = cursor.visible && usable_shape;
            std::vector<std::uint8_t> payload;
            payload.reserve(cursor_wire_header_size + pixels.size());
            payload.push_back(cursor_wire_version);
            payload.push_back(static_cast<std::uint8_t>((visible ? cursor_visible : 0U) |
                                                        (!pixels.empty() ? cursor_has_shape : 0U)));
            append_u64(payload, cursor.sequence);
            // This is a VM-local monotonic cursor-shape generation, never a
            // pointer or an implementation address exposed to a browser.
            append_u64(payload, shape_id);
            append_u32(payload, static_cast<std::uint32_t>(cursor.x));
            append_u32(payload, static_cast<std::uint32_t>(cursor.y));
            append_u16(payload, width);
            append_u16(payload, height);
            append_u16(payload, hotspot_x);
            append_u16(payload, hotspot_y);
            payload.insert(payload.end(), pixels.begin(), pixels.end());
            if (send(video_, cursor_number_.fetch_add(1U) + 1U, packet_cursor, payload)) {
                std::lock_guard lock(mutex_);
                ++cursor_records_;
                if (!pixels.empty()) {
                    ++cursor_shape_records_;
                }
            }
        } catch (...) {
            // Cursor publication must never backpressure or terminate the
            // Display1 event loop. The next mouse event will retry it.
        }
    }

    [[nodiscard]] Stats stats() const noexcept {
        std::lock_guard lock(mutex_);
        return {
            .video_access_units = video_access_units_,
            .video_records = video_records_,
            .video_send_failures = video_send_failures_,
            .cursor_records = cursor_records_,
            .cursor_shape_records = cursor_shape_records_,
        };
    }

    static int connect(const std::string &path) {
        if (path.size() >= sizeof(sockaddr_un {}.sun_path)) {
            throw WorkerError("Unix socket path is too long");
        }
        const int descriptor = ::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
        if (descriptor < 0) {
            throw WorkerError("cannot create direct media socket");
        }
        sockaddr_un address {};
        address.sun_family = AF_UNIX;
        std::memcpy(address.sun_path, path.c_str(), path.size() + 1U);
        if (::connect(descriptor, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) < 0) {
            const auto error = errno;
            ::close(descriptor);
            errno = error;
            throw WorkerError("cannot connect direct media socket");
        }
        return descriptor;
    }

private:

    bool send(int descriptor, std::uint32_t number, std::uint32_t type_flags,
              std::span<const std::uint8_t> data,
              std::uint32_t config_fragment = 0U) noexcept {
        if (descriptor < 0 || data.size() > max_access_unit_bytes) {
            return false;
        }
        std::lock_guard lock(mutex_);
        std::size_t offset = 0U;
        std::uint32_t fragment = 0U;
        do {
            const auto remaining = data.size() - offset;
            const auto length = std::min<std::size_t>(remaining, max_fragment_bytes);
            std::uint32_t flags = type_flags;
            if (fragment == 0U) {
                flags |= packet_first;
            }
            if (offset + length == data.size()) {
                flags |= packet_end;
            }
            std::array<std::uint8_t, packet_header_size> header {};
            append_u32(header, 0U, packet_magic);
            append_u32(header, 4U, number);
            append_u32(header, 8U, config_fragment != 0U ? config_fragment : fragment);
            append_u32(header, 12U, flags);
            append_u32(header, 16U, static_cast<std::uint32_t>(length));
            iovec parts[2] {
                {.iov_base = header.data(), .iov_len = header.size()},
                {.iov_base = const_cast<std::uint8_t *>(data.data() + offset), .iov_len = length},
            };
            msghdr message {};
            message.msg_iov = parts;
            message.msg_iovlen = 2U;
            const auto sent = ::sendmsg(descriptor, &message, MSG_DONTWAIT | MSG_NOSIGNAL);
            if (sent != static_cast<ssize_t>(header.size() + length)) {
                // The bridge's assembler deliberately drops this incomplete
                // AU and recovers on the next FIRST packet. Do not ever make
                // Display1 or the encoder wait behind a browser reader.
                if (descriptor == video_) {
                    ++video_send_failures_;
                }
                return false;
            }
            if (descriptor == video_) {
                ++video_records_;
            }
            offset += length;
            ++fragment;
        } while (offset < data.size() || (data.empty() && fragment == 0U));
        return true;
    }

    int video_ {-1};
    int audio_ {-1};
    mutable std::mutex mutex_;
    std::uint32_t video_number_ {};
    std::uint32_t audio_number_ {};
    std::atomic<std::uint32_t> cursor_number_ {};
    std::uint64_t video_access_units_ {};
    std::uint64_t video_records_ {};
    std::uint64_t video_send_failures_ {};
    std::uint64_t cursor_records_ {};
    std::uint64_t cursor_shape_records_ {};
};

#if defined(QMDP_HAS_LIBAVCODEC)
class LibavH264Encoder final {
public:
    struct Packet {
        std::vector<std::uint8_t> bytes;
        bool keyframe {};
    };

    LibavH264Encoder(std::uint32_t width, std::uint32_t height, std::uint32_t fps) {
        if (width == 0U || height == 0U || width > static_cast<std::uint32_t>(INT_MAX) ||
            height > static_cast<std::uint32_t>(INT_MAX) || fps == 0U ||
            fps > static_cast<std::uint32_t>(INT_MAX)) {
            throw WorkerError("invalid in-process H.264 dimensions");
        }
        const auto *codec = avcodec_find_encoder_by_name("libx264");
        if (codec == nullptr) {
            throw WorkerError("libavcodec has no libx264 encoder");
        }
        context_ = avcodec_alloc_context3(codec);
        frame_ = av_frame_alloc();
        packet_ = av_packet_alloc();
        if (context_ == nullptr || frame_ == nullptr || packet_ == nullptr) {
            throw WorkerError("cannot allocate in-process H.264 encoder");
        }
        context_->width = static_cast<int>(width);
        context_->height = static_cast<int>(height);
        context_->pix_fmt = AV_PIX_FMT_YUV420P;
        context_->time_base = AVRational {1, static_cast<int>(fps)};
        context_->framerate = AVRational {static_cast<int>(fps), 1};
        context_->gop_size = 30;
        context_->max_b_frames = 0;
        context_->flags |= AV_CODEC_FLAG_LOW_DELAY;
        // Match Sunshine's CPU configuration: two slice threads give useful
        // parallelism without frame-thread reordering latency.
        context_->thread_type = FF_THREAD_SLICE;
        context_->thread_count = 2;

        AVDictionary *options = nullptr;
        (void) av_dict_set(&options, "preset", "ultrafast", 0);
        (void) av_dict_set(&options, "tune", "zerolatency", 0);
        (void) av_dict_set(&options, "aud", "1", 0);
        (void) av_dict_set(&options, "x264-params",
                           "aud=1:keyint=30:min-keyint=30:scenecut=0:bframes=0:"
                           "repeat-headers=1:sliced-threads=1:sync-lookahead=0:rc-lookahead=0",
                           0);
        const int opened = avcodec_open2(context_, codec, &options);
        av_dict_free(&options);
        if (opened < 0) {
            throw WorkerError("cannot initialise in-process libx264: " + error_string(opened));
        }

        frame_->format = context_->pix_fmt;
        frame_->width = context_->width;
        frame_->height = context_->height;
        const int allocated = av_frame_get_buffer(frame_, 32);
        if (allocated < 0) {
            throw WorkerError("cannot allocate in-process H.264 frame: " + error_string(allocated));
        }
        scale_ = sws_getContext(context_->width, context_->height, AV_PIX_FMT_BGRA,
                                context_->width, context_->height, context_->pix_fmt,
                                SWS_FAST_BILINEAR, nullptr, nullptr, nullptr);
        if (scale_ == nullptr) {
            throw WorkerError("cannot initialise BGRA to H.264 conversion");
        }
    }

    ~LibavH264Encoder() {
        if (packet_ != nullptr) { av_packet_free(&packet_); }
        if (frame_ != nullptr) { av_frame_free(&frame_); }
        if (context_ != nullptr) { avcodec_free_context(&context_); }
        if (scale_ != nullptr) { sws_freeContext(scale_); }
    }

    LibavH264Encoder(const LibavH264Encoder &) = delete;
    LibavH264Encoder &operator=(const LibavH264Encoder &) = delete;

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
            throw WorkerError("in-process libx264 rejected a frame: " + error_string(submitted));
        }
        std::vector<Packet> result;
        while (true) {
            const int received = avcodec_receive_packet(context_, packet_);
            if (received == AVERROR(EAGAIN) || received == AVERROR_EOF) {
                break;
            }
            if (received < 0) {
                throw WorkerError("in-process libx264 did not produce H.264: " + error_string(received));
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
};
#endif

class DirectMediaAdapter final : public qmdp::IMediaAdapter {
public:
    struct VideoStats {
        std::uint64_t submitted_frames {};
        std::uint64_t keyframe_requests {};
        std::uint64_t sampled_pixels {};
        std::uint64_t non_black_pixels {};
        std::uint64_t luma_sum {};
        std::uint8_t luma_min {255U};
        std::uint8_t luma_max {};
    };

    DirectMediaAdapter(PacketSink &sink, std::string encoder,
                       std::optional<std::string> vaapi_device, std::uint32_t fps)
        : sink_(sink), encoder_(std::move(encoder)), vaapi_device_(std::move(vaapi_device)), fps_(fps) {}

    ~DirectMediaAdapter() override { stop(); }

    void start() override {
        if (running_.exchange(true)) {
            throw WorkerError("direct media adapter is already running");
        }
        (void) std::signal(SIGPIPE, SIG_IGN);
        int opus_error = OPUS_OK;
        opus_.reset(opus_encoder_create(static_cast<opus_int32>(opus_sample_rate), opus_channels,
                                        OPUS_APPLICATION_AUDIO, &opus_error));
        if (!opus_ || opus_error != OPUS_OK) {
            running_ = false;
            throw WorkerError("cannot initialise Opus encoder");
        }
        (void) opus_encoder_ctl(opus_.get(), OPUS_SET_BITRATE(128000));
        (void) opus_encoder_ctl(opus_.get(), OPUS_SET_COMPLEXITY(5));
        sink_.send_audio_config();
        video_thread_ = std::thread(&DirectMediaAdapter::video_loop, this);
    }

    void stop() noexcept override {
        running_ = false;
        video_cv_.notify_all();
        if (video_thread_.joinable()) {
            video_thread_.join();
        }
        std::lock_guard lock(video_mutex_);
        close_video_process();
        opus_.reset();
        audio_pending_.clear();
    }

    void submit_frame(const qmdp::FrameToken &frame) override {
        if (!running_.load() || !frame.valid()) {
            throw WorkerError("invalid direct media frame");
        }
        const auto *cpu = frame.surface->cpu();
        if (cpu == nullptr || !cpu->bytes) {
            throw WorkerError("direct media requires CPU-readable Display1 frames");
        }
        auto bgra = std::make_shared<const std::vector<std::uint8_t>>(
            qmdp::to_bgra(*cpu->bytes, frame.surface->width, frame.surface->height,
                          cpu->stride, cpu->pixman_format));
        observe_source_frame(*bgra);
        {
            std::lock_guard lock(video_mutex_);
            if (!running_.load()) {
                throw WorkerError("direct media adapter is stopping");
            }
            latest_bgra_ = std::move(bgra);
            latest_width_ = frame.surface->width;
            latest_height_ = frame.surface->height;
            frame_changed_ = true;
        }
        video_cv_.notify_one();
    }

    void submit_cursor(const qmdp::CursorState &cursor) override {
        if (!running_.load()) {
            return;
        }
        bool include_shape = false;
        std::uint64_t shape_id = 0U;
        {
            std::lock_guard lock(cursor_mutex_);
            include_shape = !same_cursor_shape(cursor.shape, last_cursor_shape_);
            last_cursor_shape_ = cursor.shape;
            if (include_shape && cursor.shape) {
                ++cursor_shape_id_;
                if (cursor_shape_id_ == 0U) { ++cursor_shape_id_; }
            }
            shape_id = cursor.shape ? cursor_shape_id_ : 0U;
        }
        sink_.send_cursor(cursor, include_shape, shape_id);
    }

    void submit_audio(std::span<const float> interleaved_samples, std::uint32_t sample_rate,
                      std::uint16_t channels) override {
        if (!running_.load() || sample_rate != opus_sample_rate || channels != opus_channels || !opus_) {
            return;
        }
        std::lock_guard lock(audio_mutex_);
        audio_pending_.insert(audio_pending_.end(), interleaved_samples.begin(), interleaved_samples.end());
        const auto samples = static_cast<std::size_t>(opus_samples_per_frame) * opus_channels;
        while (audio_pending_.size() >= samples) {
            std::array<unsigned char, 4096U> encoded {};
            const auto result = opus_encode_float(opus_.get(), audio_pending_.data(),
                                                  opus_samples_per_frame, encoded.data(),
                                                  static_cast<opus_int32>(encoded.size()));
            audio_pending_.erase(audio_pending_.begin(), audio_pending_.begin() +
                                 static_cast<std::ptrdiff_t>(samples));
            if (result > 0) {
                sink_.send_audio({encoded.data(), static_cast<std::size_t>(result)});
            }
        }
    }

    void request_idr() override {
        ++keyframe_requests_;
        force_idr_ = true;
        video_cv_.notify_one();
    }

    [[nodiscard]] VideoStats video_stats() const noexcept {
        std::lock_guard lock(stats_mutex_);
        auto result = video_stats_;
        result.keyframe_requests = keyframe_requests_.load();
        return result;
    }

private:
    struct OpusDeleter {
        void operator()(OpusEncoder *encoder) const noexcept { opus_encoder_destroy(encoder); }
    };

    static void write_all(int descriptor, std::span<const std::uint8_t> data) {
        std::size_t offset = 0U;
        while (offset < data.size()) {
            const auto written = ::write(descriptor, data.data() + offset, data.size() - offset);
            if (written > 0) {
                offset += static_cast<std::size_t>(written);
                continue;
            }
            if (written < 0 && errno == EINTR) {
                continue;
            }
            throw WorkerError("direct H.264 encoder stopped accepting frames");
        }
    }

    void video_loop() noexcept {
        // Sunshine deliberately keeps its encoder active even for a static
        // desktop.  A browser WebRTC receiver also needs that regularity, but
        // unlike GameStream it cannot use the client's presentation queue to
        // hide a half-rate source. Repeat at the negotiated cadence. Damage
        // replaces the pending frame, but it must not bypass the next tick:
        // Display1 is damage-driven and can report more than the negotiated
        // FPS while KWin repaints a hover or an active-window decoration.
        // Emitting those extra access units while assigning each a 1/fps RTP
        // duration makes Chromium present alternating stale/current frames
        // as a visibly shaking desktop.
        const auto repeat_interval = std::chrono::microseconds(1'000'000U / fps_);
        auto next_frame = std::chrono::steady_clock::now();
        std::unique_lock lock(video_mutex_);
        while (running_.load()) {
            if (!latest_bgra_) {
                video_cv_.wait(lock, [this] { return !running_.load() || latest_bgra_ != nullptr; });
                next_frame = std::chrono::steady_clock::now();
                continue;
            }
            // Do not make frame_changed_ a wake predicate here. A busy guest
            // may replace latest_bgra_ arbitrarily often, but one negotiated
            // media tick selects exactly one newest frame. The stopping
            // predicate still makes close immediate.
            (void) video_cv_.wait_until(lock, next_frame, [this] {
                return !running_.load();
            });
            if (!running_.load()) {
                break;
            }
            if (std::chrono::steady_clock::now() < next_frame) {
                continue;
            }

            const auto bgra = latest_bgra_;
            const auto width = latest_width_;
            const auto height = latest_height_;
            frame_changed_ = false;
            lock.unlock();
            try {
                const bool force_idr = force_idr_.exchange(false);
#if defined(QMDP_HAS_LIBAVCODEC)
                if (encoder_ == "libx264") {
                    if (!software_encoder_ || width_ != width || height_ != height) {
                        close_video_process();
                        open_video_process(width, height);
                    }
                    for (auto &packet : software_encoder_->encode(*bgra, force_idr)) {
                        sink_.send_video(packet.keyframe, packet.bytes);
                    }
                } else {
#endif
                const bool fresh_encoder = video_input_ < 0 || width_ != width || height_ != height || force_idr;
                if (fresh_encoder) {
                    close_video_process();
                    open_video_process(width, height);
                }
                write_all(video_input_, *bgra);
                if (fresh_encoder) {
                    // The first raw video frame may not be emitted until FFmpeg
                    // sees another timestamp. Establish a decodable console
                    // picture immediately; steady-state repeats are paced.
                    write_all(video_input_, *bgra);
                }
#if defined(QMDP_HAS_LIBAVCODEC)
                }
#endif
            } catch (...) {
                running_ = false;
                video_cv_.notify_all();
                break;
            }
            lock.lock();
            // Keep the media cadence measured from the preceding presentation
            // deadline, not from the end of BGRA conversion and encoding.
            // Starting a new interval after libx264 work turns a nominal
            // 60-FPS console into ~48 FPS on the reference VirGL desktop.
            // A genuine encoder stall is not allowed to create a catch-up
            // burst: it resumes from the current time and the latest-frame
            // mailbox discards obsolete damage in the meantime.
            next_frame += repeat_interval;
            const auto now = std::chrono::steady_clock::now();
            if (next_frame < now) {
                next_frame = now;
            }
        }
    }

    void observe_source_frame(std::span<const std::uint8_t> bgra) noexcept {
        constexpr std::size_t maximum_samples = 4096U;
        const auto pixels = bgra.size() / 4U;
        if (pixels == 0U) {
            return;
        }
        const auto stride = std::max<std::size_t>(1U, pixels / maximum_samples);
        std::uint64_t samples = 0U;
        std::uint64_t non_black = 0U;
        std::uint64_t luma_sum = 0U;
        std::uint8_t luma_min = 255U;
        std::uint8_t luma_max = 0U;
        for (std::size_t pixel = 0U; pixel < pixels; pixel += stride) {
            const auto offset = pixel * 4U;
            // qmdp::to_bgra deliberately normalises the captured surface to
            // B, G, R, A. Integer BT.601 luma is enough to distinguish an
            // all-black GL scanout from a rendered guest without retaining
            // any guest pixels in process memory or logs.
            const auto luma = static_cast<std::uint8_t>(
                (29U * bgra[offset] + 150U * bgra[offset + 1U] +
                 77U * bgra[offset + 2U]) >> 8U);
            ++samples;
            luma_sum += luma;
            luma_min = std::min(luma_min, luma);
            luma_max = std::max(luma_max, luma);
            if (luma > 10U) {
                ++non_black;
            }
        }
        std::lock_guard lock(stats_mutex_);
        ++video_stats_.submitted_frames;
        video_stats_.sampled_pixels += samples;
        video_stats_.non_black_pixels += non_black;
        video_stats_.luma_sum += luma_sum;
        video_stats_.luma_min = std::min(video_stats_.luma_min, luma_min);
        video_stats_.luma_max = std::max(video_stats_.luma_max, luma_max);
    }

    void open_video_process(std::uint32_t width, std::uint32_t height) {
#if defined(QMDP_HAS_LIBAVCODEC)
        if (encoder_ == "libx264") {
            software_encoder_ = std::make_unique<LibavH264Encoder>(width, height, fps_);
            width_ = width;
            height_ = height;
            return;
        }
#endif
        int input[2] {-1, -1};
        int output[2] {-1, -1};
        if (::pipe2(input, O_CLOEXEC) < 0 || ::pipe2(output, O_CLOEXEC) < 0) {
            if (input[0] >= 0) { ::close(input[0]); ::close(input[1]); }
            if (output[0] >= 0) { ::close(output[0]); ::close(output[1]); }
            throw WorkerError("cannot create direct H.264 encoder pipes");
        }
        const auto pid = ::fork();
        if (pid < 0) {
            ::close(input[0]); ::close(input[1]); ::close(output[0]); ::close(output[1]);
            throw WorkerError("cannot start direct H.264 encoder");
        }
        if (pid == 0) {
            if (::dup2(input[0], STDIN_FILENO) < 0 || ::dup2(output[1], STDOUT_FILENO) < 0) {
                _exit(126);
            }
            const int null_fd = ::open("/dev/null", O_WRONLY | O_CLOEXEC);
            if (null_fd >= 0) {
                (void) ::dup2(null_fd, STDERR_FILENO);
                ::close(null_fd);
            }
            ::close(input[0]); ::close(input[1]); ::close(output[0]); ::close(output[1]);
            const std::string dimensions = std::to_string(width) + "x" + std::to_string(height);
            const std::string fps = std::to_string(fps_);
            std::vector<std::string> arguments {
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-fflags", "nobuffer",
                // FFmpeg otherwise starts a threaded demux/filter pipeline
                // with several raw frames in flight. It is throughput-friendly
                // but defeats an interactive desktop where only the newest
                // frame is useful.
                "-filter_threads", "1", "-filter_complex_threads", "1", "-thread_queue_size", "1",
                "-f", "rawvideo", "-pixel_format", "bgra", "-video_size", dimensions,
                "-framerate", fps, "-i", "pipe:0", "-an", "-c:v", encoder_, "-flags", "low_delay",
            };
            if (encoder_ == "h264_vaapi") {
                arguments.insert(arguments.end(), {"-vaapi_device", *vaapi_device_, "-vf", "format=nv12,hwupload"});
            } else if (encoder_ == "h264_qsv") {
                arguments.insert(arguments.end(), {"-vf", "format=nv12"});
            }
            if (encoder_ == "h264_nvenc") {
                arguments.insert(arguments.end(), {"-preset", "p1", "-tune", "ll", "-forced-idr", "1",
                                                   "-zerolatency", "1", "-delay", "0", "-rc-lookahead", "0",
                                                   // A CBR NVENC stream pads a motionless desktop with H.264
                                                   // filler NALs up to its configured rate.  aiortc then has to
                                                   // pace bytes which carry no new pixels; on the real Chrome
                                                   // route that accumulated an approximately one-second queue
                                                   // before a password bullet could be presented.  Low-latency
                                                   // VBR retains the same 20 Mbps ceiling for changed desktops,
                                                   // but lets static and sparse GUI frames stay small enough for
                                                   // WebRTC's congestion controller to send immediately.
                                                   "-rc", "vbr", "-cq", "19", "-b:v", "8M", "-maxrate", "20M",
                                                   "-bufsize", "333k", "-g", "30", "-bf", "0"});
            } else if (encoder_ == "libx264") {
                // Keep software H.264 on slice, rather than frame, threads.
                // Frame threading retains several pictures before emitting the
                // first one; Sunshine uses two slice threads for exactly this
                // reason.  It gives a current desktop frame to the transport
                // without sacrificing the modest parallelism needed for CPU
                // fallback.
                arguments.insert(arguments.end(), {"-threads", "2", "-thread_type", "slice", "-slices", "2",
                                                   "-preset", "ultrafast", "-tune", "zerolatency",
                                                   "-x264-params", "aud=1:keyint=30:min-keyint=30:scenecut=0:bframes=0:repeat-headers=1"});
            }
            // FFmpeg's NVENC default is an effectively unbounded output
            // delay. `-delay 0` above is therefore essential rather than a
            // cosmetic tuning flag. The output bitstream filter repeats the
            // encoder's extradata on every IDR, allowing a late WebRTC fanout
            // subscriber to decode its cached bootstrap frame immediately.
            if (encoder_ == "h264_nvenc") {
                arguments.insert(arguments.end(), {"-bsf:v", "dump_extra=freq=k"});
            }
            // Do not force generic AVCodecContext latency flags here: some
            // supported hardware FFmpeg builds accept the WebRTC offer but
            // reject those flags only after the encoder process has started.
            // libx264 and NVENC receive their documented low-latency knobs
            // above; other approved encoders retain their known-good defaults.
            // Every supported H.264 encoder can emit an access-unit delimiter.
            // The packetizer uses that unambiguous boundary to keep its private
            // socket records frame-aligned; do not rely on encoder-specific
            // slice layouts or on a compatibility transport framing convention.
            // `pipe:1` otherwise uses FFmpeg's normal AVIO buffering.  That
            // can retain several H.264 access units before the reader thread
            // sees any bytes, turning a low-latency encoder into a visibly
            // delayed desktop. Flush each complete packet into the private
            // sequenced-packet tap instead.
            arguments.insert(arguments.end(), {"-aud", "1", "-pix_fmt", "yuv420p",
                                               "-flush_packets", "1", "-f", "h264", "pipe:1"});
            std::vector<char *> argv;
            argv.reserve(arguments.size() + 1U);
            for (auto &argument : arguments) { argv.push_back(argument.data()); }
            argv.push_back(nullptr);
            ::execvp(argv.front(), argv.data());
            _exit(127);
        }
        ::close(input[0]);
        ::close(output[1]);
        video_pid_ = pid;
        video_input_ = input[1];
        video_output_ = output[0];
        width_ = width;
        height_ = height;
        h264_thread_ = std::thread(&DirectMediaAdapter::h264_reader, this, video_output_);
    }

    static std::optional<std::pair<std::size_t, std::size_t>> start_code(
        std::span<const std::uint8_t> data, std::size_t begin) {
        for (std::size_t index = begin; index + 3U < data.size(); ++index) {
            if (data[index] == 0U && data[index + 1U] == 0U && data[index + 2U] == 1U) {
                return std::pair {index, 3U};
            }
            if (index + 4U <= data.size() && data[index] == 0U && data[index + 1U] == 0U &&
                data[index + 2U] == 0U && data[index + 3U] == 1U) {
                return std::pair {index, 4U};
            }
        }
        return std::nullopt;
    }

    void flush_nal(std::span<const std::uint8_t> nal, std::size_t prefix,
                   std::vector<std::uint8_t> &leading, std::vector<std::uint8_t> &access_unit,
                   bool &have_aud, bool &keyframe) noexcept {
        if (nal.size() <= prefix) {
            return;
        }
        const auto type = static_cast<std::uint8_t>(nal[prefix] & 0x1fU);
        if (type == 9U) { // Access-unit delimiter.
            if (have_aud && !access_unit.empty()) {
                sink_.send_video(keyframe, access_unit);
            }
            access_unit = std::move(leading);
            leading.clear();
            access_unit.insert(access_unit.end(), nal.begin(), nal.end());
            have_aud = true;
            keyframe = false;
            return;
        }
        if (!have_aud) {
            leading.insert(leading.end(), nal.begin(), nal.end());
            return;
        }
        access_unit.insert(access_unit.end(), nal.begin(), nal.end());
        keyframe = keyframe || type == 5U;
    }

    void h264_reader(int descriptor) noexcept {
        std::vector<std::uint8_t> buffer;
        std::vector<std::uint8_t> leading;
        std::vector<std::uint8_t> access_unit;
        bool have_aud = false;
        bool keyframe = false;
        std::array<std::uint8_t, 64U * 1024U> bytes {};
        const auto flush_final_access_unit = [&]() noexcept {
            const auto prefix = start_code(buffer, 0U);
            if (!prefix || prefix->first != 0U) {
                return;
            }
            // This runs only after FFmpeg has closed its stdout.  A pipe
            // becoming briefly empty is *not* an Annex-B boundary: NVENC can
            // emit a large NAL in several writes with scheduler gaps greater
            // than 2 ms. Flushing on such a gap used to split the NAL and
            // make Chrome discard otherwise received RTP frames. Steady
            // capture repeats at the negotiated FPS, so the next AUD closes
            // every live access unit with at most one frame of delay.
            flush_nal({buffer.data(), buffer.size()}, prefix->second,
                      leading, access_unit, have_aud, keyframe);
            buffer.clear();
            if (have_aud && !access_unit.empty()) {
                sink_.send_video(keyframe, access_unit);
                access_unit.clear();
                have_aud = false;
                keyframe = false;
            }
        };
        while (true) {
            pollfd wait_for_data {.fd = descriptor, .events = POLLIN, .revents = 0};
            const auto ready = ::poll(&wait_for_data, 1, -1);
            if (ready < 0) {
                if (errno == EINTR) { continue; }
                break;
            }
            if (wait_for_data.revents & (POLLERR | POLLNVAL)) {
                break;
            }
            const auto count = ::read(descriptor, bytes.data(), bytes.size());
            if (count == 0) { break; }
            if (count < 0) {
                if (errno == EINTR) { continue; }
                break;
            }
            buffer.insert(buffer.end(), bytes.begin(), bytes.begin() + count);
            if (buffer.size() > max_access_unit_bytes * 2U) {
                buffer.clear(); leading.clear(); access_unit.clear(); have_aud = false; keyframe = false;
                continue;
            }
            while (true) {
                const auto first = start_code(buffer, 0U);
                if (!first) { buffer.clear(); break; }
                if (first->first != 0U) {
                    buffer.erase(buffer.begin(), buffer.begin() + static_cast<std::ptrdiff_t>(first->first));
                }
                const auto prefix = start_code(buffer, 0U);
                const auto next = prefix ? start_code(buffer, prefix->second) : std::nullopt;
                if (!prefix || !next) { break; }
                flush_nal({buffer.data(), next->first}, prefix->second, leading, access_unit, have_aud, keyframe);
                buffer.erase(buffer.begin(), buffer.begin() + static_cast<std::ptrdiff_t>(next->first));
            }
        }
        flush_final_access_unit();
        if (have_aud && !access_unit.empty()) {
            sink_.send_video(keyframe, access_unit);
        }
        ::close(descriptor);
    }

    void close_video_process() noexcept {
#if defined(QMDP_HAS_LIBAVCODEC)
        software_encoder_.reset();
#endif
        if (video_input_ >= 0) {
            ::close(video_input_);
            video_input_ = -1;
        }
        if (h264_thread_.joinable()) {
            h264_thread_.join();
        }
        if (video_pid_ > 0) {
            int status = 0;
            const auto deadline = std::chrono::steady_clock::now() + 2s;
            while (::waitpid(video_pid_, &status, WNOHANG) == 0 && std::chrono::steady_clock::now() < deadline) {
                std::this_thread::sleep_for(10ms);
            }
            if (::waitpid(video_pid_, &status, WNOHANG) == 0) {
                (void) ::kill(video_pid_, SIGTERM);
                (void) ::waitpid(video_pid_, &status, 0);
            }
            video_pid_ = -1;
        }
        video_output_ = -1;
        width_ = 0U;
        height_ = 0U;
    }

    PacketSink &sink_;
    std::string encoder_;
    std::optional<std::string> vaapi_device_;
    std::uint32_t fps_ {};
    std::atomic<bool> running_ {false};
    std::atomic<bool> force_idr_ {false};
    std::atomic<std::uint64_t> keyframe_requests_ {};
    std::mutex video_mutex_;
    std::condition_variable video_cv_;
    std::thread video_thread_;
    std::shared_ptr<const std::vector<std::uint8_t>> latest_bgra_;
    std::uint32_t latest_width_ {};
    std::uint32_t latest_height_ {};
    bool frame_changed_ {false};
#if defined(QMDP_HAS_LIBAVCODEC)
    std::unique_ptr<LibavH264Encoder> software_encoder_;
#endif
    int video_input_ {-1};
    int video_output_ {-1};
    pid_t video_pid_ {-1};
    std::thread h264_thread_;
    std::uint32_t width_ {};
    std::uint32_t height_ {};
    mutable std::mutex stats_mutex_;
    VideoStats video_stats_;
    std::mutex cursor_mutex_;
    std::shared_ptr<const qmdp::CursorShape> last_cursor_shape_;
    std::uint64_t cursor_shape_id_ {};
    std::mutex audio_mutex_;
    std::unique_ptr<OpusEncoder, OpusDeleter> opus_;
    std::vector<float> audio_pending_;
};

class InputReceiver {
public:
    InputReceiver(std::string path, qmdp::DesktopSession &session,
                  DirectMediaAdapter &media, std::uint32_t fps)
        : path_(std::move(path)), session_(session), media_(media), fps_(fps) {}

    ~InputReceiver() { stop(); }

    void start() {
        descriptor_ = PacketSink::connect(path_);
        // Display1's pointer mode is a capability of the emulated device;
        // it does not change as the user moves the pointer.  Querying the
        // D-Bus property for every browser mouse event put a synchronous
        // round trip in front of every SetAbsPosition call.  Determine it
        // once when the authenticated input receiver attaches, so movement
        // is one D-Bus method call rather than a Get + method call sequence.
        absolute_pointer_ = session_.is_absolute_pointer();
        stopping_ = false;
        thread_ = std::thread(&InputReceiver::run, this);
    }

    void stop() noexcept {
        stopping_ = true;
        if (descriptor_ >= 0) {
            (void) ::shutdown(descriptor_, SHUT_RDWR);
        }
        if (thread_.joinable()) { thread_.join(); }
        // A browser may disappear while a mouse or key is held (notably when
        // Escape leaves browser full screen).  Display1 owns the emulated
        // input state, not the Unix peer, so merely closing this socket does
        // not synthesize the corresponding Release call in QEMU.
        release_held_input();
        if (descriptor_ >= 0) { ::close(descriptor_); descriptor_ = -1; }
    }

private:
    void set_size(std::uint32_t width, std::uint32_t height, std::uint32_t fps) {
        if (width < 64U || height < 64U || width > 16'384U || height > 16'384U || fps < 10U || fps > 240U) {
            return;
        }
        session_.set_ui_info({.request_id = ++resize_id_, .width = width, .height = height,
                              .refresh_millihz = fps * 1000U, .remote_scale_percent = 100U});
    }

    void release_held_input() noexcept {
        // This method runs after the receiver thread has stopped, therefore
        // it cannot race a Press from handle().  Best-effort release is the
        // safety boundary: an already disconnected Display1 peer must not
        // turn a normal console close into a process failure.
        for (std::uint8_t button = 0U; button < pressed_buttons_.size(); ++button) {
            if (!pressed_buttons_[button]) { continue; }
            try { session_.button(button, false); } catch (...) {}
            pressed_buttons_[button] = false;
        }
        for (const auto key : pressed_keys_) {
            try { session_.key(key, false); } catch (...) {}
        }
        pressed_keys_.clear();
    }

    void handle(std::span<const std::uint8_t> data) {
        if (data.size() < input_header_size || read_u32(data, 0U) != input_magic || data[4U] != input_version ||
            data.size() != input_header_size + read_u16(data, 6U)) {
            return;
        }
        const auto op = data[5U];
        const auto payload = data.subspan(input_header_size);
        if (op == input_mouse_position && (payload.size() == 8U || payload.size() == 12U)) {
            const auto x = read_u16(payload, 0U);
            const auto y = read_u16(payload, 2U);
            const auto width = read_u16(payload, 4U);
            const auto height = read_u16(payload, 6U);
            if (width == 0U || height == 0U || x >= width || y >= height) { return; }
            if (payload.size() == 12U) {
                const auto sequence = read_u32(payload, 8U);
                if (last_pointer_sequence_ && !pointer_sequence_is_newer(sequence, *last_pointer_sequence_)) {
                    return;
                }
                last_pointer_sequence_ = sequence;
            }
            if (absolute_pointer_) {
                session_.absolute_pointer(x, y);
            } else if (last_x_ && last_y_) {
                session_.relative_pointer(static_cast<std::int32_t>(x) - *last_x_,
                                          static_cast<std::int32_t>(y) - *last_y_);
            }
            last_x_ = x; last_y_ = y;
        } else if (op == input_mouse_button && payload.size() == 2U && payload[0] >= 1U && payload[0] <= 5U &&
                   (payload[1] == 0U || payload[1] == 1U)) {
            const auto button = static_cast<std::uint8_t>(payload[0] - 1U);
            const auto down = payload[1] == 1U;
            session_.button(button, down);
            pressed_buttons_[button] = down;
        } else if (op == input_keyboard && payload.size() == 4U && (payload[2] == 0U || payload[2] == 1U)) {
            const auto key = read_u16(payload, 0U);
            const auto down = payload[2] == 1U;
            session_.key(key, down);
            if (down) { pressed_keys_.insert(key); }
            else { pressed_keys_.erase(key); }
        } else if (op == input_scroll && payload.size() == 4U) {
            const auto vertical = read_i16(payload, 0U);
            // Display1 represents wheel movement with the conventional extra
            // button numbers. One bounded click per browser event prevents a
            // large DOM delta from becoming an unbounded QEMU call loop.
            if (vertical != 0) {
                const auto button = static_cast<std::uint8_t>(vertical > 0 ? 3U : 4U);
                session_.button(button, true); session_.button(button, false);
            }
        } else if (op == input_resize && payload.size() == 10U) {
            set_size(read_u32(payload, 0U), read_u32(payload, 4U), read_u16(payload, 8U));
        } else if (op == input_keyframe_request && payload.empty()) {
            // The browser decoder lost H.264 reference data. The following
            // media tick publishes an independently decodable IDR instead of
            // leaving visible macroblocks until the periodic keyframe.
            media_.request_idr();
        }
    }

    void run() noexcept {
        std::array<std::uint8_t, 1024U> data {};
        while (!stopping_.load()) {
            const auto received = ::recv(descriptor_, data.data(), data.size(), 0);
            if (received <= 0) { break; }
            try { handle({data.data(), static_cast<std::size_t>(received)}); }
            catch (const std::exception &) { break; }
        }
        release_held_input();
    }

    std::string path_;
    qmdp::DesktopSession &session_;
    DirectMediaAdapter &media_;
    std::uint32_t fps_ {};
    int descriptor_ {-1};
    std::atomic<bool> stopping_ {false};
    std::thread thread_;
    std::optional<std::uint16_t> last_x_;
    std::optional<std::uint16_t> last_y_;
    std::optional<std::uint32_t> last_pointer_sequence_;
    std::array<bool, 5U> pressed_buttons_ {};
    std::unordered_set<std::uint16_t> pressed_keys_;
    bool absolute_pointer_ {false};
    std::uint64_t resize_id_ {};
};

std::atomic<bool> stopping {false};
void on_signal(int) noexcept { stopping = true; }

std::string recent_error_summary(const qmdp::DesktopSession::Stats &stats) {
    if (stats.recent_errors.empty()) {
        return "none";
    }
    std::string result;
    constexpr std::size_t maximum = 160U;
    for (const unsigned char character : stats.recent_errors.back()) {
        if (result.size() >= maximum) {
            result += "...";
            break;
        }
        // The D-Bus peer owns this message. Keep the root-only diagnostic on
        // one printable journal line; it must never make an arbitrary log
        // line or terminal escape sequence.
        result += (character >= 0x20U && character <= 0x7eU) ?
            static_cast<char>(character) : '?';
    }
    return result.empty() ? "empty" : result;
}

void write_session_diagnostic(std::string_view event,
                              const qmdp::DesktopSession::Stats &stats,
                              const qmdp::QemuDbusDisplay::Stats &display,
                              const DirectMediaAdapter::VideoStats &source,
                              const PacketSink::Stats &egress) {
    const auto source_luma_mean = source.sampled_pixels == 0U ? 0U :
        source.luma_sum / source.sampled_pixels;
    const auto dmabuf_readback_mean = display.dmabuf_readback_samples == 0U ? 0U :
        display.dmabuf_readback_total_microseconds / display.dmabuf_readback_samples;
    std::cerr << "QSM_DIRECT_MEDIA_" << event
              << " encoded_frames=" << stats.encoded_frames
              << " errors=" << stats.errors
              << " display_failed=" << (stats.display_failed ? "yes" : "no")
              << " latest_frame_published=" << stats.mailbox.published
              << " latest_frame_consumed=" << stats.mailbox.consumed
              << " latest_frame_dropped=" << stats.mailbox.dropped
              << " dmabuf_readback_samples=" << display.dmabuf_readback_samples
              << " dmabuf_readback_mean_us=" << dmabuf_readback_mean
              << " dmabuf_readback_max_us=" << display.dmabuf_readback_max_microseconds
              << " cursor_definitions=" << display.cursor_definitions
              << " cursor_moves=" << display.cursor_moves
              << " source_frames=" << source.submitted_frames
              << " idr_requests=" << source.keyframe_requests
              << " source_luma_min=" << static_cast<unsigned int>(source.luma_min)
              << " source_luma_max=" << static_cast<unsigned int>(source.luma_max)
              << " source_luma_mean=" << source_luma_mean
              << " source_nonblack_samples=" << source.non_black_pixels
              << " source_samples=" << source.sampled_pixels
              << " h264_access_units=" << egress.video_access_units
              << " h264_records=" << egress.video_records
              << " h264_send_failures=" << egress.video_send_failures
              << " cursor_records=" << egress.cursor_records
              << " cursor_shape_records=" << egress.cursor_shape_records
              << " recent_error=" << recent_error_summary(stats)
              << '\n' << std::flush;
}

int run(const Options &options) {
    PacketSink sink(options.video_socket, options.audio_socket);
    DirectMediaAdapter media(sink, options.encoder, options.vaapi_device, options.fps);
    qmdp::QemuDbusOptions display_options;
    display_options.bus_address = options.dbus_address;
    // The package-owned per-VM endpoint is a private session bus.  QEMU owns
    // the org.qemu name there; this is not a peer-to-peer D-Bus socket.
    display_options.destination = "org.qemu";
    display_options.enable_audio = true;
    display_options.require_audio = false;
    display_options.pump_interval = 10ms;
    qmdp::QemuDbusDisplay display(std::move(display_options));
    qmdp::DesktopSession session(display, media, {.frame_wait = 20ms});
    session.start();
    InputReceiver input(options.input_socket, session, media, options.fps);
    try {
        input.start();
        if (options.initial_size) {
            session.set_ui_info({.request_id = 1U, .width = options.initial_size->width,
                                 .height = options.initial_size->height,
                                 .refresh_millihz = options.fps * 1000U,
                                 .remote_scale_percent = 100U});
        }
        std::cout << "QSM_DIRECT_MEDIA_READY encoder=" << options.encoder << " fps=" << options.fps << '\n' << std::flush;
        // Display1 has no reconnect protocol. A QEMU stop closes its D-Bus
        // listener; terminate this per-console worker immediately so the
        // terminal service closes the corresponding WebRTC peer and the
        // browser console window follows the VM lifecycle.
        const auto capture_deadline = std::chrono::steady_clock::now() + 3s;
        bool capture_diagnostic_written = false;
        while (!stopping.load() && !session.display_failed()) {
            if (!capture_diagnostic_written && std::chrono::steady_clock::now() >= capture_deadline) {
                const auto stats = session.stats();
                write_session_diagnostic("CAPTURE_AFTER_3S", stats, display.stats(), media.video_stats(), sink.stats());
                capture_diagnostic_written = true;
            }
            std::this_thread::sleep_for(100ms);
        }
        write_session_diagnostic(session.display_failed() ? "DISPLAY_ENDED" : "STOPPING",
                                 session.stats(), display.stats(), media.video_stats(), sink.stats());
        input.stop();
        session.stop();
    } catch (...) {
        input.stop();
        session.stop();
        throw;
    }
    return EXIT_SUCCESS;
}

} // namespace

int main(int argc, char **argv) {
    try {
        std::signal(SIGINT, on_signal);
        std::signal(SIGTERM, on_signal);
        return run(parse_options(argc, argv));
    } catch (const std::exception &error) {
        std::cerr << "qsm-direct-media-worker: " << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
