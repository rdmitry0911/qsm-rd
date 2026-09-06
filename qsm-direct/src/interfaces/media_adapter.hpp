#pragma once

#include "core/frame.hpp"

#include <cstdint>
#include <span>

namespace qmdp {

// Encoded-media boundary shared by every transport.  QEMU Display1 is the
// capture/input authority; a direct WebRTC packetizer or a diagnostic file
// encoder is merely one possible consumer.  Keeping the
// name transport-neutral makes the browser route independent of a legacy
// remote-desktop implementation.
class IMediaAdapter {
public:
    virtual ~IMediaAdapter() = default;

    virtual void start() = 0;
    virtual void stop() noexcept = 0;

    virtual void submit_frame(const FrameToken& frame) = 0;
    // A transport that has an out-of-band cursor path can override this.
    // Keeping the default preserves adapters which deliberately composite a
    // cursor into their video or do not expose one to their client.
    virtual void submit_cursor(const CursorState&) {}
    virtual void submit_audio(std::span<const float> interleaved_samples,
                              std::uint32_t sample_rate,
                              std::uint16_t channels) = 0;
    virtual void request_idr() = 0;
};

}  // namespace qmdp
