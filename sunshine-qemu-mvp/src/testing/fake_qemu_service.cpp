#include "testing/fake_qemu_service.hpp"

#include "core/pixel_format.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <exception>
#include <fcntl.h>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <thread>
#include <unistd.h>
#include <vector>
#ifdef __linux__
#include <linux/memfd.h>
#endif

namespace qmdp::testing {
namespace {

using namespace std::chrono_literals;

constexpr std::string_view console_interface = "org.qemu.Display1.Console";
constexpr std::string_view keyboard_interface = "org.qemu.Display1.Keyboard";
constexpr std::string_view mouse_interface = "org.qemu.Display1.Mouse";
constexpr std::string_view properties_interface = "org.freedesktop.DBus.Properties";
constexpr std::string_view introspect_interface = "org.freedesktop.DBus.Introspectable";
constexpr std::string_view peer_interface = "org.freedesktop.DBus.Peer";
constexpr std::string_view listener_path = "/org/qemu/Display1/Listener";
constexpr std::string_view listener_interface = "org.qemu.Display1.Listener";
constexpr std::string_view map_interface = "org.qemu.Display1.Listener.Unix.Map";

constexpr const char console_xml[] = R"xml(
<node>
  <interface name="org.qemu.Display1.Console">
    <method name="RegisterListener"><arg type="h" direction="in"/></method>
    <method name="SetUIInfo"><arg type="q" direction="in"/><arg type="q" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/></method>
    <property name="Label" type="s" access="read"/>
    <property name="Head" type="u" access="read"/>
    <property name="Type" type="s" access="read"/>
    <property name="Width" type="u" access="read"/>
    <property name="Height" type="u" access="read"/>
    <property name="DeviceAddress" type="s" access="read"/>
    <property name="Interfaces" type="as" access="read"/>
  </interface>
  <interface name="org.qemu.Display1.Keyboard"><method name="Press"><arg type="u" direction="in"/></method><method name="Release"><arg type="u" direction="in"/></method></interface>
  <interface name="org.qemu.Display1.Mouse"><method name="Press"><arg type="u" direction="in"/></method><method name="Release"><arg type="u" direction="in"/></method><method name="SetAbsPosition"><arg type="u" direction="in"/><arg type="u" direction="in"/></method><method name="RelMotion"><arg type="i" direction="in"/><arg type="i" direction="in"/></method><property name="IsAbsolute" type="b" access="read"/></interface>
  <interface name="org.freedesktop.DBus.Introspectable"><method name="Introspect"><arg type="s" direction="out"/></method></interface>
  <interface name="org.freedesktop.DBus.Peer"><method name="Ping"/><method name="GetMachineId"><arg type="s" direction="out"/></method></interface>
</node>)xml";

UniqueFd duplicate_cloexec(int fd) {
    const int copy = ::fcntl(fd, F_DUPFD_CLOEXEC, 0);
    if (copy < 0) {
        throw std::system_error(errno, std::generic_category(), "duplicate listener socket");
    }
    return UniqueFd(copy);
}

UniqueFd create_memfd(std::string_view name, std::size_t size) {
#ifdef __linux__
    const std::string stable_name(name);
    const long raw = ::syscall(SYS_memfd_create, stable_name.c_str(), MFD_CLOEXEC);
    if (raw < 0) {
        throw std::system_error(errno, std::generic_category(), "memfd_create");
    }
    UniqueFd fd(static_cast<int>(raw));
    if (size > static_cast<std::size_t>(std::numeric_limits<off_t>::max())) {
        throw std::overflow_error("test framebuffer is too large");
    }
    if (::ftruncate(fd.get(), static_cast<off_t>(size)) < 0) {
        throw std::system_error(errno, std::generic_category(), "ftruncate memfd");
    }
    return fd;
#else
    (void) name;
    (void) size;
    throw std::runtime_error("fake shared-map service requires Linux memfd_create");
#endif
}

class WritableMap {
public:
    WritableMap(int fd, std::size_t length) : length_(length) {
        address_ = ::mmap(nullptr, length_, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (address_ == MAP_FAILED) {
            address_ = nullptr;
            throw std::system_error(errno, std::generic_category(), "mmap fake framebuffer");
        }
    }
    ~WritableMap() {
        if (address_ != nullptr) {
            (void) ::munmap(address_, length_);
        }
    }
    WritableMap(const WritableMap&) = delete;
    WritableMap& operator=(const WritableMap&) = delete;
    [[nodiscard]] std::span<std::uint8_t> bytes() noexcept {
        return {static_cast<std::uint8_t *>(address_), length_};
    }

private:
    void *address_ {nullptr};
    std::size_t length_ {};
};

void fill_background(std::span<std::uint8_t> pixels,
                     std::uint32_t width,
                     std::uint32_t height,
                     std::uint32_t stride) {
    for (std::uint32_t y = 0; y < height; ++y) {
        for (std::uint32_t x = 0; x < width; ++x) {
            auto *p = pixels.data() + static_cast<std::size_t>(y) * stride +
                      static_cast<std::size_t>(x) * 4U;
            p[0] = static_cast<std::uint8_t>((x * 255U) / std::max(width - 1U, 1U));
            p[1] = static_cast<std::uint8_t>((y * 255U) / std::max(height - 1U, 1U));
            p[2] = static_cast<std::uint8_t>(40U + ((x / 16U + y / 16U) % 2U) * 40U);
            p[3] = 0U;
        }
    }
}

struct Rect {
    std::uint32_t x {};
    std::uint32_t y {};
    std::uint32_t width {};
    std::uint32_t height {};
};

Rect moving_rect(std::uint32_t index,
                 std::uint32_t width,
                 std::uint32_t height) {
    const std::uint32_t box_w = std::min<std::uint32_t>(48U, std::max(width / 4U, 1U));
    const std::uint32_t box_h = std::min<std::uint32_t>(32U, std::max(height / 4U, 1U));
    const std::uint32_t x_range = width > box_w ? width - box_w : 1U;
    const std::uint32_t y_range = height > box_h ? height - box_h : 1U;
    return {
        .x = (index * 7U) % x_range,
        .y = (index * 5U) % y_range,
        .width = box_w,
        .height = box_h,
    };
}

Rect union_rect(const Rect& a, const Rect& b) {
    const std::uint32_t left = std::min(a.x, b.x);
    const std::uint32_t top = std::min(a.y, b.y);
    const std::uint32_t right = std::max(a.x + a.width, b.x + b.width);
    const std::uint32_t bottom = std::max(a.y + a.height, b.y + b.height);
    return {left, top, right - left, bottom - top};
}

void paint_rect(std::span<std::uint8_t> pixels,
                std::uint32_t stride,
                const Rect& rect,
                std::array<std::uint8_t, 4> color) {
    for (std::uint32_t y = rect.y; y < rect.y + rect.height; ++y) {
        for (std::uint32_t x = rect.x; x < rect.x + rect.width; ++x) {
            auto *p = pixels.data() + static_cast<std::size_t>(y) * stride +
                      static_cast<std::size_t>(x) * 4U;
            std::copy(color.begin(), color.end(), p);
        }
    }
}

void restore_rect(std::span<std::uint8_t> target,
                  std::span<const std::uint8_t> background,
                  std::uint32_t stride,
                  const Rect& rect) {
    const auto bytes = static_cast<std::size_t>(rect.width) * 4U;
    for (std::uint32_t row = 0; row < rect.height; ++row) {
        const auto offset = static_cast<std::size_t>(rect.y + row) * stride +
                            static_cast<std::size_t>(rect.x) * 4U;
        std::memcpy(target.data() + offset, background.data() + offset, bytes);
    }
}

void call_empty(dbus::Bus& bus,
                const char *path,
                const char *interface,
                const char *member,
                const char *signature = "",
                ...) {
    // Kept only for signature-less calls. Variadic forwarding is intentionally
    // avoided elsewhere because C varargs cannot be forwarded portably.
    if (signature[0] != '\0') {
        throw std::logic_error("call_empty only accepts an empty signature");
    }
    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call_method(bus.get(),
                                          nullptr,
                                          path,
                                          interface,
                                          member,
                                          error.get(),
                                          reply.put(),
                                          "");
    dbus::check(result, std::string("fake peer call ") + member, error.get());
}

void call_byte_array(dbus::Bus& bus,
                     const char *interface,
                     const char *member,
                     std::span<const std::uint8_t> data,
                     std::span<const std::uint32_t> unsigned_args,
                     std::span<const std::int32_t> signed_args) {
    dbus::Message call;
    dbus::check(sd_bus_message_new_method_call(bus.get(),
                                               call.put(),
                                               nullptr,
                                               listener_path.data(),
                                               interface,
                                               member),
                std::string("new fake peer ") + member);
    for (const auto value : signed_args) {
        dbus::check(sd_bus_message_append(call.get(), "i", value),
                    "append signed peer argument");
    }
    for (const auto value : unsigned_args) {
        dbus::check(sd_bus_message_append(call.get(), "u", value),
                    "append unsigned peer argument");
    }
    dbus::check(sd_bus_message_append_array(call.get(), 'y', data.data(), data.size()),
                "append peer byte array");

    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call(bus.get(), call.get(), 5'000'000U, error.get(), reply.put());
    dbus::check(result, std::string("fake peer call ") + member, error.get());
}

}  // namespace

FakeQemuService::FakeQemuService(FakeQemuOptions options)
    : options_(std::move(options)) {
    if (options_.bus_address.empty()) {
        throw std::invalid_argument("fake QEMU service requires a bus address");
    }
    if (options_.width == 0U || options_.height == 0U ||
        options_.frames == 0U || options_.fps == 0U) {
        throw std::invalid_argument("fake QEMU geometry, frame count and FPS must be non-zero");
    }
}

FakeQemuService::~FakeQemuService() {
    stop();
}

std::string FakeQemuService::console_path() const {
    return "/org/qemu/Display1/Console_" + std::to_string(options_.console_id);
}

void FakeQemuService::set_failure(std::string message) noexcept {
    std::lock_guard lock(state_mutex_);
    if (failure_.empty()) {
        failure_ = std::move(message);
    }
}

int FakeQemuService::main_filter(sd_bus_message *message,
                                 void *userdata,
                                 sd_bus_error *) noexcept {
    auto *self = static_cast<FakeQemuService *>(userdata);
    try {
        return self->handle_main_message(message);
    } catch (const std::exception& ex) {
        self->set_failure(ex.what());
        (void) sd_bus_reply_method_errorf(message,
                                          "org.qemu.Display1.Error.Failed",
                                          "%s",
                                          ex.what());
        return 1;
    } catch (...) {
        self->set_failure("unknown fake QEMU service failure");
        (void) sd_bus_reply_method_errorf(message,
                                          "org.qemu.Display1.Error.Failed",
                                          "%s",
                                          "unknown fake QEMU service failure");
        return 1;
    }
}

int FakeQemuService::handle_main_message(sd_bus_message *message) {
    const char *path = sd_bus_message_get_path(message);
    if (path == nullptr || std::string_view(path) != console_path()) {
        return 0;
    }

    if (sd_bus_message_is_method_call(message,
                                      properties_interface.data(),
                                      "Get")) {
        const char *interface_name = nullptr;
        const char *property_name = nullptr;
        dbus::check(sd_bus_message_read(message,
                                        "ss",
                                        &interface_name,
                                        &property_name),
                    "read fake Properties.Get");
        if (std::string_view(interface_name) != mouse_interface ||
            std::string_view(property_name) != "IsAbsolute") {
            return sd_bus_reply_method_errorf(
                message,
                "org.freedesktop.DBus.Error.UnknownProperty",
                "%s.%s is not available",
                interface_name,
                property_name);
        }

        dbus::Message reply;
        dbus::check(sd_bus_message_new_method_return(message, reply.put()),
                    "new fake Properties.Get reply");
        dbus::check(sd_bus_message_open_container(reply.get(), 'v', "b"),
                    "open fake Mouse.IsAbsolute variant");
        const int is_absolute = 0;
        dbus::check(sd_bus_message_append_basic(reply.get(), 'b', &is_absolute),
                    "append fake Mouse.IsAbsolute value");
        dbus::check(sd_bus_message_close_container(reply.get()),
                    "close fake Mouse.IsAbsolute variant");
        dbus::check(sd_bus_send(sd_bus_message_get_bus(message), reply.get(), nullptr),
                    "send fake Properties.Get reply");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      console_interface.data(),
                                      "RegisterListener")) {
        int fd = -1;
        dbus::check(sd_bus_message_read(message, "h", &fd),
                    "read fake RegisterListener fd");
        UniqueFd socket = duplicate_cloexec(fd);
        {
            std::lock_guard lock(state_mutex_);
            if (listener_registered_) {
                return sd_bus_reply_method_errorf(
                    message,
                    "org.qemu.Display1.Error.Busy",
                    "%s",
                    "the fake service supports one listener");
            }
            listener_registered_ = true;
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply fake RegisterListener");
        peer_thread_ = std::thread(&FakeQemuService::peer_worker,
                                   this,
                                   std::move(socket));
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      console_interface.data(),
                                      "SetUIInfo")) {
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
                    "read fake SetUIInfo");
        (void) width_mm;
        (void) height_mm;
        (void) xoff;
        (void) yoff;
        {
            std::lock_guard lock(state_mutex_);
            ++ui_info_calls_;
            requested_width_ = width;
            requested_height_ = height;
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply fake SetUIInfo");
        return 1;
    }

