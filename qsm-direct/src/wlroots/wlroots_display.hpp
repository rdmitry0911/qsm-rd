#pragma once

#include "interfaces/qemu_display.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace qmdp {

struct WlrootsDisplayOptions {
    // Absolute path of the headless wlroots compositor's Wayland socket.  For
    // an LXC console this is reached from the PVE host through the container's
    // root, e.g. /proc/<ct-init-pid>/root/run/user/<uid>/qsm-outer/wayland-1.
    std::string socket_path;
    // Alternatively an already connected socket (preferred for untrusted
    // containers: the caller resolves the path safely and hands over the fd).
    // Ownership passes to the display, which closes it on stop().
    int socket_fd {-1};
    // Upper bound of the capture rate.  The encoder repeats the latest frame
    // at its own cadence, so capturing faster only burns host CPU.
    std::uint32_t max_fps {60U};
    // Keyboard layout of the virtual keyboard's keymap.  The browser sends
    // physical key positions; the nested session applies its own layout.
    std::string xkb_layout {"us"};
};

// IQemuDisplay backed by a headless wlroots compositor (sway) that hosts a
// container's desktop session (a nested KWin/Plasma, or a rootful Xwayland
// running an X11 session).  Protocols, all unrestricted in sway:
//   capture  zwlr_screencopy_manager_v1   (damage-driven, SHM, cursor composited)
//   pointer  zwlr_virtual_pointer_v1      (absolute motion, buttons, wheel)
//   keyboard zwp_virtual_keyboard_v1      (evdev keycodes + xkb modifiers)
//   resize   zwlr_output_manager_v1       (custom mode on the headless output)
// No code runs inside the container and nothing needs the container's pid or
// user namespace: the compositor socket is the whole interface.
class WlrootsDisplay final : public IQemuDisplay {
public:
    explicit WlrootsDisplay(WlrootsDisplayOptions options);
    ~WlrootsDisplay() override;

    WlrootsDisplay(const WlrootsDisplay&) = delete;
    WlrootsDisplay& operator=(const WlrootsDisplay&) = delete;

    void start(QemuDisplayCallbacks callbacks) override;
    void stop() noexcept override;

    void set_ui_info(const ViewportRequest& request) override;

    void key(std::uint32_t qemu_key_number, bool pressed) override;
    void button(std::uint8_t qemu_button, bool pressed) override;
    [[nodiscard]] bool is_absolute_pointer() override { return true; }
    void absolute_pointer(std::uint32_t x, std::uint32_t y) override;
    void relative_pointer(std::int32_t dx, std::int32_t dy) override;

    struct Stats {
        std::uint64_t frames {};
        std::uint64_t capture_failures {};
        std::uint64_t reallocations {};
        std::uint64_t resizes_applied {};
        std::uint64_t resizes_failed {};
        std::uint64_t pointer_events {};
        std::uint64_t key_events {};
        std::uint32_t width {};
        std::uint32_t height {};
        std::uint32_t shm_format {};
        std::uint64_t convert_total_microseconds {};
        std::uint64_t convert_max_microseconds {};
    };
    [[nodiscard]] Stats stats() const;

    // Maps a QEMU key number (as sent by the browser console) to a Linux evdev
    // key code; 0 when the key has no mapping.  Exposed for unit tests.
    [[nodiscard]] static std::uint32_t evdev_from_qemu_key(std::uint32_t qemu_key_number) noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace qmdp
