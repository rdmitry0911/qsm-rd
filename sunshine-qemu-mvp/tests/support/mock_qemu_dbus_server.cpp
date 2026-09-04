#include "support/mock_qemu_dbus_server.hpp"

#include "core/pixel_format.hpp"

#include <algorithm>
#include <array>
#include <bit>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <numbers>
#include <stdexcept>
#include <span>
#include <system_error>
#include <string_view>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace qmdp::test {
namespace {

using namespace std::chrono_literals;

constexpr std::string_view console_path = "/org/qemu/Display1/Console_0";
constexpr std::string_view console_interface = "org.qemu.Display1.Console";
constexpr std::string_view keyboard_interface = "org.qemu.Display1.Keyboard";
constexpr std::string_view mouse_interface = "org.qemu.Display1.Mouse";
constexpr std::string_view audio_path = "/org/qemu/Display1/Audio";
constexpr std::string_view audio_interface = "org.qemu.Display1.Audio";
constexpr std::string_view audio_listener_path = "/org/qemu/Display1/AudioOutListener";
constexpr std::string_view audio_listener_interface = "org.qemu.Display1.AudioOutListener";
constexpr std::string_view listener_path = "/org/qemu/Display1/Listener";
constexpr std::string_view listener_interface = "org.qemu.Display1.Listener";
constexpr std::string_view map_interface = "org.qemu.Display1.Listener.Unix.Map";
constexpr std::string_view properties_interface = "org.freedesktop.DBus.Properties";
constexpr std::string_view peer_interface = "org.freedesktop.DBus.Peer";
constexpr std::uint64_t method_timeout_usec = 2'000'000U;

UniqueFd duplicate_cloexec(int fd) {
    const int copy = ::fcntl(fd, F_DUPFD_CLOEXEC, 3);
    if (copy < 0) {
        throw std::system_error(errno,
                                std::generic_category(),
                                "fcntl(F_DUPFD_CLOEXEC)");
    }
    return UniqueFd(copy);
}

bool is_expected_disconnect(std::string_view message) noexcept {
    return message.find("org.freedesktop.DBus.Error.Disconnected") != std::string_view::npos ||
           message.find("Connection reset by peer") != std::string_view::npos ||
           message.find("Broken pipe") != std::string_view::npos ||
           message.find("Transport endpoint is not connected") != std::string_view::npos;
}

std::size_t checked_frame_bytes(std::uint32_t width,
                                std::uint32_t height) {
    const std::uint64_t bytes = static_cast<std::uint64_t>(width) * height * 4U;
    if (width == 0U || height == 0U ||
        bytes > std::numeric_limits<std::size_t>::max()) {
        throw std::invalid_argument("invalid mock framebuffer dimensions");
    }
    return static_cast<std::size_t>(bytes);
}

void call_no_reply_payload(dbus::Bus& bus,
                           const char *interface,
                           const char *member,
                           const char *signature,
                           ...) = delete;

void send_cursor_call(dbus::Bus& bus,
                      std::span<const std::uint8_t> pixels) {
    dbus::Message call;
    dbus::check(sd_bus_message_new_method_call(bus.get(),
                                               call.put(),
                                               nullptr,
                                               listener_path.data(),
                                               listener_interface.data(),
                                               "CursorDefine"),
                "new CursorDefine call");
    dbus::check(sd_bus_message_append(call.get(), "iiii", 16, 16, 1, 1),
                "append CursorDefine geometry");
    dbus::check(sd_bus_message_append_array(call.get(),
                                            'y',
                                            pixels.data(),
                                            pixels.size()),
                "append CursorDefine pixels");
    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call(bus.get(),
                                   call.get(),
                                   method_timeout_usec,
                                   error.get(),
                                   reply.put());
    dbus::check(result, "Listener.CursorDefine", error.get());
}

}  // namespace

struct MockQemuDbusServer::SharedFramebuffer {
    UniqueFd fd;
    void *mapping {nullptr};
    std::size_t size {};
    std::uint32_t width {};
    std::uint32_t height {};
    std::uint32_t stride {};

    ~SharedFramebuffer() {
        if (mapping != nullptr) {
            (void) ::munmap(mapping, size);
        }
    }

