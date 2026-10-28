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

    bool ready(Clock::time_point now, std::uint32_t bitrate_kbps, unsigned ui_reservation = 0) {
        std::lock_guard lock(mutex_);
        const double rate = static_cast<double>(bitrate_kbps) * 1000.0 / 8.0;
        if (updated_) {
            const double refill = std::max(0.0, std::chrono::duration<double>(now - *updated_).count()) * rate;
            const double reserved = refill * std::min(ui_reservation, 50U) / 100.0;
            // Repay source UI progressively, even after the peer's short
            // reservation has ended. Debiting a whole tile burst immediately
            // stalls video for that burst's complete transmission time.
            // Include wire/liveness headroom so a sustained 15% payload
            // share can retire its overhead rather than accumulating debt.
            const double observed = std::min(auxiliary_, std::max(reserved * 1.12, refill * .20));
            credit_ += refill - std::max(reserved, observed);
            auxiliary_ -= observed;
        }
        updated_ = now;
        // Idle time must not accumulate a large burst. A rate change also
        // retains outstanding IDR debt instead of refilling the bucket.
        credit_ = std::min(credit_, rate * 0.020);
        return (!backpressure_until_ || now >= *backpressure_until_) && credit_ >= 0.0;
    }

    // Pause raw admission while the bridge waits for a completed frame's
    // receipt. Keep the latest scanout replaceable and codec references
    // intact instead of filling/dropping the small encoded-frame FIFO.
    // A lost release or a dead bridge cannot leave capture paused forever.
    void backpressure(Clock::time_point now, std::uint32_t milliseconds) {
        std::lock_guard lock(mutex_);
        if (!milliseconds) { backpressure_until_.reset(); }
        else { backpressure_until_ = now + std::chrono::milliseconds(std::min(milliseconds, 100U)); }
    }

    void account(std::size_t bytes) {
        std::lock_guard lock(mutex_);
        // Leave room for RTP/SRTP/IP and retransmissions. Tiny idle P-frames
        // remain inexpensive and can keep the negotiated cadence.
        credit_ -= static_cast<double>(bytes) * 1.12;
    }

    void account_auxiliary(std::size_t bytes) {
        std::lock_guard lock(mutex_);
        const double cost = static_cast<double>(bytes) * 1.12;
        auxiliary_ += cost;
    }

private:
    std::mutex mutex_;
    std::optional<Clock::time_point> updated_;
    std::optional<Clock::time_point> backpressure_until_;
    double credit_ {}, auxiliary_ {};
};

}  // namespace qmdp
