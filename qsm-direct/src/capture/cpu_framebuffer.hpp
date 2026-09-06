#pragma once

#include "capture/mapped_region.hpp"
#include "core/frame.hpp"

#include <cstdint>
#include <mutex>
#include <span>
#include <string>

namespace qmdp {

struct CpuFramebufferLimits {
    std::uint32_t max_width {16384U};
    std::uint32_t max_height {16384U};
    std::uint64_t max_bytes {512ULL * 1024ULL * 1024ULL};
};

// Owns the current QEMU CPU scanout and publishes immutable snapshots.
//
// The first working path intentionally copies into system memory. It supports
// QEMU Listener.Scanout/Update and Listener.Unix.Map ScanoutMap/UpdateMap, so
// functional development and software encoding do not depend on a GPU.
class CpuFramebuffer {
public:
    // A QEMU mode switch can deliver an Update for the old scanout just before
    // its replacement Scanout reaches the listener.  The D-Bus adapter uses
    // this non-throwing check to acknowledge that stale update safely instead
    // of applying it to a surface with incompatible geometry.
    enum class DamageCompatibility {
        accepted,
        awaiting_scanout,
        malformed,
        out_of_bounds,
    };

    explicit CpuFramebuffer(CpuFramebufferLimits limits = {});

    [[nodiscard]] FrameToken scanout_inline(
        std::uint32_t width,
        std::uint32_t height,
        std::uint32_t stride,
        std::uint32_t pixman_format,
        std::span<const std::uint8_t> data,
        std::string debug_name = "qemu-inline");

    [[nodiscard]] FrameToken update_inline(
        std::int32_t x,
        std::int32_t y,
        std::int32_t width,
        std::int32_t height,
        std::uint32_t source_stride,
        std::uint32_t pixman_format,
        std::span<const std::uint8_t> data);

    // DMA-BUF readback has already produced a complete CPU frame. Transfer
    // its owned storage into the immutable snapshot rather than first copying
    // it into a mutable backing vector and then cloning that vector again.
    // Older frame tokens retain their shared storage while an encoder drains.
    [[nodiscard]] FrameToken scanout_owned(
        std::uint32_t width,
        std::uint32_t height,
        std::uint32_t stride,
        std::uint32_t pixman_format,
        std::vector<std::uint8_t> data,
        std::string debug_name = "qemu-owned");

    [[nodiscard]] FrameToken replace_full_owned(
        std::int32_t x,
        std::int32_t y,
        std::int32_t width,
        std::int32_t height,
        std::uint32_t pixman_format,
        std::vector<std::uint8_t> data);

    [[nodiscard]] FrameToken scanout_map(
        UniqueFd fd,
        std::uint32_t offset,
        std::uint32_t width,
        std::uint32_t height,
        std::uint32_t stride,
        std::uint32_t pixman_format,
        std::string debug_name = "qemu-map");

    [[nodiscard]] FrameToken update_map(
        std::int32_t x,
        std::int32_t y,
        std::int32_t width,
        std::int32_t height);

    void disable() noexcept;
    [[nodiscard]] DamageCompatibility damage_compatibility(
        std::int32_t x,
        std::int32_t y,
        std::int32_t width,
        std::int32_t height) const noexcept;
    void set_cursor_position(std::int32_t x, std::int32_t y, bool visible);
    void set_cursor_shape(std::int32_t width,
                          std::int32_t height,
                          std::int32_t hotspot_x,
                          std::int32_t hotspot_y,
                          std::span<const std::uint8_t> argb);
    [[nodiscard]] CursorState cursor_state() const;

    struct Stats {
        std::uint64_t generations {};
        std::uint64_t frames {};
        std::uint64_t copied_bytes {};
        std::uint32_t width {};
        std::uint32_t height {};
        bool mapped {};
        bool enabled {};
    };

    [[nodiscard]] Stats stats() const;

private:
    struct ValidatedGeometry {
        std::size_t byte_size {};
        std::size_t row_bytes {};
    };

    [[nodiscard]] ValidatedGeometry validate_geometry(
        std::uint32_t width,
        std::uint32_t height,
        std::uint32_t stride,
        std::uint32_t pixman_format) const;
    [[nodiscard]] DamageRect validate_damage(std::int32_t x,
                                             std::int32_t y,
                                             std::int32_t width,
                                             std::int32_t height) const;
    void copy_full_from(std::span<const std::uint8_t> source);
    void copy_damage_from(std::span<const std::uint8_t> source,
                          std::uint32_t source_stride,
                          const DamageRect& damage,
                          bool source_is_full_frame);
    [[nodiscard]] FrameToken snapshot(const DamageRect& damage);

    CpuFramebufferLimits limits_;
    mutable std::mutex mutex_;
    std::uint32_t width_ {};
    std::uint32_t height_ {};
    std::uint32_t stride_ {};
    std::uint32_t pixman_format_ {};
    std::uint64_t generation_ {};
    std::uint64_t sequence_ {};
    std::uint64_t copied_bytes_ {};
    std::shared_ptr<const std::vector<std::uint8_t>> backing_;
    MappedRegion mapping_;
    CursorState cursor_;
    std::string debug_name_;
    bool enabled_ {false};
};

}  // namespace qmdp
