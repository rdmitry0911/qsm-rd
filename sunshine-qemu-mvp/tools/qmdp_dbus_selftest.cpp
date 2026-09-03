#include "pipeline/desktop_session.hpp"
#include "qemu/qemu_dbus_display.hpp"
#include "software/ffmpeg_software_adapter.hpp"
#include "support/mock_qemu_dbus_server.hpp"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <string>
#include <thread>

namespace {

using namespace std::chrono_literals;

struct Options {
    std::filesystem::path output {"qmdp-output"};
    std::chrono::milliseconds duration {1500};
};

Options parse_options(int argc, char **argv) {
    Options result;
    for (int index = 1; index < argc; ++index) {
        const std::string argument(argv[index]);
        if (argument == "--output" && index + 1 < argc) {
            result.output = argv[++index];
        } else if (argument == "--duration-ms" && index + 1 < argc) {
            const auto value = std::stoll(argv[++index]);
            if (value <= 0) {
                throw std::invalid_argument("duration must be positive");
            }
            result.duration = std::chrono::milliseconds(value);
        } else if (argument == "--help") {
            std::cout << "usage: qmdp_dbus_selftest [--output DIR] "
                         "[--duration-ms N]\n";
            std::exit(EXIT_SUCCESS);
        } else {
            throw std::invalid_argument("unknown option: " + argument);
        }
    }
    return result;
}

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

int main(int argc, char **argv) {
    try {
        const auto options = parse_options(argc, argv);
        qmdp::test::MockQemuDbusServer mock({
            .width = 640U,
            .height = 360U,
            .fps = 30U,
        });
        auto client_fd = mock.take_client_fd();
        mock.start();

        qmdp::QemuDbusOptions display_options;
        display_options.destination.clear();
        display_options.p2p_fd = std::move(client_fd);
        display_options.call_timeout = 3s;
        display_options.pump_interval = 10ms;
        qmdp::QemuDbusDisplay display(std::move(display_options));

        qmdp::FfmpegSoftwareAdapter encoder({
            .output_directory = options.output,
            .filename_prefix = "qmdp-cpu",
            .ffmpeg_binary = "ffmpeg",
            .video_encoder = "libx264",
            .vaapi_device = std::nullopt,
            .fps = 30U,
        });
        qmdp::DesktopSession session(display, encoder, {.frame_wait = 20ms});
        session.start();

        if (!wait_until(2s, [&] { return encoder.stats().frames >= 3U; })) {
            throw std::runtime_error("no display frames arrived");
        }
        std::this_thread::sleep_for(options.duration / 2);
        session.set_ui_info({
            .request_id = 1U,
            .width = 854U,
            .height = 480U,
            .refresh_millihz = 60000U,
            .remote_scale_percent = 100U,
        });
        session.key(0x1EU, true);
        session.key(0x1EU, false);
        session.absolute_pointer(427U, 240U);
        std::this_thread::sleep_for(options.duration - options.duration / 2);

        session.stop();
        mock.stop();

        const auto session_stats = session.stats();
        const auto encoder_stats = encoder.stats();
        const auto display_stats = display.stats();
        const auto mock_stats = mock.stats();

        std::cout << "QMDP no-GPU vertical slice\n"
                  << "  encoded frames: " << encoder_stats.frames << '\n'
                  << "  output segments: " << encoder_stats.segments << '\n'
                  << "  final mode: " << encoder_stats.width << 'x'
                  << encoder_stats.height << '\n'
                  << "  shared-map scanouts/updates: "
                  << display_stats.mapped_scanouts << '/'
                  << display_stats.mapped_updates << '\n'
                  << "  mailbox dropped: "
                  << session_stats.mailbox.dropped << '\n'
                  << "  audio callbacks/frames: "
                  << session_stats.audio_callbacks << '/'
                  << encoder_stats.audio_frames << '\n'
                  << "  ffmpeg exit status: "
                  << encoder_stats.last_exit_status << '\n'
                  << "  display audio registered/failures/writes: "
                  << display_stats.audio_listener_registered << '/'
                  << display_stats.audio_registration_failures << '/'
                  << display_stats.audio_writes << '\n'
                  << "  mock audio listeners/writes/frames: "
                  << mock_stats.audio_listeners_registered << '/'
                  << mock_stats.audio_writes_sent << '/'
                  << mock_stats.audio_frames_sent << '\n'
                  << "  session errors: " << session_stats.errors << '\n'
                  << "  mock last error: "
                  << (mock_stats.last_error.empty() ? "<none>" : mock_stats.last_error)
                  << '\n'
                  << "  output directory: "
                  << std::filesystem::absolute(options.output) << '\n';

        const bool passed = encoder_stats.frames >= 10U &&
                            encoder_stats.segments >= 2U &&
                            encoder_stats.last_exit_status == 0 &&
                            encoder_stats.width == 854U &&
                            encoder_stats.height == 480U &&
                            encoder_stats.audio_frames >= 4800U &&
                            display_stats.audio_writes >= 10U &&
                            display_stats.audio_frames == encoder_stats.audio_frames &&
                            display_stats.mapped_scanouts >= 2U &&
                            display_stats.audio_listener_registered &&
                            display_stats.audio_registration_failures == 0U &&
                            display_stats.audio_writes >= 2U &&
                            encoder_stats.audio_frames >= 960U &&
                            mock_stats.audio_listeners_registered == 1U &&
                            session_stats.audio_submissions >= 2U &&
                            session_stats.rejected_audio_callbacks == 0U &&
                            session_stats.audio_fifo.queued_frames == 0U &&
                            session_stats.audio_fifo.dropped_frames == 0U &&
                            mock_stats.last_error.empty() &&
                            session_stats.errors == 0U;
        return passed ? EXIT_SUCCESS : EXIT_FAILURE;
    } catch (const std::exception& ex) {
        std::cerr << "fatal: " << ex.what() << '\n';
        return EXIT_FAILURE;
    }
}
