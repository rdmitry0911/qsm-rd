// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include "core/pixel_history.hpp"

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <span>
#include <utility>
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
    static constexpr auto input_response = std::chrono::milliseconds(300);
    // A narrow path can take a few hundred ms to deliver one exposed strip.
    // That is better than a second of video; sustained overload still falls back.
    static constexpr auto copy_band_patience = std::chrono::milliseconds(400);
    // Renewed by each further change, so it only decides how soon a scrolled
    // area returns to exact pixels once its motion has really stopped.
    static constexpr auto copy_band_hold = std::chrono::milliseconds(150);
    static constexpr std::size_t input_response_tiles = 32U;
    enum Op : std::uint8_t { hello = 0, reset = 1, patch = 2, clear = 3, heartbeat = 4, scene = 5, copy = 10 };
    using Mask = std::shared_ptr<const std::vector<std::uint8_t>>;
    // One moved rectangle of already displayed exact pixels. Detection is only
    // a hint: every destination pixel is compared before a tile relies on it.
    struct Copy {
        std::uint32_t x {}, y {}, width {}, height {}, src_x {}, src_y {};
    };
    struct Region {
        std::uint32_t x {}, y {}, width {}, height {};
    };
    [[nodiscard]] std::optional<Region> motion_region() const noexcept { return region_; }
    void prioritize(std::optional<Region> region, Clock::time_point until, bool interactive = true) noexcept {
        focus_ = region; focus_until_ = until; borrow_ = interactive;
    }
    struct Stats {
        std::uint64_t patches {}, clears {}, bytes {}, resets {}, busy_tiles {}, copies {}, copied_tiles {};
    };

    void disable();
    void advanced(bool enabled) noexcept {
        if (advanced_ != enabled) { advanced_ = enabled; reset_needed_ = true; }
    }
    void layout(std::uint64_t value, bool early) noexcept {
        if (layout_ != value) {
            layout_ = value; scene_pending_ = true;
            // The viewer retires old-layout images which led its video.
            // Re-publish those tiles even if their pixels did not change;
            // a cropped video cannot restore the missing exact coverage.
            for (auto& tile : tiles_) {
                if (tile.active && tile.early_history) { tile.refresh_layout = true; }
            }
        }
        early_ = early;
    }
    void copies(bool enabled) noexcept {
        if (copies_ != enabled) { copies_ = enabled; reset_needed_ = true; }
    }
    // Reference earlier sent pixels. Every viewer must take every record in order.
    void history(bool enabled) noexcept {
        if (history_ != enabled) { history_ = enabled; reset_needed_ = true; }
    }
    // A pointer press, release or wheel event. The next visible change of a
    // small area is its response: it need not settle or wait for video.
    void input_event(std::uint64_t generation, Clock::time_point until) noexcept {
        input_generation_ = generation; input_until_ = until;
    }
    // Bytes per second the viewer's path delivers while it is the limit; 0 = unknown.
    void delivery_limit(std::uint32_t bytes_per_second) noexcept { delivery_limit_ = bytes_per_second; }
    // Per-tile: 1 when the viewer holds exact pixels over this tile. Null until
    // a copy made the video base differ from the desktop under exact tiles.
    [[nodiscard]] const Mask& exact_mask() const noexcept { return mask_; }
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
        Clock::time_point changed {}, dense_changed {}, busy_until {}, published {}, dirty_since {};
        unsigned streak {}, dense_streak {};
        bool active {}, incompressible {}, dirty {}, urgent {}, sparse_dirty {}, focus_confined {};
        // Pixels a copy exposed or could not explain. They belong to the
        // copy's presentation, not to video motion or a settling background.
        bool copy_band {};
        // Video is only carrying scrolled pixels that fell behind, not a clip.
        bool band_video {};
        // Motion ended: these pixels complete it and need no settling or fence.
        bool band_return {};
        // The input event whose response this tile already showed. A further
        // change is motion, so an animation or started clip still reaches video.
        std::uint64_t input_served {};
        std::uint64_t band_copies {};
        // A later ordinary patch/clear may still be waiting at the viewer.
        // Keep this conservative history for the complete cache epoch.
        bool early_history {}, refresh_layout {};
        std::uint8_t encoding {};
        std::vector<std::uint8_t> pending;
        // History position the pending tokens were encoded against.
        std::uint32_t pending_position {};
    };
    [[nodiscard]] std::optional<Copy> find_copy(const std::vector<std::uint8_t>& current, std::uint32_t width,
                                                std::uint32_t height, std::uint32_t cols, std::uint32_t rows, Clock::time_point now);
    void shadow_record(Op op, std::uint32_t x, std::uint32_t y, std::uint32_t w, std::uint32_t h,
                       std::span<const std::uint8_t> payload) noexcept;
    bool emit(Op op, std::uint16_t x, std::uint16_t y, std::uint16_t w,
              std::uint16_t h, std::span<const std::uint8_t> payload, const Send& send,
              std::uint8_t encoding = 0);
    std::uint32_t width_ {}, height_ {}, epoch_ {}, sequence_ {};
    bool reset_needed_ {true};
    std::uint64_t capture_ {};
    std::uint64_t layout_ {};
    bool advanced_ {}, early_ {}, scene_pending_ {}, borrow_ {true};
    Pixels previous_;
    std::vector<Tile> tiles_;
    Clock::time_point credit_at_ {}, heartbeat_at_ {};
    std::optional<Clock::time_point> next_wakeup_;
    double credit_ {};
    Stats stats_;
    std::optional<Region> region_, focus_;
    Clock::time_point focus_until_ {};
    bool copies_ {};
    bool history_ {};
    PixelHistory pixel_history_;
    std::vector<std::uint32_t> history_pixels_;
    std::vector<std::uint8_t> history_tokens_;
    Mask mask_;
    Clock::time_point copy_until_ {}, copy_retry_ {};
    unsigned copy_misses_ {};
    std::uint32_t delivery_limit_ {};
    std::int64_t near_left_ {}, near_top_ {}, near_right_ {}, near_bottom_ {};
    // What the viewer holds after every record sent: pixels and, per pixel,
    // whether it is opaque there. A tile's edge may be stale while the rest of
    // it is exact, so whole-tile state would stop copies at the first late strip.
    std::vector<std::uint8_t> shadow_, shadow_valid_;
    const std::vector<std::uint8_t>* current_ {};
    std::uint64_t input_generation_ {};
    Clock::time_point input_until_ {};
    std::optional<std::pair<std::int32_t, std::int32_t>> copy_vector_;
    std::vector<std::uint64_t> copy_hashes_;
};

} // namespace qmdp
