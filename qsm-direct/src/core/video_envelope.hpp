// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <optional>

#include "core/lossless_ui.hpp"

namespace qmdp {

// Chooses the video plane from the area in motion. The envelope grows and never
// shrinks as motion touches fewer tiles: changing it reopens the encoder, and that
// costs an independent picture (100 KB over text, seconds on a narrow path).
//
// Motion over most of the desktop is full-screen video, which keeps the whole
// picture at its original quality. Only motion that itself covers it qualifies, and
// only once sustained: a window dragged across the screen widens the envelope without
// being that, and leaving a plane for one tick would reopen the encoder both ways.
class VideoEnvelope {
public:
    using Clock = std::chrono::steady_clock;
    using Region = LosslessUi::Region;
    static constexpr auto full_motion_sustain = std::chrono::seconds(1);
    static constexpr auto full_motion_hold = std::chrono::seconds(5);
    static constexpr std::uint32_t minimum_start_pixels = 4U * 64U * 64U;

    void reset() noexcept { region_.reset(); since_.reset(); full_ = false; }

    // The plane to encode, or nothing for the whole picture at original quality.
    std::optional<Region> select(Clock::time_point now, const std::optional<Region>& motion,
                                 std::uint32_t width, std::uint32_t height,
                                 std::uint32_t minimum_width, std::uint32_t minimum_height) {
        if (motion) {
            const bool most = static_cast<std::uint64_t>(motion->width) * motion->height * 5U >=
                              static_cast<std::uint64_t>(width) * height * 4U;
            if (most) {
                if (!since_) { since_ = now; }
                until_ = now + full_motion_hold;
                if (!region_ || now - *since_ >= full_motion_sustain) { full_ = true; }
            } else {
                since_.reset();
            }
            if (!most || !full_) { grow(*motion, width, height, minimum_width, minimum_height); }
        }
        if (full_ && now >= until_) { full_ = false; since_.reset(); }
        return full_ ? std::nullopt : region_;
    }

private:
    void grow(Region area, std::uint32_t width, std::uint32_t height,
              std::uint32_t minimum_width, std::uint32_t minimum_height) {
        if (region_) {
            const auto left = std::min(area.x, region_->x), top = std::min(area.y, region_->y);
            const auto right = std::max(area.x + area.width, region_->x + region_->width);
            const auto bottom = std::max(area.y + area.height, region_->y + region_->height);
            area = {left, top, right - left, bottom - top};
        } else if (static_cast<std::uint64_t>(area.width) * area.height < minimum_start_pixels) {
            return;
        }
        // Encoders have minimum sizes: AMD VA-API refuses below 128 pixels and the
        // failure stops video. Tile multiples keep this even and valid for every one.
        if (area.width < minimum_width && width >= minimum_width) {
            area.x = std::min(area.x, width - minimum_width);
            area.width = minimum_width;
        }
        if (area.height < minimum_height && height >= minimum_height) {
            area.y = std::min(area.y, height - minimum_height);
            area.height = minimum_height;
        }
        region_ = area;
    }

    std::optional<Region> region_;
    std::optional<Clock::time_point> since_;
    Clock::time_point until_ {};
    bool full_ {};
};

}  // namespace qmdp
