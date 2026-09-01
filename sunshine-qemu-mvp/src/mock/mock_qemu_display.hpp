#pragma once

#include "interfaces/qemu_display.hpp"

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <thread>

namespace qmdp {

class MockQemuDisplay final : public IQemuDisplay {
public:
    explicit MockQemuDisplay(std::uint32_t video_fps = 120U);
    ~MockQemuDisplay() override;

    void start(QemuDisplayCallbacks callbacks) override;
    void stop() noexcept override;
    void set_ui_info(const ViewportRequest& request) override;

    void key(std::uint32_t qemu_key_number, bool pressed) override;
    void button(std::uint8_t qemu_button, bool pressed) override;
    [[nodiscard]] bool is_absolute_pointer() override;
    void absolute_pointer(std::uint32_t x, std::uint32_t y) override;
    void relative_pointer(std::int32_t dx, std::int32_t dy) override;

    [[nodiscard]] std::uint64_t input_event_count() const noexcept;

private:
    void video_loop();
    void audio_loop();
    void rebuild_surface(std::uint32_t width, std::uint32_t height);

    const std::uint32_t video_fps_;
    std::atomic<bool> running_ {false};
    std::thread video_thread_;
    std::thread audio_thread_;
    QemuDisplayCallbacks callbacks_;

    mutable std::mutex surface_mutex_;
    std::shared_ptr<const FrameSurface> surface_;
    std::uint64_t surface_generation_ {};

    std::atomic<std::uint64_t> input_events_ {};
};

}  // namespace qmdp
