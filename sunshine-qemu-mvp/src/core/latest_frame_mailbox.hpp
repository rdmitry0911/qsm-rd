#pragma once

#include "core/frame.hpp"

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <optional>

namespace qmdp {

// A bounded one-slot queue. A producer never waits for the encoder; when a
// newer frame arrives, the older unconsumed frame is intentionally discarded.
class LatestFrameMailbox {
public:
    bool publish(FrameToken frame) {
        if (!frame.valid()) {
            return false;
        }

        {
            std::lock_guard lock(mutex_);
            if (closed_) {
                return false;
            }
            if (slot_.has_value()) {
                ++dropped_;
            }
            slot_ = std::move(frame);
            ++published_;
        }
        cv_.notify_one();
        return true;
    }

    [[nodiscard]] std::optional<FrameToken> try_pop() {
        std::lock_guard lock(mutex_);
        if (!slot_) {
            return std::nullopt;
        }
        auto result = std::move(slot_);
        slot_.reset();
        ++consumed_;
        return result;
    }

    template <class Rep, class Period>
    [[nodiscard]] std::optional<FrameToken> wait_pop(
        const std::chrono::duration<Rep, Period>& timeout) {
        std::unique_lock lock(mutex_);
        cv_.wait_for(lock, timeout, [this] { return closed_ || slot_.has_value(); });
        if (!slot_) {
            return std::nullopt;
        }
        auto result = std::move(slot_);
        slot_.reset();
        ++consumed_;
        return result;
    }

    void close() {
        {
            std::lock_guard lock(mutex_);
            closed_ = true;
        }
        cv_.notify_all();
    }

    struct Stats {
        std::uint64_t published {};
        std::uint64_t consumed {};
        std::uint64_t dropped {};
        bool closed {};
    };

    [[nodiscard]] Stats stats() const {
        std::lock_guard lock(mutex_);
        return Stats{published_, consumed_, dropped_, closed_};
    }

private:
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::optional<FrameToken> slot_;
    std::uint64_t published_ {};
    std::uint64_t consumed_ {};
    std::uint64_t dropped_ {};
    bool closed_ {false};
};

}  // namespace qmdp
