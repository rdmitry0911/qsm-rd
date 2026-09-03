#include "qemu/qemu_dbus_display.hpp"

#ifdef QMDP_HAS_GBM
#include "capture/dmabuf_readback.hpp"
#endif

#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstddef>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <exception>
#include <fcntl.h>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <vector>
#include <sys/socket.h>
#include <unistd.h>

namespace qmdp {
namespace {

using namespace std::chrono_literals;

constexpr std::string_view listener_path = "/org/qemu/Display1/Listener";
constexpr std::string_view listener_interface = "org.qemu.Display1.Listener";
constexpr std::string_view map_interface = "org.qemu.Display1.Listener.Unix.Map";
constexpr std::string_view audio_path = "/org/qemu/Display1/Audio";
constexpr std::string_view audio_interface = "org.qemu.Display1.Audio";
constexpr std::string_view audio_listener_path = "/org/qemu/Display1/AudioOutListener";
constexpr std::string_view audio_listener_interface = "org.qemu.Display1.AudioOutListener";
constexpr std::string_view properties_interface = "org.freedesktop.DBus.Properties";
constexpr std::string_view introspect_interface = "org.freedesktop.DBus.Introspectable";
constexpr std::string_view peer_interface = "org.freedesktop.DBus.Peer";

constexpr const char listener_introspection_xml[] = R"xml(
<node>
  <interface name="org.qemu.Display1.Listener">
    <method name="Scanout"><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="ay" direction="in"/></method>
    <method name="Update"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="ay" direction="in"/></method>
    <method name="ScanoutDMABUF"><arg type="h" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="t" direction="in"/><arg type="b" direction="in"/></method>
    <method name="UpdateDMABUF"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Disable"/>
    <method name="MouseSet"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="CursorDefine"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="ay" direction="in"/></method>
    <property name="Interfaces" type="as" access="read"/>
  </interface>
  <interface name="org.qemu.Display1.Listener.Unix.Map">
    <method name="ScanoutMap"><arg type="h" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/></method>
    <method name="UpdateMap"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
    <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/><method name="GetMachineId"><arg type="s" direction="out"/></method>
  </interface>
</node>)xml";

constexpr const char audio_introspection_xml[] = R"xml(
<node>
  <interface name="org.qemu.Display1.AudioOutListener">
    <method name="Init"><arg type="t" direction="in"/><arg type="y" direction="in"/><arg type="b" direction="in"/><arg type="b" direction="in"/><arg type="u" direction="in"/><arg type="y" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="b" direction="in"/></method>
    <method name="Fini"><arg type="t" direction="in"/></method>
    <method name="SetEnabled"><arg type="t" direction="in"/><arg type="b" direction="in"/></method>
    <method name="SetVolume"><arg type="t" direction="in"/><arg type="b" direction="in"/><arg type="ay" direction="in"/></method>
    <method name="Write"><arg type="t" direction="in"/><arg type="ay" direction="in"/></method>
    <property name="Interfaces" type="as" access="read"/>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
    <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/><method name="GetMachineId"><arg type="s" direction="out"/></method>
  </interface>
</node>)xml";

UniqueFd duplicate_cloexec(int fd) {
    if (fd < 0) {
        throw std::invalid_argument("received invalid Unix file descriptor");
    }
    const int copy = ::fcntl(fd, F_DUPFD_CLOEXEC, 0);
    if (copy < 0) {
        throw std::system_error(errno, std::generic_category(), "duplicate D-Bus fd");
    }
    return UniqueFd(copy);
}

// A filter slot owns a reference to the sd-bus object.  `sd_bus_close()` moves
// the bus to its closed state, but a live slot can delay final destruction of
// the externally supplied transport.  QEMU keeps rendering into a registered
// Display1 listener until it observes the socket close, so explicitly shut
// down our duplicate first while the handler is still installed.  This turns
// an in-flight frame into a normal disconnect instead of an UnknownMethod
// reply from a listener whose filter has already gone away.
void shutdown_listener_transport(UniqueFd& transport) noexcept {
    if (!transport) {
        return;
    }
    while (::shutdown(transport.get(), SHUT_RDWR) < 0 && errno == EINTR) {
    }
    transport.reset();
}

int append_interfaces_variant(
    sd_bus_message *reply,
    std::span<const std::string_view> interfaces) {
    dbus::check(sd_bus_message_open_container(reply, 'v', "as"),
                "open Interfaces variant");
    dbus::check(sd_bus_message_open_container(reply, 'a', "s"),
                "open Interfaces array");
    for (const auto interface_name : interfaces) {
        const std::string stable_name(interface_name);
        dbus::check(sd_bus_message_append(reply, "s", stable_name.c_str()),
                    "append advertised interface");
    }
    dbus::check(sd_bus_message_close_container(reply), "close Interfaces array");
    dbus::check(sd_bus_message_close_container(reply), "close Interfaces variant");
    return 0;
}

std::uint64_t read_unsigned_sample(std::span<const std::uint8_t> bytes,
                                   bool big_endian) {
    std::uint64_t value = 0U;
    if (big_endian) {
        for (const auto byte : bytes) {
            value = (value << 8U) | byte;
        }
    } else {
        for (std::size_t index = 0U; index < bytes.size(); ++index) {
            value |= static_cast<std::uint64_t>(bytes[index]) << (index * 8U);
        }
    }
    return value;
}

float integer_pcm_to_float(std::span<const std::uint8_t> bytes,
                           std::uint8_t bits,
                           bool is_signed,
                           bool big_endian) {
    const auto raw = read_unsigned_sample(bytes, big_endian);
    if (bits == 0U || bits > 32U) {
        throw std::invalid_argument("unsupported integer PCM bit depth");
    }
    const std::uint64_t mask = (std::uint64_t {1U} << bits) - 1U;
    const std::uint64_t value = raw & mask;
    const double scale = static_cast<double>(std::uint64_t {1U} << (bits - 1U));
    if (is_signed) {
        const std::uint64_t sign_bit = std::uint64_t {1U} << (bits - 1U);
        const std::int64_t signed_value = (value & sign_bit) != 0U
            ? static_cast<std::int64_t>(value | ~mask)
            : static_cast<std::int64_t>(value);
        return static_cast<float>(std::clamp(
            static_cast<double>(signed_value) / scale, -1.0, 1.0));
    }
    return static_cast<float>(std::clamp(
        (static_cast<double>(value) - scale) / scale, -1.0, 1.0));
}

