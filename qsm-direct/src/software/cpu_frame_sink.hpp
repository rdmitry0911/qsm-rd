#pragma once

#include "interfaces/media_adapter.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <mutex>
#include <vector>

namespace qmdp {

struct CpuFrameSinkOptions {
    // When non-empty, the most recently submitted frame is written as a PPM
    // file at stop(). This is a deterministic no-GPU validation artifact.
    std::filesystem::path snapshot_path;
    std::chrono::milliseconds artificial_encode_delay {0};
};

class CpuFrameSink final : public IMediaAdapter {
public:
    explicit CpuFrameSink(CpuFrameSinkOptions options = {});

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
        std::uint64_t mode_changes {};
        std::uint64_t checksum {};
        std::uint32_t width {};
        std::uint32_t height {};
    };

    [[nodiscard]] Stats stats() const noexcept;

private:
    void write_snapshot() const;
    static std::uint64_t fnv1a(std::span<const std::uint8_t> bytes,
                               std::uint64_t seed) noexcept;

    CpuFrameSinkOptions options_;
    std::atomic<bool> running_ {false};
    mutable std::mutex frame_mutex_;
    std::vector<std::uint8_t> last_bgra_;
    std::atomic<std::uint64_t> frames_ {};
    std::atomic<std::uint64_t> input_bytes_ {};
    std::atomic<std::uint64_t> audio_frames_ {};
    std::atomic<std::uint64_t> idr_requests_ {};
    std::atomic<std::uint64_t> mode_changes_ {};
    std::atomic<std::uint64_t> checksum_ {14695981039346656037ULL};
    std::atomic<std::uint32_t> width_ {};
    std::atomic<std::uint32_t> height_ {};
};

}  // namespace qmdp
