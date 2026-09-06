#include "core/audio_fifo.hpp"
#include "core/latest_frame_mailbox.hpp"
#include "core/resize_coalescer.hpp"
#include "core/session.hpp"
#include "mock/mock_qemu_display.hpp"
#include "mock/mock_media_adapter.hpp"

#include <chrono>
#include <cstdlib>
#include <exception>
#include <iostream>
#include <thread>

using namespace std::chrono_literals;

int main() {
    try {
        qmdp::LatestFrameMailbox mailbox;
        qmdp::AudioFifo audio_fifo(48000U, 2U, 2400U);  // 50 ms max latency budget.
        qmdp::ResizeCoalescer resize;
        qmdp::SessionStateMachine session;
        qmdp::MockQemuDisplay qemu(120U);
        qmdp::MockMediaAdapter media;

        (void) session.dispatch(qmdp::SessionEvent::prepare);
        media.start();
        qemu.start({
            .on_frame = [&mailbox](qmdp::FrameToken frame) {
                mailbox.publish(std::move(frame));
            },
            .on_audio = [&audio_fifo](std::span<const float> samples,
                                      std::uint32_t sample_rate,
                                      std::uint16_t channels) {
                if (sample_rate == audio_fifo.sample_rate() &&
                    channels == audio_fifo.channels()) {
                    audio_fifo.push(samples);
                }
            },
            .on_cursor = nullptr,
            .on_error = [](std::string message) {
                std::cerr << "QEMU error: " << message << '\n';
            },
        });
        (void) session.dispatch(qmdp::SessionEvent::prepared);

        const auto start = std::chrono::steady_clock::now();
        auto next_encode = start;
        auto next_audio = start;
        bool resize_burst_sent = false;
        bool resize_waiting_for_frame = false;
        std::uint64_t expected_surface_generation = 0U;

        while (std::chrono::steady_clock::now() - start < 1500ms) {
            const auto now = std::chrono::steady_clock::now();

            if (!resize_burst_sent && now - start >= 300ms) {
                // Simulate a client window being dragged through several sizes.
                resize.submit({1U, 2101U, 1181U, 60000U, 100U}, now);
                resize.submit({2U, 2303U, 1297U, 60000U, 100U}, now + 20ms);
                resize.submit({3U, 2561U, 1441U, 60000U, 125U}, now + 40ms);
                resize_burst_sent = true;
            }

            if (auto request = resize.poll(now); request) {
                (void) session.dispatch(qmdp::SessionEvent::resize_begin);
                qemu.set_ui_info(*request);
                resize_waiting_for_frame = true;
                expected_surface_generation = 2U;
            }

            if (now >= next_encode) {
                next_encode += 16667us;  // 60 Hz encoder cadence.
                if (auto frame = mailbox.try_pop(); frame) {
                    media.submit_frame(*frame);
                    if (resize_waiting_for_frame &&
                        frame->surface->generation >= expected_surface_generation) {
                        media.request_idr();
                        (void) session.dispatch(qmdp::SessionEvent::resize_applied);
                        resize_waiting_for_frame = false;
                    }
                }
            }

            if (now >= next_audio) {
                next_audio += 10ms;
                auto samples = audio_fifo.pop(480U);
                if (!samples.empty()) {
                    media.submit_audio(samples, 48000U, 2U);
                }
            }

            std::this_thread::sleep_for(1ms);
        }

        qemu.key(0x1EU, true);
        qemu.key(0x1EU, false);
        qemu.absolute_pointer(960U, 540U);
        qemu.button(1U, true);
        qemu.button(1U, false);

        qemu.stop();
        mailbox.close();
        media.stop();
        (void) session.dispatch(qmdp::SessionEvent::stop);
        (void) session.dispatch(qmdp::SessionEvent::stopped);

        const auto mailbox_stats = mailbox.stats();
        const auto audio_stats = audio_fifo.stats();
        const auto media_stats = media.stats();

        std::cout << "Media-QEMU MVP core simulation\n"
                  << "  session: " << qmdp::SessionStateMachine::name(session.state()) << '\n'
                  << "  frames published/encoded/dropped: "
                  << mailbox_stats.published << '/' << media_stats.frames << '/'
                  << mailbox_stats.dropped << '\n'
                  << "  encoded audio frames: " << media_stats.audio_frames << '\n'
                  << "  audio FIFO dropped frames: " << audio_stats.dropped_frames << '\n'
                  << "  resize requests superseded: " << resize.superseded_count() << '\n'
                  << "  resulting mode: " << media_stats.last_width << 'x'
                  << media_stats.last_height << '\n'
                  << "  encoder IDR requests: " << media_stats.idr_requests << '\n'
                  << "  QEMU input events: " << qemu.input_event_count() << '\n';

        const bool passed = media_stats.frames > 30U &&
                            media_stats.audio_frames > 1000U &&
                            media_stats.last_width == 2560U &&
                            media_stats.last_height == 1440U &&
                            media_stats.idr_requests == 1U &&
                            qemu.input_event_count() == 5U &&
                            session.state() == qmdp::SessionState::idle;
        return passed ? EXIT_SUCCESS : EXIT_FAILURE;
    } catch (const std::exception& ex) {
        std::cerr << "fatal: " << ex.what() << '\n';
        return EXIT_FAILURE;
    }
}