float floating_pcm_to_float(std::span<const std::uint8_t> bytes,
                            std::uint8_t bits,
                            bool big_endian) {
    const auto raw = read_unsigned_sample(bytes, big_endian);
    if (bits == 32U && bytes.size() == sizeof(std::uint32_t)) {
        const auto value = std::bit_cast<float>(static_cast<std::uint32_t>(raw));
        return std::isfinite(value) ? std::clamp(value, -1.0F, 1.0F) : 0.0F;
    }
    if (bits == 64U && bytes.size() == sizeof(std::uint64_t)) {
        const auto value = std::bit_cast<double>(raw);
        return std::isfinite(value)
                   ? static_cast<float>(std::clamp(value, -1.0, 1.0))
                   : 0.0F;
    }
    throw std::invalid_argument("unsupported floating-point PCM format");
}

int send_message_reply(sd_bus_message *call, dbus::Message& reply) {
    auto *bus = sd_bus_message_get_bus(call);
    if (bus == nullptr) {
        throw std::runtime_error("D-Bus call has no associated bus");
    }
    dbus::check(sd_bus_send(bus, reply.get(), nullptr), "sd_bus_send reply");
    return 1;
}

std::string machine_id() {
    constexpr std::array<const char *, 2> paths {"/etc/machine-id", "/var/lib/dbus/machine-id"};
    for (const char *path : paths) {
        FILE *file = std::fopen(path, "r");
        if (file == nullptr) {
            continue;
        }
        std::array<char, 64> buffer {};
        const char *line = std::fgets(buffer.data(), static_cast<int>(buffer.size()), file);
        std::fclose(file);
        if (line != nullptr) {
            std::string result(line);
            result.erase(std::remove(result.begin(), result.end(), '\n'), result.end());
            result.erase(std::remove(result.begin(), result.end(), '\r'), result.end());
            if (!result.empty()) {
                return result;
            }
        }
    }
    return "00000000000000000000000000000000";
}

}  // namespace

QemuDbusDisplay::QemuDbusDisplay(QemuDbusOptions options)
    : options_(std::move(options)) {
    if (!options_.p2p_fd && options_.bus_address.empty()) {
        throw std::invalid_argument("QEMU D-Bus address or inherited p2p fd is required");
    }
}

QemuDbusDisplay::~QemuDbusDisplay() {
    stop();
}

std::string QemuDbusDisplay::console_path() const {
    return "/org/qemu/Display1/Console_" + std::to_string(options_.console_id);
}

const char *QemuDbusDisplay::destination() const noexcept {
    return options_.destination.empty() ? nullptr : options_.destination.c_str();
}

void QemuDbusDisplay::start(QemuDisplayCallbacks callbacks) {
    std::lock_guard lifecycle_lock(lifecycle_mutex_);
    if (started_) {
        throw std::logic_error("QEMU D-Bus display is already started");
    }
    if (!callbacks.on_frame) {
        throw std::invalid_argument("QEMU display requires an on_frame callback");
    }

    callbacks_ = std::move(callbacks);
    stopping_.store(false);
    display_disabled_.store(false);

    try {
        if (options_.p2p_fd) {
            main_bus_ = dbus::Bus::p2p_client_fd(std::move(options_.p2p_fd));
        } else {
            main_bus_ = dbus::Bus::connect_address(
                options_.bus_address,
                !options_.destination.empty(),
                std::chrono::duration_cast<std::chrono::microseconds>(options_.call_timeout));
        }
        main_bus_.start();

        int sockets[2] {-1, -1};
        if (::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) < 0) {
            throw std::system_error(errno, std::generic_category(), "socketpair for QEMU listener");
        }
        UniqueFd client_fd(sockets[0]);
        UniqueFd qemu_fd(sockets[1]);

        dbus::Error error;
        dbus::Message reply;
        const std::string path = console_path();
        const int result = sd_bus_call_method(
            main_bus_.get(),
            destination(),
            path.c_str(),
            "org.qemu.Display1.Console",
            "RegisterListener",
            error.get(),
            reply.put(),
            "h",
            qemu_fd.get());
        dbus::check(result, "QEMU RegisterListener", error.get());
        qemu_fd.reset();

        peer_transport_shutdown_fd_ = duplicate_cloexec(client_fd.get());
        peer_bus_ = dbus::Bus::p2p_client_fd(std::move(client_fd));
        dbus::check(sd_bus_add_filter(peer_bus_.get(),
                                      peer_filter_slot_.put(),
                                      &QemuDbusDisplay::peer_filter,
                                      this),
                    "sd_bus_add_filter(QEMU listener)");
        peer_bus_.start();

        if (options_.enable_audio) {
            try {
                register_audio_listener();
            } catch (...) {
                {
                    std::lock_guard lock(stats_mutex_);
                    ++audio_registration_failures_;
                }
                shutdown_listener_transport(audio_transport_shutdown_fd_);
                audio_bus_.close();
                audio_filter_slot_.reset();
                {
                    std::lock_guard lock(stats_mutex_);
                    audio_listener_active_ = false;
                }
                if (options_.require_audio) {
                    throw;
                }
            }
        }

        started_ = true;
        peer_thread_ = std::thread(&QemuDbusDisplay::peer_loop, this);
        if (audio_bus_) {
            audio_thread_ = std::thread(&QemuDbusDisplay::audio_loop, this);
        }
    } catch (...) {
        shutdown_listener_transport(audio_transport_shutdown_fd_);
        audio_bus_.close();
        audio_filter_slot_.reset();
        shutdown_listener_transport(peer_transport_shutdown_fd_);
        peer_bus_.close();
        peer_filter_slot_.reset();
        main_bus_.close();
        callbacks_ = {};
        started_ = false;
        throw;
    }
}

