// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

namespace qmdp {

// Exact pixels every viewer has already received, in the order they were sent.
//
// A tile of text costs little once the same glyphs have been sent: rows of a
// glyph repeat at a fixed distance, but only across messages, which per-tile
// deflate cannot see. Records reference this history by distance, so it needs
// the ordered reliable channel and one record order for every viewer.
//
// Encoded tokens, before deflate:
//   u32 checksum, u16 ops size, u16 distances size, ops, distances, R..., G-R..., B-R...
// ops alternate varint literal count and varint match length, ending once all
// pixels are produced; each match takes one varint distance. A source may
// overlap the pixels being produced. Pixels are 0xBBGGRR.
class PixelHistory {
public:
    // About four 720p screens, since each record repeats a whole tile.
    static constexpr std::uint32_t capacity = 1U << 22U;
    static constexpr std::uint32_t max_tile_pixels = 4096U;
    // A decoder writes the current tile over the oldest pixels, so a source lies
    // at most this far behind the tile's start, and a tile further from its own pixel.
    static constexpr std::uint32_t max_distance = capacity - 2U * max_tile_pixels;
    static constexpr std::uint32_t min_match = 4U;
    static constexpr unsigned search_depth = 16U;

    void reset() {
        ring_.resize(capacity); previous_.resize(capacity); heads_.assign(hash_size, 0U);
        position_ = 0U;
    }
    [[nodiscard]] std::uint32_t position() const noexcept { return position_; }
    // Positions are relative to the epoch; a new epoch restarts them long before this.
    [[nodiscard]] bool exhausted() const noexcept { return position_ >= 0x7fff0000U; }

    static std::uint32_t checksum(std::span<const std::uint32_t> pixels) noexcept {
        std::uint32_t sum = 2166136261U;
        for (const auto pixel : pixels) { sum = (sum ^ pixel) * 16777619U; }
        return sum;
    }

    // Reads the history only: tokens stay valid until the next commit().
    void encode(std::span<const std::uint32_t> pixels, std::vector<std::uint8_t>& out) {
        const auto count = static_cast<std::uint32_t>(pixels.size());
        ops_.clear(); distances_.clear(); literals_.clear();
        std::fill(local_heads_.begin(), local_heads_.end(), std::uint16_t {0});
        const auto at = [&](std::uint32_t absolute) {
            return absolute < position_ ? ring_[absolute & (capacity - 1U)] : pixels[absolute - position_];
        };
        std::uint32_t i = 0U, run = 0U;
        const auto insert_local = [&](std::uint32_t n) {
            if (n + 2U >= count) { return; }
            const auto slot = hash(pixels[n], pixels[n + 1U], pixels[n + 2U]) >> local_shift;
            local_previous_[n] = local_heads_[slot];
            local_heads_[slot] = static_cast<std::uint16_t>(n + 1U);
        };
        while (i < count) {
            std::uint32_t best = 0U, best_distance = 0U;
            const auto left = count - i;
            if (left >= min_match) {
                const auto current = position_ + i;
                const auto consider = [&](std::uint32_t source) {
                    if (best && (best >= left || at(source + best) != pixels[i + best])) { return; }
                    std::uint32_t length = 0U;
                    while (length < left && at(source + length) == pixels[i + length]) { ++length; }
                    if (length > best) { best = length; best_distance = current - source; }
                };
                const auto key = hash(pixels[i], pixels[i + 1U], pixels[i + 2U]);
                unsigned tries = search_depth;
                for (auto link = local_heads_[key >> local_shift]; link && tries && best < left; --tries) {
                    consider(position_ + link - 1U);
                    link = local_previous_[link - 1U];
                }
                tries = search_depth;
                for (auto link = heads_[key >> hash_shift]; link && tries && best < left; --tries) {
                    const auto source = link - 1U;
                    if (source >= position_ || position_ - source > max_distance) { break; }
                    consider(source);
                    const auto next = previous_[source & (capacity - 1U)];
                    if (next >= link) { break; }
                    link = next;
                }
            }
            if (best >= min_match) {
                varint(ops_, run); varint(ops_, best); varint(distances_, best_distance); run = 0U;
                for (std::uint32_t n = 0U; n < best; ++n) { insert_local(i + n); }
                i += best;
            } else {
                literals_.push_back(pixels[i]); ++run;
                insert_local(i); ++i;
            }
        }
        // The decoder stops once every pixel is produced, so a final match has no count after it.
        if (run) { varint(ops_, run); }
        out.clear();
        out.reserve(8U + ops_.size() + distances_.size() + literals_.size() * 3U);
        const auto sum = checksum(pixels);
        for (const unsigned shift : {24U, 16U, 8U, 0U}) { out.push_back(static_cast<std::uint8_t>(sum >> shift)); }
        out.push_back(static_cast<std::uint8_t>(ops_.size() >> 8U)); out.push_back(static_cast<std::uint8_t>(ops_.size()));
        out.push_back(static_cast<std::uint8_t>(distances_.size() >> 8U)); out.push_back(static_cast<std::uint8_t>(distances_.size()));
        out.insert(out.end(), ops_.begin(), ops_.end());
        out.insert(out.end(), distances_.begin(), distances_.end());
        // Neighbouring text pixels share a value per channel, and neutral text across them.
        for (const auto pixel : literals_) { out.push_back(static_cast<std::uint8_t>(pixel)); }
        for (const unsigned shift : {8U, 16U}) {
            for (const auto pixel : literals_) { out.push_back(static_cast<std::uint8_t>((pixel >> shift) - pixel)); }
        }
    }

    // These pixels have been sent; every viewer now holds them at this position.
    void commit(std::span<const std::uint32_t> pixels) {
        const auto count = static_cast<std::uint32_t>(pixels.size());
        for (std::uint32_t n = 0U; n < count; ++n) {
            const auto slot = (position_ + n) & (capacity - 1U);
            ring_[slot] = pixels[n];
        }
        for (std::uint32_t n = 0U; n + 2U < count; ++n) {
            const auto key = hash(pixels[n], pixels[n + 1U], pixels[n + 2U]) >> hash_shift;
            const auto absolute = position_ + n;
            previous_[absolute & (capacity - 1U)] = heads_[key];
            heads_[key] = absolute + 1U;
        }
        position_ += count;
    }

private:
    static constexpr std::uint32_t hash_size = 1U << 16U;
    static constexpr unsigned hash_shift = 16U;
    static constexpr unsigned local_shift = 20U;

    static std::uint32_t hash(std::uint32_t a, std::uint32_t b, std::uint32_t c) noexcept {
        auto h = a * 0x9E3779B1U;
        h = (h ^ b) * 0x85EBCA77U;
        return (h ^ c) * 0xC2B2AE3DU;
    }
    static void varint(std::vector<std::uint8_t>& out, std::uint32_t n) {
        while (n >= 128U) { out.push_back(static_cast<std::uint8_t>(n | 128U)); n >>= 7U; }
        out.push_back(static_cast<std::uint8_t>(n));
    }

    std::vector<std::uint32_t> ring_, previous_, heads_;
    std::uint32_t position_ {};
    std::vector<std::uint8_t> ops_, distances_;
    std::vector<std::uint32_t> literals_;
    std::vector<std::uint16_t> local_heads_ = std::vector<std::uint16_t>(1U << (32U - local_shift));
    std::vector<std::uint16_t> local_previous_ = std::vector<std::uint16_t>(max_tile_pixels);
};

} // namespace qmdp
