#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <span>
#include <vector>

namespace qmdp {

// Bounded interleaved-float PCM FIFO. To preserve interactive latency, an
// overflow drops the oldest frames instead of growing the queue indefinitely.
class AudioFifo {
public:
    AudioFifo(std::uint32_t sample_rate,
              std::uint16_t channels,
              std::size_t capacity_frames);

    void push(std::span<const float> interleaved_samples);

    [[nodiscard]] std::vector<float> pop(std::size_t requested_frames);

    struct Stats {
        std::size_t queued_frames {};
        std::uint64_t pushed_frames {};
        std::uint64_t popped_frames {};
        std::uint64_t dropped_frames {};
        std::uint64_t underflow_events {};
    };

    [[nodiscard]] Stats stats() const;
    [[nodiscard]] std::uint32_t sample_rate() const noexcept;
    [[nodiscard]] std::uint16_t channels() const noexcept;

private:
    [[nodiscard]] std::size_t queued_samples_unsafe() const noexcept;
    void drop_front_samples_unsafe(std::size_t samples);
    void compact_unsafe();

    const std::uint32_t sample_rate_;
    const std::uint16_t channels_;
    const std::size_t capacity_frames_;

    mutable std::mutex mutex_;
    std::vector<float> data_;
    std::size_t head_samples_ {};
    std::uint64_t pushed_frames_ {};
    std::uint64_t popped_frames_ {};
    std::uint64_t dropped_frames_ {};
    std::uint64_t underflow_events_ {};
};

}  // namespace qmdp