    const bool keyboard_press = sd_bus_message_is_method_call(message, keyboard_interface.data(), "Press") > 0;
    const bool keyboard_release = sd_bus_message_is_method_call(message, keyboard_interface.data(), "Release") > 0;
    if (keyboard_press || keyboard_release) {
        std::uint32_t keycode = 0U;
        dbus::check(sd_bus_message_read(message, "u", &keycode),
                    "read fake keyboard event");
        (void) keycode;
        {
            std::lock_guard lock(state_mutex_);
            ++keyboard_calls_;
            if (keyboard_press) { ++keyboard_presses_; }
            else { ++keyboard_releases_; }
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply fake keyboard event");
        return 1;
    }

    const bool button_press = sd_bus_message_is_method_call(message, mouse_interface.data(), "Press") > 0;
    const bool button_release = sd_bus_message_is_method_call(message, mouse_interface.data(), "Release") > 0;
    if (button_press || button_release) {
        std::uint32_t button = 0U;
        dbus::check(sd_bus_message_read(message, "u", &button),
                    "read fake mouse button");
        (void) button;
        {
            std::lock_guard lock(state_mutex_);
            ++mouse_calls_;
            if (button_press) { ++button_presses_; }
            else { ++button_releases_; }
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply fake mouse button");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      mouse_interface.data(),
                                      "SetAbsPosition")) {
        std::uint32_t x = 0U;
        std::uint32_t y = 0U;
        dbus::check(sd_bus_message_read(message, "uu", &x, &y),
                    "read fake absolute pointer");
        {
            std::lock_guard lock(state_mutex_);
            ++mouse_calls_;
            last_absolute_x_ = x;
            last_absolute_y_ = y;
            has_absolute_position_ = true;
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply fake absolute pointer");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      mouse_interface.data(),
                                      "RelMotion")) {
        std::int32_t dx = 0;
        std::int32_t dy = 0;
        dbus::check(sd_bus_message_read(message, "ii", &dx, &dy),
                    "read fake relative pointer");
        {
            std::lock_guard lock(state_mutex_);
            ++mouse_calls_;
            last_relative_dx_ = dx;
            last_relative_dy_ = dy;
            has_relative_motion_ = true;
        }
        dbus::check(sd_bus_reply_method_return(message, ""),
                    "reply fake relative pointer");
        return 1;
    }

    if (sd_bus_message_is_method_call(message,
                                      introspect_interface.data(),
                                      "Introspect")) {
        dbus::check(sd_bus_reply_method_return(message, "s", console_xml),
                    "reply fake Introspect");
        return 1;
    }

    if (sd_bus_message_is_method_call(message, peer_interface.data(), "Ping")) {
        dbus::check(sd_bus_reply_method_return(message, ""), "reply fake Peer.Ping");
        return 1;
    }
    if (sd_bus_message_is_method_call(message,
                                      peer_interface.data(),
                                      "GetMachineId")) {
        dbus::check(sd_bus_reply_method_return(
                        message,
                        "s",
                        "0123456789abcdef0123456789abcdef"),
                    "reply fake Peer.GetMachineId");
        return 1;
    }

    // The probe currently does not need console properties. Return an empty
    // dictionary for GetAll so generic D-Bus tooling can still introspect it.
    if (sd_bus_message_is_method_call(message,
                                      properties_interface.data(),
                                      "GetAll")) {
        const char *interface_name = nullptr;
        dbus::check(sd_bus_message_read(message, "s", &interface_name),
                    "read fake Properties.GetAll");
        (void) interface_name;
        dbus::Message reply;
        dbus::check(sd_bus_message_new_method_return(message, reply.put()),
                    "new fake GetAll reply");
        dbus::check(sd_bus_message_open_container(reply.get(), 'a', "{sv}"),
                    "open fake GetAll dictionary");
        dbus::check(sd_bus_message_close_container(reply.get()),
                    "close fake GetAll dictionary");
        dbus::check(sd_bus_send(sd_bus_message_get_bus(message), reply.get(), nullptr),
                    "send fake GetAll reply");
        return 1;
    }

    return 0;
}

void FakeQemuService::send_cursor(dbus::Bus& peer) {
    constexpr std::int32_t cursor_width = 8;
    constexpr std::int32_t cursor_height = 8;
    std::vector<std::uint8_t> cursor(
        static_cast<std::size_t>(cursor_width * cursor_height * 4), 0U);
    for (std::int32_t y = 0; y < cursor_height; ++y) {
        for (std::int32_t x = 0; x <= y && x < cursor_width; ++x) {
            const auto offset = static_cast<std::size_t>(y * cursor_width + x) * 4U;
            // QEMU's PIXMAN_a8r8g8b8 cursor words are laid out as BGRA on
            // the little-endian PVE hosts supported by the browser bridge.
            // Keep this non-grey so the browser cursor conversion is covered
            // by the worker-to-WebRTC qualification path.
            cursor[offset + 0U] = 0x11U;  // B
            cursor[offset + 1U] = 0x22U;  // G
            cursor[offset + 2U] = 0x33U;  // R
            cursor[offset + 3U] = 0xffU;  // A
        }
    }
    const std::array<std::int32_t, 4> geometry {
        cursor_width, cursor_height, 0, 0
    };
    // QEMU may redraw/reannounce the same pointer shape without a visual
    // change.  The worker must deduplicate those freshly allocated D-Bus
    // payloads before they congest the browser's ordered cursor channel.
    for (int repeat = 0; repeat < 3; ++repeat) {
        call_byte_array(peer,
                        listener_interface.data(),
                        "CursorDefine",
                        cursor,
                        {},
                        geometry);
    }

    dbus::Error error;
    dbus::Message reply;
    const int result = sd_bus_call_method(peer.get(),
                                          nullptr,
                                          listener_path.data(),
                                          listener_interface.data(),
                                          "MouseSet",
                                          error.get(),
                                          reply.put(),
                                          "iii",
                                          20,
                                          15,
                                          1);
    dbus::check(result, "fake peer MouseSet", error.get());
}

void FakeQemuService::stream_shared_map(dbus::Bus& peer) {
    const std::uint32_t stride = options_.width * 4U;
    const std::size_t byte_size = static_cast<std::size_t>(stride) * options_.height;
    UniqueFd memfd = create_memfd("qmdp-fake-qemu", byte_size);
    WritableMap writable(memfd.get(), byte_size);
    std::vector<std::uint8_t> background(byte_size);
    fill_background(background, options_.width, options_.height, stride);
    std::copy(background.begin(), background.end(), writable.bytes().begin());

    dbus::Error error;
    dbus::Message reply;
    int result = sd_bus_call_method(peer.get(),
                                    nullptr,
                                    listener_path.data(),
                                    map_interface.data(),
                                    "ScanoutMap",
                                    error.get(),
                                    reply.put(),
                                    "huuuuu",
                                    memfd.get(),
                                    0U,
                                    options_.width,
                                    options_.height,
                                    stride,
                                    pixman_x8r8g8b8);
    dbus::check(result, "fake peer ScanoutMap", error.get());
    {
        std::lock_guard lock(state_mutex_);
        ++frames_sent_;
    }

    send_cursor(peer);

    Rect previous = moving_rect(0U, options_.width, options_.height);
    paint_rect(writable.bytes(), stride, previous, {20U, 20U, 240U, 0U});
    auto next = std::chrono::steady_clock::now();
    const auto period = std::chrono::nanoseconds(1'000'000'000LL / options_.fps);

    for (std::uint32_t index = 1U;
         index < options_.frames && !stopping_.load();
         ++index) {
        next += period;
        const Rect current = moving_rect(index, options_.width, options_.height);
        const Rect damage = union_rect(previous, current);
        restore_rect(writable.bytes(), background, stride, previous);
        paint_rect(writable.bytes(), stride, current, {20U, 20U, 240U, 0U});

        {
            dbus::Error update_error;
            dbus::Message update_reply;
            result = sd_bus_call_method(peer.get(),
                                        nullptr,
                                        listener_path.data(),
                                        map_interface.data(),
                                        "UpdateMap",
                                        update_error.get(),
                                        update_reply.put(),
                                        "iiii",
                                        static_cast<std::int32_t>(damage.x),
                                        static_cast<std::int32_t>(damage.y),
                                        static_cast<std::int32_t>(damage.width),
                                        static_cast<std::int32_t>(damage.height));
            dbus::check(result, "fake peer UpdateMap", update_error.get());
        }
        {
            std::lock_guard lock(state_mutex_);
            ++frames_sent_;
        }
        previous = current;
        std::this_thread::sleep_until(next);
    }
}

void FakeQemuService::stream_inline(dbus::Bus& peer) {
    const std::uint32_t stride = options_.width * 4U;
    const std::size_t byte_size = static_cast<std::size_t>(stride) * options_.height;
    std::vector<std::uint8_t> frame(byte_size);
    fill_background(frame, options_.width, options_.height, stride);
    Rect previous = moving_rect(0U, options_.width, options_.height);
    paint_rect(frame, stride, previous, {20U, 20U, 240U, 0U});

    const std::array<std::uint32_t, 4> scanout_args {
        options_.width, options_.height, stride, pixman_x8r8g8b8
    };
    call_byte_array(peer,
                    listener_interface.data(),
                    "Scanout",
                    frame,
                    scanout_args,
                    {});
    {
        std::lock_guard lock(state_mutex_);
        ++frames_sent_;
    }
    send_cursor(peer);

    auto next = std::chrono::steady_clock::now();
    const auto period = std::chrono::nanoseconds(1'000'000'000LL / options_.fps);
    for (std::uint32_t index = 1U;
         index < options_.frames && !stopping_.load();
         ++index) {
        next += period;
        const Rect current = moving_rect(index, options_.width, options_.height);
        std::vector<std::uint8_t> update(
            static_cast<std::size_t>(current.width) * current.height * 4U,
            0U);
        const std::uint32_t update_stride = current.width * 4U;
        paint_rect(update,
                   update_stride,
                   {0U, 0U, current.width, current.height},
                   {20U, 20U, 240U, 0U});
        const std::array<std::int32_t, 4> signed_args {
            static_cast<std::int32_t>(current.x),
            static_cast<std::int32_t>(current.y),
            static_cast<std::int32_t>(current.width),
            static_cast<std::int32_t>(current.height),
        };
        const std::array<std::uint32_t, 2> unsigned_args {
            update_stride, pixman_x8r8g8b8
        };
        call_byte_array(peer,
                        listener_interface.data(),
                        "Update",
                        update,
                        unsigned_args,
                        signed_args);
        {
            std::lock_guard lock(state_mutex_);
            ++frames_sent_;
        }
        previous = current;
        std::this_thread::sleep_until(next);
    }
}

void FakeQemuService::peer_worker(UniqueFd socket) noexcept {
    try {
        auto peer = dbus::Bus::p2p_server_fd(std::move(socket));
        peer.start();

        // Exercise the listener's standard property endpoint exactly as QEMU's
        // generated GDBus proxy does during capability discovery.
        {
            dbus::Error error;
            dbus::Message reply;
            const int result = sd_bus_call_method(peer.get(),
                                                  nullptr,
                                                  listener_path.data(),
                                                  properties_interface.data(),
                                                  "GetAll",
                                                  error.get(),
                                                  reply.put(),
                                                  "s",
                                                  listener_interface.data());
            dbus::check(result, "fake peer Properties.GetAll", error.get());
        }

        if (options_.use_shared_map) {
            stream_shared_map(peer);
        } else {
            stream_inline(peer);
        }
        call_empty(peer,
                   listener_path.data(),
                   listener_interface.data(),
                   "Disable");
        peer.close();
    } catch (const std::exception& ex) {
        set_failure(std::string("fake peer failed: ") + ex.what());
    } catch (...) {
        set_failure("fake peer failed with an unknown exception");
    }
    peer_completed_.store(true);
}

int FakeQemuService::run() {
    main_bus_ = dbus::Bus::connect_address(options_.bus_address, true, 5s);
    dbus::check(sd_bus_add_filter(main_bus_.get(),
                                  main_filter_slot_.put(),
                                  &FakeQemuService::main_filter,
                                  this),
                "sd_bus_add_filter(fake QEMU)");
    main_bus_.start();
    dbus::check(sd_bus_request_name(main_bus_.get(),
                                    options_.bus_name.c_str(),
                                    0U),
                "sd_bus_request_name(fake QEMU)");

    std::cout << "FAKE_QEMU_READY name=" << options_.bus_name
              << " path=" << console_path() << std::endl;

    while (!stopping_.load() && !peer_completed_.load()) {
        if (!main_bus_.pump_once(50ms)) {
            set_failure("fake QEMU main bus disconnected");
            break;
        }
    }

    stop();
    std::lock_guard lock(state_mutex_);
    return failure_.empty() ? 0 : 1;
}

void FakeQemuService::stop() noexcept {
    stopping_.store(true);
    if (peer_thread_.joinable()) {
        peer_thread_.join();
    }
    main_filter_slot_.reset();
    main_bus_.close();
}

FakeQemuService::Stats FakeQemuService::stats() const {
    std::lock_guard lock(state_mutex_);
    return {
        .frames_sent = frames_sent_,
        .ui_info_calls = ui_info_calls_,
        .keyboard_calls = keyboard_calls_,
        .keyboard_presses = keyboard_presses_,
        .keyboard_releases = keyboard_releases_,
        .mouse_calls = mouse_calls_,
        .button_presses = button_presses_,
        .button_releases = button_releases_,
        .last_absolute_x = last_absolute_x_,
        .last_absolute_y = last_absolute_y_,
        .has_absolute_position = has_absolute_position_,
        .last_relative_dx = last_relative_dx_,
        .last_relative_dy = last_relative_dy_,
        .has_relative_motion = has_relative_motion_,
        .requested_width = requested_width_,
        .requested_height = requested_height_,
        .listener_registered = listener_registered_,
        .peer_completed = peer_completed_.load(),
    };
}

}  // namespace qmdp::testing
