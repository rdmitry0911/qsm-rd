#include "pipeline/desktop_session.hpp"
#include "qemu/qemu_dbus_display.hpp"
#include "software/cpu_frame_sink.hpp"
#include "software/ffmpeg_software_adapter.hpp"

#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <fcntl.h>
#include <unistd.h>

namespace {

using namespace std::chrono_literals;

struct Size {
    std::uint32_t width {};
    std::uint32_t height {};
};

struct Options {
    std::string dbus_address;
    std::string destination;
    std::optional<int> inherited_fd;
    std::uint32_t console_id {0U};
    std::chrono::milliseconds duration {3000};
    std::chrono::milliseconds frame_wait {20};
    std::chrono::milliseconds artificial_delay {0};
    std::optional<Size> requested_size;
    std::filesystem::path snapshot {"qemu-probe-last.ppm"};
    bool snapshot_enabled {true};
    std::optional<std::filesystem::path> encode_directory;
    std::string ffmpeg_binary {"ffmpeg"};
    std::string video_encoder {"libx264"};
    std::uint32_t fps {30U};
    bool enable_audio {true};
    bool require_audio {false};
    bool input_smoke {false};
};

[[noreturn]] void usage(int status) {
    std::ostream& output = status == EXIT_SUCCESS ? std::cout : std::cerr;
    output
        << "usage: qemu-display-probe (--dbus-address ADDRESS | "
           "--dbus-address-file FILE | --fd N) [options]\n\n"
        << "Connection options:\n"
        << "  --dbus-address ADDRESS       QEMU D-Bus address\n"
        << "  --dbus-address-file FILE     read the address from the first line\n"
        << "  --fd N                       inherited connected peer D-Bus fd\n"
        << "  --destination NAME           message-bus destination (default: p2p)\n"
        << "  --console N                  QEMU console id (default: 0)\n\n"
        << "Capture options:\n"
        << "  --duration-ms N              run duration (default: 3000)\n"
        << "  --request-size WIDTHxHEIGHT  call Console.SetUIInfo after startup\n"
        << "  --snapshot FILE              write the final CPU frame as PPM\n"
        << "  --no-snapshot                do not write a PPM snapshot\n"
        << "  --encode-dir DIR             encode H.264 MKV segments with ffmpeg\n"
        << "  --ffmpeg FILE                ffmpeg executable (default: ffmpeg)\n"
        << "  --video-encoder NAME         ffmpeg encoder (default: libx264)\n"
        << "  --fps N                      encoder input rate (default: 30)\n"
        << "  --encode-delay-ms N          artificial CPU sink delay for stress tests\n"
        << "  --frame-wait-ms N            mailbox wait period (default: 20)\n"
        << "  --input-smoke                send one keyboard/mouse smoke sequence\n\n"
        << "Audio options:\n"
        << "  --no-audio                   skip Audio.RegisterOutListener\n"
        << "  --require-audio              fail when QEMU audio is unavailable\n"
        << "  --help                       show this help\n";
    std::exit(status);
}

template <typename Integer>
Integer parse_integer(std::string_view text, std::string_view name) {
    Integer result {};
    const auto *first = text.data();
    const auto *last = first + text.size();
    const auto parsed = std::from_chars(first, last, result);
    if (parsed.ec != std::errc {} || parsed.ptr != last) {
        throw std::invalid_argument("invalid " + std::string(name) + ": " +
                                    std::string(text));
    }
    return result;
}

Size parse_size(std::string_view text) {
    const auto separator = text.find_first_of("xX");
    if (separator == std::string_view::npos) {
        throw std::invalid_argument("size must have WIDTHxHEIGHT form");
    }
    const auto width = parse_integer<std::uint32_t>(text.substr(0U, separator),
                                                    "width");
    const auto height = parse_integer<std::uint32_t>(text.substr(separator + 1U),
                                                     "height");
    if (width == 0U || height == 0U || width > 16384U || height > 16384U) {
        throw std::invalid_argument("requested size is outside 1..16384 pixels");
    }
    return {.width = width, .height = height};
}

std::string read_address_file(const std::filesystem::path& path) {
    std::ifstream input(path);
    if (!input) {
        throw std::runtime_error("cannot read D-Bus address file: " +
                                 path.string());
    }
    std::string address;
    std::getline(input, address);
    if (address.empty()) {
        throw std::runtime_error("D-Bus address file is empty: " + path.string());
    }
    return address;
}

Options parse_options(int argc, char **argv) {
    Options result;
    auto value_after = [&](int& index, std::string_view option) -> std::string_view {
        if (index + 1 >= argc) {
            throw std::invalid_argument(std::string(option) + " requires a value");
        }
        return argv[++index];
    };

    for (int index = 1; index < argc; ++index) {
        const std::string_view argument(argv[index]);
        if (argument == "--dbus-address") {
            result.dbus_address = value_after(index, argument);
        } else if (argument == "--dbus-address-file") {
            result.dbus_address = read_address_file(value_after(index, argument));
        } else if (argument == "--fd") {
            result.inherited_fd =
                parse_integer<int>(value_after(index, argument), "fd");
        } else if (argument == "--destination") {
            result.destination = value_after(index, argument);
        } else if (argument == "--console") {
            result.console_id = parse_integer<std::uint32_t>(
                value_after(index, argument), "console id");
        } else if (argument == "--duration-ms") {
            result.duration = std::chrono::milliseconds(parse_integer<long long>(
                value_after(index, argument), "duration"));
        } else if (argument == "--frame-wait-ms") {
            result.frame_wait = std::chrono::milliseconds(parse_integer<long long>(
                value_after(index, argument), "frame wait"));
        } else if (argument == "--encode-delay-ms") {
            result.artificial_delay = std::chrono::milliseconds(
                parse_integer<long long>(value_after(index, argument),
                                         "encode delay"));
        } else if (argument == "--request-size") {
            result.requested_size = parse_size(value_after(index, argument));
        } else if (argument == "--snapshot") {
            result.snapshot = value_after(index, argument);
            result.snapshot_enabled = true;
        } else if (argument == "--no-snapshot") {
            result.snapshot_enabled = false;
        } else if (argument == "--encode-dir") {
            result.encode_directory = value_after(index, argument);
        } else if (argument == "--ffmpeg") {
            result.ffmpeg_binary = value_after(index, argument);
        } else if (argument == "--video-encoder") {
            result.video_encoder = value_after(index, argument);
        } else if (argument == "--fps") {
            result.fps = parse_integer<std::uint32_t>(
                value_after(index, argument), "fps");
        } else if (argument == "--input-smoke") {
            result.input_smoke = true;
        } else if (argument == "--no-audio") {
            result.enable_audio = false;
            result.require_audio = false;
        } else if (argument == "--require-audio") {
            result.enable_audio = true;
            result.require_audio = true;
        } else if (argument == "--help" || argument == "-h") {
            usage(EXIT_SUCCESS);
        } else {
            throw std::invalid_argument("unknown option: " + std::string(argument));
        }
    }

    if (result.dbus_address.empty() == !result.inherited_fd.has_value()) {
        throw std::invalid_argument(
            "specify exactly one of --dbus-address/--dbus-address-file or --fd");
    }
    if (result.inherited_fd && *result.inherited_fd < 0) {
        throw std::invalid_argument("inherited fd cannot be negative");
    }
    if (result.duration.count() <= 0 || result.frame_wait.count() <= 0 ||
        result.artificial_delay.count() < 0 || result.fps == 0U) {
        throw std::invalid_argument("duration, frame wait and fps must be positive");
    }
    return result;
}

qmdp::UniqueFd duplicate_inherited_fd(int fd) {
    const int duplicate = ::fcntl(fd, F_DUPFD_CLOEXEC, 3);
    if (duplicate < 0) {
        throw std::runtime_error("cannot duplicate inherited fd " +
                                 std::to_string(fd));
    }
    return qmdp::UniqueFd(duplicate);
}

}  // namespace

