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

    // A one-frame VBV crushes a scene cut even when the average movie fits
    // the path. A cropped plane may borrow at most 450 ms for a complex
    // picture; VideoBudget and the RTP pacer still pay every emitted byte
    // before admitting another picture. This is codec burst capacity, not
    // permission to queue future frames. The full-desktop fallback retains
    // its original one-frame codec budget.
    static std::uint32_t buffer_bits(std::uint32_t bitrate_kbps,
                                     std::uint32_t fps, bool cropped) {
        const auto bits = static_cast<std::uint64_t>(std::clamp(bitrate_kbps, 500U, 100'000U)) * 1000U;
        const auto frame = bits / std::max(1U, fps);
        // Leave room below the transport's 4 MiB access-unit ceiling even
        // when a user selects a 100 Mbit/s encoder ceiling.
        constexpr std::uint64_t max_burst_bits = 2U * 1024U * 1024U * 8U;
        const auto burst = std::min(bits * 450U / 1000U, max_burst_bits);
        return static_cast<std::uint32_t>(cropped ? std::max(frame, burst) : frame);
    }

    std::uint32_t update(Clock::time_point now, bool cropped,
                         std::uint32_t requested, std::uint32_t maximum,
                         bool opening, std::uint64_t previous_access_unit_bytes = 0,
                         bool confirmed_decrease = false) {
        maximum = std::clamp(maximum, 500U, 100'000U);
        requested = std::clamp(requested, 500U, maximum);
        const auto desired = cropped
            ? std::max(500U, requested * 70U / 100U / 50U * 50U) : maximum;
        if (!current_ || opening || cropped != cropped_) {
            current_ = desired;
            cropped_ = cropped;
            changed_ = now;
            pending_.reset();
            boundary_deferred_ = false;
            return current_;
        }
        if (!cropped) { return current_; }
        // Only the negotiated host repair command may bypass estimate dwell.
        // It follows authenticated packet evidence of a severe path collapse.
        // Keep the reset quiet period; ordinary estimates use the guards below.
        if (confirmed_decrease && desired < current_ &&
                now - changed_ >= std::chrono::seconds(1)) {
            current_ = desired;
            changed_ = now;
            pending_.reset();
            boundary_deferred_ = false;
            return current_;
        }

        // Ignore small rate fluctuations. A single loss report or a short
        // typing burst cannot reset the codec. Even a sustained change must
        // wait for the previous reset's quiet period. Track a direction,
        // not an identical sample, so real network jitter cannot indefinitely
        // postpone adaptation to a persistently smaller/larger link.
        const int direction = desired * 100U <= current_ * 80U ? -1
            : desired * 100U >= current_ * 125U ? 1 : 0;
        if (!direction) { pending_.reset(); boundary_deferred_ = false; return current_; }
        if (!pending_ || direction != direction_) {
            pending_ = now;
            direction_ = direction;
            boundary_deferred_ = false;
        }
        // Raw admission and pacing react immediately, but an old high-rate
        // codec can still produce a scene-sized AU before its normal dwell
        // expires. A sustained halving needs a faster downward boundary.
        // Keep brief estimates and all ordinary retunes hysteretic.
        const bool urgent = direction < 0 && desired * 2U <= current_;
        const auto dwell = urgent ? std::chrono::milliseconds(600)
            : direction < 0 ? std::chrono::milliseconds(1500)
                                         : std::chrono::milliseconds(5000);
        const auto quiet = urgent ? std::chrono::seconds(1) : std::chrono::seconds(5);
        if (now - *pending_ >= dwell && now - changed_ >= quiet) {
            // A scene-sized P-frame can spend hundreds of milliseconds in
            // byte admission and RTP pacing. Immediately resetting the codec
            // then sends another large IDR while that displayed picture is
            // already old. Admit one fresh predictive picture first. This
            // postpones only this rate boundary, never drops references or
            // grants bandwidth, and cannot keep postponing a real decrease
            // if the next picture is large too. Opening and explicit repair
            // bypass this hint; ordinary dwell/quiet periods still apply.
            const auto large_picture = static_cast<std::uint64_t>(requested) * 1000U / 80U;
            if (!boundary_deferred_ && previous_access_unit_bytes > large_picture) {
                boundary_deferred_ = true;
                return current_;
            }
            current_ = desired;
            changed_ = now;
            pending_.reset();
            boundary_deferred_ = false;
        }
        return current_;
    }

private:
    std::uint32_t current_ {};
    bool cropped_ {};
    Clock::time_point changed_ {};
    std::optional<Clock::time_point> pending_;
    int direction_ {};
    bool boundary_deferred_ {};
};

}  // namespace qmdp
