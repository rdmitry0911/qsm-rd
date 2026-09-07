#pragma once

#include "core/unix_fd.hpp"
#include "dbus/sd_bus.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>

namespace qmdp::testing {

struct FakeQemuOptions {
    std::string bus_address;
    std::string bus_name {"org.qemu"};
    std::uint32_t console_id {0U};
    std::uint32_t width {320U};
    std::uint32_t height {180U};
    std::uint32_t frames {45U};
    std::uint32_t fps {30U};
    bool use_shared_map {true};
};

class FakeQemuService {
public:
    explicit FakeQemuService(FakeQemuOptions options);
    ~FakeQemuService();

    FakeQemuService(const FakeQemuService&) = delete;
    FakeQemuService& operator=(const FakeQemuService&) = delete;

    // Runs the main bus loop until the synthetic display stream completes.
    int run();
    void stop() noexcept;

    struct Stats {
        std::uint64_t frames_sent {};
        std::uint64_t ui_info_calls {};
        std::uint64_t keyboard_calls {};
        std::uint64_t keyboard_presses {};
        std::uint64_t keyboard_releases {};
        std::uint64_t mouse_calls {};
        std::uint64_t button_presses {};
        std::uint64_t button_releases {};
        // QEMU InputButton 3/4 presses: a browser scroll-down must arrive
        // as wheel-down, so the worker's wheel direction is observable.
        std::uint64_t wheel_up_clicks {};
        std::uint64_t wheel_down_clicks {};
        std::uint32_t last_absolute_x {};
        std::uint32_t last_absolute_y {};
        bool has_absolute_position {};
        std::int32_t last_relative_dx {};
        std::int32_t last_relative_dy {};
        bool has_relative_motion {};
        std::uint32_t requested_width {};
        std::uint32_t requested_height {};
        bool listener_registered {};
        bool peer_completed {};
    };

    [[nodiscard]] Stats stats() const;

private:
    static int main_filter(sd_bus_message *message,
                           void *userdata,
                           sd_bus_error *ret_error) noexcept;
    int handle_main_message(sd_bus_message *message);
    void peer_worker(UniqueFd socket) noexcept;
    void stream_shared_map(dbus::Bus& peer);
    void stream_inline(dbus::Bus& peer);
    void send_cursor(dbus::Bus& peer);
    void set_failure(std::string message) noexcept;
    [[nodiscard]] std::string console_path() const;

    FakeQemuOptions options_;
    dbus::Bus main_bus_;
    dbus::Slot main_filter_slot_;
    std::thread peer_thread_;
    std::atomic<bool> stopping_ {false};
    std::atomic<bool> peer_completed_ {false};

    mutable std::mutex state_mutex_;
    std::string failure_;
    std::uint64_t frames_sent_ {};
    std::uint64_t ui_info_calls_ {};
    std::uint64_t keyboard_calls_ {};
    std::uint64_t keyboard_presses_ {};
    std::uint64_t keyboard_releases_ {};
    std::uint64_t mouse_calls_ {};
    std::uint64_t button_presses_ {};
    std::uint64_t button_releases_ {};
    std::uint64_t wheel_up_clicks_ {};
    std::uint64_t wheel_down_clicks_ {};
    std::uint32_t last_absolute_x_ {};
    std::uint32_t last_absolute_y_ {};
    bool has_absolute_position_ {false};
    std::int32_t last_relative_dx_ {};
    std::int32_t last_relative_dy_ {};
    bool has_relative_motion_ {false};
    std::uint32_t requested_width_ {};
    std::uint32_t requested_height_ {};
    bool listener_registered_ {false};
};

}  // namespace qmdp::testing
