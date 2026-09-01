#pragma once

#include "core/frame.hpp"

#include <cstdint>
#include <span>

namespace qmdp {

class ISunshineAdapter {
public:
    virtual ~ISunshineAdapter() = default;

    virtual void start() = 0;
    virtual void stop() noexcept = 0;

    virtual void submit_frame(const FrameToken& frame) = 0;
    virtual void submit_audio(std::span<const float> interleaved_samples,
                              std::uint32_t sample_rate,
                              std::uint16_t channels) = 0;
    virtual void request_idr() = 0;
};

}  // namespace qmdp
