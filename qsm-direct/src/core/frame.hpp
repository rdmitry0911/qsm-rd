#pragma once

#include "core/unix_fd.hpp"

#include <chrono>
#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <variant>
#include <vector>

namespace qmdp {

struct DamageRect {
    std::uint32_t x {};
    std::uint32_t y {};
    std::uint32_t width {};
    std::uint32_t height {};
};

struct CpuPixels {
    std::uint32_t stride {};
    std::uint32_t pixman_format {};
    std::shared_ptr<const std::vector<std::uint8_t>> bytes;
};

struct DmaBufPlane {
    UniqueFd fd;
    std::uint32_t stride {};
    std::uint32_t offset {};
};

struct DmaBufPixels {
    std::uint32_t drm_fourcc {};
    std::uint64_t modifier {};
    std::vector<DmaBufPlane> planes;
};

using FrameStorage = std::variant<CpuPixels, DmaBufPixels>;

enum class FrameMemoryKind {
    cpu,
    dmabuf,
};

struct FrameSurface {
    std::uint32_t width {};
    std::uint32_t height {};
    bool y0_top {true};
    std::uint64_t generation {};
    std::string debug_name;
    FrameStorage storage;

    [[nodiscard]] FrameMemoryKind memory_kind() const noexcept {
        return std::holds_alternative<CpuPixels>(storage)
                   ? FrameMemoryKind::cpu
                   : FrameMemoryKind::dmabuf;
    }

    [[nodiscard]] const CpuPixels *cpu() const noexcept {
        return std::get_if<CpuPixels>(&storage);
    }

    [[nodiscard]] const DmaBufPixels *dmabuf() const noexcept {
        return std::get_if<DmaBufPixels>(&storage);
    }
};

struct CursorShape {
    std::uint32_t width {};
    std::uint32_t height {};
    std::uint32_t hotspot_x {};
    std::uint32_t hotspot_y {};
    std::vector<std::uint8_t> argb;
};

struct CursorState {
    bool visible {false};
    std::int32_t x {};
    std::int32_t y {};
    std::shared_ptr<const CursorShape> shape;
    std::uint64_t sequence {};
};

struct FrameToken {
    std::shared_ptr<const FrameSurface> surface;
    DamageRect damage;
    CursorState cursor;
    std::uint64_t sequence {};
    std::chrono::steady_clock::time_point produced_at {};

    [[nodiscard]] bool valid() const noexcept {
        if (!surface || surface->width == 0U || surface->height == 0U) {
            return false;
        }
        if (const auto *cpu = surface->cpu()) {
            return cpu->bytes && !cpu->bytes->empty();
        }
        const auto *dma = surface->dmabuf();
        return dma != nullptr && !dma->planes.empty() &&
               static_cast<bool>(dma->planes.front().fd);
    }
};

}  // namespace qmdp
