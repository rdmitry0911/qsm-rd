#include "mock/mock_sunshine_adapter.hpp"

#include <stdexcept>

namespace qmdp {

void MockSunshineAdapter::start() {
    running_ = true;
}

void MockSunshineAdapter::stop() noexcept {
    running_ = false;
}

void MockSunshineAdapter::submit_frame(const FrameToken& frame) {
    if (!running_ || !frame.valid()) {
        throw std::logic_error("invalid frame submission");
    }

    const auto previous_width = last_width_.exchange(frame.surface->width);
    const auto previous_height = last_height_.exchange(frame.surface->height);
    if (previous_width != 0U &&
        (previous_width != frame.surface->width || previous_height != frame.surface->height)) {
        ++mode_changes_;
    }
    ++frames_;
}

void MockSunshineAdapter::submit_audio(std::span<const float> interleaved_samples,
                                       std::uint32_t,
                                       std::uint16_t channels) {
    if (!running_ || channels == 0U || interleaved_samples.size() % channels != 0U) {
        throw std::logic_error("invalid audio submission");
    }
    audio_frames_ += interleaved_samples.size() / channels;
}

void MockSunshineAdapter::request_idr() {
    ++idr_requests_;
}

MockSunshineAdapter::Stats MockSunshineAdapter::stats() const noexcept {
    return Stats{
        frames_.load(),
        audio_frames_.load(),
        idr_requests_.load(),
        mode_changes_.load(),
        last_width_.load(),
        last_height_.load(),
    };
}

}  // namespace qmdp
