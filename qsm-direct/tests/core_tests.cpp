#include "core/audio_fifo.hpp"
#include "core/frame.hpp"
#include "core/latest_frame_mailbox.hpp"
#include "core/resize_coalescer.hpp"
#include "core/session.hpp"
#include "core/unix_fd.hpp"
#include "capture/cpu_framebuffer.hpp"
#include "interfaces/media_adapter.hpp"
#include "interfaces/qemu_display.hpp"
#include "pipeline/desktop_session.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <numbers>
#include <string_view>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

int failures = 0;

#define CHECK(condition)                                                        \
    do {                                                                        \
        if (!(condition)) {                                                     \
            std::cerr << __FILE__ << ':' << __LINE__                           \
                      << ": CHECK failed: " #condition << '\n';                \
            ++failures;                                                         \
        }                                                                       \
    } while (false)

qmdp::FrameToken frame(std::uint64_t sequence, std::uint64_t generation = 1U) {
    auto surface = std::make_shared<qmdp::FrameSurface>();
    surface->width = 2U;
    surface->height = 2U;
    surface->generation = generation;
    auto bytes = std::make_shared<const std::vector<std::uint8_t>>(16U, 0U);
    surface->storage = qmdp::CpuPixels{
        .stride = 8U,
        .pixman_format = 0x20020888U,
        .bytes = std::move(bytes),
    };
    qmdp::FrameToken token;
    token.surface = std::move(surface);
    token.sequence = sequence;
    token.produced_at = std::chrono::steady_clock::now();
    return token;
}

class ReMappingDisplay final : public qmdp::IQemuDisplay {
public:
    ~ReMappingDisplay() override { stop(); }

    void start(qmdp::QemuDisplayCallbacks callbacks) override {
        callbacks_ = std::move(callbacks);
        running_ = true;
        worker_ = std::thread([this] {
            for (std::uint64_t generation = 1U; generation <= 3U && running_; ++generation) {
                auto token = frame(generation, generation);
                // A new D-Bus ScanoutMap backing buffer can have the same
                // visible mode. This is the condition which formerly forced
                // a hardware encoder restart for every ordinary repaint.
                callbacks_.on_frame(std::move(token));
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
        });
    }

    void stop() noexcept override {
        running_ = false;
        if (worker_.joinable()) {
            worker_.join();
        }
    }

    void set_ui_info(const qmdp::ViewportRequest&) override {}
    void key(std::uint32_t, bool) override {}
    void button(std::uint8_t, bool) override {}
    [[nodiscard]] bool is_absolute_pointer() override { return true; }
    void absolute_pointer(std::uint32_t, std::uint32_t) override {}
    void relative_pointer(std::int32_t, std::int32_t) override {}

private:
    std::atomic<bool> running_ {false};
    std::thread worker_;
    qmdp::QemuDisplayCallbacks callbacks_;
};

// Emits one second of a 1 kHz mono sine at 44.1 kHz, QEMU's dbus audiodev
// default, in 10 ms callbacks.
class AudioDisplay final : public qmdp::IQemuDisplay {
public:
    void start(qmdp::QemuDisplayCallbacks callbacks) override {
        std::vector<float> chunk(441U);
        for (std::size_t callback = 0U; callback < 100U; ++callback) {
            for (std::size_t frame = 0U; frame < chunk.size(); ++frame) {
                const double time = static_cast<double>(callback * chunk.size() + frame) / 44100.0;
                chunk[frame] = static_cast<float>(0.5 * std::sin(2.0 * std::numbers::pi * 1000.0 * time));
            }
            callbacks.on_audio(chunk, 44100U, 1U);
        }
    }
    void stop() noexcept override {}
    void set_ui_info(const qmdp::ViewportRequest&) override {}
    void key(std::uint32_t, bool) override {}
    void button(std::uint8_t, bool) override {}
    [[nodiscard]] bool is_absolute_pointer() override { return true; }
    void absolute_pointer(std::uint32_t, std::uint32_t) override {}
    void relative_pointer(std::int32_t, std::int32_t) override {}
};

class AudioRecordingMediaAdapter final : public qmdp::IMediaAdapter {
public:
    void start() override {}
    void stop() noexcept override {}
    void submit_frame(const qmdp::FrameToken&) override {}
    void submit_audio(std::span<const float> samples, std::uint32_t rate, std::uint16_t channels) override {
        std::lock_guard lock(mutex_);
        rate_ = rate;
        channels_ = channels;
        samples_.insert(samples_.end(), samples.begin(), samples.end());
    }
    void request_idr() override {}

