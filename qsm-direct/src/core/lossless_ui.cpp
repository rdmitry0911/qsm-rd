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

std::vector<std::uint8_t> LosslessUi::greeting() {
    std::vector<std::uint8_t> b(header_size, 0U);
    b[0] = 'Q'; b[1] = 'U'; b[2] = 'I'; b[3] = '1';
    return b;
}

void LosslessUi::disable() {
    previous_.reset(); tiles_.clear(); width_ = height_ = 0;
    reset_needed_ = true;
}

bool LosslessUi::emit(Op op, std::uint16_t x, std::uint16_t y, std::uint16_t w,
                     std::uint16_t h, std::span<const std::uint8_t> payload, const Send& send,
                     std::uint8_t encoding) {
    std::vector<std::uint8_t> b {'Q', 'U', 'I', '1', static_cast<std::uint8_t>(op), encoding, 0, 0};
    put32(b, epoch_); put32(b, ++sequence_);
    put16(b, static_cast<std::uint16_t>(width_)); put16(b, static_cast<std::uint16_t>(height_));
    put16(b, x); put16(b, y); put16(b, w); put16(b, h);
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
        if (!emit(reset, 0, 0, 0, 0, {}, send)) { return; }
    }
    // Reserve a modest UI share, with a bounded burst. Video accounts for
    // these bytes too: this is not an unmetered second desktop stream.
    const double rate = std::min(128.0 * 1024.0, static_cast<double>(bitrate_kbps) * 125.0 * 0.15);
    credit_ = std::min(16384.0, credit_ + std::max(0.0, std::chrono::duration<double>(now - credit_at_).count()) * rate);
    credit_at_ = now;
    std::vector<std::size_t> urgent, background;
    unsigned cleared = 0;
    for (std::size_t i = 0; i < tiles_.size(); ++i) {
        auto& t = tiles_[i];
        const auto x = static_cast<std::uint32_t>(i % cols) * tile_edge;
        const auto y = static_cast<std::uint32_t>(i / cols) * tile_edge;
        const auto w = std::min<std::uint32_t>(tile_edge, width - x);
        const auto h = std::min<std::uint32_t>(tile_edge, height - y);
        bool changed = !previous_;
        if (previous_ && pixels != previous_) {
            for (std::uint32_t row = 0; row < h && !changed; ++row) {
                const auto offset = (static_cast<std::size_t>(y + row) * width + x) * 4U;
                changed = std::memcmp(pixels->data() + offset, previous_->data() + offset, w * 4U) != 0;
            }
        }
        if (changed) {
            t.streak = t.changed != Clock::time_point {} && now - t.changed < std::chrono::milliseconds(75)
                ? t.streak + 1U : 1U;
            t.changed = now; t.pending.clear(); t.incompressible = false; t.dirty = true;
            if (t.streak >= 3U) { t.busy_until = now + std::chrono::milliseconds(250); }
        }
        if (now < t.busy_until) {
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
            ((changed || (t.active && t.dirty)) ? urgent : background).push_back(i);
        }
    }
    if (!reset_needed_) {
        urgent.insert(urgent.end(), background.begin(), background.end());
        unsigned published = 0;
        for (const auto i : urgent) {
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
                // An obsolete exact tile must not hide newer video while
                // its replacement waits for bandwidth.
                if (t.active && t.dirty) {
                    if (!emit(clear, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                        static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), {}, send)) { break; }
                    t.active = false;
                }
                continue;
            }
            if (!emit(patch, static_cast<std::uint16_t>(x), static_cast<std::uint16_t>(y),
                      static_cast<std::uint16_t>(w), static_cast<std::uint16_t>(h), t.pending, send, t.encoding)) { break; }
            t.active = true; t.dirty = false; t.pending.clear();
            if (++published >= 16U) { break; }
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
    if (reset_needed_) { (void) emit(reset, 0, 0, 0, 0, {}, send); }
    previous_ = std::move(pixels);
    if (!reset_needed_ && now - heartbeat_at_ >= std::chrono::milliseconds(200)) {
        heartbeat_at_ = now;
        (void) emit(heartbeat, 0, 0, 0, 0, {}, send);
    }
}

} // namespace qmdp
