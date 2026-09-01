#pragma once

#include "capture/cpu_framebuffer.hpp"
#include "dbus/sd_bus.hpp"
#include "interfaces/qemu_display.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <vector>

#include "core/unix_fd.hpp"

namespace qmdp {

struct QemuDbusOptions {
    // Exactly one main transport is used:
    //   * p2p_fd for an inherited QEMU peer socket;
    //   * bus_address plus a non-empty destination for a message bus;
    //   * bus_address plus an empty destination for peer-to-peer D-Bus.
    std::string bus_address;
    std::string destination {"org.qemu"};
    UniqueFd p2p_fd;
    std::uint32_t console_id {0U};
    std::chrono::milliseconds call_timeout {5000};
    std::chrono::milliseconds pump_interval {50};
    bool enable_audio {true};
    bool require_audio {false};
};

// QEMU Display1 adapter with a production-compatible peer-to-peer listener.
// The implemented capture paths are deliberately GPU-independent:
//   - org.qemu.Display1.Listener.Scanout / Update
//   - org.qemu.Display1.Listener.Unix.Map.ScanoutMap / UpdateMap
// DMA-BUF messages are recognized and reported as not implemented yet.
class QemuDbusDisplay final : public IQemuDisplay {
public:
    explicit QemuDbusDisplay(QemuDbusOptions options);
    ~QemuDbusDisplay() override;

    QemuDbusDisplay(const QemuDbusDisplay&) = delete;
    QemuDbusDisplay& operator=(const QemuDbusDisplay&) = delete;

    void start(QemuDisplayCallbacks callbacks) override;
    void stop() noexcept override;

    void set_ui_info(const ViewportRequest& request) override;
    void key(std::uint32_t qemu_key_number, bool pressed) override;
    void button(std::uint8_t qemu_button, bool pressed) override;
    [[nodiscard]] bool is_absolute_pointer() override;
    void absolute_pointer(std::uint32_t x, std::uint32_t y) override;
    void relative_pointer(std::int32_t dx, std::int32_t dy) override;

    struct Stats {
        CpuFramebuffer::Stats framebuffer;
        std::uint64_t inline_scanouts {};
        std::uint64_t inline_updates {};
        std::uint64_t mapped_scanouts {};
        std::uint64_t mapped_updates {};
        std::uint64_t stale_geometry_update_drops {};
        std::uint64_t cursor_definitions {};
        std::uint64_t cursor_moves {};
        std::uint64_t unsupported_dmabuf_messages {};
        std::uint64_t audio_inits {};
        std::uint64_t audio_writes {};
        std::uint64_t audio_frames {};
        std::uint64_t audio_bytes {};
        std::uint64_t audio_stream_finishes {};
        std::uint64_t audio_enable_changes {};
        std::uint64_t audio_volume_changes {};
        std::uint64_t audio_registration_failures {};
        bool audio_listener_registered {};
        bool audio_listener_active {};
    };

    [[nodiscard]] Stats stats() const;
    [[nodiscard]] std::string console_path() const;

private:
    struct AudioStreamState {
        std::uint64_t id {};
        std::uint8_t bits {};
        bool is_signed {};
        bool is_float {};
        std::uint32_t sample_rate {};
        std::uint8_t channels {};
        std::uint32_t bytes_per_frame {};
        std::uint32_t bytes_per_second {};
        bool big_endian {};
        bool enabled {};
        bool muted {};
        std::vector<std::uint8_t> volume;
    };

    static int peer_filter(sd_bus_message *message,
                           void *userdata,
                           sd_bus_error *ret_error) noexcept;
    int handle_peer_message(sd_bus_message *message);
    static int audio_filter(sd_bus_message *message,
                            void *userdata,
                            sd_bus_error *ret_error) noexcept;
    int handle_audio_message(sd_bus_message *message);
    int handle_properties(sd_bus_message *message,
                          std::string_view object_interface,
                          std::span<const std::string_view> extra_interfaces);
    int handle_introspection(sd_bus_message *message, const char *xml);
    int handle_peer_standard(sd_bus_message *message);
    void peer_loop() noexcept;
    void audio_loop() noexcept;
    void register_audio_listener();
    void publish(FrameToken frame);
    void report_error(std::string message) noexcept;
    void ensure_started() const;
    [[nodiscard]] const char *destination() const noexcept;

    template <typename Function>
    void main_bus_call(Function&& function) {
        std::lock_guard lock(main_bus_mutex_);
        ensure_started();
        function();
    }

    QemuDbusOptions options_;
    CpuFramebuffer framebuffer_;
    QemuDisplayCallbacks callbacks_;

    mutable std::mutex lifecycle_mutex_;
    mutable std::mutex main_bus_mutex_;
    dbus::Bus main_bus_;
    dbus::Bus peer_bus_;
    dbus::Slot peer_filter_slot_;
    std::thread peer_thread_;
    dbus::Bus audio_bus_;
    dbus::Slot audio_filter_slot_;
    std::thread audio_thread_;
    mutable std::mutex audio_state_mutex_;
    std::unordered_map<std::uint64_t, AudioStreamState> audio_streams_;
    std::atomic<bool> stopping_ {false};
    std::atomic<bool> display_disabled_ {false};
    bool started_ {false};

    mutable std::mutex stats_mutex_;
    std::uint64_t inline_scanouts_ {};
    std::uint64_t inline_updates_ {};
    std::uint64_t mapped_scanouts_ {};
    std::uint64_t mapped_updates_ {};
    std::uint64_t stale_geometry_update_drops_ {};
    std::uint64_t cursor_definitions_ {};
    std::uint64_t cursor_moves_ {};
    std::uint64_t unsupported_dmabuf_messages_ {};
    std::uint64_t audio_inits_ {};
    std::uint64_t audio_writes_ {};
    std::uint64_t audio_frames_ {};
    std::uint64_t audio_bytes_ {};
    std::uint64_t audio_stream_finishes_ {};
    std::uint64_t audio_enable_changes_ {};
    std::uint64_t audio_volume_changes_ {};
    std::uint64_t audio_registration_failures_ {};
    bool audio_listener_registered_ {false};
    bool audio_listener_active_ {false};
};

}  // namespace qmdp
