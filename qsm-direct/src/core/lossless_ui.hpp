// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <span>
#include <vector>

namespace qmdp {

// Experimental pixel-exact UI layer. Never modifies the video reference chain.
// A record fits one small SCTP message; RGB pixels use bounded zlib compression.
class LosslessUi {
public:
    using Clock = std::chrono::steady_clock;
    using Pixels = std::shared_ptr<const std::vector<std::uint8_t>>;
    using Send = std::function<bool(std::span<const std::uint8_t>)>;
    static constexpr std::uint16_t tile_edge = 64;
    static constexpr std::size_t header_size = 28;
    static constexpr std::size_t max_payload = 6144;
    static constexpr std::uint64_t max_pixels = 3840U * 2160U;
    static constexpr auto settle_delay = std::chrono::milliseconds(120);
    static constexpr auto motion_hold = std::chrono::milliseconds(1000);
    enum Op : std::uint8_t { hello = 0, reset = 1, patch = 2, clear = 3, heartbeat = 4 };
    struct Region {
        std::uint32_t x {}, y {}, width {}, height {};
    };
    [[nodiscard]] std::optional<Region> motion_region() const noexcept { return region_; }
    void prioritize(std::optional<Region> region, Clock::time_point until) noexcept {
        focus_ = region; focus_until_ = until;
    }
    struct Stats {
        std::uint64_t patches {}, clears {}, bytes {}, resets {}, busy_tiles {};
    };

    void disable();
    void capture(std::uint64_t timestamp) noexcept { capture_ = timestamp; }
    void resync() noexcept { reset_needed_ = true; }
    void update(Pixels pixels, std::uint32_t width, std::uint32_t height,
                Clock::time_point now, std::uint32_t bitrate_kbps, const Send& send);
    [[nodiscard]] const Stats& stats() const noexcept { return stats_; }
    [[nodiscard]] static std::vector<std::uint8_t> greeting(std::uint32_t capabilities = 0);
    // Capture/mode changes wake us immediately. Only settling, bandwidth
    // refill, failed local sends and the liveness lease need a timer.
    [[nodiscard]] std::optional<Clock::time_point> next_wakeup() const noexcept { return next_wakeup_; }

private:
    struct Tile {
        Clock::time_point changed {}, dense_changed {}, busy_until {}, published {};
        unsigned streak {}, dense_streak {};
        bool active {}, incompressible {}, dirty {}, urgent {}, sparse_dirty {};
        std::uint8_t encoding {};
        std::vector<std::uint8_t> pending;
    };
    bool emit(Op op, std::uint16_t x, std::uint16_t y, std::uint16_t w,
              std::uint16_t h, std::span<const std::uint8_t> payload, const Send& send,
              std::uint8_t encoding = 0);
    std::uint32_t width_ {}, height_ {}, epoch_ {}, sequence_ {};
    bool reset_needed_ {true};
    std::uint64_t capture_ {};
    Pixels previous_;
    std::vector<Tile> tiles_;
    Clock::time_point credit_at_ {}, heartbeat_at_ {};
    std::optional<Clock::time_point> next_wakeup_;
    double credit_ {};
    Stats stats_;
    std::optional<Region> region_, focus_;
    Clock::time_point focus_until_ {};
};

} // namespace qmdp
