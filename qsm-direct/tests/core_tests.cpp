#include "core/audio_fifo.hpp"
#include "core/frame.hpp"
#include "core/latest_frame_mailbox.hpp"
#include "core/resize_coalescer.hpp"
#include "core/session.hpp"
#include "core/unix_fd.hpp"
#include "core/video_budget.hpp"
#include "core/cropped_video_rate.hpp"
#include "core/capture_frame_queue.hpp"
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

void test_video_budget_limits_encoded_bytes_before_admitting_raw_frames() {
    using namespace std::chrono_literals;
    qmdp::VideoBudget budget;
    const qmdp::VideoBudget::Clock::time_point start {};
    std::size_t admitted = 0U;
    for (int tick = 0; tick < 600; ++tick) {
        if (budget.ready(start + tick * 16667us, 1500U)) {
            budget.account(20000U);
            ++admitted;
        }
    }
    // Ten seconds at 1.5 Mbit/s with 12% transport reserve: around 84
    // complete pictures, rather than 600 coded pictures and broken refs.
    CHECK(admitted >= 80U && admitted <= 86U);

    qmdp::VideoBudget idle;
    std::size_t idle_frames = 0U;
    for (int tick = 0; tick < 600; ++tick) {
        if (idle.ready(start + tick * 16667us, 500U)) {
            idle.account(100U);
            ++idle_frames;
        }
    }
    CHECK(idle_frames == 600U);
}

void test_raw_backpressure_expires_and_preserves_encoded_byte_debt() {
    using namespace std::chrono_literals;
    const qmdp::VideoBudget::Clock::time_point start {};
    qmdp::VideoBudget idle, indebted;
    CHECK(idle.ready(start, 1000U));
    CHECK(indebted.ready(start, 1000U));
    indebted.account(10000U);
    idle.backpressure(start, 75U);
    indebted.backpressure(start, 75U);
    CHECK(!idle.ready(start + 50ms, 1000U));
    CHECK(idle.ready(start + 75ms, 1000U));
    CHECK(!indebted.ready(start + 75ms, 1000U));
    CHECK(indebted.ready(start + 100ms, 1000U));
    // No release packet is needed after a bridge dies, and even an excessive
    // caller lease cannot suppress new raw pictures beyond100ms.
    idle.backpressure(start + 100ms, 5000U);
    CHECK(!idle.ready(start + 199ms, 1000U));
    CHECK(idle.ready(start + 200ms, 1000U));
    idle.backpressure(start + 200ms, 75U);
    CHECK(!idle.ready(start + 210ms, 1000U));
    idle.backpressure(start + 210ms, 0U);
    CHECK(idle.ready(start + 210ms, 1000U));
}

void test_ui_replay_reserves_capacity_without_double_charging_fresh_tiles() {
    using namespace std::chrono_literals;
    using Clock = qmdp::VideoBudget::Clock;
    const Clock::time_point start {};
    qmdp::VideoBudget reserved, normal, fresh;
    CHECK(reserved.ready(start, 8000U, 50));
    CHECK(normal.ready(start, 8000U));
    CHECK(fresh.ready(start, 8000U, 50));
    reserved.account(100000U); normal.account(100000U);
    fresh.account(100000U);
    // Fifty percent cache replay takes twice as long to repay encoder debt.
    CHECK(!reserved.ready(start + 150ms, 8000U, 50));
    CHECK(normal.ready(start + 150ms, 8000U));
    // Source tiles pay the reservation once rather than doubling its cost.
    fresh.account_auxiliary(10000U);
    CHECK(!fresh.ready(start + 20ms, 8000U, 50));
    CHECK(fresh.ready(start + 250ms, 8000U, 50));
    CHECK(reserved.ready(start + 250ms, 8000U, 50));
}