void QemuDbusDisplay::stop() noexcept {
    std::unique_lock lifecycle_lock(lifecycle_mutex_);
    if (!started_) {
        return;
    }
    stopping_.store(true);
    lifecycle_lock.unlock();

    if (peer_thread_.joinable()) {
        peer_thread_.join();
    }
    if (audio_thread_.joinable()) {
        audio_thread_.join();
    }

    lifecycle_lock.lock();
    // The QEMU Display1 peer can keep scheduling asynchronous frame updates
    // until it observes its end of the socket has gone away.  Keep the filter
    // installed until the client endpoint is explicitly shut down: removing
    // it first leaves a live D-Bus connection with no object at listener_path,
    // and turns that small teardown window into a burst of UnknownMethod
    // replies from QEMU.  A slot retains an sd-bus reference, hence the
    // explicit shutdown duplicate rather than relying on final bus unref.
    shutdown_listener_transport(peer_transport_shutdown_fd_);
    peer_bus_.close();
    peer_filter_slot_.reset();

#ifdef QMDP_HAS_GBM
    dmabuf_readback_.reset();
#endif
    shutdown_listener_transport(audio_transport_shutdown_fd_);
    audio_bus_.close();
    audio_filter_slot_.reset();
    {
        std::lock_guard audio_lock(audio_state_mutex_);
        audio_streams_.clear();
    }
    {
        std::lock_guard stats_lock(stats_mutex_);
        audio_listener_active_ = false;
    }
    {
        std::lock_guard main_lock(main_bus_mutex_);
        main_bus_.close();
    }
    callbacks_ = {};
    started_ = false;
    lifecycle_lock.unlock();
}

void QemuDbusDisplay::ensure_started() const {
    if (!started_ || !main_bus_) {
        throw std::logic_error("QEMU D-Bus display is not started");
    }
}

void QemuDbusDisplay::set_ui_info(const ViewportRequest& request) {
    main_bus_call([&] {
        const std::uint16_t width_mm = 0U;
        const std::uint16_t height_mm = 0U;
        const std::int32_t xoff = 0;
        const std::int32_t yoff = 0;
        dbus::Error error;
        dbus::Message reply;
        const std::string path = console_path();
        const int result = sd_bus_call_method(
            main_bus_.get(),
            destination(),
            path.c_str(),
            "org.qemu.Display1.Console",
            "SetUIInfo",
            error.get(),
            reply.put(),
            "qqiiuu",
            static_cast<unsigned int>(width_mm),
            static_cast<unsigned int>(height_mm),
            xoff,
            yoff,
            request.width,
            request.height);
        dbus::check(result, "QEMU SetUIInfo", error.get());
    });
}

void QemuDbusDisplay::key(std::uint32_t qemu_key_number, bool pressed) {
    main_bus_send([&] {
        dbus::Message call;
        const std::string path = console_path();
        const char *member = pressed ? "Press" : "Release";
        dbus::check(sd_bus_message_new_method_call(main_bus_.get(), call.put(),
                                                   destination(), path.c_str(),
                                                   "org.qemu.Display1.Keyboard", member),
                    std::string("new QEMU Keyboard.") + member);
        dbus::check(sd_bus_message_append(call.get(), "u", qemu_key_number),
                    std::string("append QEMU Keyboard.") + member);
        dbus::check(sd_bus_message_set_expect_reply(call.get(), 0),
                    std::string("mark QEMU Keyboard.") + member + " no-reply");
        dbus::check(sd_bus_send(main_bus_.get(), call.get(), nullptr),
                    std::string("send QEMU Keyboard.") + member);
    });
}

void QemuDbusDisplay::button(std::uint8_t qemu_button, bool pressed) {
    main_bus_send([&] {
        dbus::Message call;
        const std::string path = console_path();
        const char *member = pressed ? "Press" : "Release";
        dbus::check(sd_bus_message_new_method_call(main_bus_.get(), call.put(),
                                                   destination(), path.c_str(),
                                                   "org.qemu.Display1.Mouse", member),
                    std::string("new QEMU Mouse.") + member);
        dbus::check(sd_bus_message_append(call.get(), "u",
                                          static_cast<std::uint32_t>(qemu_button)),
                    std::string("append QEMU Mouse.") + member);
        dbus::check(sd_bus_message_set_expect_reply(call.get(), 0),
                    std::string("mark QEMU Mouse.") + member + " no-reply");
        dbus::check(sd_bus_send(main_bus_.get(), call.get(), nullptr),
                    std::string("send QEMU Mouse.") + member);
    });
}

bool QemuDbusDisplay::is_absolute_pointer() {
    bool absolute = false;
    main_bus_call([&] {
        dbus::Error error;
        dbus::Message reply;
        const std::string path = console_path();
        const int result = sd_bus_call_method(
            main_bus_.get(),
            destination(),
            path.c_str(),
            "org.freedesktop.DBus.Properties",
            "Get",
            error.get(),
            reply.put(),
            "ss",
            "org.qemu.Display1.Mouse",
            "IsAbsolute");
        dbus::check(result, "QEMU Mouse.IsAbsolute", error.get());

        int value = 0;
        dbus::check(sd_bus_message_enter_container(reply.get(), 'v', "b"),
                    "read QEMU Mouse.IsAbsolute variant");
        dbus::check(sd_bus_message_read_basic(reply.get(), 'b', &value),
                    "read QEMU Mouse.IsAbsolute value");
        dbus::check(sd_bus_message_exit_container(reply.get()),
                    "finish QEMU Mouse.IsAbsolute variant");
        absolute = value != 0;
    });
    return absolute;
}

