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
    next_wakeup_.reset();
}

bool LosslessUi::emit(Op op, std::uint16_t x, std::uint16_t y, std::uint16_t w,
                     std::uint16_t h, std::span<const std::uint8_t> payload, const Send& send,
                     std::uint8_t encoding) {
    std::vector<std::uint8_t> b {'Q', 'U', 'I', '1', static_cast<std::uint8_t>(op), encoding, 0, 0};
    if (capture_) { b[7] = 8; }
    put32(b, epoch_); put32(b, ++sequence_);
    put16(b, static_cast<std::uint16_t>(width_)); put16(b, static_cast<std::uint16_t>(height_));
    put16(b, x); put16(b, y); put16(b, w); put16(b, h);
    if (capture_) { put32(b, static_cast<std::uint32_t>(capture_ >> 32U)); put32(b, static_cast<std::uint32_t>(capture_)); }
    b.insert(b.end(), payload.begin(), payload.end());
    if (!send(b)) { reset_needed_ = true; return false; }
    stats_.bytes += b.size();
    if (op == patch) { ++stats_.patches; credit_ -= static_cast<double>(b.size()); }
    if (op == clear) { ++stats_.clears; }
    if (op == reset) { ++stats_.resets; }
    return true;
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
    if (reset_needed_ || width != width_ || height != height_) {
        width_ = width; height_ = height; ++epoch_;
        if (epoch_ == 0) { ++epoch_; }
        tiles_.assign(static_cast<std::size_t>(cols) * rows, Tile {});
        previous_.reset(); reset_needed_ = false;
        credit_ = 16384.0; credit_at_ = heartbeat_at_ = now;
        if (!emit(reset, 0, 0, 0, 0, {}, send)) {
            next_wakeup_ = now + std::chrono::milliseconds(10); return;
        }
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
    for (std::size_t i = 0; i < tiles_.size(); ++i) {
        auto& t = tiles_[i];
        const auto x = static_cast<std::uint32_t>(i % cols) * tile_edge;
        const auto y = static_cast<std::uint32_t>(i / cols) * tile_edge;
        const auto w = std::min<std::uint32_t>(tile_edge, width - x);
        const auto h = std::min<std::uint32_t>(tile_edge, height - y);
        bool changed = !previous_;
        std::uint32_t damage = 0;
        const auto sparse_limit = w * h / 8U;
        if (previous_ && pixels != previous_) {
            for (std::uint32_t row = 0; row < h && damage <= sparse_limit; ++row) {
                const auto offset = (static_cast<std::size_t>(y + row) * width + x) * 4U;
                if (std::memcmp(pixels->data() + offset, previous_->data() + offset, w * 4U) == 0) { continue; }
                changed = true;
                for (std::uint32_t col = 0; col < w && damage <= sparse_limit; ++col) {
                    const auto at = offset + col * 4U;
                    if (std::memcmp(pixels->data() + at, previous_->data() + at, 4U) != 0) {
                        ++damage;
                    }
                }
            }
        }
        const bool sparse = previous_ && changed && damage <= sparse_limit && now >= t.busy_until && in_focus(x, y, w, h);
        if (changed) {
            t.streak = t.changed != Clock::time_point {} && now - t.changed < std::chrono::milliseconds(75)
                ? t.streak + 1U : 1U;
            t.urgent = static_cast<bool>(previous_);
            t.changed = now; t.pending.clear(); t.incompressible = false; t.dirty = true;
            // Fast typing/caret changes touch little of a tile. Frequency
            // alone must not turn them into video and stall exact input
            // behind the 250 ms motion-settling window.
            if (sparse) { t.busy_until = {}; }
            else if (t.streak >= 3U) { t.busy_until = now + std::chrono::milliseconds(250); }
        }
        if (now < t.busy_until) {
            wake_by(t.busy_until);
            ++stats_.busy_tiles;
            if (t.active) {
                if (++cleared > 32U) { reset_needed_ = true; break; }
                if (!emit(clear, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                          static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), {}, send)) { break; }
                t.active = false;
            }
            continue;
        }
        if ((!t.active || t.dirty) && !t.incompressible) {
            // A newly typed character has priority over an unfinished idle
            // snapshot, even near the bottom of a large desktop.
            (in_focus(x, y, w, h) ? interactive : t.urgent ? urgent : background).push_back(i);
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
    // Foreground changes and a substantial snapshot may borrow half the
    // common budget. Every emitted byte is also charged to VideoBudget;
    // frame admission, rather than encoder quality, pays for the burst.
    const double share = !interactive.empty() || !urgent.empty() || background.size() > 16 ? 0.50 : 0.15;
    const double rate = std::min(512.0 * 1024.0, static_cast<double>(bitrate_kbps) * 125.0 * share);
    credit_ = std::min(16384.0, credit_ + std::max(0.0, std::chrono::duration<double>(now - credit_at_).count()) * rate);
    credit_at_ = now;
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
            if (t.pending.empty()) {
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
            const auto cost = header_size + t.pending.size();
            if (credit_ < static_cast<double>(cost)) {
                if (rate > 0) {
                    const auto refill = std::chrono::ceil<Clock::duration>(
                        std::chrono::duration<double>((static_cast<double>(cost) - credit_) / rate));
                    wake_by(now + std::max(Clock::duration(1), refill));
                }
                // An obsolete exact tile must not hide newer video while
                // its replacement waits for bandwidth.
                if (t.active && t.dirty) {
                    if (!emit(clear, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                        static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), {}, send)) { break; }
                    t.active = false;
                }
                // Preserve credit for the oldest foreground tile. Small
                // background patches cannot continually spend its refill.
                if (t.urgent || in_focus(x, y, w, h)) { break; }
                continue;
            }
            if (!emit(patch, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                      static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), t.pending, send, t.encoding)) { break; }
            t.active = true; t.dirty = false; t.urgent = false; t.pending.clear();
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
        const auto x = static_cast<std::uint32_t>(i % cols) * tile_edge;
        const auto y = static_cast<std::uint32_t>(i / cols) * tile_edge;
        if (++cleared > 32U) { reset_needed_ = true; break; }
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
    if (!reset_needed_ && now - heartbeat_at_ >= std::chrono::milliseconds(200)) {
        heartbeat_at_ = now;
        (void) emit(heartbeat, 0, 0, 0, 0, {}, send);
    }
    if (reset_needed_) { next_wakeup_ = now + std::chrono::milliseconds(10); }
    else { wake_by(heartbeat_at_ + std::chrono::milliseconds(200)); }
}

} // namespace qmdp