int main(int argc, char **argv) {
    try {
        const auto options = parse_options(argc, argv);

        qmdp::QemuDbusOptions display_options;
        display_options.bus_address = options.dbus_address;
        display_options.destination = options.destination;
        display_options.console_id = options.console_id;
        display_options.enable_audio = options.enable_audio;
        display_options.require_audio = options.require_audio;
        display_options.call_timeout = 5s;
        display_options.pump_interval = 10ms;
        if (options.inherited_fd) {
            display_options.p2p_fd = duplicate_inherited_fd(*options.inherited_fd);
        }
        qmdp::QemuDbusDisplay display(std::move(display_options));

        std::unique_ptr<qmdp::ISunshineAdapter> adapter;
        qmdp::CpuFrameSink *cpu_sink = nullptr;
        qmdp::FfmpegSoftwareAdapter *ffmpeg_sink = nullptr;
        if (options.encode_directory) {
            auto sink = std::make_unique<qmdp::FfmpegSoftwareAdapter>(
                qmdp::FfmpegSoftwareOptions {
                    .output_directory = *options.encode_directory,
                    .filename_prefix = "qemu-probe",
                    .ffmpeg_binary = options.ffmpeg_binary,
                    .video_encoder = options.video_encoder,
                    .fps = options.fps,
                });
            ffmpeg_sink = sink.get();
            adapter = std::move(sink);
        } else {
            auto sink = std::make_unique<qmdp::CpuFrameSink>(
                qmdp::CpuFrameSinkOptions {
                    .snapshot_path = options.snapshot_enabled
                        ? options.snapshot
                        : std::filesystem::path {},
                    .artificial_encode_delay = options.artificial_delay,
                });
            cpu_sink = sink.get();
            adapter = std::move(sink);
        }

        qmdp::DesktopSession session(display,
                                     *adapter,
                                     {.frame_wait = options.frame_wait});
        session.start();

        const auto started = std::chrono::steady_clock::now();
        bool resize_sent = false;
        bool input_sent = false;
        std::optional<bool> input_pointer_absolute;
        while (std::chrono::steady_clock::now() - started < options.duration) {
            if (!resize_sent && options.requested_size &&
                std::chrono::steady_clock::now() - started >= 250ms) {
                session.set_ui_info({
                    .request_id = 1U,
                    .width = options.requested_size->width,
                    .height = options.requested_size->height,
                    .refresh_millihz = options.fps * 1000U,
                    .remote_scale_percent = 100U,
                });
                resize_sent = true;
            }
            if (!input_sent && options.input_smoke &&
                std::chrono::steady_clock::now() - started >= 400ms) {
                // QEMU key number 0x1e is the XT keyboard scan code for A.
                // A console exposes either absolute or relative pointer motion;
                // `Mouse.IsAbsolute` selects the one valid call. Sending both
                // is rejected by a real QEMU.
                session.key(0x1eU, true);
                session.key(0x1eU, false);
                input_pointer_absolute = session.is_absolute_pointer();
                if (*input_pointer_absolute) {
                    const auto pointer_width = options.requested_size
                        ? options.requested_size->width
                        : 640U;
                    const auto pointer_height = options.requested_size
                        ? options.requested_size->height
                        : 480U;
                    session.absolute_pointer(pointer_width / 2U,
                                             pointer_height / 2U);
                } else {
                    session.relative_pointer(3, -2);
                }
                session.button(0U, true);
                session.button(0U, false);
                input_sent = true;
            }
            std::this_thread::sleep_for(20ms);
        }

        session.stop();
        const auto session_stats = session.stats();
        const auto display_stats = display.stats();

        std::uint64_t adapter_frames = 0U;
        std::uint64_t adapter_audio_frames = 0U;
        int encoder_status = 0;
        if (cpu_sink != nullptr) {
            const auto stats = cpu_sink->stats();
            adapter_frames = stats.frames;
            adapter_audio_frames = stats.audio_frames;
        } else if (ffmpeg_sink != nullptr) {
            const auto stats = ffmpeg_sink->stats();
            adapter_frames = stats.frames;
            adapter_audio_frames = stats.audio_frames;
            encoder_status = stats.last_exit_status;
        }

        std::cout
            << "QEMU_DISPLAY_PROBE_RESULT\n"
            << "  frames published/encoded/dropped: "
            << session_stats.mailbox.published << '/'
            << session_stats.encoded_frames << '/'
            << session_stats.mailbox.dropped << '\n'
            << "  adapter frames/audio frames: "
            << adapter_frames << '/' << adapter_audio_frames << '\n'
            << "  scanout inline/map: "
            << display_stats.inline_scanouts << '/'
            << display_stats.mapped_scanouts << '\n'
            << "  updates inline/map: "
            << display_stats.inline_updates << '/'
            << display_stats.mapped_updates << '\n'
            << "  stale geometry updates dropped: "
            << display_stats.stale_geometry_update_drops << '\n'
            << "  cursor definitions/moves: "
            << display_stats.cursor_definitions << '/'
            << display_stats.cursor_moves << '\n'
            << "  input pointer mode: "
            << (!input_pointer_absolute.has_value()
                    ? "not-sent"
                    : (*input_pointer_absolute ? "absolute" : "relative"))
            << '\n'
            << "  audio init/writes/frames: "
            << display_stats.audio_inits << '/'
            << display_stats.audio_writes << '/'
            << display_stats.audio_frames << '\n'
            << "  audio registration failures: "
            << display_stats.audio_registration_failures << '\n'
            << "  ffmpeg exit status: " << encoder_status << '\n'
            << "  session errors: " << session_stats.errors << '\n';
        for (const auto& error : session_stats.recent_errors) {
            std::cerr << "  error: " << error << '\n';
        }

        const bool video_ok = adapter_frames > 0U &&
                              session_stats.encoded_frames > 0U;
        const bool audio_ok = !options.require_audio ||
                              (adapter_audio_frames > 0U &&
                               display_stats.audio_registration_failures == 0U);
        const bool encoder_ok = ffmpeg_sink == nullptr || encoder_status == 0;
        return video_ok && audio_ok && encoder_ok && session_stats.errors == 0U
            ? EXIT_SUCCESS
            : EXIT_FAILURE;
    } catch (const std::exception& ex) {
        std::cerr << "fatal: " << ex.what() << '\n';
        return EXIT_FAILURE;
    }
}