    SharedFramebuffer() = default;
    SharedFramebuffer(const SharedFramebuffer&) = delete;
    SharedFramebuffer& operator=(const SharedFramebuffer&) = delete;
    SharedFramebuffer(SharedFramebuffer&& other) noexcept
        : fd(std::move(other.fd)),
          mapping(other.mapping),
          size(other.size),
          width(other.width),
          height(other.height),
          stride(other.stride) {
        other.mapping = nullptr;
        other.size = 0U;
    }
    SharedFramebuffer& operator=(SharedFramebuffer&& other) noexcept {
        if (this != &other) {
            if (mapping != nullptr) {
                (void) ::munmap(mapping, size);
            }
            fd = std::move(other.fd);
            mapping = other.mapping;
            size = other.size;
            width = other.width;
            height = other.height;
            stride = other.stride;
            other.mapping = nullptr;
            other.size = 0U;
        }
        return *this;
    }

    [[nodiscard]] std::uint8_t *bytes() noexcept {
        return static_cast<std::uint8_t *>(mapping);
    }
};

MockQemuDbusServer::MockQemuDbusServer(MockQemuDbusOptions options)
    : options_(options),
      requested_width_(options.width),
      requested_height_(options.height) {
    if (options_.width == 0U || options_.height == 0U || options_.fps == 0U ||
        (options_.enable_audio &&
         (options_.audio_sample_rate == 0U || options_.audio_channels == 0U ||
          options_.audio_channels > 8U))) {
        throw std::invalid_argument("invalid mock QEMU video or audio parameters");
    }

    int sockets[2] {-1, -1};
    if (::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) < 0) {
        throw std::system_error(errno,
                                std::generic_category(),
                                "socketpair(mock QEMU main bus)");
    }
    main_server_fd_.reset(sockets[0]);
    main_client_fd_.reset(sockets[1]);
}

MockQemuDbusServer::~MockQemuDbusServer() {
    stop();
}

UniqueFd MockQemuDbusServer::take_client_fd() {
    if (!main_client_fd_) {
        throw std::logic_error("mock QEMU client fd was already taken");
    }
    return UniqueFd(main_client_fd_.release());
}

void MockQemuDbusServer::start() {
    if (running_.exchange(true)) {
        throw std::logic_error("mock QEMU D-Bus server is already running");
    }
    if (!main_server_fd_) {
        running_ = false;
        throw std::logic_error("mock QEMU server transport is unavailable");
    }
    main_thread_ = std::thread(&MockQemuDbusServer::main_loop, this);
}

void MockQemuDbusServer::stop() noexcept {
    if (!running_.exchange(false) && !main_thread_.joinable() &&
        !listener_thread_.joinable() && !audio_thread_.joinable()) {
        return;
    }

    if (main_server_fd_) {
        (void) ::shutdown(main_server_fd_.get(), SHUT_RDWR);
    }
    if (main_thread_.joinable()) {
        main_thread_.join();
    }
    if (listener_thread_.joinable()) {
        listener_thread_.join();
    }
    if (audio_thread_.joinable()) {
        audio_thread_.join();
    }
    main_filter_slot_.reset();
    main_bus_.close();
    listener_connected_ = false;
    audio_listener_connected_ = false;
}

int MockQemuDbusServer::main_filter(sd_bus_message *message,
                                    void *userdata,
                                    sd_bus_error *) noexcept {
    auto *self = static_cast<MockQemuDbusServer *>(userdata);
    try {
        return self->handle_main_message(message);
    } catch (const std::exception& ex) {
        self->record_error(std::string("mock QEMU main handler: ") + ex.what());
        (void) sd_bus_reply_method_errorf(message,
                                          "org.qemu.Display1.Error.Failed",
                                          "%s",
                                          ex.what());
        return 1;
    } catch (...) {
        self->record_error("mock QEMU main handler: unknown exception");
        (void) sd_bus_reply_method_errorf(message,
                                          "org.qemu.Display1.Error.Failed",
                                          "%s",
                                          "unknown exception");
        return 1;
    }
}

