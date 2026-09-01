#include "mock/mock_qemu_display.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <numbers>
#include <stdexcept>
#include <vector>

namespace qmdp {

MockQemuDisplay::MockQemuDisplay(std::uint32_t video_fps)
    : video_fps_(video_fps) {
    if (video_fps_ == 0U) {
        throw std::invalid_argument("video fps must be positive");
    }
    rebuild_surface(1920U, 1080U);
}

MockQemuDisplay::~MockQemuDisplay() {
    stop();
}

void MockQemuDisplay::start(QemuDisplayCallbacks callbacks) {
    if (running_.exchange(true)) {
        throw std::logic_error("mock QEMU display is already running");
    }
    callbacks_ = std::move(callbacks);
    video_thread_ = std::thread(&MockQemuDisplay::video_loop, this);
    audio_thread_ = std::thread(&MockQemuDisplay::audio_loop, this);
}

void MockQemuDisplay::stop() noexcept {
    running_ = false;
    if (video_thread_.joinable()) {
        video_thread_.join();
    }
    if (audio_thread_.joinable()) {
        audio_thread_.join();
    }
}

void MockQemuDisplay::set_ui_info(const ViewportRequest& request) {
    rebuild_surface(request.width, request.height);
}

void MockQemuDisplay::key(std::uint32_t, bool) {
    ++input_events_;
}

void MockQemuDisplay::button(std::uint8_t, bool) {
    ++input_events_;
}

bool MockQemuDisplay::is_absolute_pointer() {
    return true;
}

void MockQemuDisplay::absolute_pointer(std::uint32_t, std::uint32_t) {
    ++input_events_;
}

void MockQemuDisplay::relative_pointer(std::int32_t, std::int32_t) {
    ++input_events_;
}

std::uint64_t MockQemuDisplay::input_event_count() const noexcept {
    return input_events_.load();
}

void MockQemuDisplay::video_loop() {
    const auto period = std::chrono::nanoseconds{1'000'000'000LL / video_fps_};
    auto next = std::chrono::steady_clock::now();
    std::uint64_t sequence = 0U;

    while (running_) {
        next += period;
        std::shared_ptr<const FrameSurface> surface;
        {
            std::lock_guard lock(surface_mutex_);
            surface = surface_;
        }
        if (callbacks_.on_frame) {
            FrameToken frame;
            frame.surface = std::move(surface);
            frame.damage = DamageRect{0U, 0U, frame.surface->width, frame.surface->height};
            frame.sequence = ++sequence;
            frame.produced_at = std::chrono::steady_clock::now();
            callbacks_.on_frame(std::move(frame));
        }
        std::this_thread::sleep_until(next);
    }
}

void MockQemuDisplay::audio_loop() {
    constexpr std::uint32_t sample_rate = 48000U;
    constexpr std::uint16_t channels = 2U;
    constexpr std::size_t frames_per_chunk = 480U;
    constexpr double frequency = 440.0;
    constexpr double amplitude = 0.05;

    std::vector<float> chunk(frames_per_chunk * channels);
    double phase = 0.0;
    const double step = 2.0 * std::numbers::pi * frequency /
                        static_cast<double>(sample_rate);
    auto next = std::chrono::steady_clock::now();

    while (running_) {
        next += std::chrono::milliseconds{10};
        for (std::size_t frame = 0U; frame < frames_per_chunk; ++frame) {
            const float sample = static_cast<float>(amplitude * std::sin(phase));
            phase += step;
            if (phase >= 2.0 * std::numbers::pi) {
                phase -= 2.0 * std::numbers::pi;
            }
            chunk[frame * channels] = sample;
            chunk[frame * channels + 1U] = sample;
        }
        if (callbacks_.on_audio) {
            callbacks_.on_audio(chunk, sample_rate, channels);
        }
        std::this_thread::sleep_until(next);
    }
}

void MockQemuDisplay::rebuild_surface(std::uint32_t width, std::uint32_t height) {
    auto surface = std::make_shared<FrameSurface>();
    surface->width = std::max(width, 1U);
    surface->height = std::max(height, 1U);
    surface->y0_top = true;
    surface->generation = ++surface_generation_;
    surface->debug_name = "mock-qemu-cpu-scanout";
    const auto stride = surface->width * 4U;
    auto pixels = std::make_shared<const std::vector<std::uint8_t>>(
        static_cast<std::size_t>(stride) * surface->height, 0U);
    surface->storage = CpuPixels{
        .stride = stride,
        .pixman_format = 0x20020888U,  // PIXMAN_x8r8g8b8
        .bytes = std::move(pixels),
    };

    std::lock_guard lock(surface_mutex_);
    surface_ = std::move(surface);
}

}  // namespace qmdp