void QemuDbusDisplay::absolute_pointer(std::uint32_t x, std::uint32_t y) {
    main_bus_send([&] {
        dbus::Message call;
        const std::string path = console_path();
        dbus::check(sd_bus_message_new_method_call(main_bus_.get(), call.put(),
                                                   destination(), path.c_str(),
                                                   "org.qemu.Display1.Mouse",
                                                   "SetAbsPosition"),
                    "new QEMU Mouse.SetAbsPosition");
        dbus::check(sd_bus_message_append(call.get(), "uu", x, y),
                    "append QEMU Mouse.SetAbsPosition");
        dbus::check(sd_bus_message_set_expect_reply(call.get(), 0),
                    "mark QEMU Mouse.SetAbsPosition no-reply");
        dbus::check(sd_bus_send(main_bus_.get(), call.get(), nullptr),
                    "send QEMU Mouse.SetAbsPosition");
    });
}

void QemuDbusDisplay::relative_pointer(std::int32_t dx, std::int32_t dy) {
    main_bus_send([&] {
        dbus::Message call;
        const std::string path = console_path();
        dbus::check(sd_bus_message_new_method_call(main_bus_.get(), call.put(),
                                                   destination(), path.c_str(),
                                                   "org.qemu.Display1.Mouse", "RelMotion"),
                    "new QEMU Mouse.RelMotion");
        dbus::check(sd_bus_message_append(call.get(), "ii", dx, dy),
                    "append QEMU Mouse.RelMotion");
        dbus::check(sd_bus_message_set_expect_reply(call.get(), 0),
                    "mark QEMU Mouse.RelMotion no-reply");
        dbus::check(sd_bus_send(main_bus_.get(), call.get(), nullptr),
                    "send QEMU Mouse.RelMotion");
    });
}

int QemuDbusDisplay::peer_filter(sd_bus_message *message,
                                 void *userdata,
                                 sd_bus_error *) noexcept {
    auto *self = static_cast<QemuDbusDisplay *>(userdata);
    try {
        return self->handle_peer_message(message);
    } catch (const std::exception& ex) {
        self->report_error(ex.what());
        (void) sd_bus_reply_method_errorf(message,
                                          "org.qemu.Display1.Error.Failed",
                                          "%s",
                                          ex.what());
        return 1;
    } catch (...) {
        self->report_error("unknown exception in QEMU peer listener");
        (void) sd_bus_reply_method_errorf(message,
                                          "org.qemu.Display1.Error.Failed",
                                          "%s",
                                          "unknown listener failure");
        return 1;
    }
}

int QemuDbusDisplay::handle_properties(
    sd_bus_message *message,
    std::string_view object_interface,
    std::span<const std::string_view> extra_interfaces) {
    if (sd_bus_message_is_method_call(message,
                                      properties_interface.data(),
                                      "Get")) {
        const char *interface_name = nullptr;
        const char *property_name = nullptr;
        dbus::check(sd_bus_message_read(message,
                                        "ss",
                                        &interface_name,
                                        &property_name),
                    "read Properties.Get");
        if (interface_name == nullptr || property_name == nullptr ||
            std::string_view(interface_name) != object_interface ||
            std::string_view(property_name) != "Interfaces") {
            return sd_bus_reply_method_errorf(message,
                                              "org.freedesktop.DBus.Error.UnknownProperty",
                                              "Unknown property %s.%s",
                                              interface_name ? interface_name : "",
                                              property_name ? property_name : "");
        }
        dbus::Message reply;
        dbus::check(sd_bus_message_new_method_return(message, reply.put()),
                    "new Properties.Get reply");
        append_interfaces_variant(reply.get(), extra_interfaces);
        return send_message_reply(message, reply);
    }

    if (sd_bus_message_is_method_call(message,
                                      properties_interface.data(),
                                      "GetAll")) {
        const char *interface_name = nullptr;
        dbus::check(sd_bus_message_read(message, "s", &interface_name),
                    "read Properties.GetAll");
        dbus::Message reply;
        dbus::check(sd_bus_message_new_method_return(message, reply.put()),
                    "new Properties.GetAll reply");
        dbus::check(sd_bus_message_open_container(reply.get(), 'a', "{sv}"),
                    "open property dictionary");
        if (interface_name != nullptr &&
            std::string_view(interface_name) == object_interface) {
            dbus::check(sd_bus_message_open_container(reply.get(), 'e', "sv"),
                        "open Interfaces dictionary entry");
            dbus::check(sd_bus_message_append(reply.get(), "s", "Interfaces"),
                        "append Interfaces property name");
            append_interfaces_variant(reply.get(), extra_interfaces);
            dbus::check(sd_bus_message_close_container(reply.get()),
                        "close Interfaces dictionary entry");
        }
        dbus::check(sd_bus_message_close_container(reply.get()),
                    "close property dictionary");
        return send_message_reply(message, reply);
    }
    return 0;
}

int QemuDbusDisplay::handle_introspection(sd_bus_message *message,
                                          const char *xml) {
    if (!sd_bus_message_is_method_call(message,
                                       introspect_interface.data(),
                                       "Introspect")) {
        return 0;
    }
    dbus::check(sd_bus_reply_method_return(message, "s", xml),
                "reply Introspect");
    return 1;
}

int QemuDbusDisplay::handle_peer_standard(sd_bus_message *message) {
    if (sd_bus_message_is_method_call(message, peer_interface.data(), "Ping")) {
        dbus::check(sd_bus_reply_method_return(message, ""), "reply Peer.Ping");
        return 1;
    }
    if (sd_bus_message_is_method_call(message,
                                      peer_interface.data(),
                                      "GetMachineId")) {
        const std::string id = machine_id();
        dbus::check(sd_bus_reply_method_return(message, "s", id.c_str()),
                    "reply Peer.GetMachineId");
        return 1;
    }
    return 0;
}

