#pragma once

#include "core/unix_fd.hpp"
#include "interfaces/sunshine_adapter.hpp"

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <mutex>
#include <string>
#include <sys/types.h>

namespace qmdp {

struct FfmpegSoftwareOptions {
    std::filesystem::path output_directory {"qmdp-output"};
    std::string filename_prefix {"qemu-software"};
    std::string ffmpeg_binary {"ffmpeg"};
    std::string video_encoder {"libx264"};
    std::uint32_t fps {30U};
};

// Diagnostic software-encoding adapter. It deliberately uses ffmpeg as a
// subprocess, so the no-GPU vertical slice can produce a real H.264 Matroska
// stream without libavcodec development headers. Production networking remains
// Sunshine's responsibility; this class validates everything up to its CPU
// encoder boundary.
class FfmpegSoftwareAdapter final : public ISunshineAdapter {
public:
    explicit FfmpegSoftwareAdapter(FfmpegSoftwareOptions options = {});
    ~FfmpegSoftwareAdapter() override;

    FfmpegSoftwareAdapter(const FfmpegSoftwareAdapter&) = delete;
    FfmpegSoftwareAdapter& operator=(const FfmpegSoftwareAdapter&) = delete;

    void start() override;
    void stop() noexcept override;
    void submit_frame(const FrameToken& frame) override;
    void submit_audio(std::span<const float> interleaved_samples,
                      std::uint32_t sample_rate,
                      std::uint16_t channels) override;
    void request_idr() override;

    struct Stats {
        std::uint64_t frames {};
        std::uint64_t input_bytes {};
        std::uint64_t audio_frames {};
        std::uint64_t idr_requests {};
        std::uint64_t segments {};
        std::uint32_t width {};
        std::uint32_t height {};
        int last_exit_status {};
    };

    [[nodiscard]] Stats stats() const noexcept;

private:
    void open_segment(std::uint32_t width, std::uint32_t height);
    int close_segment() noexcept;
    void write_all(std::span<const std::uint8_t> bytes);
    [[nodiscard]] std::filesystem::path segment_path(std::uint64_t index) const;

    FfmpegSoftwareOptions options_;
    std::atomic<bool> running_ {false};
    mutable std::mutex process_mutex_;
    UniqueFd input_pipe_;
    pid_t child_pid_ {-1};
    std::uint64_t next_segment_ {};
    std::atomic<std::uint64_t> frames_ {};
    std::atomic<std::uint64_t> input_bytes_ {};
    std::atomic<std::uint64_t> audio_frames_ {};
    std::atomic<std::uint64_t> idr_requests_ {};
    std::atomic<std::uint64_t> segments_ {};
    std::atomic<std::uint32_t> width_ {};
    std::atomic<std::uint32_t> height_ {};
    std::atomic<int> last_exit_status_ {};
};

}  // namespace qmdp
