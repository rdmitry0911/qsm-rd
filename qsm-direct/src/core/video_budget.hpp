// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>

namespace qmdp {

// Admit raw frames before encoding. Dropping an encoded P-frame breaks its
// dependants; resetting hardware rate control changes even unchanged pixels.
// Pay for every encoded AU, including IDRs, before admitting another picture.
class VideoBudget {
public:
    using Clock = std::chrono::steady_clock;

    bool ready(Clock::time_point now, std::uint32_t bitrate_kbps) {
        std::lock_guard lock(mutex_);
        const double rate = static_cast<double>(bitrate_kbps) * 1000.0 / 8.0;
        if (updated_) {
            credit_ += std::max(0.0, std::chrono::duration<double>(now - *updated_).count()) * rate;
        }
        updated_ = now;
        // Idle time must not accumulate a large burst. A rate change also
        // retains outstanding IDR debt instead of refilling the bucket.
        credit_ = std::min(credit_, rate * 0.020);
        return credit_ >= 0.0;
    }

    void account(std::size_t bytes) {
        std::lock_guard lock(mutex_);
        // Leave room for RTP/SRTP/IP and retransmissions. Tiny idle P-frames
        // remain inexpensive and can keep the negotiated cadence.
        credit_ -= static_cast<double>(bytes) * 1.12;
    }

private:
    std::mutex mutex_;
    std::optional<Clock::time_point> updated_;
    double credit_ {};
};

}  // namespace qmdp