int QemuDbusDisplay::handle_peer_message(sd_bus_message *message) {
    const char *path = sd_bus_message_get_path(message);
    if (path == nullptr || std::string_view(path) != listener_path) {
        return 0;
    }

    constexpr std::array<std::string_view, 1U> display_extensions {
        map_interface,
    };
    if (const int handled = handle_properties(message,
                                               listener_interface,
                                               display_extensions);
        handled != 0) {
        return handled;
    }
    if (const int handled = handle_introspection(message,
                                                  listener_introspection_xml);
        handled != 0) {
        return handled;
    }
    if (const int handled = handle_peer_standard(message); handled != 0) {
        return handled;
    }

    if (sd_bus_message_is_method_call(message,
                                      listener_interface.data(),
                                      "Scanout")) {
#ifdef QMDP_HAS_GBM
        if (dmabuf_readback_) {
            dmabuf_readback_->reset();
        }
#endif
        std::uint32_t width = 0U;
        std::uint32_t height = 0U;
        std::uint32_t stride = 0U;
        std::uint32_t format = 0U;
        dbus::check(sd_bus_message_read(message,
                                        "uuuu",
                                        &width,
                                        &height,
                                        &stride,
                                        &format),
                    "read Listener.Scanout geometry");
        const void *data = nullptr;
        std::size_t size = 0U;
        dbus::check(sd_bus_message_read_array(message, 'y', &data, &size),
                    "read Listener.Scanout data");
        publish(framebuffer_.scanout_inline(
            width,
            height,
            stride,
            format,
            {static_cast<const std::uint8_t *>(data), size}));
        {
            std::lock_guard lock(stats_mutex_);
            ++inline_scanouts_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""), "reply Scanout");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      listener_interface.data(),
                                      "Update")) {
        std::int32_t x = 0;
        std::int32_t y = 0;
        std::int32_t width = 0;
        std::int32_t height = 0;
        std::uint32_t stride = 0U;
        std::uint32_t format = 0U;
        dbus::check(sd_bus_message_read(message,
                                        "iiiiuu",
                                        &x,
                                        &y,
                                        &width,
                                        &height,
                                        &stride,
                                        &format),
                    "read Listener.Update geometry");
        const void *data = nullptr;
        std::size_t size = 0U;
        dbus::check(sd_bus_message_read_array(message, 'y', &data, &size),
                    "read Listener.Update data");
        const auto compatibility = framebuffer_.damage_compatibility(
            x, y, width, height);
        if (compatibility == CpuFramebuffer::DamageCompatibility::awaiting_scanout ||
            compatibility == CpuFramebuffer::DamageCompatibility::out_of_bounds) {
            // QEMU can emit a text-mode update after a graphics-mode Scanout,
            // before the replacement Scanout arrives.  It is not safe to copy
            // that rectangle into the old backing store, but acknowledging it
            // lets QEMU continue to the authoritative replacement Scanout.
            {
                std::lock_guard lock(stats_mutex_);
                ++stale_geometry_update_drops_;
            }
            dbus::check(sd_bus_reply_method_return(message, ""),
                        "reply stale Update");
            return 1;
        }
        publish(framebuffer_.update_inline(
            x,
            y,
            width,
            height,
            stride,
            format,
            {static_cast<const std::uint8_t *>(data), size}));
        {
            std::lock_guard lock(stats_mutex_);
            ++inline_updates_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""), "reply Update");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      map_interface.data(),
                                      "ScanoutMap")) {
#ifdef QMDP_HAS_GBM
        if (dmabuf_readback_) {
            dmabuf_readback_->reset();
        }
#endif
        int fd = -1;
        std::uint32_t offset = 0U;
        std::uint32_t width = 0U;
        std::uint32_t height = 0U;
        std::uint32_t stride = 0U;
        std::uint32_t format = 0U;
        dbus::check(sd_bus_message_read(message,
                                        "huuuuu",
                                        &fd,
                                        &offset,
                                        &width,
                                        &height,
                                        &stride,
                                        &format),
                    "read Listener.Unix.Map.ScanoutMap");
        publish(framebuffer_.scanout_map(duplicate_cloexec(fd),
                                         offset,
                                         width,
                                         height,
                                         stride,
                                         format));
        {
            std::lock_guard lock(stats_mutex_);
            ++mapped_scanouts_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""), "reply ScanoutMap");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      map_interface.data(),
                                      "UpdateMap")) {
        std::int32_t x = 0;
        std::int32_t y = 0;
        std::int32_t width = 0;
        std::int32_t height = 0;
        dbus::check(sd_bus_message_read(message,
                                        "iiii",
                                        &x,
                                        &y,
                                        &width,
                                        &height),
                    "read Listener.Unix.Map.UpdateMap");
        const auto compatibility = framebuffer_.damage_compatibility(
            x, y, width, height);
        if (compatibility == CpuFramebuffer::DamageCompatibility::awaiting_scanout ||
            compatibility == CpuFramebuffer::DamageCompatibility::out_of_bounds) {
            {
                std::lock_guard lock(stats_mutex_);
                ++stale_geometry_update_drops_;
            }
            dbus::check(sd_bus_reply_method_return(message, ""),
                        "reply stale UpdateMap");
            return 1;
        }
        publish(framebuffer_.update_map(x, y, width, height));
        {
            std::lock_guard lock(stats_mutex_);
            ++mapped_updates_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""), "reply UpdateMap");
        return 1;
    }

