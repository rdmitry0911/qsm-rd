#include "core/audio_fifo.hpp"

#include <algorithm>
#include <stdexcept>

namespace qmdp {

AudioFifo::AudioFifo(std::uint32_t sample_rate,
                     std::uint16_t channels,
                     std::size_t capacity_frames)
    : sample_rate_(sample_rate),
      channels_(channels),
      capacity_frames_(capacity_frames) {
    if (sample_rate_ == 0U || channels_ == 0U || capacity_frames_ == 0U) {
        throw std::invalid_argument("invalid audio fifo dimensions");
    }
    data_.reserve(capacity_frames_ * channels_ * 2U);
}

void AudioFifo::push(std::span<const float> interleaved_samples) {
    if (interleaved_samples.empty()) {
        return;
    }
    if (interleaved_samples.size() % channels_ != 0U) {
        throw std::invalid_argument("audio input is not frame-aligned");
    }

    std::lock_guard lock(mutex_);
    const std::size_t incoming_frames = interleaved_samples.size() / channels_;
    pushed_frames_ += incoming_frames;

    const std::size_t capacity_samples = capacity_frames_ * channels_;
    if (interleaved_samples.size() >= capacity_samples) {
        const std::size_t lost_existing_frames = queued_samples_unsafe() / channels_;
        const std::size_t discarded_prefix_frames = incoming_frames - capacity_frames_;
        dropped_frames_ += lost_existing_frames + discarded_prefix_frames;
        data_.assign(interleaved_samples.end() - static_cast<std::ptrdiff_t>(capacity_samples),
                     interleaved_samples.end());
        head_samples_ = 0U;
        return;
    }

    const std::size_t required_samples = queued_samples_unsafe() + interleaved_samples.size();
    if (required_samples > capacity_samples) {
        const std::size_t overflow_samples = required_samples - capacity_samples;
        const std::size_t overflow_frames = overflow_samples / channels_;
        drop_front_samples_unsafe(overflow_frames * channels_);
        dropped_frames_ += overflow_frames;
    }

    compact_unsafe();
    data_.insert(data_.end(), interleaved_samples.begin(), interleaved_samples.end());
}

std::vector<float> AudioFifo::pop(std::size_t requested_frames) {
    std::lock_guard lock(mutex_);
    const std::size_t available_frames = queued_samples_unsafe() / channels_;
    const std::size_t actual_frames = std::min(requested_frames, available_frames);
    if (actual_frames < requested_frames) {
        ++underflow_events_;
    }

    const std::size_t actual_samples = actual_frames * channels_;
    std::vector<float> result;
    result.reserve(actual_samples);
    result.insert(result.end(),
                  data_.begin() + static_cast<std::ptrdiff_t>(head_samples_),
                  data_.begin() + static_cast<std::ptrdiff_t>(head_samples_ + actual_samples));
    head_samples_ += actual_samples;
    popped_frames_ += actual_frames;
    compact_unsafe();
    return result;
}

AudioFifo::Stats AudioFifo::stats() const {
    std::lock_guard lock(mutex_);
    return Stats{
        queued_samples_unsafe() / channels_,
        pushed_frames_,
        popped_frames_,
        dropped_frames_,
        underflow_events_,
    };
}

std::uint32_t AudioFifo::sample_rate() const noexcept {
    return sample_rate_;
}

std::uint16_t AudioFifo::channels() const noexcept {
    return channels_;
}

std::size_t AudioFifo::queued_samples_unsafe() const noexcept {
    return data_.size() - head_samples_;
}

void AudioFifo::drop_front_samples_unsafe(std::size_t samples) {
    head_samples_ += std::min(samples, queued_samples_unsafe());
    compact_unsafe();
}

void AudioFifo::compact_unsafe() {
    if (head_samples_ == 0U) {
        return;
    }
    if (head_samples_ >= data_.size()) {
        data_.clear();
        head_samples_ = 0U;
        return;
    }
    if (head_samples_ > data_.size() / 2U || head_samples_ > capacity_frames_ * channels_) {
        data_.erase(data_.begin(), data_.begin() + static_cast<std::ptrdiff_t>(head_samples_));
        head_samples_ = 0U;
    }
}

}  // namespace qmdp
