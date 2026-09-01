#pragma once

#include <cstdint>
#include <span>
#include <string_view>
#include <vector>

namespace qmdp {

// Public pixman wire-format values used by QEMU D-Bus Display.
constexpr std::uint32_t pixman_format(std::uint32_t bpp,
                                      std::uint32_t type,
                                      std::uint32_t alpha,
                                      std::uint32_t red,
                                      std::uint32_t green,
                                      std::uint32_t blue) noexcept {
    return (bpp << 24U) | (type << 16U) | (alpha << 12U) |
           (red << 8U) | (green << 4U) | blue;
}

constexpr std::uint32_t pixman_type_argb = 2U;
constexpr std::uint32_t pixman_type_abgr = 3U;
constexpr std::uint32_t pixman_type_bgra = 8U;
constexpr std::uint32_t pixman_type_rgba = 9U;

constexpr std::uint32_t pixman_a8r8g8b8 =
    pixman_format(32U, pixman_type_argb, 8U, 8U, 8U, 8U);
constexpr std::uint32_t pixman_x8r8g8b8 =
    pixman_format(32U, pixman_type_argb, 0U, 8U, 8U, 8U);
constexpr std::uint32_t pixman_a8b8g8r8 =
    pixman_format(32U, pixman_type_abgr, 8U, 8U, 8U, 8U);
constexpr std::uint32_t pixman_x8b8g8r8 =
    pixman_format(32U, pixman_type_abgr, 0U, 8U, 8U, 8U);
constexpr std::uint32_t pixman_b8g8r8a8 =
    pixman_format(32U, pixman_type_bgra, 8U, 8U, 8U, 8U);
constexpr std::uint32_t pixman_b8g8r8x8 =
    pixman_format(32U, pixman_type_bgra, 0U, 8U, 8U, 8U);
constexpr std::uint32_t pixman_r8g8b8a8 =
    pixman_format(32U, pixman_type_rgba, 8U, 8U, 8U, 8U);
constexpr std::uint32_t pixman_r8g8b8x8 =
    pixman_format(32U, pixman_type_rgba, 0U, 8U, 8U, 8U);

enum class PackedPixelLayout {
    bgra,
    bgrx,
    rgba,
    rgbx,
    argb,
    xrgb,
    abgr,
    xbgr,
    unsupported,
};

[[nodiscard]] PackedPixelLayout pixman_layout(std::uint32_t format) noexcept;
[[nodiscard]] std::string_view pixel_layout_name(PackedPixelLayout layout) noexcept;
[[nodiscard]] bool is_supported_32bit_pixman(std::uint32_t format) noexcept;

// Convert one full frame to tightly packed BGRA, the format accepted by the
// diagnostic software encoder. Alpha is forced to 255 for X formats.
[[nodiscard]] std::vector<std::uint8_t> to_bgra(
    std::span<const std::uint8_t> source,
    std::uint32_t width,
    std::uint32_t height,
    std::uint32_t stride,
    std::uint32_t pixman_format_code);

}  // namespace qmdp
