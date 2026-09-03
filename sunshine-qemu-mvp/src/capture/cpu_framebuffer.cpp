#include "capture/cpu_framebuffer.hpp"

#include "core/pixel_format.hpp"

#include <algorithm>
#include <cstring>
#include <limits>
#include <memory>
#include <stdexcept>

namespace qmdp {
namespace {

std::uint64_t checked_mul(std::uint64_t left, std::uint64_t right) {
    if (left != 0U && right > std::numeric_limits<std::uint64_t>::max() / left) {
        throw std::overflow_error("framebuffer size overflow");
    }
    return left * right;
}

}  // namespace

CpuFramebuffer::CpuFramebuffer(CpuFramebufferLimits limits)
    : limits_(limits) {
    if (limits_.max_width == 0U || limits_.max_height == 0U ||
        limits_.max_bytes == 0U) {
        throw std::invalid_argument("CPU framebuffer limits must be non-zero");
    }
}

CpuFramebuffer::ValidatedGeometry CpuFramebuffer::validate_geometry(
    std::uint32_t width,
    std::uint32_t height,
    std::uint32_t stride,
    std::uint32_t pixman_format) const {
    if (width == 0U || height == 0U) {
        throw std::invalid_argument("framebuffer dimensions must be non-zero");
    }
    if (width > limits_.max_width || height > limits_.max_height) {
        throw std::invalid_argument("framebuffer dimensions exceed configured limits");
    }
    if (!is_supported_32bit_pixman(pixman_format)) {
        throw std::invalid_argument("unsupported QEMU pixman format");
    }
    const auto row_bytes64 = checked_mul(width, 4U);
    if (stride < row_bytes64) {
        throw std::invalid_argument("framebuffer stride is smaller than a pixel row");
    }
    const auto byte_size64 = checked_mul(stride, height);
    if (byte_size64 > limits_.max_bytes ||
        byte_size64 > std::numeric_limits<std::size_t>::max()) {
        throw std::invalid_argument("framebuffer exceeds configured byte limit");
    }
    return {
        .byte_size = static_cast<std::size_t>(byte_size64),
        .row_bytes = static_cast<std::size_t>(row_bytes64),
    };
}

DamageRect CpuFramebuffer::validate_damage(std::int32_t x,
                                           std::int32_t y,
                                           std::int32_t width,
                                           std::int32_t height) const {
    if (!enabled_) {
        throw std::logic_error("received framebuffer update before scanout");
    }
    if (x < 0 || y < 0 || width <= 0 || height <= 0) {
        throw std::invalid_argument("invalid framebuffer damage rectangle");
    }
    const auto ux = static_cast<std::uint64_t>(x);
    const auto uy = static_cast<std::uint64_t>(y);
    const auto uw = static_cast<std::uint64_t>(width);
    const auto uh = static_cast<std::uint64_t>(height);
    if (ux + uw > width_ || uy + uh > height_) {
        throw std::invalid_argument(
            "framebuffer damage rectangle is out of bounds: x=" +
            std::to_string(x) + " y=" + std::to_string(y) +
            " width=" + std::to_string(width) +
            " height=" + std::to_string(height) + " surface=" +
            std::to_string(width_) + "x" + std::to_string(height_));
    }
    return {
        .x = static_cast<std::uint32_t>(x),
        .y = static_cast<std::uint32_t>(y),
        .width = static_cast<std::uint32_t>(width),
        .height = static_cast<std::uint32_t>(height),
    };
}

CpuFramebuffer::DamageCompatibility CpuFramebuffer::damage_compatibility(
    std::int32_t x,
    std::int32_t y,
    std::int32_t width,
    std::int32_t height) const noexcept {
    std::lock_guard lock(mutex_);
    if (!enabled_) {
        return DamageCompatibility::awaiting_scanout;
    }
    if (x < 0 || y < 0 || width <= 0 || height <= 0) {
        return DamageCompatibility::malformed;
    }
    const auto ux = static_cast<std::uint64_t>(x);
    const auto uy = static_cast<std::uint64_t>(y);
    const auto uw = static_cast<std::uint64_t>(width);
    const auto uh = static_cast<std::uint64_t>(height);
    if (ux + uw > width_ || uy + uh > height_) {
        return DamageCompatibility::out_of_bounds;
    }
    return DamageCompatibility::accepted;
}

void CpuFramebuffer::copy_full_from(std::span<const std::uint8_t> source) {
    if (!backing_ || source.size() < backing_->size()) {
        throw std::invalid_argument("scanout payload is smaller than advertised");
    }
    auto replacement = std::make_shared<std::vector<std::uint8_t>>(backing_->size());
    std::memcpy(replacement->data(), source.data(), replacement->size());
    copied_bytes_ += replacement->size();
    backing_ = std::move(replacement);
}

void CpuFramebuffer::copy_damage_from(std::span<const std::uint8_t> source,
                                      std::uint32_t source_stride,
                                      const DamageRect& damage,
                                      bool source_is_full_frame) {
    const std::size_t row_bytes = static_cast<std::size_t>(damage.width) * 4U;
    if (source_stride < row_bytes) {
        throw std::invalid_argument("update stride is smaller than the damage row");
    }

    const std::uint64_t required_rows = source_is_full_frame
                                            ? static_cast<std::uint64_t>(damage.y) + damage.height
                                            : damage.height;
    const std::uint64_t required_size = checked_mul(source_stride, required_rows);
    const std::uint64_t source_x = source_is_full_frame
                                       ? static_cast<std::uint64_t>(damage.x) * 4U
                                       : 0U;
    if (required_size > source.size() || source_x + row_bytes > source_stride) {
        throw std::invalid_argument("update payload is smaller than advertised");
    }

    if (!backing_) {
        throw std::logic_error("framebuffer has no backing storage");
    }
    auto replacement = std::make_shared<std::vector<std::uint8_t>>(*backing_);
    for (std::uint32_t row = 0; row < damage.height; ++row) {
        const std::size_t source_row = source_is_full_frame
                                           ? static_cast<std::size_t>(damage.y + row)
                                           : static_cast<std::size_t>(row);
        const std::size_t source_offset = source_row * source_stride +
                                          static_cast<std::size_t>(source_x);
        const std::size_t destination_offset =
            static_cast<std::size_t>(damage.y + row) * stride_ +
            static_cast<std::size_t>(damage.x) * 4U;
        std::memcpy(replacement->data() + destination_offset,
                    source.data() + source_offset,
                    row_bytes);
    }
    copied_bytes_ += static_cast<std::uint64_t>(row_bytes) * damage.height;
    backing_ = std::move(replacement);
}

FrameToken CpuFramebuffer::snapshot(const DamageRect& damage) {
    if (!backing_) {
        throw std::logic_error("framebuffer has no backing storage");
    }
    auto surface = std::make_shared<FrameSurface>();
    surface->width = width_;
    surface->height = height_;
    surface->y0_top = true;
    surface->generation = generation_;
    surface->debug_name = debug_name_;
    surface->storage = CpuPixels{
        .stride = stride_,
        .pixman_format = pixman_format_,
        .bytes = backing_,
    };

    FrameToken token;
    token.surface = std::move(surface);
    token.damage = damage;
    token.cursor = cursor_;
    token.sequence = ++sequence_;
    token.produced_at = std::chrono::steady_clock::now();
    return token;
}

FrameToken CpuFramebuffer::scanout_inline(
    std::uint32_t width,
    std::uint32_t height,
    std::uint32_t stride,
    std::uint32_t pixman_format,
    std::span<const std::uint8_t> data,
    std::string debug_name) {
    std::lock_guard lock(mutex_);
    const auto geometry = validate_geometry(width, height, stride, pixman_format);
    if (data.size() < geometry.byte_size) {
        throw std::invalid_argument("inline scanout payload is too small");
    }

    width_ = width;
    height_ = height;
    stride_ = stride;
    pixman_format_ = pixman_format;
    debug_name_ = std::move(debug_name);
    backing_ = std::make_shared<const std::vector<std::uint8_t>>(geometry.byte_size, 0U);
    mapping_.reset();
    enabled_ = true;
    ++generation_;
    copy_full_from(data.first(geometry.byte_size));
    return snapshot({0U, 0U, width_, height_});
}

FrameToken CpuFramebuffer::scanout_owned(
    std::uint32_t width,
    std::uint32_t height,
    std::uint32_t stride,
    std::uint32_t pixman_format,
    std::vector<std::uint8_t> data,
    std::string debug_name) {
    std::lock_guard lock(mutex_);
    const auto geometry = validate_geometry(width, height, stride, pixman_format);
    if (data.size() < geometry.byte_size) {
        throw std::invalid_argument("owned scanout payload is too small");
    }
    data.resize(geometry.byte_size);
    width_ = width;
    height_ = height;
    stride_ = stride;
    pixman_format_ = pixman_format;
    debug_name_ = std::move(debug_name);
    backing_ = std::make_shared<const std::vector<std::uint8_t>>(std::move(data));
    mapping_.reset();
    enabled_ = true;
    ++generation_;
    copied_bytes_ += geometry.byte_size;
    return snapshot({0U, 0U, width_, height_});
}

FrameToken CpuFramebuffer::replace_full_owned(
    std::int32_t x,
    std::int32_t y,
    std::int32_t width,
    std::int32_t height,
    std::uint32_t pixman_format,
    std::vector<std::uint8_t> data) {
    std::lock_guard lock(mutex_);
    const auto damage = validate_damage(x, y, width, height);
    if (pixman_format != pixman_format_) {
        throw std::invalid_argument("pixman format changed without a new scanout");
    }
    const auto bytes = checked_mul(stride_, height_);
    if (data.size() < bytes) {
        throw std::invalid_argument("owned update payload is too small");
    }
    data.resize(static_cast<std::size_t>(bytes));
    backing_ = std::make_shared<const std::vector<std::uint8_t>>(std::move(data));
    copied_bytes_ += bytes;
    return snapshot(damage);
}

FrameToken CpuFramebuffer::update_inline(
    std::int32_t x,
    std::int32_t y,
    std::int32_t width,
    std::int32_t height,
    std::uint32_t source_stride,
    std::uint32_t pixman_format,
    std::span<const std::uint8_t> data) {
    std::lock_guard lock(mutex_);
    const auto damage = validate_damage(x, y, width, height);
    if (pixman_format != pixman_format_) {
        throw std::invalid_argument("pixman format changed without a new scanout");
    }
    copy_damage_from(data, source_stride, damage, false);
    return snapshot(damage);
}

FrameToken CpuFramebuffer::scanout_map(
    UniqueFd fd,
    std::uint32_t offset,
    std::uint32_t width,
    std::uint32_t height,
    std::uint32_t stride,
    std::uint32_t pixman_format,
    std::string debug_name) {
    std::lock_guard lock(mutex_);
    const auto geometry = validate_geometry(width, height, stride, pixman_format);
    MappedRegion replacement(std::move(fd), offset, geometry.byte_size);

    width_ = width;
    height_ = height;
    stride_ = stride;
    pixman_format_ = pixman_format;
    debug_name_ = std::move(debug_name);
    backing_ = std::make_shared<const std::vector<std::uint8_t>>(geometry.byte_size, 0U);
    mapping_ = std::move(replacement);
    enabled_ = true;
    ++generation_;
    copy_full_from(mapping_.bytes());
    return snapshot({0U, 0U, width_, height_});
}

FrameToken CpuFramebuffer::update_map(std::int32_t x,
                                      std::int32_t y,
                                      std::int32_t width,
                                      std::int32_t height) {
    std::lock_guard lock(mutex_);
    const auto damage = validate_damage(x, y, width, height);
    if (!mapping_.valid()) {
        throw std::logic_error("received UpdateMap without an active shared map");
    }
    copy_damage_from(mapping_.bytes(), stride_, damage, true);
    return snapshot(damage);
}

void CpuFramebuffer::disable() noexcept {
    std::lock_guard lock(mutex_);
    enabled_ = false;
    width_ = 0U;
    height_ = 0U;
    stride_ = 0U;
    pixman_format_ = 0U;
    backing_.reset();
    mapping_.reset();
    debug_name_.clear();
}

void CpuFramebuffer::set_cursor_position(std::int32_t x,
                                         std::int32_t y,
                                         bool visible) {
    std::lock_guard lock(mutex_);
    cursor_.x = x;
    cursor_.y = y;
    cursor_.visible = visible;
    ++cursor_.sequence;
}

void CpuFramebuffer::set_cursor_shape(std::int32_t width,
                                      std::int32_t height,
                                      std::int32_t hotspot_x,
                                      std::int32_t hotspot_y,
                                      std::span<const std::uint8_t> argb) {
    if (width <= 0 || height <= 0 || hotspot_x < 0 || hotspot_y < 0 ||
        hotspot_x >= width || hotspot_y >= height) {
        throw std::invalid_argument("invalid cursor geometry");
    }
    const auto pixels = checked_mul(static_cast<std::uint64_t>(width),
                                    static_cast<std::uint64_t>(height));
    const auto bytes = checked_mul(pixels, 4U);
    if (bytes > argb.size() || bytes > std::numeric_limits<std::size_t>::max()) {
        throw std::invalid_argument("cursor payload is too small");
    }

    auto shape = std::make_shared<CursorShape>();
    shape->width = static_cast<std::uint32_t>(width);
    shape->height = static_cast<std::uint32_t>(height);
    shape->hotspot_x = static_cast<std::uint32_t>(hotspot_x);
    shape->hotspot_y = static_cast<std::uint32_t>(hotspot_y);
    shape->argb.assign(argb.begin(), argb.begin() + static_cast<std::ptrdiff_t>(bytes));

    std::lock_guard lock(mutex_);
    cursor_.shape = std::move(shape);
    ++cursor_.sequence;
}

CpuFramebuffer::Stats CpuFramebuffer::stats() const {
    std::lock_guard lock(mutex_);
    return {
        .generations = generation_,
        .frames = sequence_,
        .copied_bytes = copied_bytes_,
        .width = width_,
        .height = height_,
        .mapped = mapping_.valid(),
        .enabled = enabled_,
    };
}

}  // namespace qmdp