int MockQemuDbusServer::handle_main_message(sd_bus_message *message) {
    if (sd_bus_message_is_method_call(message,
                                      peer_interface.data(),
                                      "Ping") > 0) {
        dbus::check(sd_bus_reply_method_return(message, ""), "reply Peer.Ping");
        return 1;
    }

    const char *path = sd_bus_message_get_path(message);
    if (path == nullptr) {
        return 0;
    }

    if (std::string_view(path) == audio_path && options_.enable_audio &&
        sd_bus_message_is_method_call(message,
                                      audio_interface.data(),
                                      "RegisterOutListener") > 0) {
        int received_fd = -1;
        dbus::check(sd_bus_message_read(message, "h", &received_fd),
                    "read Audio.RegisterOutListener");
        UniqueFd listener_fd = duplicate_cloexec(received_fd);
        {
            std::lock_guard lock(audio_mutex_);
            if (audio_thread_.joinable()) {
                return sd_bus_reply_method_errorf(
                    message,
                    "org.qemu.Display1.Error.LimitReached",
                    "%s",
                    "the mock supports one audio listener");
            }
            audio_thread_ = std::thread(&MockQemuDbusServer::audio_loop,
                                        this,
                                        std::move(listener_fd));
        }
        ++audio_listeners_registered_;
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply Audio.RegisterOutListener");
        return 1;
    }

    if (std::string_view(path) != console_path) {
        return 0;
    }

    if (sd_bus_message_is_method_call(message,
                                      console_interface.data(),
                                      "RegisterListener") > 0) {
        int received_fd = -1;
        dbus::check(sd_bus_message_read(message, "h", &received_fd),
                    "read Console.RegisterListener");
        UniqueFd listener_fd = duplicate_cloexec(received_fd);
        {
            std::lock_guard lock(listener_mutex_);
            if (listener_thread_.joinable()) {
                return sd_bus_reply_method_errorf(
                    message,
                    "org.qemu.Display1.Error.LimitReached",
                    "%s",
                    "the mock supports one display listener");
            }
            listener_thread_ = std::thread(&MockQemuDbusServer::listener_loop,
                                           this,
                                           std::move(listener_fd));
        }
        ++listeners_registered_;
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply Console.RegisterListener");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      console_interface.data(),
                                      "SetUIInfo") > 0) {
        std::uint16_t width_mm = 0U;
        std::uint16_t height_mm = 0U;
        std::int32_t xoff = 0;
        std::int32_t yoff = 0;
        std::uint32_t width = 0U;
        std::uint32_t height = 0U;
        dbus::check(sd_bus_message_read(message,
                                        "qqiiuu",
                                        &width_mm,
                                        &height_mm,
                                        &xoff,
                                        &yoff,
                                        &width,
                                        &height),
                    "read Console.SetUIInfo");
        (void) xoff;
        (void) yoff;
        if (width == 0U || height == 0U || width > 4096U || height > 4096U) {
            return sd_bus_reply_method_errorf(message,
                                              "org.qemu.Display1.Error.InvalidMode",
                                              "%s",
                                              "invalid mock display mode");
        }
        requested_width_ = width;
        requested_height_ = height;
        requested_width_mm_ = width_mm;
        requested_height_mm_ = height_mm;
        ++resize_requests_;
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply Console.SetUIInfo");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      keyboard_interface.data(),
                                      "Press") > 0 ||
        sd_bus_message_is_method_call(message,
                                      keyboard_interface.data(),
                                      "Release") > 0) {
        std::uint32_t keycode = 0U;
        dbus::check(sd_bus_message_read(message, "u", &keycode),
                    "read keyboard event");
        (void) keycode;
        ++keyboard_events_;
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply keyboard event");
        return 1;
    }

    const bool button_event =
        sd_bus_message_is_method_call(message, mouse_interface.data(), "Press") > 0 ||
        sd_bus_message_is_method_call(message, mouse_interface.data(), "Release") > 0;
    if (button_event) {
        std::uint32_t button = 0U;
        dbus::check(sd_bus_message_read(message, "u", &button),
                    "read mouse button");
        (void) button;
        ++mouse_events_;
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply mouse button");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      mouse_interface.data(),
                                      "SetAbsPosition") > 0) {
        std::uint32_t x = 0U;
        std::uint32_t y = 0U;
        dbus::check(sd_bus_message_read(message, "uu", &x, &y),
                    "read absolute mouse position");
        (void) x;
        (void) y;
        ++mouse_events_;
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply absolute mouse position");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      mouse_interface.data(),
                                      "RelMotion") > 0) {
        std::int32_t dx = 0;
        std::int32_t dy = 0;
        dbus::check(sd_bus_message_read(message, "ii", &dx, &dy),
                    "read relative mouse motion");
        (void) dx;
        (void) dy;
        ++mouse_events_;
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply relative mouse motion");
        return 1;
    }

    return 0;
}