#ifdef QMDP_HAS_GBM
    if (sd_bus_message_is_method_call(message,
                                      listener_interface.data(),
                                      "ScanoutDMABUF")) {
        int fd = -1;
        std::uint32_t width = 0U;
        std::uint32_t height = 0U;
        std::uint32_t stride = 0U;
        std::uint32_t fourcc = 0U;
        std::uint64_t modifier = 0U;
        int y0_top = 0;
        dbus::check(sd_bus_message_read(message,
                                        "huuuutb",
                                        &fd,
                                        &width,
                                        &height,
                                        &stride,
                                        &fourcc,
                                        &modifier,
                                        &y0_top),
                    "read Listener.ScanoutDMABUF");
        const auto readback_started = std::chrono::steady_clock::now();
        try {
            if (!dmabuf_readback_) {
                dmabuf_readback_ = std::make_unique<DmaBufReadback>();
            }
            publish(dmabuf_readback_->scanout(framebuffer_,
                                               duplicate_cloexec(fd),
                                               width,
                                               height,
                                               stride,
                                               fourcc,
                                               modifier,
                                               y0_top != 0));
        } catch (...) {
            std::lock_guard lock(stats_mutex_);
            ++dmabuf_readback_failures_;
            throw;
        }
        {
            std::lock_guard lock(stats_mutex_);
            ++dmabuf_scanouts_;
            const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - readback_started).count();
            ++dmabuf_readback_samples_;
            dmabuf_readback_total_microseconds_ += static_cast<std::uint64_t>(elapsed);
            dmabuf_readback_max_microseconds_ = std::max(
                dmabuf_readback_max_microseconds_, static_cast<std::uint64_t>(elapsed));
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply ScanoutDMABUF");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      listener_interface.data(),
                                      "UpdateDMABUF")) {
        std::int32_t x = 0;
        std::int32_t y = 0;
        std::int32_t width = 0;
        std::int32_t height = 0;
        dbus::check(sd_bus_message_read(message, "iiii", &x, &y, &width, &height),
                    "read Listener.UpdateDMABUF");
        const auto compatibility = framebuffer_.damage_compatibility(x, y, width, height);
        if (compatibility == CpuFramebuffer::DamageCompatibility::awaiting_scanout ||
            compatibility == CpuFramebuffer::DamageCompatibility::out_of_bounds) {
            {
                std::lock_guard lock(stats_mutex_);
                ++stale_geometry_update_drops_;
            }
            dbus::check(sd_bus_reply_method_return(message, ""),
                        "reply stale UpdateDMABUF");
            return 1;
        }
        const auto readback_started = std::chrono::steady_clock::now();
        try {
            if (!dmabuf_readback_ || !dmabuf_readback_->active()) {
                throw std::logic_error("DMA-BUF update arrived without an active scanout");
            }
            publish(dmabuf_readback_->update(framebuffer_, x, y, width, height));
        } catch (...) {
            std::lock_guard lock(stats_mutex_);
            ++dmabuf_readback_failures_;
            throw;
        }
        {
            std::lock_guard lock(stats_mutex_);
            ++dmabuf_updates_;
            const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - readback_started).count();
            ++dmabuf_readback_samples_;
            dmabuf_readback_total_microseconds_ += static_cast<std::uint64_t>(elapsed);
            dmabuf_readback_max_microseconds_ = std::max(
                dmabuf_readback_max_microseconds_, static_cast<std::uint64_t>(elapsed));
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply UpdateDMABUF");
        return 1;
    }
#endif

    if (sd_bus_message_is_method_call(message,
                                      listener_interface.data(),
                                      "Disable")) {
#ifdef QMDP_HAS_GBM
        if (dmabuf_readback_) {
            dmabuf_readback_->reset();
        }
#endif
        framebuffer_.disable();
        display_disabled_.store(true);
        dbus::check(sd_bus_reply_method_return(message, ""), "reply Disable");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      listener_interface.data(),
                                      "MouseSet")) {
        std::int32_t x = 0;
        std::int32_t y = 0;
        std::int32_t on = 0;
        dbus::check(sd_bus_message_read(message, "iii", &x, &y, &on),
                    "read Listener.MouseSet");
        framebuffer_.set_cursor_position(x, y, on != 0);
        {
            std::lock_guard lock(stats_mutex_);
            ++cursor_moves_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""), "reply MouseSet");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      listener_interface.data(),
                                      "CursorDefine")) {
        std::int32_t width = 0;
        std::int32_t height = 0;
        std::int32_t hot_x = 0;
        std::int32_t hot_y = 0;
        dbus::check(sd_bus_message_read(message,
                                        "iiii",
                                        &width,
                                        &height,
                                        &hot_x,
                                        &hot_y),
                    "read Listener.CursorDefine geometry");
        const void *data = nullptr;
        std::size_t size = 0U;
        dbus::check(sd_bus_message_read_array(message, 'y', &data, &size),
                    "read Listener.CursorDefine data");
        framebuffer_.set_cursor_shape(
            width,
            height,
            hot_x,
            hot_y,
            {static_cast<const std::uint8_t *>(data), size});
        {
            std::lock_guard lock(stats_mutex_);
            ++cursor_definitions_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""), "reply CursorDefine");
        return 1;
    }

#ifndef QMDP_HAS_GBM
    if (sd_bus_message_is_method_call(message,
                                      listener_interface.data(),
                                      "ScanoutDMABUF") ||
        sd_bus_message_is_method_call(message,
                                      listener_interface.data(),
                                      "UpdateDMABUF")) {
        {
            std::lock_guard lock(stats_mutex_);
            ++unsupported_dmabuf_messages_;
        }
        return sd_bus_reply_method_errorf(
            message,
            "org.qemu.Display1.Error.NotSupported",
            "%s",
            "DMA-BUF capture is not enabled in the CPU-first MVP");
    }
#endif

    return 0;
}


void QemuDbusDisplay::register_audio_listener() {
    int sockets[2] {-1, -1};
    if (::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) < 0) {
        throw std::system_error(errno,
                                std::generic_category(),
                                "socketpair for QEMU audio listener");
    }
    UniqueFd client_fd(sockets[0]);
    UniqueFd qemu_fd(sockets[1]);

    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call_method(main_bus_.get(),
                                          destination(),
                                          audio_path.data(),
                                          audio_interface.data(),
                                          "RegisterOutListener",
                                          error.get(),
                                          reply.put(),
                                          "h",
                                          qemu_fd.get());
    dbus::check(result, "QEMU RegisterOutListener", error.get());
    qemu_fd.reset();

    audio_transport_shutdown_fd_ = duplicate_cloexec(client_fd.get());
    audio_bus_ = dbus::Bus::p2p_client_fd(std::move(client_fd));
    dbus::check(sd_bus_add_filter(audio_bus_.get(),
                                  audio_filter_slot_.put(),
                                  &QemuDbusDisplay::audio_filter,
                                  this),
                "sd_bus_add_filter(QEMU audio listener)");
    audio_bus_.start();
    std::lock_guard lock(stats_mutex_);
    audio_listener_registered_ = true;
    audio_listener_active_ = true;
}

