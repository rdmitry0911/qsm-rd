#pragma once

#include "core/frame.hpp"
#include "core/resize_coalescer.hpp"

#include <cstdint>
#include <functional>
#include <span>
#include <string>

namespace qmdp {

struct QemuDisplayCallbacks {
    std::function<void(FrameToken)> on_frame;
    std::function<void(std::span<const float>, std::uint32_t, std::uint16_t)> on_audio;
    std::function<void(std::string)> on_error;
};

class IQemuDisplay {
public:
    virtual ~IQemuDisplay() = default;

    virtual void start(QemuDisplayCallbacks callbacks) = 0;
    virtual void stop() noexcept = 0;

    virtual void set_ui_info(const ViewportRequest& request) = 0;

    virtual void key(std::uint32_t qemu_key_number, bool pressed) = 0;
    virtual void button(std::uint8_t qemu_button, bool pressed) = 0;
    [[nodiscard]] virtual bool is_absolute_pointer() = 0;
    virtual void absolute_pointer(std::uint32_t x, std::uint32_t y) = 0;
    virtual void relative_pointer(std::int32_t dx, std::int32_t dy) = 0;
};

}  // namespace qmdp
