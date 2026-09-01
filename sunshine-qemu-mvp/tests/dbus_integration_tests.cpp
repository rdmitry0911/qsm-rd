#include "pipeline/desktop_session.hpp"
#include "qemu/qemu_dbus_display.hpp"
#include "software/cpu_frame_sink.hpp"
#include "support/mock_qemu_dbus_server.hpp"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>

namespace {

using namespace std::chrono_literals;
int failures = 0;

#define CHECK(condition)                                                        \
    do {                                                                        \
        if (!(condition)) {                                                     \
            std::cerr << __FILE__ << ':' << __LINE__                           \
                      << ": CHECK failed: " #condition << '\n';                \
            ++failures;                                                         \
        }                                                                       \
    } while (false)

template <typename Predicate>
bool wait_until(std::chrono::milliseconds timeout, Predicate&& predicate) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (std::chrono::steady_clock::now() < deadline) {
        if (predicate()) {
            return true;
        }
        std::this_thread::sleep_for(10ms);
    }
    return predicate();
}

}  // namespace

int main() {
    try {
        const auto snapshot = std::filesystem::current_path() /
                              "dbus-integration-last.ppm";
        std::filesystem::remove(snapshot);

        qmdp::test::MockQemuDbusServer qemu_server({
            .width = 320U,
            .height = 180U,
            .fps = 60U,
        });
        auto client_fd = qemu_server.take_client_fd();
        qemu_server.start();

        qmdp::QemuDbusOptions display_options;
        display_options.destination.clear();
        display_options.p2p_fd = std::move(client_fd);
        display_options.call_timeout = 2s;
        display_options.pump_interval = 10ms;
        qmdp::QemuDbusDisplay display(std::move(display_options));

        qmdp::CpuFrameSink sink({
            .snapshot_path = snapshot,
            .artificial_encode_delay = 25ms,
        });
        qmdp::DesktopSession session(display, sink, {.frame_wait = 20ms});
        session.start();

        CHECK(wait_until(2s, [&] {
            return sink.stats().frames >= 5U &&
                   sink.stats().audio_frames >= 4800U &&
                   qemu_server.stats().listener_connected &&
                   qemu_server.stats().audio_listener_connected;
        }));

        session.key(0x1EU, true);
        session.key(0x1EU, false);
        session.button(0U, true);
        session.button(0U, false);
        session.absolute_pointer(100U, 80U);
        session.relative_pointer(3, -2);

        session.set_ui_info({
            .request_id = 1U,
            .width = 426U,
            .height = 242U,
            .refresh_millihz = 60000U,
            .remote_scale_percent = 100U,
        });

        CHECK(wait_until(3s, [&] {
            const auto stats = sink.stats();
            return stats.width == 426U && stats.height == 242U &&
                   stats.idr_requests >= 1U;
        }));
        std::this_thread::sleep_for(500ms);

        session.stop();

        // Keep the mock QEMU peer alive after the client stops.  A teardown
        // that removes the listener handler before closing its socket makes
        // the peer observe an unexpected UnknownMethod rather than a normal
        // disconnect while it is still producing display updates.
        CHECK(wait_until(2s, [&] {
            const auto stats = qemu_server.stats();
            return !stats.listener_connected &&
                   !stats.audio_listener_connected;
        }));
        const auto stopped_server_stats = qemu_server.stats();
        CHECK(stopped_server_stats.last_error.empty());
        qemu_server.stop();

        const auto session_stats = session.stats();
        const auto sink_stats = sink.stats();
        const auto display_stats = display.stats();
        const auto server_stats = qemu_server.stats();

        CHECK(session_stats.encoded_frames >= 10U);
        CHECK(session_stats.mailbox.published > session_stats.encoded_frames);
        CHECK(session_stats.mailbox.dropped > 0U);
        CHECK(session_stats.idr_requests >= 1U);
        CHECK(session_stats.audio_callbacks >= 2U);
        CHECK(session_stats.audio_submissions >= 2U);
        CHECK(session_stats.rejected_audio_callbacks == 0U);
        CHECK(session_stats.audio_fifo.queued_frames == 0U);
        CHECK(session_stats.audio_fifo.dropped_frames == 0U);
        CHECK(session_stats.errors == 0U);

        CHECK(sink_stats.frames == session_stats.encoded_frames);
        CHECK(sink_stats.audio_frames >= 960U);
        CHECK(sink_stats.width == 426U);
        CHECK(sink_stats.height == 242U);
        CHECK(sink_stats.mode_changes >= 1U);
        CHECK(sink_stats.checksum != 14695981039346656037ULL);
        CHECK(sink_stats.audio_frames >= 4800U);
        CHECK(session_stats.audio_callbacks >= 10U);

        CHECK(display_stats.mapped_scanouts >= 2U);
        CHECK(display_stats.mapped_updates >= 5U);
        CHECK(display_stats.cursor_definitions >= 1U);
        CHECK(display_stats.cursor_moves >= 2U);
        CHECK(display_stats.unsupported_dmabuf_messages == 0U);
        CHECK(display_stats.audio_registration_failures == 0U);
        CHECK(display_stats.audio_inits == 1U);
        CHECK(display_stats.audio_writes >= 10U);
        CHECK(display_stats.audio_frames == sink_stats.audio_frames);
        CHECK(display_stats.audio_bytes ==
              display_stats.audio_frames * 2U * sizeof(float));
        CHECK(display_stats.audio_enable_changes == 1U);
        CHECK(display_stats.audio_volume_changes == 1U);

        CHECK(server_stats.listeners_registered == 1U);
        CHECK(server_stats.scanouts_sent >= 2U);
        CHECK(server_stats.updates_sent >= 5U);
        CHECK(server_stats.resize_requests == 1U);
        CHECK(server_stats.keyboard_events == 2U);
        CHECK(server_stats.mouse_events == 4U);
        CHECK(server_stats.audio_listeners_registered == 1U);
        CHECK(server_stats.audio_inits_sent == 1U);
        CHECK(server_stats.audio_writes_sent == display_stats.audio_writes);
        CHECK(server_stats.audio_frames_sent == display_stats.audio_frames);
        CHECK(server_stats.width == 426U);
        CHECK(server_stats.height == 242U);
        if (!server_stats.last_error.empty()) {
            std::cerr << "mock server error: " << server_stats.last_error << '\n';
        }
        CHECK(server_stats.last_error.empty());

        CHECK(std::filesystem::exists(snapshot));
        CHECK(std::filesystem::file_size(snapshot) > 426U * 242U * 3U);
        std::ifstream ppm(snapshot, std::ios::binary);
        std::string magic;
        ppm >> magic;
        CHECK(magic == "P6");

        std::cout << "QEMU D-Bus CPU integration\n"
                  << "  frames published/encoded/dropped: "
                  << session_stats.mailbox.published << '/'
                  << session_stats.encoded_frames << '/'
                  << session_stats.mailbox.dropped << '\n'
                  << "  map scanouts/updates: "
                  << display_stats.mapped_scanouts << '/'
                  << display_stats.mapped_updates << '\n'
                  << "  final mode: " << sink_stats.width << 'x'
                  << sink_stats.height << '\n'
                  << "  input keyboard/mouse: "
                  << server_stats.keyboard_events << '/'
                  << server_stats.mouse_events << '\n'
                  << "  audio writes/frames: "
                  << display_stats.audio_writes << '/'
                  << display_stats.audio_frames << '\n'
                  << "  snapshot: " << snapshot << '\n';

        if (failures == 0) {
            std::cout << "all QEMU D-Bus CPU integration checks passed\n";
            return EXIT_SUCCESS;
        }
        std::cerr << failures << " integration assertion(s) failed\n";
        return EXIT_FAILURE;
    } catch (const std::exception& ex) {
        std::cerr << "fatal integration failure: " << ex.what() << '\n';
        return EXIT_FAILURE;
    }
}
