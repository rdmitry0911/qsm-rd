#include "core/pixel_format.hpp"

#include <cstddef>
#include <limits>
#include <stdexcept>

namespace qmdp {

PackedPixelLayout pixman_layout(std::uint32_t format) noexcept {
    switch (format) {
        case pixman_a8r8g8b8: return PackedPixelLayout::bgra;
        case pixman_x8r8g8b8: return PackedPixelLayout::bgrx;
        case pixman_a8b8g8r8: return PackedPixelLayout::rgba;
        case pixman_x8b8g8r8: return PackedPixelLayout::rgbx;
        case pixman_b8g8r8a8: return PackedPixelLayout::argb;
        case pixman_b8g8r8x8: return PackedPixelLayout::xrgb;
        case pixman_r8g8b8a8: return PackedPixelLayout::abgr;
        case pixman_r8g8b8x8: return PackedPixelLayout::xbgr;
        default: return PackedPixelLayout::unsupported;
    }
}

std::string_view pixel_layout_name(PackedPixelLayout layout) noexcept {
    switch (layout) {
        case PackedPixelLayout::bgra: return "BGRA";
        case PackedPixelLayout::bgrx: return "BGRX";
        case PackedPixelLayout::rgba: return "RGBA";
        case PackedPixelLayout::rgbx: return "RGBX";
        case PackedPixelLayout::argb: return "ARGB";
        case PackedPixelLayout::xrgb: return "XRGB";
        case PackedPixelLayout::abgr: return "ABGR";
        case PackedPixelLayout::xbgr: return "XBGR";
        case PackedPixelLayout::unsupported: return "unsupported";
    }
    return "unsupported";
}

bool is_supported_32bit_pixman(std::uint32_t format) noexcept {
    return pixman_layout(format) != PackedPixelLayout::unsupported;
}

std::vector<std::uint8_t> to_bgra(std::span<const std::uint8_t> source,
                                  std::uint32_t width,
                                  std::uint32_t height,
                                  std::uint32_t stride,
                                  std::uint32_t format) {
    const auto layout = pixman_layout(format);
    if (layout == PackedPixelLayout::unsupported) {
        throw std::invalid_argument("unsupported pixman pixel format");
    }
    const std::uint64_t minimum_stride = static_cast<std::uint64_t>(width) * 4U;
    const std::uint64_t source_size = static_cast<std::uint64_t>(stride) * height;
    const std::uint64_t output_size = minimum_stride * height;
    if (stride < minimum_stride || source_size > source.size() ||
        output_size > std::numeric_limits<std::size_t>::max()) {
        throw std::invalid_argument("invalid CPU frame dimensions");
    }

    std::vector<std::uint8_t> output(static_cast<std::size_t>(output_size));
    for (std::uint32_t y = 0; y < height; ++y) {
        const auto *src = source.data() + static_cast<std::size_t>(y) * stride;
        auto *dst = output.data() + static_cast<std::size_t>(y) * width * 4U;
        for (std::uint32_t x = 0; x < width; ++x) {
            const auto *p = src + static_cast<std::size_t>(x) * 4U;
            auto *q = dst + static_cast<std::size_t>(x) * 4U;
            switch (layout) {
                case PackedPixelLayout::bgra:
                    q[0] = p[0]; q[1] = p[1]; q[2] = p[2]; q[3] = p[3]; break;
                case PackedPixelLayout::bgrx:
                    q[0] = p[0]; q[1] = p[1]; q[2] = p[2]; q[3] = 255U; break;
                case PackedPixelLayout::rgba:
                    q[0] = p[2]; q[1] = p[1]; q[2] = p[0]; q[3] = p[3]; break;
                case PackedPixelLayout::rgbx:
                    q[0] = p[2]; q[1] = p[1]; q[2] = p[0]; q[3] = 255U; break;
                case PackedPixelLayout::argb:
                    q[0] = p[3]; q[1] = p[2]; q[2] = p[1]; q[3] = p[0]; break;
                case PackedPixelLayout::xrgb:
                    q[0] = p[3]; q[1] = p[2]; q[2] = p[1]; q[3] = 255U; break;
                case PackedPixelLayout::abgr:
                    q[0] = p[1]; q[1] = p[2]; q[2] = p[3]; q[3] = p[0]; break;
                case PackedPixelLayout::xbgr:
                    q[0] = p[1]; q[1] = p[2]; q[2] = p[3]; q[3] = 255U; break;
                case PackedPixelLayout::unsupported:
                    break;
            }
        }
    }
    return output;
}

}  // namespace qmdp