int QemuDbusDisplay::audio_filter(sd_bus_message *message,
                                  void *userdata,
                                  sd_bus_error *) noexcept {
    auto *self = static_cast<QemuDbusDisplay *>(userdata);
    try {
        return self->handle_audio_message(message);
    } catch (const std::exception& ex) {
        self->report_error(std::string("QEMU audio listener: ") + ex.what());
        (void) sd_bus_reply_method_errorf(message,
                                          "org.qemu.Display1.Error.Failed",
                                          "%s",
                                          ex.what());
        return 1;
    } catch (...) {
        self->report_error("unknown exception in QEMU audio listener");
        (void) sd_bus_reply_method_errorf(message,
                                          "org.qemu.Display1.Error.Failed",
                                          "%s",
                                          "unknown audio listener failure");
        return 1;
    }
}

int QemuDbusDisplay::handle_audio_message(sd_bus_message *message) {
    const char *path = sd_bus_message_get_path(message);
    if (path == nullptr || std::string_view(path) != audio_listener_path) {
        return 0;
    }

    constexpr std::array<std::string_view, 0U> no_extensions {};
    if (const int handled = handle_properties(message,
                                               audio_listener_interface,
                                               no_extensions);
        handled != 0) {
        return handled;
    }
    if (const int handled = handle_introspection(message,
                                                  audio_introspection_xml);
        handled != 0) {
        return handled;
    }
    if (const int handled = handle_peer_standard(message); handled != 0) {
        return handled;
    }

    if (sd_bus_message_is_method_call(message,
                                      audio_listener_interface.data(),
                                      "Init")) {
        AudioStreamState state;
        int is_signed = 0;
        int is_float = 0;
        int big_endian = 0;
        dbus::check(sd_bus_message_read(message,
                                        "tybbuyuub",
                                        &state.id,
                                        &state.bits,
                                        &is_signed,
                                        &is_float,
                                        &state.sample_rate,
                                        &state.channels,
                                        &state.bytes_per_frame,
                                        &state.bytes_per_second,
                                        &big_endian),
                    "read AudioOutListener.Init");
        state.is_signed = is_signed != 0;
        state.is_float = is_float != 0;
        state.big_endian = big_endian != 0;
        state.enabled = false;
        state.muted = false;

        const std::uint32_t sample_bytes =
            (static_cast<std::uint32_t>(state.bits) + 7U) / 8U;
        if (state.id == 0U || state.bits == 0U || state.sample_rate == 0U ||
            state.channels == 0U || state.bytes_per_frame == 0U ||
            sample_bytes == 0U ||
            static_cast<std::uint64_t>(sample_bytes) * state.channels >
                state.bytes_per_frame ||
            (state.is_float && state.bits != 32U && state.bits != 64U) ||
            (!state.is_float && state.bits > 32U)) {
            return sd_bus_reply_method_errorf(
                message,
                "org.qemu.Display1.Error.InvalidAudioFormat",
                "%s",
                "unsupported or internally inconsistent PCM format");
        }
        state.volume.assign(state.channels, 255U);
        {
            std::lock_guard lock(audio_state_mutex_);
            audio_streams_.insert_or_assign(state.id, std::move(state));
        }
        {
            std::lock_guard lock(stats_mutex_);
            ++audio_inits_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply AudioOutListener.Init");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      audio_listener_interface.data(),
                                      "Fini")) {
        std::uint64_t id = 0U;
        dbus::check(sd_bus_message_read(message, "t", &id),
                    "read AudioOutListener.Fini");
        {
            std::lock_guard lock(audio_state_mutex_);
            audio_streams_.erase(id);
        }
        {
            std::lock_guard lock(stats_mutex_);
            ++audio_stream_finishes_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply AudioOutListener.Fini");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      audio_listener_interface.data(),
                                      "SetEnabled")) {
        std::uint64_t id = 0U;
        int enabled = 0;
        dbus::check(sd_bus_message_read(message, "tb", &id, &enabled),
                    "read AudioOutListener.SetEnabled");
        {
            std::lock_guard lock(audio_state_mutex_);
            const auto stream = audio_streams_.find(id);
            if (stream == audio_streams_.end()) {
                return sd_bus_reply_method_errorf(
                    message,
                    "org.qemu.Display1.Error.UnknownAudioStream",
                    "Unknown audio stream %llu",
                    static_cast<unsigned long long>(id));
            }
            stream->second.enabled = enabled != 0;
        }
        {
            std::lock_guard lock(stats_mutex_);
            ++audio_enable_changes_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply AudioOutListener.SetEnabled");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      audio_listener_interface.data(),
                                      "SetVolume")) {
        std::uint64_t id = 0U;
        int muted = 0;
        dbus::check(sd_bus_message_read(message, "tb", &id, &muted),
                    "read AudioOutListener.SetVolume header");
        const void *volume_data = nullptr;
        std::size_t volume_size = 0U;
        dbus::check(sd_bus_message_read_array(message,
                                              'y',
                                              &volume_data,
                                              &volume_size),
                    "read AudioOutListener.SetVolume values");
        {
            std::lock_guard lock(audio_state_mutex_);
            const auto stream = audio_streams_.find(id);
            if (stream == audio_streams_.end()) {
                return sd_bus_reply_method_errorf(
                    message,
                    "org.qemu.Display1.Error.UnknownAudioStream",
                    "Unknown audio stream %llu",
                    static_cast<unsigned long long>(id));
            }
            stream->second.muted = muted != 0;
            const auto *bytes = static_cast<const std::uint8_t *>(volume_data);
            stream->second.volume.assign(bytes, bytes + volume_size);
            if (stream->second.volume.size() < stream->second.channels) {
                stream->second.volume.resize(stream->second.channels, 255U);
            }
        }
        {
            std::lock_guard lock(stats_mutex_);
            ++audio_volume_changes_;
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply AudioOutListener.SetVolume");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      audio_listener_interface.data(),
                                      "Write")) {
        std::uint64_t id = 0U;
        dbus::check(sd_bus_message_read(message, "t", &id),
                    "read AudioOutListener.Write stream id");
        const void *pcm_data = nullptr;
        std::size_t pcm_size = 0U;
        dbus::check(sd_bus_message_read_array(message, 'y', &pcm_data, &pcm_size),
                    "read AudioOutListener.Write data");

        AudioStreamState state;
        {
            std::lock_guard lock(audio_state_mutex_);
            const auto stream = audio_streams_.find(id);
            if (stream == audio_streams_.end()) {
                return sd_bus_reply_method_errorf(
                    message,
                    "org.qemu.Display1.Error.UnknownAudioStream",
                    "Unknown audio stream %llu",
                    static_cast<unsigned long long>(id));
            }
            state = stream->second;
        }
        if (pcm_size % state.bytes_per_frame != 0U) {
            return sd_bus_reply_method_errorf(
                message,
                "org.qemu.Display1.Error.InvalidAudioData",
                "%s",
                "PCM byte count is not a whole number of frames");
        }

        const auto *pcm = static_cast<const std::uint8_t *>(pcm_data);
        const std::size_t frame_count = pcm_size / state.bytes_per_frame;
        const std::size_t sample_count = frame_count * state.channels;
        const std::size_t sample_bytes =
            (static_cast<std::size_t>(state.bits) + 7U) / 8U;
        std::vector<float> samples(sample_count, 0.0F);
        if (state.enabled && !state.muted) {
            for (std::size_t frame_index = 0U;
                 frame_index < frame_count;
                 ++frame_index) {
                const std::size_t frame_offset =
                    frame_index * state.bytes_per_frame;
                for (std::size_t channel = 0U;
                     channel < state.channels;
                     ++channel) {
                    const std::size_t sample_offset =
                        frame_offset + channel * sample_bytes;
                    const std::span<const std::uint8_t> encoded(
                        pcm + sample_offset,
                        sample_bytes);
                    float value = state.is_float
                        ? floating_pcm_to_float(encoded,
                                                state.bits,
                                                state.big_endian)
                        : integer_pcm_to_float(encoded,
                                               state.bits,
                                               state.is_signed,
                                               state.big_endian);
                    const std::uint8_t volume = channel < state.volume.size()
                        ? state.volume[channel]
                        : 255U;
                    value *= static_cast<float>(volume) / 255.0F;
                    samples[frame_index * state.channels + channel] = value;
                }
            }
        }

        if (state.enabled && callbacks_.on_audio && !samples.empty()) {
            callbacks_.on_audio(samples, state.sample_rate, state.channels);
        }
        {
            std::lock_guard lock(stats_mutex_);
            ++audio_writes_;
            audio_bytes_ += pcm_size;
            if (state.enabled) {
                audio_frames_ += frame_count;
            }
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply AudioOutListener.Write");
        return 1;
    }

    return 0;
}

void QemuDbusDisplay::audio_loop() noexcept {
    try {
        const auto interval = std::chrono::duration_cast<std::chrono::microseconds>(
            options_.pump_interval);
        while (!stopping_.load()) {
            if (!audio_bus_.pump_once(interval)) {
                if (!stopping_.load()) {
                    report_error("QEMU audio listener connection closed");
                }
                break;
            }
        }
    } catch (const std::exception& ex) {
        if (!stopping_.load()) {
            report_error(std::string("QEMU audio event loop failed: ") + ex.what());
        }
    } catch (...) {
        if (!stopping_.load()) {
            report_error("QEMU audio event loop failed with an unknown exception");
        }
    }
    std::lock_guard lock(stats_mutex_);
    audio_listener_active_ = false;
}

void QemuDbusDisplay::publish(FrameToken frame) {
    if (callbacks_.on_frame) {
        callbacks_.on_frame(std::move(frame));
    }
}

void QemuDbusDisplay::report_error(std::string message) noexcept {
    try {
        if (callbacks_.on_error) {
            callbacks_.on_error(std::move(message));
        }
    } catch (...) {
    }
}

void QemuDbusDisplay::peer_loop() noexcept {
    try {
        const auto interval = std::chrono::duration_cast<std::chrono::microseconds>(
            options_.pump_interval);
        while (!stopping_.load()) {
            if (!peer_bus_.pump_once(interval)) {
                if (!stopping_.load() && !display_disabled_.load()) {
                    report_error("QEMU display listener connection closed unexpectedly");
                }
                break;
            }
        }
    } catch (const std::exception& ex) {
        if (!stopping_.load()) {
            report_error(std::string("QEMU peer event loop failed: ") + ex.what());
        }
    } catch (...) {
        if (!stopping_.load()) {
            report_error("QEMU peer event loop failed with an unknown exception");
        }
    }
}

QemuDbusDisplay::Stats QemuDbusDisplay::stats() const {
    std::lock_guard lock(stats_mutex_);
    return {
        .framebuffer = framebuffer_.stats(),
        .inline_scanouts = inline_scanouts_,
        .inline_updates = inline_updates_,
        .mapped_scanouts = mapped_scanouts_,
        .mapped_updates = mapped_updates_,
        .stale_geometry_update_drops = stale_geometry_update_drops_,
        .cursor_definitions = cursor_definitions_,
        .cursor_moves = cursor_moves_,
        .dmabuf_scanouts = dmabuf_scanouts_,
        .dmabuf_updates = dmabuf_updates_,
        .dmabuf_readback_failures = dmabuf_readback_failures_,
        .dmabuf_readback_samples = dmabuf_readback_samples_,
        .dmabuf_readback_total_microseconds = dmabuf_readback_total_microseconds_,
        .dmabuf_readback_max_microseconds = dmabuf_readback_max_microseconds_,
        .unsupported_dmabuf_messages = unsupported_dmabuf_messages_,
        .audio_inits = audio_inits_,
        .audio_writes = audio_writes_,
        .audio_frames = audio_frames_,
        .audio_bytes = audio_bytes_,
        .audio_stream_finishes = audio_stream_finishes_,
        .audio_enable_changes = audio_enable_changes_,
        .audio_volume_changes = audio_volume_changes_,
        .audio_registration_failures = audio_registration_failures_,
        .audio_listener_registered = audio_listener_registered_,
        .audio_listener_active = audio_listener_active_,
    };
}

}  // namespace qmdp
