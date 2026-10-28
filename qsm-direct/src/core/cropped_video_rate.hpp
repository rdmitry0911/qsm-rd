// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <optional>

namespace qmdp {

// Only a separate video plane may trade codec quality for motion cadence.
// Full-desktop video retains its original quality and adapts raw admission.
// The exact UI uses the same path budget, so leave 20% for UI and 12% for
// video transport: .70 * 1.12 < .80. The byte pacer remains authoritative.
class CroppedVideoRate {
public:
    using Clock = std::chrono::steady_clock;

    std::uint32_t update(Clock::time_point now, bool cropped,
                         std::uint32_t requested, std::uint32_t maximum,
                         bool opening) {
        maximum = std::clamp(maximum, 500U, 100'000U);
        requested = std::clamp(requested, 500U, maximum);
        const auto desired = cropped
            ? std::max(500U, requested * 70U / 100U / 50U * 50U) : maximum;
        if (!current_ || opening || cropped != cropped_) {
            current_ = desired;
            cropped_ = cropped;
            changed_ = now;
            pending_.reset();
            return current_;
        }
        if (!cropped) { return current_; }

        // Ignore small rate fluctuations. A single loss report or a short
        // typing burst cannot reset the codec. Even a sustained change must
        // wait for the previous reset's quiet period. Track a direction,
        // not an identical sample, so real network jitter cannot indefinitely
        // postpone adaptation to a persistently smaller/larger link.
        const int direction = desired * 100U <= current_ * 80U ? -1
            : desired * 100U >= current_ * 125U ? 1 : 0;
        if (!direction) { pending_.reset(); return current_; }
        if (!pending_ || direction != direction_) {
            pending_ = now;
            direction_ = direction;
        }
        const auto dwell = direction < 0 ? std::chrono::milliseconds(1500)
                                         : std::chrono::milliseconds(5000);
        if (now - *pending_ >= dwell && now - changed_ >= std::chrono::seconds(5)) {
            current_ = desired;
            changed_ = now;
            pending_.reset();
        }
        return current_;
    }

private:
    std::uint32_t current_ {};
    bool cropped_ {};
    Clock::time_point changed_ {};
    std::optional<Clock::time_point> pending_;
    int direction_ {};
};

}  // namespace qmdp
