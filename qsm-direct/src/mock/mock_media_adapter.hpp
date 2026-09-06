#pragma once

#include "interfaces/media_adapter.hpp"

#include <atomic>
#include <cstdint>

namespace qmdp {

class MockMediaAdapter final : public IMediaAdapter {
public:
    void start() override;
    void stop() noexcept override;
    void submit_frame(const FrameToken& frame) override;
    void submit_audio(std::span<const float> interleaved_samples,
                      std::uint32_t sample_rate,
                      std::uint16_t channels) override;
    void request_idr() override;

    struct Stats {
        std::uint64_t frames {};
        std::uint64_t audio_frames {};
        std::uint64_t idr_requests {};
        std::uint64_t mode_changes {};
        std::uint32_t last_width {};
        std::uint32_t last_height {};
    };

    [[nodiscard]] Stats stats() const noexcept;

private:
    std::atomic<bool> running_ {false};
    std::atomic<std::uint64_t> frames_ {};
    std::atomic<std::uint64_t> audio_frames_ {};
    std::atomic<std::uint64_t> idr_requests_ {};
    std::atomic<std::uint64_t> mode_changes_ {};
    std::atomic<std::uint32_t> last_width_ {};
    std::atomic<std::uint32_t> last_height_ {};
};

}  // namespace qmdp