void test_ui_burst_repays_bandwidth_without_a_long_video_admission_pause() {
    using namespace std::chrono_literals;
    qmdp::VideoBudget mixed, video;
    const qmdp::VideoBudget::Clock::time_point start {};
    CHECK(mixed.ready(start, 1500U)); CHECK(video.ready(start, 1500U));
    mixed.account_auxiliary(16000U);
    unsigned mixed_frames = 0, video_frames = 0, missed = 0, max_missed = 0;
    for (unsigned n = 1; n <= 40; ++n) {
        const auto tick = start + n * 16ms;
        if (mixed.ready(tick, 1500U)) {
            mixed.account(2500U); ++mixed_frames; missed = 0;
        } else {
            max_missed = std::max(max_missed, ++missed);
        }
        if (video.ready(tick, 1500U)) { video.account(2500U); ++video_frames; }
    }
    CHECK(mixed_frames < video_frames); // UI bytes still consume capacity.
    CHECK(max_missed <= 1U); // No six-tick pause to pay the whole UI burst.

    // A rate decrease must retain UI debt, rather than clamp away unpaid
    // bytes or turn the complete old-rate burst into an immediate pause.
    qmdp::VideoBudget reduced, plain;
    CHECK(reduced.ready(start, 8000U)); CHECK(plain.ready(start, 8000U));
    reduced.account_auxiliary(100000U);
    mixed_frames = video_frames = missed = max_missed = 0;
    for (unsigned n = 1; n <= 400; ++n) {
        const auto tick = start + n * 16ms;
        if (reduced.ready(tick, 500U)) {
            reduced.account(900U); if (n > 200) { ++mixed_frames; } missed = 0;
        } else { max_missed = std::max(max_missed, ++missed); }
        if (plain.ready(tick, 500U)) { plain.account(900U); if (n > 200) { ++video_frames; } }
    }
    CHECK(mixed_frames < video_frames);
    CHECK(max_missed <= 1U);
}

void test_video_budget_keeps_keyframe_debt_across_rate_changes() {
    using namespace std::chrono_literals;
    qmdp::VideoBudget budget;
    const qmdp::VideoBudget::Clock::time_point start {};
    CHECK(budget.ready(start, 8000U));
    budget.account(200000U);
    CHECK(!budget.ready(start + 100ms, 2000U));
    CHECK(!budget.ready(start + 800ms, 2000U));
    CHECK(budget.ready(start + 900ms, 2000U));
}

void test_cropped_video_rate_preserves_desktop_quality_and_ignores_brief_loss() {
    using namespace std::chrono_literals;
    using Clock = qmdp::CroppedVideoRate::Clock;
    const Clock::time_point start {};
    qmdp::CroppedVideoRate rate;
    CHECK(rate.update(start, false, 8000, 8000, true) == 8000U);
    // The original no-crop desktop cannot change quality, even on a long
    // constrained link. Its raw byte admission remains the fallback.
    for (unsigned n = 1; n < 100; ++n) {
        CHECK(rate.update(start + n * 1s, false, 500, 8000, false) == 8000U);
    }
    const auto cropped = rate.update(start + 100s, true, 8000, 8000, true);
    CHECK(cropped < 8000U);
    // A sub-second bad estimate must not change the codec or force an IDR.
    CHECK(rate.update(start + 110s, true, 1500, 8000, false) == cropped);
    CHECK(rate.update(start + 110500ms, true, 1500, 8000, false) == cropped);
    CHECK(rate.update(start + 111s, true, 8000, 8000, false) == cropped);
    CHECK(rate.update(start + 120s, true, 8000, 8000, false) == cropped);
    // A new fullscreen picture restores the fixed-quality original rate.
    CHECK(rate.update(start + 121s, false, 1500, 8000, false) == 8000U);
}

void test_cropped_video_rate_tracks_sustained_steps_without_chasing_jitter() {
    using namespace std::chrono_literals;
    using Clock = qmdp::CroppedVideoRate::Clock;
    const Clock::time_point start {};
    qmdp::CroppedVideoRate rate;
    const auto high = rate.update(start, true, 8000, 8000, true);
    // Allow steady jitter while a real link decrease persists. Requiring
    // identical samples would leave the encoder permanently at the old rate.
    for (unsigned n = 0; n < 6; ++n) {
        CHECK(rate.update(start + 10s + n * 100ms, true, 1400 + n % 3 * 50, 8000, false) == high);
    }
    const auto low = rate.update(start + 10600ms, true, 1500, 8000, false);
    CHECK(low < high / 2U);
    CHECK(low * 112U <= 1500U * 80U);
    // Small fluctuations cannot retune the codec, including after cooldown.
    CHECK(rate.update(start + 20s, true, 1600, 8000, false) == low);
    CHECK(rate.update(start + 30s, true, 1400, 8000, false) == low);
    // A brief high estimate also cannot trigger a quality/IDR pulse.
    CHECK(rate.update(start + 31s, true, 8000, 8000, false) == low);
    CHECK(rate.update(start + 35s, true, 1500, 8000, false) == low);
    CHECK(rate.update(start + 40s, true, 8000, 8000, false) == low);
    CHECK(rate.update(start + 44999ms, true, 8000, 8000, false) == low);
    CHECK(rate.update(start + 45s, true, 8000, 8000, false) == high);
    // Codec floor still defers to byte admission rather than inventing an
    // unsupported encoder rate below 500 kbit/s.
    CHECK(rate.update(start + 50s, true, 500, 8000, true) == 500U);
    CHECK(rate.update(start + 60s, true, 100000, 8000, true) <= 8000U);
}

