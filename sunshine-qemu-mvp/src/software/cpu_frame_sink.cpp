#include "software/cpu_frame_sink.hpp"

#include "core/pixel_format.hpp"

#include <chrono>
#include <fstream>
#include <stdexcept>
#include <thread>
#include <utility>

namespace qmdp {

CpuFrameSink::CpuFrameSink(CpuFrameSinkOptions options)
    : options_(std::move(options)) {
    if (options_.artificial_encode_delay.count() < 0) {
        throw std::invalid_argument("artificial encode delay cannot be negative");
    }
}

void CpuFrameSink::start() {
    if (running_.exchange(true)) {
        throw std::logic_error("CPU frame sink is already running");
    }
}

void CpuFrameSink::stop() noexcept {
    if (!running_.exchange(false)) {
        return;
    }
    try {
        write_snapshot();
    } catch (...) {
    }
}

void CpuFrameSink::submit_frame(const FrameToken& frame) {
    if (!running_.load() || !frame.valid()) {
        throw std::logic_error("invalid CPU frame submission");
    }
    const auto *cpu = frame.surface->cpu();
    if (cpu == nullptr || !cpu->bytes) {
        throw std::invalid_argument("CPU frame sink does not accept DMA-BUF frames");
    }

    auto bgra = to_bgra(*cpu->bytes,
                        frame.surface->width,
                        frame.surface->height,
                        cpu->stride,
                        cpu->pixman_format);
    const auto old_width = width_.exchange(frame.surface->width);
    const auto old_height = height_.exchange(frame.surface->height);
    if (old_width != 0U &&
        (old_width != frame.surface->width || old_height != frame.surface->height)) {
        ++mode_changes_;
    }

    const auto next_checksum = fnv1a(bgra, checksum_.load());
    checksum_ = next_checksum;
    input_bytes_ += bgra.size();
    {
        std::lock_guard lock(frame_mutex_);
        last_bgra_ = std::move(bgra);
    }
    ++frames_;

    if (options_.artificial_encode_delay.count() > 0) {
        std::this_thread::sleep_for(options_.artificial_encode_delay);
    }
}

void CpuFrameSink::submit_audio(std::span<const float> interleaved_samples,
                                std::uint32_t,
                                std::uint16_t channels) {
    if (!running_.load() || channels == 0U ||
        interleaved_samples.size() % channels != 0U) {
        throw std::logic_error("invalid CPU audio submission");
    }
    audio_frames_ += interleaved_samples.size() / channels;
}

void CpuFrameSink::request_idr() {
    ++idr_requests_;
}

std::uint64_t CpuFrameSink::fnv1a(std::span<const std::uint8_t> bytes,
                                  std::uint64_t seed) noexcept {
    constexpr std::uint64_t prime = 1099511628211ULL;
    std::uint64_t value = seed;
    for (const auto byte : bytes) {
        value ^= byte;
        value *= prime;
    }
    return value;
}

void CpuFrameSink::write_snapshot() const {
    if (options_.snapshot_path.empty()) {
        return;
    }
    std::vector<std::uint8_t> bgra;
    {
        std::lock_guard lock(frame_mutex_);
        bgra = last_bgra_;
    }
    const auto width = width_.load();
    const auto height = height_.load();
    if (bgra.empty() || width == 0U || height == 0U) {
        return;
    }

    if (options_.snapshot_path.has_parent_path()) {
        std::filesystem::create_directories(options_.snapshot_path.parent_path());
    }
    std::ofstream output(options_.snapshot_path, std::ios::binary | std::ios::trunc);
    if (!output) {
        throw std::runtime_error("cannot create CPU snapshot file");
    }
    output << "P6\n" << width << ' ' << height << "\n255\n";
    for (std::size_t offset = 0U; offset + 3U < bgra.size(); offset += 4U) {
        const char rgb[3] {
            static_cast<char>(bgra[offset + 2U]),
            static_cast<char>(bgra[offset + 1U]),
            static_cast<char>(bgra[offset + 0U]),
        };
        output.write(rgb, 3);
    }
    if (!output) {
        throw std::runtime_error("failed writing CPU snapshot file");
    }
}

CpuFrameSink::Stats CpuFrameSink::stats() const noexcept {
    return {
        .frames = frames_.load(),
        .input_bytes = input_bytes_.load(),
        .audio_frames = audio_frames_.load(),
        .idr_requests = idr_requests_.load(),
        .mode_changes = mode_changes_.load(),
        .checksum = checksum_.load(),
        .width = width_.load(),
        .height = height_.load(),
    };
}

}  // namespace qmdp
