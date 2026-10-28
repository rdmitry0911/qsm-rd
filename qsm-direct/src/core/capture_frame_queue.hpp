// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <optional>
#include <utility>
#include <vector>

namespace qmdp {

// Keep short capture bursts without building video history. The caller keeps
// its latest image separately and clears this queue during backpressure,
// recovery and layout changes. All access is serialized by the caller.
class CaptureFrameQueue {
public:
    using Clock = std::chrono::steady_clock;
    static constexpr std::size_t maximum_frames = 2;
    static constexpr std::size_t maximum_bytes = 64U * 1024U * 1024U;

    struct Frame {
        std::shared_ptr<const std::vector<std::uint8_t>> pixels;
        std::uint64_t capture;
        std::uint32_t width, height;
        Clock::time_point received;
    };

    void push(Frame frame) {
        if (!frame.pixels || frame.pixels->size() > maximum_bytes) {
            clear();
            return;
        }
        if (!frames_.empty() && (frames_.back().width != frame.width || frames_.back().height != frame.height)) {
            clear();
        }
        const auto size = frame.pixels->size();
        frames_.push_back(std::move(frame));
        bytes_ += size;
        while (frames_.size() > maximum_frames || bytes_ > maximum_bytes) {
            discard_front();
        }
    }

    [[nodiscard]] std::optional<Frame> pop(Clock::time_point now, Clock::duration maximum_age) {
        while (!frames_.empty() && now - frames_.front().received > maximum_age) {
            discard_front();
        }
        if (frames_.empty()) { return std::nullopt; }
        auto result = std::move(frames_.front());
        bytes_ -= result.pixels->size();
        frames_.pop_front();
        return result;
    }

    void clear() noexcept { frames_.clear(); bytes_ = 0; }
    [[nodiscard]] std::size_t size() const noexcept { return frames_.size(); }
    [[nodiscard]] std::size_t bytes() const noexcept { return bytes_; }

private:
    void discard_front() noexcept {
        bytes_ -= frames_.front().pixels->size();
        frames_.pop_front();
    }
    std::deque<Frame> frames_;
    std::size_t bytes_ {};
};

}  // namespace qmdp