void test_cropped_video_rate_fits_the_shared_wire_budget_at_full_cadence() {
    using namespace std::chrono_literals;
    const qmdp::VideoBudget::Clock::time_point start {};
    qmdp::CroppedVideoRate rate;
    qmdp::VideoBudget budget;
    const auto codec = rate.update(start, true, 1500, 8000, true);
    unsigned frames = 0;
    // A video that saturates its codec target plus sustained 15% exact UI
    // should fit through a 1.5 Mbit/s complete path, not self-throttle solely
    // because RTP overhead and UI were omitted from the encoder target.
    for (unsigned n = 0; n < 600; ++n) {
        const auto now = start + n * 16667us;
        budget.account_auxiliary(1500U * 1000U * 15U / 100U / 8U / 60U);
        if (budget.ready(now, 1500)) {
            budget.account(codec * 1000U / 8U / 60U);
            ++frames;
        }
    }
    CHECK(frames >= 595U);
}

void test_cropped_scene_burst_is_bounded_and_paid_before_another_capture() {
    using namespace std::chrono_literals;
    for (const auto fps : {10U, 60U, 240U}) {
        for (const auto bitrate : {500U, 1050U, 5600U, 100'000U}) {
            const auto bits = static_cast<std::uint64_t>(bitrate) * 1000U;
            CHECK(qmdp::CroppedVideoRate::buffer_bits(bitrate, fps, false) == bits / fps);
            const auto burst = qmdp::CroppedVideoRate::buffer_bits(bitrate, fps, true);
            CHECK(burst >= bits / fps);
            CHECK(burst <= bits * 450U / 1000U);
            CHECK(burst <= 2U * 1024U * 1024U * 8U);
        }
    }
    // Enlarging the codec VBV cannot give the next capture free byte credit,
    // including on a constrained link with an active exact-UI reservation.
    qmdp::VideoBudget budget;
    const qmdp::VideoBudget::Clock::time_point start {};
    CHECK(budget.ready(start, 1500U, 15U));
    budget.account(qmdp::CroppedVideoRate::buffer_bits(1050U, 60U, true) / 8U);
    CHECK(!budget.ready(start + 50ms, 1500U, 15U));
    CHECK(!budget.ready(start + 100ms, 1500U, 15U));
    CHECK(!budget.ready(start + 150ms, 1500U, 15U));
    CHECK(!budget.ready(start + 300ms, 1500U, 15U));
    CHECK(budget.ready(start + 450ms, 1500U, 15U));
}

void test_severe_crop_decrease_has_a_short_confirmed_boundary_only() {
    using namespace std::chrono_literals;
    const qmdp::CroppedVideoRate::Clock::time_point start {};
    qmdp::CroppedVideoRate rate;
    const auto high = rate.update(start, true, 8000U, 8000U, true);
    CHECK(rate.update(start + 100ms, true, 1500U, 8000U, false) == high);
    CHECK(rate.update(start + 500ms, true, 1500U, 8000U, false) == high);
    // A 400ms bad estimate cannot change the codec, even a severe one.
    CHECK(rate.update(start + 550ms, true, 8000U, 8000U, false) == high);
    CHECK(rate.update(start + 2s, true, 1500U, 8000U, false) == high);
    CHECK(rate.update(start + 2500ms, true, 1500U, 8000U, false) == high);
    CHECK(rate.update(start + 2600ms, true, 1500U, 8000U, false) < high / 2U);
    qmdp::CroppedVideoRate recent;
    CHECK(recent.update(start, true, 8000U, 8000U, true) == high);
    CHECK(recent.update(start + 100ms, true, 1500U, 8000U, false) == high);
    CHECK(recent.update(start + 700ms, true, 1500U, 8000U, false) == high);
    CHECK(recent.update(start + 1s, true, 1500U, 8000U, false) < high / 2U);
    qmdp::CroppedVideoRate ordinary;
    CHECK(ordinary.update(start, true, 8000U, 8000U, true) == high);
    CHECK(ordinary.update(start + 1s, true, 6000U, 8000U, false) == high);
    CHECK(ordinary.update(start + 3s, true, 6000U, 8000U, false) == high);
    CHECK(ordinary.update(start + 5s, true, 6000U, 8000U, false) < high);
}


void test_codec_retune_after_a_scene_burst_admits_one_fresh_picture() {
    using namespace std::chrono_literals;
    const qmdp::CroppedVideoRate::Clock::time_point start {};
    qmdp::CroppedVideoRate rate;
    // The retained native trace: an old 1750 kbit/s codec, a new 1529
    // kbit/s path, then a 47101-byte scene P immediately before retuning.
    CHECK(rate.update(start, true, 2521, 8000, true) == 1750U);
    CHECK(rate.update(start + 10s, true, 1529, 8000, false, 4000) == 1750U);
    CHECK(rate.update(start + 11500ms, true, 1529, 8000, false, 47101) == 1750U);
    // Byte admission remains responsible for this intermediate P. Even a
    // second large picture cannot defer the confirmed decrease again.
    CHECK(rate.update(start + 11517ms, true, 1529, 8000, false, 47101) == 1050U);
    CHECK(rate.update(start + 12s, true, 1529, 8000, false, 4000) == 1050U);

    qmdp::CroppedVideoRate quiet;
    CHECK(quiet.update(start, true, 2521, 8000, true) == 1750U);
    CHECK(quiet.update(start + 10s, true, 1529, 8000, false) == 1750U);
    CHECK(quiet.update(start + 11500ms, true, 1529, 8000, false, 4000) == 1050U);
}

void test_codec_scene_guard_preserves_urgent_repair_and_pending_lifetimes() {
    using namespace std::chrono_literals;
    const qmdp::CroppedVideoRate::Clock::time_point start {};
    qmdp::CroppedVideoRate rate;
    CHECK(rate.update(start, true, 8000, 8000, true) == 5600U);
    CHECK(rate.update(start + 10s, true, 1500, 8000, false, 50000) == 5600U);
    CHECK(rate.update(start + 10500ms, true, 1500, 8000, false, 50000) == 5600U);
    CHECK(rate.update(start + 10600ms, true, 1500, 8000, false, 50000) == 5600U);
    // A recovered target cancels both the dwell and its one-picture guard.
    CHECK(rate.update(start + 10617ms, true, 8000, 8000, false, 50000) == 5600U);
    CHECK(rate.update(start + 11s, true, 1500, 8000, false, 50000) == 5600U);
    CHECK(rate.update(start + 11600ms, true, 1500, 8000, false, 50000) == 5600U);
    // Explicit recovery passes no previous-picture hint: never postpone
    // the IDR needed to repair an invalid prediction chain.
    CHECK(rate.update(start + 11617ms, true, 1500, 8000, false, 0) == 1050U);
    CHECK(rate.update(start + 12s, false, 500, 8000, false, 50000) == 8000U);
    CHECK(rate.update(start + 13s, true, 1500, 8000, true, 50000) == 1050U);
}

void test_packet_confirmed_crop_repair_preserves_ordinary_quality_guards() {
    using namespace std::chrono_literals;
    const qmdp::CroppedVideoRate::Clock::time_point start {};
    qmdp::CroppedVideoRate rate;
    CHECK(rate.update(start, true, 8000, 8000, true) == 5600U);
    // Explicit packet confirmation can retune before ordinary600ms dwell.
    CHECK(rate.update(start + 2s, true, 1500, 8000, false, 80000, true) == 1050U);
    // A second repair cannot repeatedly reset the codec inside its quiet period.
    CHECK(rate.update(start + 2100ms, true, 500, 8000, false, 0, true) == 1050U);
    CHECK(rate.update(start + 3100ms, true, 500, 8000, false, 0, true) == 500U);
    // Full-desktop quality remains fixed even with explicit confirmation.
    CHECK(rate.update(start + 4s, false, 500, 8000, false, 0, true) == 8000U);
    // A halving of the path can require a smaller-than-half codec cut after
    // the previous upward codec dwell. Confirmation applies that real target.
    qmdp::CroppedVideoRate stepped;
    CHECK(stepped.update(start, true, 2815, 8000, true) == 1950U);
    CHECK(stepped.update(start + 2s, true, 1672, 8000, false, 9000, true) == 1150U);
    qmdp::CroppedVideoRate ordinary;
    CHECK(ordinary.update(start, true, 8000, 8000, true) == 5600U);
    CHECK(ordinary.update(start + 2s, true, 1500, 8000, false) == 5600U);
    CHECK(ordinary.update(start + 2500ms, true, 1500, 8000, false) == 5600U);
    CHECK(ordinary.update(start + 2600ms, true, 1500, 8000, false) == 1050U);
}

void test_capture_bursts_keep_both_fresh_images_in_order() {
    using namespace std::chrono_literals;
    qmdp::CaptureFrameQueue queue;
    const qmdp::CaptureFrameQueue::Clock::time_point start {};
    auto pixels = std::make_shared<const std::vector<std::uint8_t>>(16U, 0U);
    // Two captures arrive between encode ticks. The old latest-only slot
    // discarded the first even though both fit the next two media ticks.
    queue.push({pixels, 10, 2, 2, start + 1ms});
    queue.push({pixels, 11, 2, 2, start + 13ms});
    CHECK(queue.size() == 2 && queue.bytes() == 32);
    auto first = queue.pop(start + 16ms, 33ms);
    auto second = queue.pop(start + 32ms, 33ms);
    CHECK(first && first->capture == 10);
    CHECK(second && second->capture == 11);
    CHECK(queue.size() == 0 && queue.bytes() == 0);
}

void test_capture_history_expires_and_is_cleared_for_modes_and_recovery() {
    using namespace std::chrono_literals;
    qmdp::CaptureFrameQueue queue;
    const qmdp::CaptureFrameQueue::Clock::time_point start {};
    auto pixels = std::make_shared<const std::vector<std::uint8_t>>(16U, 0U);
    queue.push({pixels, 1, 2, 2, start});
    queue.push({pixels, 2, 2, 2, start + 1ms});
    queue.push({pixels, 3, 2, 2, start + 2ms});
    auto retained = queue.pop(start + 10ms, 33ms);
    CHECK(retained && retained->capture == 2);
    CHECK(!queue.pop(start + 40ms, 33ms));
    CHECK(queue.size() == 0 && queue.bytes() == 0);
    queue.push({pixels, 4, 2, 2, start + 50ms});
    queue.push({pixels, 5, 4, 1, start + 51ms});
    auto resized = queue.pop(start + 52ms, 33ms);
    CHECK(resized && resized->capture == 5 && resized->width == 4);
    queue.push({pixels, 6, 4, 1, start + 53ms});
    queue.clear();  // Caller discards history for backpressure/IDR/layout.
    CHECK(!queue.pop(start + 54ms, 33ms));
    CHECK(queue.bytes() == 0);
}

void test_capture_history_has_a_byte_bound_as_well_as_a_frame_bound() {
    using namespace std::chrono_literals;
    qmdp::CaptureFrameQueue queue;
    const qmdp::CaptureFrameQueue::Clock::time_point start {};
    auto large = std::make_shared<const std::vector<std::uint8_t>>(4096U * 2049U * 4U, 0U);
    queue.push({large, 1, 4096, 2049, start});
    queue.push({large, 2, 4096, 2049, start + 1ms});
    CHECK(queue.size() == 1 && queue.bytes() == large->size());
    auto latest = queue.pop(start + 2ms, 33ms);
    CHECK(latest && latest->capture == 2);
    latest.reset();
    queue.push({large, 3, 4096, 2049, start + 3ms});
    auto oversized = std::make_shared<const std::vector<std::uint8_t>>(4096U * 4097U * 4U, 0U);
    queue.push({oversized, 4, 4096, 4097, start + 4ms});
    CHECK(queue.size() == 0 && queue.bytes() == 0);
    CHECK(!queue.pop(start + 5ms, 33ms));  // Caller uses its latest image.
    CHECK(large.use_count() == 1 && oversized.use_count() == 1);
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
    test_video_budget_limits_encoded_bytes_before_admitting_raw_frames();
    test_raw_backpressure_expires_and_preserves_encoded_byte_debt();
    test_video_budget_keeps_keyframe_debt_across_rate_changes();
    test_ui_replay_reserves_capacity_without_double_charging_fresh_tiles();
    test_ui_burst_repays_bandwidth_without_a_long_video_admission_pause();
    test_cropped_video_rate_preserves_desktop_quality_and_ignores_brief_loss();
    test_cropped_video_rate_tracks_sustained_steps_without_chasing_jitter();
    test_cropped_video_rate_fits_the_shared_wire_budget_at_full_cadence();
    test_cropped_scene_burst_is_bounded_and_paid_before_another_capture();
    test_severe_crop_decrease_has_a_short_confirmed_boundary_only();
    test_codec_retune_after_a_scene_burst_admits_one_fresh_picture();
    test_codec_scene_guard_preserves_urgent_repair_and_pending_lifetimes();
    test_packet_confirmed_crop_repair_preserves_ordinary_quality_guards();
    test_capture_bursts_keep_both_fresh_images_in_order();
    test_capture_history_expires_and_is_cleared_for_modes_and_recovery();
    test_capture_history_has_a_byte_bound_as_well_as_a_frame_bound();

    if (failures == 0) {
        std::cout << "all qmdp core tests passed\n";
        return EXIT_SUCCESS;
    }
    std::cerr << failures << " test assertion(s) failed\n";
    return EXIT_FAILURE;
}
