#pragma once

#include "capture/cpu_framebuffer.hpp"
#include "dbus/sd_bus.hpp"
#include "interfaces/qemu_display.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <mutex>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <vector>

#include "core/unix_fd.hpp"

namespace qmdp {

#ifdef QMDP_HAS_GBM
class DmaBufReadback;
#endif

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
    // Optional: a stream to the host speaking the QSF guest-agent line
    // protocol (PING, CLIP_GET, CLIP_SET; EVENT_CLIP).  The display then
    // registers as QEMU's org.qemu.Display1.Clipboard peer and bridges the
    // guest clipboard (qemu-vdagent + spice-vdagent in the guest) to it.
    UniqueFd clipboard_fd;
};

// QEMU Display1 adapter with a production-compatible peer-to-peer listener.
// The implemented capture paths are deliberately GPU-independent:
//   - org.qemu.Display1.Listener.Scanout / Update
//   - org.qemu.Display1.Listener.Unix.Map.ScanoutMap / UpdateMap
// When built with GBM/libdrm, ScanoutDMABUF/UpdateDMABUF are imported through
// a bounded CPU readback boundary.  This keeps the software encoder path and
// avoids any X11/Wayland runtime dependency.
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
        std::uint64_t dmabuf_scanouts {};
        std::uint64_t dmabuf_updates {};
        std::uint64_t dmabuf_readback_failures {};
        // Wall time spent importing and reading completed DMA-BUF frames.
        // This is an operator diagnostic for the headless VirGL path: GPU
        // fence waits are invisible to CPU utilisation, yet directly affect
        // input-to-pixel latency.
        std::uint64_t dmabuf_readback_samples {};
        std::uint64_t dmabuf_readback_total_microseconds {};
        std::uint64_t dmabuf_readback_max_microseconds {};
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
    // Clipboard (all on clipboard_thread_, which alone processes main_bus_
    // messages; everything below runs with main_bus_mutex_ held).
    static int clipboard_filter(sd_bus_message *message,
                                void *userdata,
                                sd_bus_error *ret_error) noexcept;
    int handle_clipboard_message(sd_bus_message *message);
    static int clipboard_request_reply(sd_bus_message *message,
                                       void *userdata,
                                       sd_bus_error *ret_error) noexcept;
    static int clipboard_grab_reply(sd_bus_message *message,
                                    void *userdata,
                                    sd_bus_error *ret_error) noexcept;
    static int audio_register_reply(sd_bus_message *message,
                                    void *userdata,
                                    sd_bus_error *error) noexcept;
    static int clipboard_register_reply(sd_bus_message *message,
                                        void *userdata,
                                        sd_bus_error *ret_error) noexcept;
    void clipboard_loop() noexcept;
    void clipboard_command(const std::string& line);
    void clipboard_request_guest_text();
    void clipboard_write(const std::string& line) noexcept;
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

    // Input is edge-triggered state delivered to QEMU; waiting for its method
    // reply puts a VM scheduling round trip in front of every mouse sample.
    // Callers construct a no-reply method message and this helper serializes
    // its immediate flush against service shutdown.
    template <typename Function>
    void main_bus_send(Function&& function) {
        std::lock_guard lock(main_bus_mutex_);
        ensure_started();
        function();
        dbus::check(sd_bus_flush(main_bus_.get()), "flush QEMU input message");
    }

    QemuDbusOptions options_;
    CpuFramebuffer framebuffer_;
    QemuDisplayCallbacks callbacks_;

    mutable std::mutex lifecycle_mutex_;
    mutable std::mutex main_bus_mutex_;
    dbus::Bus main_bus_;
    dbus::Bus peer_bus_;
    // `sd_bus_slot` keeps a reference to its bus.  Retain a separate duplicate
    // of the registered listener transport solely so teardown can force the
    // peer endpoint down before releasing the filter slot.
    UniqueFd peer_transport_shutdown_fd_;
    dbus::Slot peer_filter_slot_;
    std::thread peer_thread_;
#ifdef QMDP_HAS_GBM
    std::unique_ptr<DmaBufReadback> dmabuf_readback_;
#endif
    dbus::Bus audio_bus_;
    UniqueFd audio_transport_shutdown_fd_;
    dbus::Slot audio_filter_slot_;
    std::thread audio_thread_;
    UniqueFd clipboard_fd_;
    dbus::Slot clipboard_filter_slot_;
    std::thread clipboard_thread_;
    std::uint32_t clipboard_serial_ {};
    bool clipboard_owned_ {};          // the host's text is the current grab
    std::string clipboard_host_text_;  // offered to the guest while owned
    std::string clipboard_guest_text_; // last text the guest grabbed
    std::uint64_t clipboard_generation_ {};
    std::uint64_t clipboard_pending_set_ {};
    int clipboard_register_state_ {};  // 0 pending, 1 registered, 2 refused
    int audio_register_state_ {};      // likewise for RegisterOutListener
    std::string audio_register_error_;
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
    std::uint64_t dmabuf_scanouts_ {};
    std::uint64_t dmabuf_updates_ {};
    std::uint64_t dmabuf_readback_failures_ {};
    std::uint64_t dmabuf_readback_samples_ {};
    std::uint64_t dmabuf_readback_total_microseconds_ {};
    std::uint64_t dmabuf_readback_max_microseconds_ {};
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
