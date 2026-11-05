// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/lossless_ui.hpp"

#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <zlib.h>

namespace qmdp {
namespace {
void put16(std::vector<std::uint8_t>& b, std::uint16_t n) {
    b.push_back(static_cast<std::uint8_t>(n >> 8U)); b.push_back(static_cast<std::uint8_t>(n));
}
void put32(std::vector<std::uint8_t>& b, std::uint32_t n) {
    put16(b, static_cast<std::uint16_t>(n >> 16U)); put16(b, static_cast<std::uint16_t>(n));
}

constexpr std::uint8_t encoding_history = 8U;
constexpr std::uint32_t copy_min_tiles = 4U;
// Long enough that a run of glyphs rarely repeats elsewhere in a text page.
constexpr std::uint32_t hash_pixels = 48U;
constexpr std::uint32_t hash_row_stride = 8U;
constexpr std::uint32_t hash_slots = 1U << 17U;
constexpr std::uint32_t hash_base = 0x01000193U;
constexpr std::uint32_t hash_top = [] {
    std::uint32_t value = 1U;
    for (std::uint32_t i = 1U; i < hash_pixels; ++i) { value *= hash_base; }
    return value;
}();

std::uint32_t rgb_at(const std::uint8_t* at) noexcept {
    return static_cast<std::uint32_t>(at[0]) | (static_cast<std::uint32_t>(at[1]) << 8U) |
        (static_cast<std::uint32_t>(at[2]) << 16U);
}
std::size_t hash_slot(std::uint32_t hash) noexcept { return (hash * 0x9E3779B1U) >> 15U; }

const std::uint8_t* row_at(const std::vector<std::uint8_t>& pixels, std::uint32_t stride, std::int64_t x, std::int64_t y) noexcept {
    return pixels.data() + (static_cast<std::size_t>(y) * stride + static_cast<std::size_t>(x)) * 4U;
}

// new(x, y) == old(x - dx, y - dy) over a rectangle already known to be in bounds.
bool moved(const std::vector<std::uint8_t>& current, const std::vector<std::uint8_t>& old, std::uint32_t stride,
           std::uint32_t x, std::uint32_t y, std::uint32_t w, std::uint32_t h, std::int32_t dx, std::int32_t dy) noexcept {
    for (std::uint32_t row = 0; row < h; ++row) {
        if (std::memcmp(row_at(current, stride, x, y + row), row_at(old, stride, static_cast<std::int64_t>(x) - dx,
                static_cast<std::int64_t>(y + row) - dy), static_cast<std::size_t>(w) * 4U) != 0) { return false; }
    }
    return true;
}
}

std::vector<std::uint8_t> LosslessUi::greeting(std::uint32_t capabilities) {
    std::vector<std::uint8_t> b(header_size, 0U);
    b[0] = 'Q'; b[1] = 'U'; b[2] = 'I'; b[3] = '1';
    if (capabilities) { put32(b, capabilities); }
    return b;
}

void LosslessUi::disable() {
    region_.reset();
    previous_.reset(); tiles_.clear(); width_ = height_ = 0;
    reset_needed_ = true;
    mask_.reset(); copy_vector_.reset();
    shadow_.clear(); shadow_valid_.clear(); current_ = nullptr;
    next_wakeup_.reset();
}

void LosslessUi::shadow_record(Op op, std::uint32_t x, std::uint32_t y, std::uint32_t w, std::uint32_t h,
                               std::span<const std::uint8_t> payload) noexcept {
    if (shadow_.empty()) { return; }
    const auto stride = static_cast<std::size_t>(width_);
    if (op == reset) {
        std::fill(shadow_valid_.begin(), shadow_valid_.end(), std::uint8_t {0});
    } else if (op == clear) {
        for (auto row = y; row < y + h; ++row) { std::memset(shadow_valid_.data() + row * stride + x, 0, w); }
    } else if (op == patch && current_) {
        for (auto row = y; row < y + h; ++row) {
            std::memcpy(shadow_.data() + (row * stride + x) * 4U, current_->data() + (row * stride + x) * 4U, static_cast<std::size_t>(w) * 4U);
            std::memset(shadow_valid_.data() + row * stride + x, 1, w);
        }
    } else if (op == copy && payload.size() == 4U) {
        const auto sx = (static_cast<std::uint32_t>(payload[0]) << 8U) | payload[1];
        const auto sy = (static_cast<std::uint32_t>(payload[2]) << 8U) | payload[3];
        // Overlap: the viewer reads its source as if copied first.
        for (std::uint32_t n = 0; n < h; ++n) {
            const auto r = y > sy ? h - 1U - n : n;
            std::memmove(shadow_.data() + ((y + r) * stride + x) * 4U, shadow_.data() + ((sy + r) * stride + sx) * 4U, static_cast<std::size_t>(w) * 4U);
            std::memmove(shadow_valid_.data() + (y + r) * stride + x, shadow_valid_.data() + (sy + r) * stride + sx, w);
        }
    }
}

bool LosslessUi::emit(Op op, std::uint16_t x, std::uint16_t y, std::uint16_t w,
                     std::uint16_t h, std::span<const std::uint8_t> payload, const Send& send,
                     std::uint8_t encoding) {
    std::vector<std::uint8_t> b {'Q', 'U', 'I', '1', static_cast<std::uint8_t>(op), encoding, 0, 0};
    if (capture_) { b[7] = advanced_ ? 16 : 8; }
    put32(b, epoch_); put32(b, ++sequence_);
    put16(b, static_cast<std::uint16_t>(width_)); put16(b, static_cast<std::uint16_t>(height_));
    put16(b, x); put16(b, y); put16(b, w); put16(b, h);
    if (capture_) { put32(b, static_cast<std::uint32_t>(capture_ >> 32U)); put32(b, static_cast<std::uint32_t>(capture_)); }
    if (capture_ && advanced_) { put32(b, static_cast<std::uint32_t>(layout_ >> 32U)); put32(b, static_cast<std::uint32_t>(layout_)); }
    b.insert(b.end(), payload.begin(), payload.end());
    if (!send(b)) { reset_needed_ = true; return false; }
    if (op == reset && history_) { pixel_history_.reset(); }
    shadow_record(op, x, y, w, h, payload);
    stats_.bytes += b.size();
    if (op == patch) { ++stats_.patches; credit_ -= static_cast<double>(b.size()); }
    if (op == clear) { ++stats_.clears; }
    if (op == reset) { ++stats_.resets; }
    return true;
}

std::optional<LosslessUi::Copy> LosslessUi::find_copy(const std::vector<std::uint8_t>& current, std::uint32_t width,
                                                      std::uint32_t height, std::uint32_t cols, std::uint32_t rows, Clock::time_point now) {
    const auto& old = *previous_;
    const auto tile_x = [&](std::size_t i) { return static_cast<std::uint32_t>(i % cols) * tile_edge; };
    const auto tile_y = [&](std::size_t i) { return static_cast<std::uint32_t>(i / cols) * tile_edge; };
    const auto tile_w = [&](std::size_t i) { return std::min<std::uint32_t>(tile_edge, width - tile_x(i)); };
    const auto tile_h = [&](std::size_t i) { return std::min<std::uint32_t>(tile_edge, height - tile_y(i)); };
    // A playing clip is already video; it can neither vote nor be painted over.
    std::vector<std::uint32_t> dirty;
    std::vector<std::uint8_t> is_dirty(tiles_.size(), 0U);
    std::uint32_t left = width, top = height, right = 0, bottom = 0;
    for (std::size_t i = 0; i < tiles_.size(); ++i) {
        if (now < tiles_[i].busy_until) { continue; }
        const auto x = tile_x(i), y = tile_y(i), w = tile_w(i), h = tile_h(i);
        if (moved(current, old, width, x, y, w, h, 0, 0)) { continue; }
        dirty.push_back(static_cast<std::uint32_t>(i)); is_dirty[i] = 1U;
        left = std::min(left, x); top = std::min(top, y);
        right = std::max(right, x + w); bottom = std::max(bottom, y + h);
    }
    if (dirty.size() < copy_min_tiles) { return std::nullopt; }

    struct Vote { std::int32_t dx {}, dy {}; std::uint32_t count {}; };
    std::vector<Vote> votes;
    if (now >= copy_retry_ && right - left >= hash_pixels) {
        // Sampled rolling hashes of the old damaged area. Eight consecutive
        // probe rows below always meet a sampled row at any vertical offset.
        copy_hashes_.assign(hash_slots, 0U);
        for (std::uint32_t y = top; y < bottom; y += hash_row_stride) {
            const auto* line = row_at(old, width, left, y);
            std::uint32_t hash = 0U, run = 0U, last = 0U;
            for (std::uint32_t x = 0; x < right - left; ++x) {
                const auto pixel = rgb_at(line + static_cast<std::size_t>(x) * 4U);
                run = x && pixel == last ? run + 1U : 1U; last = pixel;
                if (x >= hash_pixels) { hash -= rgb_at(line + static_cast<std::size_t>(x - hash_pixels) * 4U) * hash_top; }
                hash = hash * hash_base + pixel;
                if (x + 1U >= hash_pixels && run < hash_pixels) {
                    copy_hashes_[hash_slot(hash)] = (static_cast<std::uint64_t>(hash) << 32U) |
                        (static_cast<std::uint64_t>(left + x + 1U - hash_pixels) << 16U) | y;
                }
            }
        }
        const auto stride = std::max<std::size_t>(1U, dirty.size() / 32U);
        for (std::size_t n = 0; n < dirty.size(); n += stride) {
            const auto i = dirty[n];
            const auto w = tile_w(i), h = tile_h(i);
            if (w < hash_pixels || h < hash_row_stride) { continue; }
            for (std::uint32_t column = 0; column < 3U; ++column) {
                const auto x = tile_x(i) + (w - hash_pixels) * column / 2U;
                const auto first = tile_y(i) + (h - hash_row_stride) / 2U;
                for (std::uint32_t y = first; y < first + hash_row_stride; ++y) {
                    const auto* probe = row_at(current, width, x, y);
                    std::uint32_t hash = 0U;
                    bool uniform = true;
                    for (std::uint32_t p = 0; p < hash_pixels; ++p) {
                        const auto pixel = rgb_at(probe + static_cast<std::size_t>(p) * 4U);
                        uniform = uniform && pixel == rgb_at(probe);
                        hash = hash * hash_base + pixel;
                    }
                    if (uniform) { continue; }
                    const auto entry = copy_hashes_[hash_slot(hash)];
                    if (!entry || static_cast<std::uint32_t>(entry >> 32U) != hash) { continue; }
                    const auto ox = static_cast<std::uint32_t>((entry >> 16U) & 0xffffU), oy = static_cast<std::uint32_t>(entry & 0xffffU);
                    const auto dx = static_cast<std::int32_t>(x) - static_cast<std::int32_t>(ox);
                    const auto dy = static_cast<std::int32_t>(y) - static_cast<std::int32_t>(oy);
                    if (!dx && !dy) { continue; }
                    auto vote = std::find_if(votes.begin(), votes.end(), [&](const Vote& v) { return v.dx == dx && v.dy == dy; });
                    if (vote != votes.end()) { ++vote->count; }
                    else if (votes.size() < 64U) { votes.push_back(Vote {dx, dy, 1U}); }
                }
            }
        }
        // Scrolling moves along one axis. Searching the same column and row is
        // cheap and survives the repeated text which defeats the sparse table.
        const auto add_vote = [&votes](std::int32_t dx, std::int32_t dy) {
            auto vote = std::find_if(votes.begin(), votes.end(), [&](const Vote& v) { return v.dx == dx && v.dy == dy; });
            if (vote != votes.end()) { ++vote->count; }
            else if (votes.size() < 64U) { votes.push_back(Vote {dx, dy, 1U}); }
        };
        std::uint32_t axis_probes = 0U;
        for (std::size_t n = 0; n < dirty.size() && axis_probes < 16U; n += stride) {
            const auto i = dirty[n];
            const auto w = tile_w(i), h = tile_h(i);
            if (w < hash_pixels) { continue; }
            const auto x = tile_x(i), y = tile_y(i) + h / 2U;
            const auto* probe = row_at(current, width, x, y);
            bool uniform = true;
            for (std::uint32_t p = 1U; p < hash_pixels && uniform; ++p) { uniform = rgb_at(probe + static_cast<std::size_t>(p) * 4U) == rgb_at(probe); }
            if (uniform) { continue; }
            ++axis_probes;
            for (std::uint32_t oy = top; oy < bottom; ++oy) {
                if (oy != y && std::memcmp(probe, row_at(old, width, x, oy), hash_pixels * 4U) == 0) {
                    add_vote(0, static_cast<std::int32_t>(y) - static_cast<std::int32_t>(oy));
                }
            }
            for (std::uint32_t ox = left; ox + hash_pixels <= right; ++ox) {
                if (ox != x && std::memcmp(probe, row_at(old, width, ox, y), hash_pixels * 4U) == 0) {
                    add_vote(static_cast<std::int32_t>(x) - static_cast<std::int32_t>(ox), 0);
                }
            }
        }
        std::sort(votes.begin(), votes.end(), [](const Vote& a, const Vote& b) { return a.count > b.count; });
        if (votes.size() > 3U) { votes.resize(3U); }
    }
    if (copy_vector_ && std::none_of(votes.begin(), votes.end(), [this](const Vote& v) {
            return v.dx == copy_vector_->first && v.dy == copy_vector_->second; })) {
        votes.push_back(Vote {copy_vector_->first, copy_vector_->second, 0U});
    }
    const auto in_bounds = [&](std::size_t i, std::int32_t dx, std::int32_t dy) {
        const auto sx = static_cast<std::int64_t>(tile_x(i)) - dx, sy = static_cast<std::int64_t>(tile_y(i)) - dy;
        return sx >= 0 && sy >= 0 && sx + tile_w(i) <= width && sy + tile_h(i) <= height;
    };
    std::vector<std::uint8_t> matched, best_matched;
    std::uint32_t best = 0U; std::int32_t best_dx = 0, best_dy = 0;
    for (const auto& vote : votes) {
        matched.assign(tiles_.size(), 0U);
        std::uint32_t score = 0U;
        for (const auto i : dirty) {
            if (in_bounds(i, vote.dx, vote.dy) && moved(current, old, width, tile_x(i), tile_y(i), tile_w(i), tile_h(i), vote.dx, vote.dy)) {
                matched[i] = 1U; ++score;
            }
        }
        if (score > best) { best = score; best_dx = vote.dx; best_dy = vote.dy; best_matched.swap(matched); }
    }
    if (best < copy_min_tiles) {
        // A fade or an unrelated repaint cannot keep paying for the search.
        if (++copy_misses_ >= 3U) { copy_retry_ = now + std::chrono::milliseconds(250); copy_misses_ = 0U; }
        copy_vector_.reset();
        return std::nullopt;
    }
    std::uint32_t c0 = cols, r0 = rows, c1 = 0, r1 = 0;
    for (std::size_t i = 0; i < best_matched.size(); ++i) {
        if (!best_matched[i]) { continue; }
        const auto c = static_cast<std::uint32_t>(i % cols), r = static_cast<std::uint32_t>(i / cols);
        c0 = std::min(c0, c); r0 = std::min(r0, r); c1 = std::max(c1, c + 1U); r1 = std::max(r1, r + 1U);
    }
    // The rectangle may cross damage which will be repainted anyway, but must
    // not disturb a clean exact tile or a tile which is currently video.
    for (unsigned attempt = 0; ; ++attempt) {
        if (attempt >= 16U) { return std::nullopt; }
        bool conflict = false; std::uint32_t cc = 0, cr = 0;
        for (std::uint32_t r = r0; r < r1 && !conflict; ++r) {
            for (std::uint32_t c = c0; c < c1 && !conflict; ++c) {
                const std::size_t i = static_cast<std::size_t>(r) * cols + c;
                if (best_matched[i] || is_dirty[i]) { continue; }
                const bool ok = now >= tiles_[i].busy_until && in_bounds(i, best_dx, best_dy) &&
                    moved(current, old, width, tile_x(i), tile_y(i), tile_w(i), tile_h(i), best_dx, best_dy);
                if (!ok) { conflict = true; cc = c; cr = r; }
            }
        }
        if (!conflict) { break; }
        const auto kept = [&](std::uint32_t a, std::uint32_t b, std::uint32_t d, std::uint32_t e) {
            std::uint32_t total = 0U;
            for (std::uint32_t r = b; r < e; ++r) {
                for (std::uint32_t c = a; c < d; ++c) { total += best_matched[static_cast<std::size_t>(r) * cols + c]; }
            }
            return total;
        };
        const std::uint32_t options[4] = {kept(cc + 1U, r0, c1, r1), kept(c0, r0, cc, r1), kept(c0, cr + 1U, c1, r1), kept(c0, r0, c1, cr)};
        const auto choice = static_cast<std::size_t>(std::max_element(options, options + 4) - options);
        if (options[choice] < copy_min_tiles) { return std::nullopt; }
        if (choice == 0U) { c0 = cc + 1U; } else if (choice == 1U) { c1 = cc; }
        else if (choice == 2U) { r0 = cr + 1U; } else { r1 = cr; }
    }
    std::int64_t x0 = static_cast<std::int64_t>(c0) * tile_edge, y0 = static_cast<std::int64_t>(r0) * tile_edge;
    std::int64_t x1 = std::min<std::int64_t>(static_cast<std::int64_t>(c1) * tile_edge, width);
    std::int64_t y1 = std::min<std::int64_t>(static_cast<std::int64_t>(r1) * tile_edge, height);
    // Keep the source inside the surface, then follow the real moved edge into
    // neighbouring tiles so a window border does not need a repaint.
    x0 = std::max<std::int64_t>(x0, best_dx); y0 = std::max<std::int64_t>(y0, best_dy);
    x1 = std::min<std::int64_t>(x1, static_cast<std::int64_t>(width) + best_dx);
    y1 = std::min<std::int64_t>(y1, static_cast<std::int64_t>(height) + best_dy);
    if (x1 <= x0 || y1 <= y0) { return std::nullopt; }
    const auto row_matches = [&](std::int64_t y) {
        return y >= 0 && y < height && y - best_dy >= 0 && y - best_dy < height &&
            std::memcmp(row_at(current, width, x0, y), row_at(old, width, x0 - best_dx, y - best_dy),
                        static_cast<std::size_t>(x1 - x0) * 4U) == 0;
    };
    const auto column_matches = [&](std::int64_t x) {
        if (x < 0 || x >= width || x - best_dx < 0 || x - best_dx >= width) { return false; }
        for (std::int64_t y = y0; y < y1; ++y) {
            if (std::memcmp(row_at(current, width, x, y), row_at(old, width, x - best_dx, y - best_dy), 4U) != 0) { return false; }
        }
        return true;
    };
    for (unsigned n = 1U; n < tile_edge && row_matches(y0 - 1); ++n) { --y0; }
    for (unsigned n = 1U; n < tile_edge && row_matches(y1); ++n) { ++y1; }
    for (unsigned n = 1U; n < tile_edge && column_matches(x0 - 1); ++n) { --x0; }
    for (unsigned n = 1U; n < tile_edge && column_matches(x1); ++n) { ++x1; }
    copy_misses_ = 0U;
    copy_vector_ = std::make_pair(best_dx, best_dy);
    return Copy {static_cast<std::uint32_t>(x0), static_cast<std::uint32_t>(y0), static_cast<std::uint32_t>(x1 - x0),
                 static_cast<std::uint32_t>(y1 - y0), static_cast<std::uint32_t>(x0 - best_dx), static_cast<std::uint32_t>(y0 - best_dy)};
}

void LosslessUi::update(Pixels pixels, std::uint32_t width, std::uint32_t height,
                        Clock::time_point now, std::uint32_t bitrate_kbps, const Send& send) {
    if (!pixels || width == 0 || height == 0 || width > 16384 || height > 16384 ||
        static_cast<std::uint64_t>(width) * height > max_pixels ||
        pixels->size() != static_cast<std::size_t>(width) * height * 4U) {
        if (width_ != 0) { (void) emit(reset, 0, 0, 0, 0, {}, send); }
        disable(); return;
    }
    const auto cols = (width + tile_edge - 1U) / tile_edge;
    const auto rows = (height + tile_edge - 1U) / tile_edge;
    current_ = pixels.get();
    if (reset_needed_ || width != width_ || height != height_) {
        width_ = width; height_ = height; ++epoch_;
        if (epoch_ == 0) { ++epoch_; }
        tiles_.assign(static_cast<std::size_t>(cols) * rows, Tile {});
        if (copies_) {
            shadow_.assign(static_cast<std::size_t>(width) * height * 4U, 0U);
            shadow_valid_.assign(static_cast<std::size_t>(width) * height, 0U);
        } else {
            shadow_.clear(); shadow_valid_.clear();
        }
        previous_.reset(); reset_needed_ = false;
        mask_.reset(); copy_vector_.reset();
        credit_ = 16384.0; credit_at_ = heartbeat_at_ = now;
        if (!emit(reset, 0, 0, 0, 0, {}, send)) {
            next_wakeup_ = now + std::chrono::milliseconds(10); return;
        }
        scene_pending_ = false;
    }
    if (advanced_ && scene_pending_) {
        if (!emit(scene, 0, 0, 0, 0, {}, send)) {
            next_wakeup_ = now + std::chrono::milliseconds(10); return;
        }
        scene_pending_ = false;
    }
    next_wakeup_.reset();
    const auto wake_by = [this](Clock::time_point deadline) {
        if (!next_wakeup_ || deadline < *next_wakeup_) { next_wakeup_ = deadline; }
    };
    const auto in_focus = [&](std::uint32_t x, std::uint32_t y, std::uint32_t w, std::uint32_t h) {
        return focus_ && now < focus_until_ && x + w / 2 >= focus_->x &&
            x + w / 2 < focus_->x + focus_->width && y + h / 2 >= focus_->y &&
            y + h / 2 < focus_->y + focus_->height;
    };
    std::vector<std::size_t> interactive, urgent, background;
    unsigned cleared = 0;
    // 0: outside the copy, 1: the copy alone yields this tile's new pixels,
    // 2: the copy touches this tile but the viewer still needs a repaint.
    enum : std::uint8_t { copy_none = 0U, copy_exact = 1U, copy_repaint = 2U };
    std::vector<std::uint8_t> copy_state;
    std::optional<Copy> moved_area;
    // A large repaint keeps its fence: mixed capture times otherwise show old
    // and new 64-pixel squares. Small responses have no such neighbours.
    bool input_response_area = false;
    if (copies_ && advanced_ && previous_ && pixels != previous_ && input_generation_ && now < input_until_) {
        std::size_t damaged = 0U;
        for (std::size_t i = 0; i < tiles_.size() && damaged <= input_response_tiles; ++i) {
            if (now < tiles_[i].busy_until) { continue; }
            const auto x = static_cast<std::uint32_t>(i % cols) * tile_edge, y = static_cast<std::uint32_t>(i / cols) * tile_edge;
            damaged += !moved(*pixels, *previous_, width, x, y, std::min<std::uint32_t>(tile_edge, width - x),
                              std::min<std::uint32_t>(tile_edge, height - y), 0, 0);
        }
        input_response_area = damaged && damaged <= input_response_tiles;
    }
    if (copies_ && advanced_ && previous_ && pixels != previous_ &&
        (moved_area = find_copy(*pixels, width, height, cols, rows, now))) {
        const auto& area = *moved_area;
        const auto dx = static_cast<std::int64_t>(area.x) - area.src_x, dy = static_cast<std::int64_t>(area.y) - area.src_y;
        copy_state.assign(tiles_.size(), copy_none);
        std::uint32_t exact = 0U;
        for (std::uint32_t r = area.y / tile_edge; r <= (area.y + area.height - 1U) / tile_edge; ++r) {
            for (std::uint32_t c = area.x / tile_edge; c <= (area.x + area.width - 1U) / tile_edge; ++c) {
                const std::size_t i = static_cast<std::size_t>(r) * cols + c;
                const auto x = c * tile_edge, y = r * tile_edge;
                const auto w = std::min<std::uint32_t>(tile_edge, width - x), h = std::min<std::uint32_t>(tile_edge, height - y);
                const auto ix0 = std::max(x, area.x), iy0 = std::max(y, area.y);
                const auto ix1 = std::min(x + w, area.x + area.width), iy1 = std::min(y + h, area.y + area.height);
                // Predict from what the viewer really holds, pixel by pixel. Pixels
                // it cannot show (never sent, cleared or moved from such) fail here.
                const auto stride = static_cast<std::size_t>(width);
                const auto same = [&](std::int64_t px, std::int64_t py, std::int64_t sx, std::int64_t sy, std::int64_t n) {
                    return !n || (std::memcmp(pixels->data() + (static_cast<std::size_t>(py) * stride + static_cast<std::size_t>(px)) * 4U,
                                              shadow_.data() + (static_cast<std::size_t>(sy) * stride + static_cast<std::size_t>(sx)) * 4U,
                                              static_cast<std::size_t>(n) * 4U) == 0 &&
                                  std::memchr(shadow_valid_.data() + static_cast<std::size_t>(sy) * stride + static_cast<std::size_t>(sx), 0,
                                              static_cast<std::size_t>(n)) == nullptr);
                };
                bool explained = true;
                for (std::uint32_t row = y; row < y + h && explained; ++row) {
                    if (row < iy0 || row >= iy1) {
                        explained = same(x, row, x, row, w);
                        continue;
                    }
                    explained = same(x, row, x, row, static_cast<std::int64_t>(ix0) - x) &&
                        same(ix0, row, static_cast<std::int64_t>(ix0) - dx, static_cast<std::int64_t>(row) - dy, static_cast<std::int64_t>(ix1) - ix0) &&
                        same(ix1, row, ix1, row, static_cast<std::int64_t>(x + w) - ix1);
                }
                copy_state[i] = explained ? copy_exact : copy_repaint;
                exact += explained;
            }
        }
        // Copy only for a real saving; otherwise this tick is an ordinary repaint.
        if (exact < copy_min_tiles) {
            moved_area.reset(); copy_state.clear();
        } else {
            std::vector<std::uint8_t> payload;
            put16(payload, static_cast<std::uint16_t>(area.src_x)); put16(payload, static_cast<std::uint16_t>(area.src_y));
            if (!emit(copy, static_cast<std::uint16_t>(area.x), static_cast<std::uint16_t>(area.y),
                      static_cast<std::uint16_t>(area.width), static_cast<std::uint16_t>(area.height), payload, send)) {
                next_wakeup_ = now + std::chrono::milliseconds(10); return;
            }
            ++stats_.copies; stats_.copied_tiles += exact;
            copy_until_ = now + std::chrono::milliseconds(300);
            near_left_ = std::min<std::int64_t>(area.x, area.src_x) - tile_edge;
            near_top_ = std::min<std::int64_t>(area.y, area.src_y) - tile_edge;
            near_right_ = std::max<std::int64_t>(area.x, area.src_x) + area.width + tile_edge;
            near_bottom_ = std::max<std::int64_t>(area.y, area.src_y) + area.height + tile_edge;
            if (!mask_) { mask_ = std::make_shared<const std::vector<std::uint8_t>>(); }
        }
    }
    for (std::size_t i = 0; i < tiles_.size(); ++i) {
        auto& t = tiles_[i];
        const auto x = static_cast<std::uint32_t>(i % cols) * tile_edge;
        const auto y = static_cast<std::uint32_t>(i / cols) * tile_edge;
        const auto w = std::min<std::uint32_t>(tile_edge, width - x);
        const auto h = std::min<std::uint32_t>(tile_edge, height - y);
        // A sparse counter/edge can share the movie's timing even when it
        // changes too slowly to become a busy tile itself. Do not keep an
        // obsolete bitmap over newer video there. Explicit text focus still
        // retains its small-edit path; unrelated quiet UI is unaffected.
        const bool near_motion = region_ && x < region_->x + region_->width + tile_edge &&
            region_->x < x + w + tile_edge && y < region_->y + region_->height + tile_edge &&
            region_->y < y + h + tile_edge;
        if (t.band_video && now >= t.busy_until) { t.band_video = false; t.band_return = true; }
        const std::uint8_t copied = copy_state.empty() ? static_cast<std::uint8_t>(copy_none) : copy_state[i];
        bool changed = !previous_;
        bool outside_focus = false;
        std::uint32_t damage = 0;
        const auto sparse_limit = w * h / 8U;
        const auto minor_limit = std::max(1U, w * h / 64U);
        if (copied == copy_exact) {
            // The viewer derives exactly these pixels from the copy command.
            t.active = true; t.dirty = false; t.urgent = false; t.sparse_dirty = false;
            t.focus_confined = false; t.copy_band = false; t.band_return = false; t.incompressible = false;
            t.dirty_since = {}; t.pending.clear();
        } else if (previous_ && pixels != previous_) {
            for (std::uint32_t row = 0; row < h && damage <= sparse_limit; ++row) {
                const auto offset = (static_cast<std::size_t>(y + row) * width + x) * 4U;
                if (std::memcmp(pixels->data() + offset, previous_->data() + offset, w * 4U) == 0) { continue; }
                for (std::uint32_t col = 0; col < w && damage <= sparse_limit; ++col) {
                    const auto at = offset + col * 4U;
                    // X/alpha bytes are not part of our opaque RGB output.
                    if (std::memcmp(pixels->data() + at, previous_->data() + at, 3U) != 0) {
                        changed = true;
                        ++damage;
                        outside_focus = outside_focus || !focus_ || now >= focus_until_ ||
                            x + col < focus_->x || x + col >= focus_->x + focus_->width ||
                            y + row < focus_->y || y + row >= focus_->y + focus_->height;
                    }
                }
            }
        }
        // The copy painted over this tile even when its own new pixels equal
        // the old ones. The worker must no longer treat that bitmap as exact.
        if (copied == copy_repaint) { changed = true; t.active = true; }
        const bool near_copy = static_cast<std::int64_t>(x) < near_right_ && near_left_ < static_cast<std::int64_t>(x + w) &&
            static_cast<std::int64_t>(y) < near_bottom_ && near_top_ < static_cast<std::int64_t>(y + h);
        // With history a tile waiting for bandwidth stays cheap and is re-encoded fresh,
        // so a capture without a usable vector in the middle of motion need not start
        // treating the band around a window as video.
        const bool copy_band = (moved_area || (history_ && now < copy_until_)) && changed && copied != copy_exact && near_copy;
        const bool input_tile = input_response_area && changed && t.input_served != input_generation_;
        if (input_tile) { t.input_served = input_generation_; }
        const bool focused_change = previous_ && changed && damage <= sparse_limit && !outside_focus;
        const bool sparse = focused_change &&
            (now >= t.busy_until || now - t.dense_changed >= settle_delay);
        if ((copy_band || input_tile) && now >= t.busy_until) {
            // One exposed strip per copy is not video motion: repeated bands
            // at a window or viewport edge must not turn it into a busy tile.
            if (!t.dirty) { t.dirty_since = now; t.band_copies = stats_.copies; }
            t.streak = 0U; t.dense_streak = 0U; t.changed = now;
            t.urgent = true; t.sparse_dirty = true; t.focus_confined = false; t.copy_band = true;
            t.pending.clear(); t.incompressible = false; t.dirty = true;
        } else if (changed) {
            t.copy_band = false; t.band_return = false;
            if (!t.dirty) { t.dirty_since = now; }
            t.streak = t.changed != Clock::time_point {} && now - t.changed < std::chrono::milliseconds(75)
                ? t.streak + 1U : 1U;
            t.urgent = static_cast<bool>(previous_);
            t.sparse_dirty = previous_ && damage <= sparse_limit &&
                ((damage <= minor_limit && !near_motion) || focused_change);
            t.focus_confined = focused_change;
            if (previous_ && damage > minor_limit && !sparse) {
                t.dense_streak = t.dense_changed != Clock::time_point {} &&
                    now - t.dense_changed < motion_hold ? t.dense_streak + 1U : 1U;
                t.dense_changed = now;
            }
            t.changed = now; t.pending.clear(); t.incompressible = false; t.dirty = true;
            // Fast typing/caret changes touch little of a tile. Frequency
            // alone must not turn them into video and stall exact input
            // behind the 250 ms motion-settling window.
            if (sparse) { t.busy_until = {}; }
            else if (t.streak >= 3U || (previous_ && damage > minor_limit && t.dense_streak >= 2U)) {
                // Scrolled text that fell behind is not a playing clip: once it
                // stops it should not wait a full second to become exact again.
                t.band_video = t.band_video || (near_copy && now < copy_until_);
                t.busy_until = now + (t.band_video ? copy_band_hold : motion_hold);
            }
        }
        // An exposed strip which the exact budget could not deliver in time
        // is motion the path cannot afford as pixels. Video must take it;
        // the crop cannot otherwise cover a tile that never becomes busy.
        // Waiting suits one isolated notch; while copies keep arriving the strips
        // only get further behind, so hand them over after the ordinary delay.
        // With history it would be paid for twice: video here and then a repaint, plus a
        // clear each way, on a path that is already short. Only a very late tile moves.
        if (t.copy_band && t.dirty && (history_ ? now >= t.dirty_since + copy_band_patience * 5 :
                (now >= t.dirty_since + copy_band_patience ||
                 (stats_.copies - t.band_copies >= 2U && now >= t.dirty_since + settle_delay)))) {
            t.copy_band = false; t.sparse_dirty = false; t.band_video = true; t.busy_until = now + copy_band_hold;
        }
        if (t.refresh_layout && t.active) {
            if (!t.dirty) { t.dirty_since = now; }
            t.dirty = true; t.urgent = true; t.sparse_dirty = false;
        }
        if (now < t.busy_until) {
            wake_by(t.busy_until);
            ++stats_.busy_tiles;
            if (t.active) {
                if (cleared >= 32U) { wake_by(now); continue; }
                ++cleared;
                if (!emit(clear, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                          static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), {}, send)) { break; }
                t.active = false;
            }
            continue;
        }
        if ((!t.active || t.dirty) && !t.incompressible) {
            // Refine only a quiet background, never the first two pictures
            // of a moving region. Sparse foreground text/caret changes can
            // replace an existing exact bitmap without a blur/clear cycle.
            const bool foreground = (!previous_ && in_focus(x, y, w, h)) ||
                (t.sparse_dirty && t.focus_confined) || t.copy_band || t.band_return;
            if (!foreground && !(t.active && t.sparse_dirty) && now < t.changed + settle_delay) {
                wake_by(t.changed + settle_delay);
                continue;
            }
            // A newly typed character has priority over an unfinished idle
            // snapshot, even near the bottom of a large desktop.
            (foreground ? interactive : t.urgent ? urgent : background).push_back(i);
        }
    }
    region_.reset();
    std::uint32_t left = width, top = height, right = 0, bottom = 0;
    unsigned moving = 0;
    for (std::size_t i = 0; i < tiles_.size(); ++i) {
        const auto& t = tiles_[i];
        if (now >= t.busy_until && !t.incompressible) { continue; }
        const auto x = static_cast<std::uint32_t>(i % cols) * tile_edge;
        const auto y = static_cast<std::uint32_t>(i / cols) * tile_edge;
        left = std::min(left, x); top = std::min(top, y);
        right = std::max(right, std::min(width, x + tile_edge));
        bottom = std::max(bottom, std::min(height, y + tile_edge)); ++moving;
    }
    // A single bounding region keeps one hardware encoder/session. Generic
    // pixel classification includes every busy or incompressible tile;
    // uncovered desktop pixels continue through the exact layer.
    // Report every busy tile, including a small input field outside an
    // existing crop and motion covering the whole screen. The video thread
    // decides whether a crop is worthwhile; missing coverage is never safe.
    if (moving && right > left && bottom > top) {
        region_ = Region {left, top, right - left, bottom - top};
    }
    // Only the bounded recent keyboard-focus area may borrow video capacity.
    // Bulk snapshots and scroll repaints must retain the background share.
    // Copied pixels leave video idle unless something else is moving, so the
    // strips that complete a copy can use most of the path until they catch up.
    // This holds until the strips arrive: after the last copy they would
    // otherwise crawl at the background share while the screen stays stale.
    const bool band_waiting = std::any_of(interactive.begin(), interactive.end(), [this](std::size_t i) { return tiles_[i].copy_band || tiles_[i].band_return; });
    const bool responding = (now < copy_until_ || now < input_until_ || band_waiting) && !interactive.empty();
    const double share = responding && !region_ ? 0.75 : (borrow_ || responding) && !interactive.empty() ? 0.50 : 0.15;
    // While video is idle its target does not describe the path: video has its own
    // TCP connection and saw only a share of it. Delay on these records' own
    // acknowledgements does, and reports its measured rate here once it sees a queue.
    const bool video_idle = responding && !region_ && history_;
    double rate = (responding ? 1024.0 : 512.0) * 1024.0;
    if (!video_idle) { rate = std::min(rate, static_cast<double>(bitrate_kbps) * 125.0 * share); }
    if (delivery_limit_) { rate = std::min(rate, static_cast<double>(delivery_limit_)); }
    const double burst = std::clamp(static_cast<double>(bitrate_kbps) * 125.0 * .020,
                                   static_cast<double>(max_payload + header_size + 16U + 10U), 16384.0);
    credit_ = std::min(burst, credit_ + std::max(0.0, std::chrono::duration<double>(now - credit_at_).count()) * rate);
    credit_at_ = now;
    if (history_ && pixel_history_.exhausted()) { reset_needed_ = true; }
    // Tile pixels in history order: R, G and B, whatever the capture's byte order.
    const auto load_history_pixels = [&](std::uint32_t x, std::uint32_t y, std::uint32_t w, std::uint32_t h) {
        history_pixels_.resize(static_cast<std::size_t>(w) * h);
        for (std::uint32_t row = 0; row < h; ++row) {
            const auto* src = pixels->data() + (static_cast<std::size_t>(y + row) * width + x) * 4U;
            auto* out = history_pixels_.data() + static_cast<std::size_t>(row) * w;
            for (std::uint32_t col = 0; col < w; ++col, src += 4U) {
                out[col] = static_cast<std::uint32_t>(src[2]) | (static_cast<std::uint32_t>(src[1]) << 8U) |
                    (static_cast<std::uint32_t>(src[0]) << 16U);
            }
        }
    };
    if (!reset_needed_) {
        interactive.insert(interactive.end(), urgent.begin(), urgent.end());
        interactive.insert(interactive.end(), background.begin(), background.end());
        unsigned published = 0;
        for (const auto i : interactive) {
            auto& t = tiles_[i];
            const auto x = static_cast<std::uint32_t>(i % cols) * tile_edge;
            const auto y = static_cast<std::uint32_t>(i / cols) * tile_edge;
            const auto w = std::min<std::uint32_t>(tile_edge, width - x);
            const auto h = std::min<std::uint32_t>(tile_edge, height - y);
            // Other tiles sent since then moved every distance in these tokens.
            // A tile that could not afford half of its last cost need not recode yet.
            const bool outdated = history_ && !t.pending.empty() && t.pending_position != pixel_history_.position();
            if (history_ && (t.pending.empty() || (outdated && credit_ >= static_cast<double>(t.pending.size() / 2U)))) {
                load_history_pixels(x, y, w, h);
                pixel_history_.encode(history_pixels_, history_tokens_);
                std::vector<std::uint8_t> compressed(compressBound(static_cast<uLong>(history_tokens_.size())) + 4U);
                auto size = static_cast<uLongf>(compressed.size() - 4U);
                const int result = compress2(compressed.data() + 4U, &size, history_tokens_.data(),
                                             static_cast<uLong>(history_tokens_.size()), 1);
                if (result != Z_OK || size + 4U > max_payload) {
                    t.pending.clear(); t.incompressible = true;
                    if (t.active && !emit(clear, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                        static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), {}, send)) { break; }
                    t.active = false; continue;
                }
                const auto inflated = static_cast<std::uint32_t>(history_tokens_.size());
                for (std::size_t n = 0; n < 4U; ++n) { compressed[n] = static_cast<std::uint8_t>(inflated >> (24U - 8U * n)); }
                compressed.resize(size + 4U);
                t.pending = std::move(compressed);
                t.pending_position = pixel_history_.position();
                t.encoding = encoding_history;
            } else if (t.pending.empty()) {
                std::vector<std::uint8_t> rgba(static_cast<std::size_t>(w) * h * 4U);
                bool grayscale = true;
                for (std::uint32_t row = 0; row < h; ++row) {
                    for (std::uint32_t col = 0; col < w; ++col) {
                        const auto src = (static_cast<std::size_t>(y + row) * width + x + col) * 4U;
                        const auto dst = (static_cast<std::size_t>(row) * w + col) * 4U;
                        rgba[dst] = (*pixels)[src + 2]; rgba[dst + 1] = (*pixels)[src + 1];
                        rgba[dst + 2] = (*pixels)[src]; rgba[dst + 3] = 255;
                        grayscale = grayscale && rgba[dst] == rgba[dst + 1] && rgba[dst] == rgba[dst + 2];
                    }
                }
                // Neutral text/background needs one exact sample per pixel,
                // rather than repeating identical R/G/B and an opaque alpha.
                t.encoding = static_cast<std::uint8_t>(grayscale);
                if (grayscale) {
                    const auto count = rgba.size() / 4U;
                    for (std::size_t n = 0; n < count; ++n) { rgba[n] = rgba[n * 4U]; }
                    rgba.resize(count);
                }
                t.pending.resize(compressBound(static_cast<uLong>(rgba.size())));
                auto size = static_cast<uLongf>(t.pending.size());
                const int result = compress2(t.pending.data(), &size, rgba.data(), static_cast<uLong>(rgba.size()), 1);
                if (result != Z_OK || size > max_payload) {
                    t.pending.clear(); t.incompressible = true;
                    if (t.active && !emit(clear, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                        static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), {}, send)) { break; }
                    t.active = false; continue;
                }
                t.pending.resize(size);
            }
            const auto cost = header_size + (capture_ ? advanced_ ? 16U : 8U : 0U) + t.pending.size();
            if (credit_ < static_cast<double>(cost)) {
                if (rate > 0) {
                    const auto refill = std::chrono::ceil<Clock::duration>(
                        std::chrono::duration<double>((static_cast<double>(cost) - credit_) / rate));
                    wake_by(now + std::max(Clock::duration(1), refill));
                }
                // An obsolete exact tile must not hide newer video while
                // its replacement waits for bandwidth.
                if (t.active && t.dirty && (!t.sparse_dirty || now >= t.dirty_since + (t.copy_band ? copy_band_patience : settle_delay))) {
                    if (!emit(clear, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                        static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), {}, send)) { break; }
                    t.active = false;
                }
                if (t.active && t.sparse_dirty) { wake_by(t.dirty_since + (t.copy_band ? copy_band_patience : settle_delay)); }
                // Preserve credit for the oldest foreground tile. Small
                // background patches cannot continually spend its refill.
                if (t.urgent || in_focus(x, y, w, h)) { break; }
                continue;
            }
            // A field can start halfway through a tile. Only changed pixels
            // must lie in the unoccluded focus area; unchanged neighbouring
            // pixels keep their exact previously displayed values.
            const bool early = advanced_ && early_ && layout_ && t.active && t.sparse_dirty && t.focus_confined && !t.refresh_layout;
            if (!emit(patch, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                      static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), t.pending, send,
                      static_cast<std::uint8_t>(t.encoding | (early ? 2U : 0U) | (t.copy_band || t.band_return ? 4U : 0U)))) { break; }
            if (history_) {
                load_history_pixels(x, y, w, h);
                pixel_history_.commit(history_pixels_);
            }
            t.copy_band = false; t.band_return = false;
            t.active = true; t.dirty = false; t.urgent = false; t.sparse_dirty = false;
            t.dirty_since = {};
            t.early_history = t.early_history || early; t.refresh_layout = false;
            t.published = now; t.pending.clear();
            if (++published >= 16U) {
                // Continue a bounded batch immediately; its next pass will
                // calculate the exact refill deadline if credit runs out.
                wake_by(now); break;
            }
        }
    }
    // Only fresh exact tiles may cover video. Clear unserved old patches
    // even when a large repaint exhausted this tick's encoding allowance.
    for (std::size_t i = 0; i < tiles_.size() && !reset_needed_; ++i) {
        auto& t = tiles_[i];
        if (!t.active || !t.dirty) { continue; }
        // A small pending text edit must not erase all unchanged glyphs.
        // Bound the grace period so an unavailable exact sender cannot hide
        // live video indefinitely.
        const auto grace = t.copy_band ? copy_band_patience : settle_delay;
        if (t.sparse_dirty && now < t.dirty_since + grace) {
            wake_by(t.dirty_since + grace);
            continue;
        }
        const auto x = static_cast<std::uint32_t>(i % cols) * tile_edge;
        const auto y = static_cast<std::uint32_t>(i / cols) * tile_edge;
        // A large damage burst must not reset unrelated static text or
        // discard every viewer's bitmap cache. Continue bounded clears on
        // the next immediate pass; only transport loss requires a reset.
        if (cleared >= 32U) { wake_by(now); continue; }
        ++cleared;
        if (!emit(clear, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
            static_cast<std::uint16_t>(std::min<std::uint32_t>(tile_edge, width - x)),
            static_cast<std::uint16_t>(std::min<std::uint32_t>(tile_edge, height - y)), {}, send)) { break; }
        t.active = false;
    }
    if (reset_needed_) {
        (void) emit(reset, 0, 0, 0, 0, {}, send);
        next_wakeup_ = now + std::chrono::milliseconds(10);
    }
    previous_ = std::move(pixels);
    if (mask_) {
        std::vector<std::uint8_t> exact(tiles_.size());
        for (std::size_t i = 0; i < tiles_.size(); ++i) {
            const auto& t = tiles_[i];
            exact[i] = t.active && !t.dirty && !t.incompressible && now >= t.busy_until;
        }
        if (*mask_ != exact) { mask_ = std::make_shared<const std::vector<std::uint8_t>>(std::move(exact)); }
    }
    if (!reset_needed_ && now - heartbeat_at_ >= std::chrono::milliseconds(200)) {
        heartbeat_at_ = now;
        (void) emit(heartbeat, 0, 0, 0, 0, {}, send);
    }
    if (reset_needed_) { next_wakeup_ = now + std::chrono::milliseconds(10); }
    else { wake_by(heartbeat_at_ + std::chrono::milliseconds(200)); }
}

} // namespace qmdp