void MockQemuDbusServer::main_loop() noexcept {
    try {
        main_bus_ = dbus::Bus::p2p_server_fd(std::move(main_server_fd_));
        dbus::check(sd_bus_set_method_call_timeout(main_bus_.get(),
                                                   method_timeout_usec),
                    "set mock main bus timeout");
        dbus::check(sd_bus_add_filter(main_bus_.get(),
                                      main_filter_slot_.put(),
                                      &MockQemuDbusServer::main_filter,
                                      this),
                    "add mock QEMU main filter");
        main_bus_.start();
        while (running_.load()) {
            if (!main_bus_.pump_once(20ms)) {
                break;
            }
        }
    } catch (const std::exception& ex) {
        if (running_.load()) {
            record_error(std::string("mock QEMU main loop: ") + ex.what());
        }
    } catch (...) {
        if (running_.load()) {
            record_error("mock QEMU main loop: unknown exception");
        }
    }
}

void MockQemuDbusServer::listener_loop(UniqueFd listener_fd) noexcept {
    try {
        dbus::Bus listener_bus = dbus::Bus::p2p_server_fd(std::move(listener_fd));
        dbus::check(sd_bus_set_method_call_timeout(listener_bus.get(),
                                                   method_timeout_usec),
                    "set mock listener timeout");
        listener_bus.start();
        listener_connected_ = true;

        send_listener_capability_probe(listener_bus);
        send_cursor(listener_bus);

        std::uint32_t width = requested_width_.load();
        std::uint32_t height = requested_height_.load();
        auto framebuffer = make_framebuffer(width, height);
        draw_frame(framebuffer, 0U);
        send_scanout_map(listener_bus, framebuffer);
        send_mouse_position(listener_bus, width, height);

        const auto period = std::chrono::nanoseconds(
            1'000'000'000LL / static_cast<long long>(options_.fps));
        auto next = std::chrono::steady_clock::now();
        std::uint64_t sequence = 0U;

        while (running_.load()) {
            next += period;
            const std::uint32_t next_width = requested_width_.load();
            const std::uint32_t next_height = requested_height_.load();
            if (next_width != width || next_height != height) {
                width = next_width;
                height = next_height;
                framebuffer = make_framebuffer(width, height);
                draw_frame(framebuffer, ++sequence);
                send_scanout_map(listener_bus, framebuffer);
                send_mouse_position(listener_bus, width, height);
            } else {
                draw_frame(framebuffer, ++sequence);
                send_update_map(listener_bus, width, height);
            }
            std::this_thread::sleep_until(next);
        }
    } catch (const std::exception& ex) {
        if (running_.load() && !is_expected_disconnect(ex.what())) {
            record_error(std::string("mock QEMU listener loop: ") + ex.what());
        }
    } catch (...) {
        if (running_.load()) {
            record_error("mock QEMU listener loop: unknown exception");
        }
    }
    listener_connected_ = false;
}


void MockQemuDbusServer::audio_loop(UniqueFd listener_fd) noexcept {
    try {
        dbus::Bus audio_bus = dbus::Bus::p2p_server_fd(std::move(listener_fd));
        dbus::check(sd_bus_set_method_call_timeout(audio_bus.get(),
                                                   method_timeout_usec),
                    "set mock audio timeout");
        audio_bus.start();
        audio_listener_connected_ = true;

        send_audio_capability_probe(audio_bus);
        send_audio_init(audio_bus);

        constexpr auto period = 10ms;
        auto next = std::chrono::steady_clock::now();
        std::uint64_t sequence = 0U;
        while (running_.load()) {
            next += period;
            send_audio_chunk(audio_bus, sequence++);
            std::this_thread::sleep_until(next);
        }
    } catch (const std::exception& ex) {
        if (running_.load() && !is_expected_disconnect(ex.what())) {
            record_error(std::string("mock QEMU audio loop: ") + ex.what());
        }
    } catch (...) {
        if (running_.load()) {
            record_error("mock QEMU audio loop: unknown exception");
        }
    }
    audio_listener_connected_ = false;
}

void MockQemuDbusServer::send_audio_capability_probe(dbus::Bus& bus) {
    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call_method(bus.get(),
                                          nullptr,
                                          audio_listener_path.data(),
                                          properties_interface.data(),
                                          "Get",
                                          error.get(),
                                          reply.put(),
                                          "ss",
                                          audio_listener_interface.data(),
                                          "Interfaces");
    dbus::check(result,
                "audio listener Properties.Get(Interfaces)",
                error.get());
}

void MockQemuDbusServer::send_audio_init(dbus::Bus& bus) {
    constexpr std::uint64_t stream_id = 1U;
    constexpr std::uint8_t bits = 32U;
    constexpr int is_signed = 1;
    constexpr int is_float = 1;
    constexpr int little_endian = 0;
    const auto channels = static_cast<std::uint8_t>(options_.audio_channels);
    const std::uint32_t bytes_per_frame =
        static_cast<std::uint32_t>(channels) * sizeof(float);
    const std::uint32_t bytes_per_second =
        options_.audio_sample_rate * bytes_per_frame;

    dbus::Error init_error;
    dbus::Message init_reply;
    const int init_result = sd_bus_call_method(bus.get(),
                                               nullptr,
                                               audio_listener_path.data(),
                                               audio_listener_interface.data(),
                                               "Init",
                                               init_error.get(),
                                               init_reply.put(),
                                               "tybbuyuub",
                                               stream_id,
                                               bits,
                                               is_signed,
                                               is_float,
                                               options_.audio_sample_rate,
                                               channels,
                                               bytes_per_frame,
                                               bytes_per_second,
                                               little_endian);
    dbus::check(init_result, "AudioOutListener.Init", init_error.get());
    ++audio_inits_sent_;

    dbus::Error enable_error;
    dbus::Message enable_reply;
    const int enable_result = sd_bus_call_method(bus.get(),
                                                 nullptr,
                                                 audio_listener_path.data(),
                                                 audio_listener_interface.data(),
                                                 "SetEnabled",
                                                 enable_error.get(),
                                                 enable_reply.put(),
                                                 "tb",
                                                 stream_id,
                                                 1);
    dbus::check(enable_result,
                "AudioOutListener.SetEnabled",
                enable_error.get());

    std::vector<std::uint8_t> volume(channels, 255U);
    dbus::Message volume_call;
    dbus::check(sd_bus_message_new_method_call(bus.get(),
                                               volume_call.put(),
                                               nullptr,
                                               audio_listener_path.data(),
                                               audio_listener_interface.data(),
                                               "SetVolume"),
                "new AudioOutListener.SetVolume call");
    dbus::check(sd_bus_message_append(volume_call.get(),
                                      "tb",
                                      stream_id,
                                      0),
                "append AudioOutListener.SetVolume header");
    dbus::check(sd_bus_message_append_array(volume_call.get(),
                                            'y',
                                            volume.data(),
                                            volume.size()),
                "append AudioOutListener.SetVolume values");
    dbus::Error volume_error;
    dbus::Message volume_reply;
    const int volume_result = sd_bus_call(bus.get(),
                                          volume_call.get(),
                                          method_timeout_usec,
                                          volume_error.get(),
                                          volume_reply.put());
    dbus::check(volume_result,
                "AudioOutListener.SetVolume",
                volume_error.get());
}

void MockQemuDbusServer::send_audio_chunk(dbus::Bus& bus,
                                           std::uint64_t sequence) {
    constexpr std::uint64_t stream_id = 1U;
    constexpr double frequency = 440.0;
    const std::uint32_t frames =
        std::max<std::uint32_t>(1U, options_.audio_sample_rate / 100U);
    const std::size_t sample_count =
        static_cast<std::size_t>(frames) * options_.audio_channels;
    std::vector<std::uint8_t> bytes(sample_count * sizeof(float));
    for (std::uint32_t frame = 0U; frame < frames; ++frame) {
        const std::uint64_t absolute_frame =
            sequence * frames + static_cast<std::uint64_t>(frame);
        const double phase = 2.0 * std::numbers::pi * frequency *
            static_cast<double>(absolute_frame) /
            static_cast<double>(options_.audio_sample_rate);
        const float sample = static_cast<float>(0.15 * std::sin(phase));
        const auto bits = std::bit_cast<std::uint32_t>(sample);
        for (std::uint16_t channel = 0U;
             channel < options_.audio_channels;
             ++channel) {
            const std::size_t sample_index =
                static_cast<std::size_t>(frame) * options_.audio_channels + channel;
            const std::size_t byte_index = sample_index * sizeof(float);
            bytes[byte_index + 0U] = static_cast<std::uint8_t>(bits & 0xFFU);
            bytes[byte_index + 1U] = static_cast<std::uint8_t>((bits >> 8U) & 0xFFU);
            bytes[byte_index + 2U] = static_cast<std::uint8_t>((bits >> 16U) & 0xFFU);
            bytes[byte_index + 3U] = static_cast<std::uint8_t>((bits >> 24U) & 0xFFU);
        }
    }

    dbus::Message call;
    dbus::check(sd_bus_message_new_method_call(bus.get(),
                                               call.put(),
                                               nullptr,
                                               audio_listener_path.data(),
                                               audio_listener_interface.data(),
                                               "Write"),
                "new AudioOutListener.Write call");
    dbus::check(sd_bus_message_append(call.get(), "t", stream_id),
                "append AudioOutListener.Write stream id");
    dbus::check(sd_bus_message_append_array(call.get(),
                                            'y',
                                            bytes.data(),
                                            bytes.size()),
                "append AudioOutListener.Write data");
    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call(bus.get(),
                                   call.get(),
                                   method_timeout_usec,
                                   error.get(),
                                   reply.put());
    dbus::check(result, "AudioOutListener.Write", error.get());
    ++audio_writes_sent_;
    audio_frames_sent_ += frames;
}

void MockQemuDbusServer::send_listener_capability_probe(dbus::Bus& bus) {
    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call_method(bus.get(),
                                          nullptr,
                                          listener_path.data(),
                                          properties_interface.data(),
                                          "Get",
                                          error.get(),
                                          reply.put(),
                                          "ss",
                                          listener_interface.data(),
                                          "Interfaces");
    dbus::check(result, "listener Properties.Get(Interfaces)", error.get());
}

void MockQemuDbusServer::send_cursor(dbus::Bus& bus) {
    std::array<std::uint8_t, 16U * 16U * 4U> cursor {};
    for (std::uint32_t y = 0U; y < 16U; ++y) {
        for (std::uint32_t x = 0U; x < 16U; ++x) {
            const bool ink = x <= y / 2U || (y > 9U && x < 8U && x + y < 20U);
            const std::size_t offset = (static_cast<std::size_t>(y) * 16U + x) * 4U;
            cursor[offset + 0U] = ink ? 255U : 0U;  // B
            cursor[offset + 1U] = ink ? 255U : 0U;  // G
            cursor[offset + 2U] = ink ? 255U : 0U;  // R
            cursor[offset + 3U] = ink ? 255U : 0U;  // A
        }
    }
    send_cursor_call(bus, cursor);
}

void MockQemuDbusServer::send_mouse_position(dbus::Bus& bus,
                                              std::uint32_t width,
                                              std::uint32_t height) {
    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call_method(bus.get(),
                                          nullptr,
                                          listener_path.data(),
                                          listener_interface.data(),
                                          "MouseSet",
                                          error.get(),
                                          reply.put(),
                                          "iii",
                                          static_cast<std::int32_t>(width / 2U),
                                          static_cast<std::int32_t>(height / 2U),
                                          1);
    dbus::check(result, "Listener.MouseSet", error.get());
}

void MockQemuDbusServer::send_scanout_map(dbus::Bus& bus,
                                          SharedFramebuffer& framebuffer) {
    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call_method(bus.get(),
                                          nullptr,
                                          listener_path.data(),
                                          map_interface.data(),
                                          "ScanoutMap",
                                          error.get(),
                                          reply.put(),
                                          "huuuuu",
                                          framebuffer.fd.get(),
                                          0U,
                                          framebuffer.width,
                                          framebuffer.height,
                                          framebuffer.stride,
                                          pixman_x8r8g8b8);
    dbus::check(result, "Listener.Unix.Map.ScanoutMap", error.get());
    ++scanouts_sent_;
}

void MockQemuDbusServer::send_update_map(dbus::Bus& bus,
                                         std::uint32_t width,
                                         std::uint32_t height) {
    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call_method(bus.get(),
                                          nullptr,
                                          listener_path.data(),
                                          map_interface.data(),
                                          "UpdateMap",
                                          error.get(),
                                          reply.put(),
                                          "iiii",
                                          0,
                                          0,
                                          static_cast<std::int32_t>(width),
                                          static_cast<std::int32_t>(height));
    dbus::check(result, "Listener.Unix.Map.UpdateMap", error.get());
    ++updates_sent_;
}

MockQemuDbusServer::SharedFramebuffer
MockQemuDbusServer::make_framebuffer(std::uint32_t width,
                                     std::uint32_t height) {
    SharedFramebuffer framebuffer;
    framebuffer.width = width;
    framebuffer.height = height;
    framebuffer.stride = width * 4U;
    framebuffer.size = checked_frame_bytes(width, height);

    const int fd = ::memfd_create("qmdp-mock-scanout", MFD_CLOEXEC);
    if (fd < 0) {
        throw std::system_error(errno, std::generic_category(), "memfd_create");
    }
    framebuffer.fd.reset(fd);
    if (::ftruncate(framebuffer.fd.get(), static_cast<off_t>(framebuffer.size)) < 0) {
        throw std::system_error(errno, std::generic_category(), "ftruncate(memfd)");
    }
    framebuffer.mapping = ::mmap(nullptr,
                                 framebuffer.size,
                                 PROT_READ | PROT_WRITE,
                                 MAP_SHARED,
                                 framebuffer.fd.get(),
                                 0);
    if (framebuffer.mapping == MAP_FAILED) {
        framebuffer.mapping = nullptr;
        throw std::system_error(errno, std::generic_category(), "mmap(memfd)");
    }
    return framebuffer;
}

void MockQemuDbusServer::draw_frame(SharedFramebuffer& framebuffer,
                                    std::uint64_t sequence) {
    auto *pixels = framebuffer.bytes();
    const std::uint32_t bar = static_cast<std::uint32_t>(
        sequence % std::max<std::uint64_t>(1U, framebuffer.width));
    for (std::uint32_t y = 0U; y < framebuffer.height; ++y) {
        for (std::uint32_t x = 0U; x < framebuffer.width; ++x) {
            const std::size_t offset = static_cast<std::size_t>(y) * framebuffer.stride +
                                       static_cast<std::size_t>(x) * 4U;
            const bool moving = x >= bar && x < std::min(framebuffer.width, bar + 12U);
            pixels[offset + 0U] = moving ? 32U : static_cast<std::uint8_t>((x + sequence) & 0xFFU);
            pixels[offset + 1U] = moving ? 220U : static_cast<std::uint8_t>((y * 2U) & 0xFFU);
            pixels[offset + 2U] = moving ? 255U : static_cast<std::uint8_t>((x + y) & 0xFFU);
            pixels[offset + 3U] = 0U;
        }
    }
}

void MockQemuDbusServer::record_error(std::string message) noexcept {
    try {
        std::lock_guard lock(error_mutex_);
        last_error_ = std::move(message);
    } catch (...) {
    }
}

MockQemuDbusServer::Stats MockQemuDbusServer::stats() const {
    std::string error;
    {
        std::lock_guard lock(error_mutex_);
        error = last_error_;
    }
    return {
        .listeners_registered = listeners_registered_.load(),
        .scanouts_sent = scanouts_sent_.load(),
        .updates_sent = updates_sent_.load(),
        .resize_requests = resize_requests_.load(),
        .keyboard_events = keyboard_events_.load(),
        .mouse_events = mouse_events_.load(),
        .audio_listeners_registered = audio_listeners_registered_.load(),
        .audio_inits_sent = audio_inits_sent_.load(),
        .audio_writes_sent = audio_writes_sent_.load(),
        .audio_frames_sent = audio_frames_sent_.load(),
        .width = requested_width_.load(),
        .height = requested_height_.load(),
        .width_mm = requested_width_mm_.load(),
        .height_mm = requested_height_mm_.load(),
        .listener_connected = listener_connected_.load(),
        .audio_listener_connected = audio_listener_connected_.load(),
        .last_error = std::move(error),
    };
}

}  // namespace qmdp::test
