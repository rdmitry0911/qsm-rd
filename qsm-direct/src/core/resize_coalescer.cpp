#include "core/resize_coalescer.hpp"

#include <algorithm>
#include <stdexcept>

namespace qmdp {
namespace {

std::uint32_t align_down(std::uint32_t value, std::uint32_t alignment) {
    return alignment == 0U ? value : value - (value % alignment);
}

bool same_mode(const ViewportRequest& a, const ViewportRequest& b) {
    return a.width == b.width && a.height == b.height &&
           a.refresh_millihz == b.refresh_millihz &&
           a.remote_scale_percent == b.remote_scale_percent;
}

}  // namespace

ResizeCoalescer::ResizeCoalescer(ResizePolicy policy) : policy_(policy) {
    if (policy_.min_width == 0U || policy_.min_height == 0U ||
        policy_.max_width < policy_.min_width ||
        policy_.max_height < policy_.min_height ||
        policy_.width_alignment == 0U || policy_.height_alignment == 0U) {
        throw std::invalid_argument("invalid resize policy");
    }
}

void ResizeCoalescer::submit(ViewportRequest request,
                             std::chrono::steady_clock::time_point now) {
    request = normalize(request);
    if (pending_) {
        ++superseded_;
    }
    pending_ = request;
    last_submit_ = now;
}

std::optional<ViewportRequest> ResizeCoalescer::poll(
    std::chrono::steady_clock::time_point now) {
    if (!pending_ || now - last_submit_ < policy_.debounce) {
        return std::nullopt;
    }

    auto emitted = *pending_;
    pending_.reset();

    if (last_emitted_ && same_mode(*last_emitted_, emitted)) {
        return std::nullopt;
    }

    last_emitted_ = emitted;
    return emitted;
}

std::optional<ViewportRequest> ResizeCoalescer::pending() const {
    return pending_;
}

std::uint64_t ResizeCoalescer::superseded_count() const noexcept {
    return superseded_;
}

ViewportRequest ResizeCoalescer::normalize(ViewportRequest request) const {
    request.width = std::clamp(request.width, policy_.min_width, policy_.max_width);
    request.height = std::clamp(request.height, policy_.min_height, policy_.max_height);
    request.width = align_down(request.width, policy_.width_alignment);
    request.height = align_down(request.height, policy_.height_alignment);
    request.refresh_millihz = std::clamp(request.refresh_millihz, 24000U, 240000U);
    request.remote_scale_percent = static_cast<std::uint16_t>(
        std::clamp<std::uint32_t>(request.remote_scale_percent, 50U, 400U));
    return request;
}

}  // namespace qmdp
