#pragma once

#include "core/unix_fd.hpp"
#include "dbus/sd_bus.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>

namespace qmdp::test {

struct MockQemuDbusOptions {
    std::uint32_t width {320U};
    std::uint32_t height {180U};
    std::uint32_t fps {30U};
    bool enable_audio {true};
    std::uint32_t audio_sample_rate {48000U};
    std::uint16_t audio_channels {2U};
};

// In-process protocol peer used to validate the QEMU Display1 integration
// without QEMU, a window system, or a GPU.  The main connection and the
// registered display-listener connection are both real authenticated D-Bus
// peer connections.  Frames are exposed through memfd + ScanoutMap.
class MockQemuDbusServer {
public:
    explicit MockQemuDbusServer(MockQemuDbusOptions options = {});
    ~MockQemuDbusServer();

    MockQemuDbusServer(const MockQemuDbusServer&) = delete;
    MockQemuDbusServer& operator=(const MockQemuDbusServer&) = delete;

    [[nodiscard]] UniqueFd take_client_fd();
    void start();
    void stop() noexcept;

    struct Stats {
        std::uint64_t listeners_registered {};
        std::uint64_t scanouts_sent {};
        std::uint64_t updates_sent {};
        std::uint64_t resize_requests {};
        std::uint64_t keyboard_events {};
        std::uint64_t mouse_events {};
        std::uint64_t audio_listeners_registered {};
        std::uint64_t audio_inits_sent {};
        std::uint64_t audio_writes_sent {};
        std::uint64_t audio_frames_sent {};
        std::uint32_t width {};
        std::uint32_t height {};
        std::uint16_t width_mm {};
        std::uint16_t height_mm {};
        bool listener_connected {};
        bool audio_listener_connected {};
        std::string last_error;
    };

    [[nodiscard]] Stats stats() const;

private:
    struct SharedFramebuffer;

    static int main_filter(sd_bus_message *message,
                           void *userdata,
                           sd_bus_error *ret_error) noexcept;
    int handle_main_message(sd_bus_message *message);
    void main_loop() noexcept;
    void listener_loop(UniqueFd listener_fd) noexcept;
    void audio_loop(UniqueFd listener_fd) noexcept;

    void send_listener_capability_probe(dbus::Bus& bus);
    void send_cursor(dbus::Bus& bus);
    void send_mouse_position(dbus::Bus& bus, std::uint32_t width, std::uint32_t height);
    void send_scanout_map(dbus::Bus& bus, SharedFramebuffer& framebuffer);
    void send_update_map(dbus::Bus& bus, std::uint32_t width, std::uint32_t height);
    void send_audio_capability_probe(dbus::Bus& bus);
    void send_audio_init(dbus::Bus& bus);
    void send_audio_chunk(dbus::Bus& bus, std::uint64_t sequence);

    static SharedFramebuffer make_framebuffer(std::uint32_t width,
                                              std::uint32_t height);
    static void draw_frame(SharedFramebuffer& framebuffer,
                           std::uint64_t sequence);
    void record_error(std::string message) noexcept;

    MockQemuDbusOptions options_;
    UniqueFd main_server_fd_;
    UniqueFd main_client_fd_;
    dbus::Bus main_bus_;
    dbus::Slot main_filter_slot_;
    std::thread main_thread_;
    std::thread listener_thread_;
    std::thread audio_thread_;
    std::atomic<bool> running_ {false};

    std::atomic<std::uint32_t> requested_width_ {};
    std::atomic<std::uint32_t> requested_height_ {};
    std::atomic<std::uint16_t> requested_width_mm_ {};
    std::atomic<std::uint16_t> requested_height_mm_ {};
    std::atomic<std::uint64_t> listeners_registered_ {};
    std::atomic<std::uint64_t> scanouts_sent_ {};
    std::atomic<std::uint64_t> updates_sent_ {};
    std::atomic<std::uint64_t> resize_requests_ {};
    std::atomic<std::uint64_t> keyboard_events_ {};
    std::atomic<std::uint64_t> mouse_events_ {};
    std::atomic<std::uint64_t> audio_listeners_registered_ {};
    std::atomic<std::uint64_t> audio_inits_sent_ {};
    std::atomic<std::uint64_t> audio_writes_sent_ {};
    std::atomic<std::uint64_t> audio_frames_sent_ {};
    std::atomic<bool> listener_connected_ {false};
    std::atomic<bool> audio_listener_connected_ {false};

    mutable std::mutex listener_mutex_;
    mutable std::mutex audio_mutex_;
    mutable std::mutex error_mutex_;
    std::string last_error_;
};

}  // namespace qmdp::test
