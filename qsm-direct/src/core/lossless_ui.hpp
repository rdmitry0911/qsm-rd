// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
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
    enum Op : std::uint8_t { hello = 0, reset = 1, patch = 2, clear = 3, heartbeat = 4 };
    struct Stats {
        std::uint64_t patches {}, clears {}, bytes {}, resets {}, busy_tiles {};
    };

    void disable();
    void resync() noexcept { reset_needed_ = true; }
    void update(Pixels pixels, std::uint32_t width, std::uint32_t height,
                Clock::time_point now, std::uint32_t bitrate_kbps, const Send& send);
    [[nodiscard]] const Stats& stats() const noexcept { return stats_; }
    [[nodiscard]] static std::vector<std::uint8_t> greeting();

private:
    struct Tile {
        Clock::time_point changed {}, busy_until {};
        unsigned streak {};
        bool active {}, incompressible {}, dirty {};
        std::uint8_t encoding {};
        std::vector<std::uint8_t> pending;
    };
    bool emit(Op op, std::uint16_t x, std::uint16_t y, std::uint16_t w,
              std::uint16_t h, std::span<const std::uint8_t> payload, const Send& send,
              std::uint8_t encoding = 0);
    std::uint32_t width_ {}, height_ {}, epoch_ {}, sequence_ {};
    bool reset_needed_ {true};
    Pixels previous_;
    std::vector<Tile> tiles_;
    Clock::time_point credit_at_ {}, heartbeat_at_ {};
    double credit_ {};
    Stats stats_;
};

} // namespace qmdp
