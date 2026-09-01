#include "core/audio_fifo.hpp"
#include "core/frame.hpp"
#include "core/latest_frame_mailbox.hpp"
#include "core/resize_coalescer.hpp"
#include "core/session.hpp"
#include "core/unix_fd.hpp"
#include "capture/cpu_framebuffer.hpp"

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <string_view>
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

qmdp::FrameToken frame(std::uint64_t sequence) {
    auto surface = std::make_shared<qmdp::FrameSurface>();
    surface->width = 2U;
    surface->height = 2U;
    surface->generation = 1U;
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

}  // namespace

int main() {
    test_unique_fd();
    test_latest_frame_mailbox();
    test_resize_coalescer();
    test_audio_fifo();
    test_stale_damage_is_detected_without_mutating_the_framebuffer();
    test_session_state_machine();

    if (failures == 0) {
        std::cout << "all qmdp core tests passed\n";
        return EXIT_SUCCESS;
    }
    std::cerr << failures << " test assertion(s) failed\n";
    return EXIT_FAILURE;
}