    std::mutex mutex_;
    std::vector<float> samples_;
    std::uint32_t rate_ {};
    std::uint16_t channels_ {};
};

class RecordingMediaAdapter final : public qmdp::IMediaAdapter {
public:
    void start() override { running_ = true; }
    void stop() noexcept override { running_ = false; }
    void submit_frame(const qmdp::FrameToken&) override {
        if (running_) { ++frames_; }
    }
    void submit_audio(std::span<const float>, std::uint32_t, std::uint16_t) override {}
    void request_idr() override { ++idr_requests_; }

    [[nodiscard]] std::uint64_t frames() const noexcept { return frames_; }
    [[nodiscard]] std::uint64_t idr_requests() const noexcept { return idr_requests_; }

private:
    std::atomic<bool> running_ {false};
    std::atomic<std::uint64_t> frames_ {0U};
    std::atomic<std::uint64_t> idr_requests_ {0U};
};

void test_unique_fd() {
    int pipe_fds[2] {-1, -1};
    CHECK(::pipe(pipe_fds) == 0);
    if (pipe_fds[0] < 0 || pipe_fds[1] < 0) {
        return;
    }
    qmdp::UniqueFd read_end{pipe_fds[0]};
    qmdp::UniqueFd write_end{pipe_fds[1]};
    auto duplicate = read_end.duplicate();
    CHECK(static_cast<bool>(duplicate));
    qmdp::UniqueFd moved = std::move(duplicate);
    CHECK(!static_cast<bool>(duplicate));
    CHECK(static_cast<bool>(moved));
    CHECK(static_cast<bool>(write_end));
}

void test_latest_frame_mailbox() {
    qmdp::LatestFrameMailbox mailbox;
    CHECK(mailbox.publish(frame(1U)));
    CHECK(mailbox.publish(frame(2U)));
    CHECK(mailbox.publish(frame(3U)));
    const auto result = mailbox.try_pop();
    CHECK(result.has_value());
    CHECK(result && result->sequence == 3U);
    const auto stats = mailbox.stats();
    CHECK(stats.published == 3U);
    CHECK(stats.consumed == 1U);
    CHECK(stats.dropped == 2U);
}

void test_resize_coalescer() {
    using namespace std::chrono_literals;
    const auto t0 = std::chrono::steady_clock::time_point{};
    qmdp::ResizeCoalescer resize;
    resize.submit({1U, 1901U, 1001U, 60000U, 100U}, t0);
    resize.submit({2U, 2561U, 1441U, 60000U, 125U}, t0 + 50ms);
    CHECK(!resize.poll(t0 + 299ms).has_value());
    const auto request = resize.poll(t0 + 300ms);
    CHECK(request.has_value());
    CHECK(request && request->request_id == 2U);
    CHECK(request && request->width == 2560U);
    CHECK(request && request->height == 1440U);
    CHECK(request && request->remote_scale_percent == 125U);
    CHECK(resize.superseded_count() == 1U);

    resize.submit({3U, 2561U, 1441U, 60000U, 125U}, t0 + 400ms);
    CHECK(!resize.poll(t0 + 650ms).has_value());  // duplicate mode is suppressed.
}

void test_audio_fifo() {
    qmdp::AudioFifo fifo(48000U, 2U, 4U);
    const std::vector<float> six_frames{
        0.F, 1.F, 2.F, 3.F, 4.F, 5.F,
        6.F, 7.F, 8.F, 9.F, 10.F, 11.F,
    };
    fifo.push(six_frames);
    const auto result = fifo.pop(4U);
    const std::vector<float> expected{4.F, 5.F, 6.F, 7.F, 8.F, 9.F, 10.F, 11.F};
    CHECK(result == expected);
    const auto stats = fifo.stats();
    CHECK(stats.pushed_frames == 6U);
    CHECK(stats.popped_frames == 4U);
    CHECK(stats.dropped_frames == 2U);
}

void test_stale_damage_is_detected_without_mutating_the_framebuffer() {
    qmdp::CpuFramebuffer framebuffer;
    CHECK(framebuffer.damage_compatibility(0, 0, 1, 1) ==
          qmdp::CpuFramebuffer::DamageCompatibility::awaiting_scanout);

    const std::vector<std::uint8_t> pixels(2U * 2U * 4U, 0U);
    (void) framebuffer.scanout_inline(2U,
                                       2U,
                                       8U,
                                       0x20020888U,
                                       pixels);
    CHECK(framebuffer.damage_compatibility(0, 0, 2, 2) ==
          qmdp::CpuFramebuffer::DamageCompatibility::accepted);
    CHECK(framebuffer.damage_compatibility(0, 1, 3, 1) ==
          qmdp::CpuFramebuffer::DamageCompatibility::out_of_bounds);
    CHECK(framebuffer.damage_compatibility(-1, 0, 1, 1) ==
          qmdp::CpuFramebuffer::DamageCompatibility::malformed);
}

void test_cursor_state_is_published_without_a_scanout_frame() {
    qmdp::CpuFramebuffer framebuffer;
    const std::vector<std::uint8_t> cursor{
        0x10U, 0x20U, 0x30U, 0xffU,
        0x40U, 0x50U, 0x60U, 0xffU,
        0x70U, 0x80U, 0x90U, 0xffU,
        0xa0U, 0xb0U, 0xc0U, 0xffU,
    };
    framebuffer.set_cursor_shape(2, 2, 1, 0, cursor);
    framebuffer.set_cursor_position(101, 202, true);
    const auto state = framebuffer.cursor_state();
    CHECK(state.visible);
    CHECK(state.x == 101);
    CHECK(state.y == 202);
    CHECK(state.sequence == 2U);
    CHECK(state.shape != nullptr);
    CHECK(state.shape && state.shape->width == 2U && state.shape->height == 2U);
    CHECK(state.shape && state.shape->hotspot_x == 1U && state.shape->hotspot_y == 0U);
    CHECK(state.shape && state.shape->argb == cursor);
}

void test_session_state_machine() {
    qmdp::SessionStateMachine machine;
    CHECK(machine.state() == qmdp::SessionState::idle);
    CHECK(machine.dispatch(qmdp::SessionEvent::prepare));
    CHECK(machine.dispatch(qmdp::SessionEvent::prepared));
    CHECK(machine.state() == qmdp::SessionState::streaming);
    CHECK(machine.dispatch(qmdp::SessionEvent::resize_begin));
    CHECK(machine.state() == qmdp::SessionState::reconfiguring);
    CHECK(machine.dispatch(qmdp::SessionEvent::resize_applied));
    CHECK(machine.dispatch(qmdp::SessionEvent::transport_lost));
    CHECK(machine.state() == qmdp::SessionState::reconnecting);
    CHECK(machine.dispatch(qmdp::SessionEvent::reconnected));
    CHECK(machine.dispatch(qmdp::SessionEvent::stop));
    CHECK(machine.dispatch(qmdp::SessionEvent::stopped));
    CHECK(machine.state() == qmdp::SessionState::idle);
    CHECK(!machine.dispatch(qmdp::SessionEvent::resize_begin));
}

void test_same_geometry_remap_does_not_request_an_idr() {
    using namespace std::chrono_literals;
    ReMappingDisplay display;
    RecordingMediaAdapter media;
    qmdp::DesktopSession session(display, media, {.frame_wait = 5ms});
    session.start();
    std::this_thread::sleep_for(80ms);
    session.stop();

    CHECK(media.frames() == 3U);
    CHECK(media.idr_requests() == 0U);
}

void test_audio_is_converted_to_the_session_format() {
    using namespace std::chrono_literals;
    AudioDisplay display;
    AudioRecordingMediaAdapter media;
    qmdp::DesktopSession session(display, media, {.frame_wait = 5ms, .audio_buffer = 2000ms});
    session.start();
    std::this_thread::sleep_for(50ms);
    session.stop();

    std::lock_guard lock(media.mutex_);
    CHECK(media.rate_ == 48000U);
    CHECK(media.channels_ == 2U);
    CHECK(session.stats().rejected_audio_callbacks == 0U);
    const std::size_t frames = media.samples_.size() / 2U;
    CHECK(frames >= 47990U && frames <= 48010U);  // one second, at 48 kHz
    float peak = 0.0F;
    std::size_t crossings = 0U;
    for (std::size_t frame = 0U; frame < frames; ++frame) {
        const float left = media.samples_[frame * 2U];
        CHECK(left == media.samples_[frame * 2U + 1U]);  // mono on both channels
        peak = std::max(peak, std::abs(left));
        if (frame > 0U && (media.samples_[(frame - 1U) * 2U] < 0.0F) != (left < 0.0F)) {
            ++crossings;
        }
    }
    CHECK(peak > 0.49F && peak <= 0.5F);
    CHECK(crossings >= 1995U && crossings <= 2005U);  // still 1 kHz
}

}  // namespace

int main() {
    test_unique_fd();
    test_latest_frame_mailbox();
    test_resize_coalescer();
    test_audio_fifo();
    test_stale_damage_is_detected_without_mutating_the_framebuffer();
    test_cursor_state_is_published_without_a_scanout_frame();
    test_session_state_machine();
    test_same_geometry_remap_does_not_request_an_idr();
    test_audio_is_converted_to_the_session_format();

    if (failures == 0) {
        std::cout << "all qmdp core tests passed\n";
        return EXIT_SUCCESS;
    }
    std::cerr << failures << " test assertion(s) failed\n";
    return EXIT_FAILURE;
}
