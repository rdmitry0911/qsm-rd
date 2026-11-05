// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/lossless_ui.hpp"
#include "core/video_envelope.hpp"
#include <algorithm>
#include <array>
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <iostream>
#include <random>
#include <zlib.h>

using UI = qmdp::LosslessUi;
using Bytes = std::vector<std::uint8_t>;
static unsigned u16(const Bytes& p, std::size_t i) { return static_cast<unsigned>(p[i]) * 256U + p[i + 1]; }
static Bytes pixels(unsigned w, unsigned h, unsigned color = 240) {
    Bytes b(static_cast<std::size_t>(w) * h * 4U, 255);
    for (std::size_t i = 0; i < b.size(); i += 4) {
        b[i] = static_cast<std::uint8_t>(color); b[i + 1] = 17; b[i + 2] = 91;
    }
    return b;
}
static void publish_stable(UI& ui, UI::Pixels image, unsigned width, unsigned height,
                           UI::Clock::time_point now, unsigned rate, const UI::Send& send) {
    ui.update(image, width, height, now - UI::settle_delay, rate, send);
    ui.update(image, width, height, now, rate, send);
}

namespace copy_oracle {
// The viewer's side of core/pixel_history.hpp: fails on any disagreement.
struct HistoryDecoder {
    static constexpr std::uint32_t capacity = qmdp::PixelHistory::capacity;
    std::vector<std::uint32_t> ring = std::vector<std::uint32_t>(capacity);
    std::uint64_t position = 0;
    std::vector<std::uint32_t> decode(const Bytes& t, std::size_t count) {
        assert(t.size() >= 8U);
        const auto ops_size = static_cast<std::size_t>(u16(t, 4)), distances_size = static_cast<std::size_t>(u16(t, 6));
        const std::size_t ops_end = 8U + ops_size, literals_at = ops_end + distances_size;
        assert(t.size() >= literals_at && (t.size() - literals_at) % 3U == 0U);
        const auto literals = (t.size() - literals_at) / 3U;
        std::size_t op = 8U, distance = ops_end, literal = 0U, produced = 0U;
        const auto varint = [&](std::size_t& at, std::size_t end) {
            std::uint32_t value = 0U; unsigned shift = 0U; std::uint8_t byte = 0U;
            do { assert(at < end && shift <= 21U); byte = t[at++]; value |= static_cast<std::uint32_t>(byte & 127U) << shift; shift += 7U; } while (byte & 128U);
            return value;
        };
        while (produced < count) {
            const auto run = varint(op, ops_end);
            assert(run <= count - produced && literal + run <= literals);
            for (std::uint32_t i = 0; i < run; ++i, ++literal) {
                const std::uint8_t red = t[literals_at + literal];
                ring[(position + produced + i) & (capacity - 1U)] = red |
                    (static_cast<std::uint32_t>(static_cast<std::uint8_t>(t[literals_at + literals + literal] + red)) << 8U) |
                    (static_cast<std::uint32_t>(static_cast<std::uint8_t>(t[literals_at + 2U * literals + literal] + red)) << 16U);
            }
            produced += run;
            if (produced >= count) { break; }
            const auto length = varint(op, ops_end);
            const auto back = varint(distance, literals_at);
            assert(length && length <= count - produced && back && back <= position + produced && back <= capacity - 4096U);
            for (std::uint32_t i = 0; i < length; ++i) {
                const auto at = position + produced + i;
                ring[at & (capacity - 1U)] = ring[(at - back) & (capacity - 1U)];
            }
            produced += length;
        }
        assert(op == ops_end && distance == literals_at && literal == literals);
        std::vector<std::uint32_t> pixels(count);
        for (std::size_t i = 0; i < count; ++i) { pixels[i] = ring[(position + i) & (capacity - 1U)]; }
        const auto sum = (static_cast<std::uint32_t>(u16(t, 0)) << 16U) | u16(t, 2);
        assert(sum == qmdp::PixelHistory::checksum(pixels));
        position += count;
        return pixels;
    }
};
// Applies the wire records as a viewer does. Transparent pixels show video.
struct Viewer {
    unsigned width, height;
    Bytes rgba;
    HistoryDecoder history;
    std::uint64_t patch_bytes = 0, copies = 0, unfenced = 0, history_records = 0;
    Viewer(unsigned w, unsigned h) : width(w), height(h), rgba(static_cast<std::size_t>(w) * h * 4U, 0) {}
    void apply(const Bytes& p) {
        const auto op = p[4], flags = p[5];
        const std::size_t at = 28U + p[7];
        const auto x = u16(p, 20), y = u16(p, 22), w = u16(p, 24), h = u16(p, 26);
        if (op == UI::reset) { std::fill(rgba.begin(), rgba.end(), 0); history = HistoryDecoder {}; }
        else if (op == UI::patch && (flags & 8U)) {
            assert(!(flags & 1U) && p.size() > at + 4U);
            const auto size = (static_cast<std::size_t>(u16(p, at)) << 16U) | u16(p, at + 2U);
            Bytes tokens(size);
            auto out = static_cast<uLongf>(tokens.size());
            assert(uncompress(tokens.data(), &out, p.data() + at + 4U, static_cast<uLong>(p.size() - at - 4U)) == Z_OK && out == size);
            const auto decoded = history.decode(tokens, static_cast<std::size_t>(w) * h);
            for (unsigned r = 0; r < h; ++r) {
                for (unsigned c = 0; c < w; ++c) {
                    auto* to = rgba.data() + ((y + r) * width + x + c) * 4U;
                    const auto value = decoded[r * w + c];
                    to[0] = static_cast<std::uint8_t>(value); to[1] = static_cast<std::uint8_t>(value >> 8U);
                    to[2] = static_cast<std::uint8_t>(value >> 16U); to[3] = 255U;
                }
            }
            patch_bytes += p.size(); ++history_records;
            unfenced += (flags & 4U) != 0U;
        }
        else if (op == UI::clear) {
            for (unsigned r = y; r < y + h; ++r) { std::fill_n(rgba.begin() + (r * width + x) * 4U, w * 4U, 0); }
        } else if (op == UI::copy) {
            assert(!flags && p.size() == at + 4U);
            const auto sx = u16(p, at), sy = u16(p, at + 2U);
            assert(x + w <= width && y + h <= height && sx + w <= width && sy + h <= height);
            const Bytes before = rgba;
            for (unsigned r = 0; r < h; ++r) {
                std::copy_n(before.begin() + ((sy + r) * width + sx) * 4U, w * 4U, rgba.begin() + ((y + r) * width + x) * 4U);
            }
            ++copies;
        } else if (op == UI::patch) {
            const bool gray = flags & 1U;
            Bytes raw(static_cast<std::size_t>(w) * h * (gray ? 1U : 4U));
            auto size = static_cast<uLongf>(raw.size());
            assert(uncompress(raw.data(), &size, p.data() + at, static_cast<uLong>(p.size() - at)) == Z_OK);
            for (unsigned r = 0; r < h; ++r) {
                for (unsigned c = 0; c < w; ++c) {
                    auto* out = rgba.data() + ((y + r) * width + x + c) * 4U;
                    if (gray) { out[0] = out[1] = out[2] = raw[r * w + c]; out[3] = 255; }
                    else { std::copy_n(raw.data() + (r * w + c) * 4U, 4U, out); }
                }
            }
            patch_bytes += p.size();
            unfenced += (flags & 4U) != 0U;
        }
    }
    // Pixels the worker reports as exact must equal the desktop, in RGB.
    void verify(const Bytes& desktop, const UI::Mask& mask) const {
        if (!mask || mask->empty()) { return; }
        const auto cols = (width + 63U) / 64U;
        for (std::size_t i = 0; i < mask->size(); ++i) {
            if (!(*mask)[i]) { continue; }
            const auto x = static_cast<unsigned>(i % cols) * 64U, y = static_cast<unsigned>(i / cols) * 64U;
            for (unsigned r = y; r < std::min(y + 64U, height); ++r) {
                for (unsigned c = x; c < std::min(x + 64U, width); ++c) {
                    const auto* a = rgba.data() + (r * width + c) * 4U;
                    const auto* b = desktop.data() + (r * width + c) * 4U;
                    assert(a[3] == 255 && a[0] == b[2] && a[1] == b[1] && a[2] == b[0]);
                }
            }
        }
    }
};
// Glyph-like low-entropy page, addressed in page coordinates, so scrolling
// and moving windows reproduce exactly the same pixels at a new position.
static std::uint8_t page(unsigned x, unsigned y) {
    const auto line = y / 20U, cell = x / 9U;
    const auto mix = (line * 2654435761U) ^ (cell * 40503U);
    if (y % 20U >= 14U || x % 9U >= 7U || mix % 5U == 0U) { return 245U; }
    return ((mix >> ((y % 20U) % 13U + (x % 9U) * 2U)) & 1U) ? 20U : 245U;
}
struct Run {
    UI ui; Viewer viewer; std::vector<Bytes> sent;
    unsigned width, height; UI::Clock::time_point now = UI::Clock::now(); std::uint64_t capture = 1;
    Run(unsigned w, unsigned h, bool history = false) : viewer(w, h), width(w), height(h) { ui.advanced(true); ui.copies(true); ui.history(history); }
    void step(const Bytes& desktop, unsigned ms = 16U, unsigned rate = 20000U) {
        now += std::chrono::milliseconds(ms); ui.capture(++capture);
        ui.update(std::make_shared<const Bytes>(desktop), width, height, now, rate, [&](std::span<const std::uint8_t> p) {
            viewer.apply(Bytes(p.begin(), p.end())); return true;
        });
        viewer.verify(desktop, ui.exact_mask());
    }
    void settle(const Bytes& desktop) { for (unsigned n = 0; n < 40U; ++n) { step(desktop, 50U); } }
    void assert_exact(const Bytes& desktop) const {
        for (std::size_t i = 0; i < desktop.size(); i += 4) {
            assert(viewer.rgba[i + 3] == 255 && viewer.rgba[i] == desktop[i + 2] && viewer.rgba[i + 1] == desktop[i + 1] && viewer.rgba[i + 2] == desktop[i]);
        }
    }
};
static Bytes filled(unsigned w, unsigned h, std::uint8_t v) {
    Bytes b(static_cast<std::size_t>(w) * h * 4U, v);
    for (std::size_t i = 3; i < b.size(); i += 4) { b[i] = 255; }
    return b;
}
static void put(Bytes& b, unsigned width, unsigned x, unsigned y, std::uint8_t v) {
    auto* at = b.data() + (static_cast<std::size_t>(y) * width + x) * 4U; at[0] = at[1] = at[2] = v;
}
static Bytes scrolled(unsigned w, unsigned h, unsigned top, unsigned bottom, unsigned offset) {
    auto b = filled(w, h, 200);
    for (unsigned y = top; y < bottom; ++y) { for (unsigned x = 40; x < w - 40; ++x) { put(b, w, x, y, page(x, y - top + offset)); } }
    return b;
}
static Bytes dragged(unsigned w, unsigned h, unsigned wx, unsigned wy, unsigned ww, unsigned wh) {
    auto b = filled(w, h, 90);
    for (unsigned y = 0; y < wh; ++y) { for (unsigned x = 0; x < ww; ++x) { put(b, w, wx + x, wy + y, page(x, y)); } }
    return b;
}
static void test_scroll() {
    const unsigned w = 960, h = 640; Run run(w, h);
    auto d = scrolled(w, h, 30, 610, 0);
    run.settle(d); run.assert_exact(d);
    const auto before = run.viewer.patch_bytes;
    for (unsigned n = 1; n <= 30; ++n) { d = scrolled(w, h, 30, 610, n * 20U); run.step(d, 32U); }
    assert(run.viewer.copies >= 25U);
    assert(run.viewer.unfenced > 0U);
    assert(run.ui.stats().copied_tiles > 25U * 60U);
    run.settle(d); run.assert_exact(d);
    // Each 20-row step exposes one 880-pixel-wide strip, not the full page.
    assert(run.viewer.patch_bytes - before < 30U * 40000U);
}
static void test_window_drag() {
    const unsigned w = 960, h = 640; Run run(w, h);
    auto d = dragged(w, h, 100, 80, 420, 300);
    run.settle(d); run.assert_exact(d);
    unsigned x = 100, y = 80;
    for (unsigned n = 0; n < 40; ++n) { x += 7U; y += 3U; d = dragged(w, h, x, y, 420, 300); run.step(d); }
    assert(run.viewer.copies >= 30U);
    // A moving exact window must not become video.
    assert(!run.ui.motion_region());
    run.settle(d); run.assert_exact(d);
}
static void test_unaffordable_strips_fall_back_to_video() {
    // Text behind the window makes each exposed strip expensive at 2 Mbit/s.
    const unsigned w = 960, h = 640; Run run(w, h);
    const auto compose = [&](unsigned wx, unsigned wy) {
        auto b = filled(w, h, 90);
        for (unsigned y = 0; y < h; ++y) { for (unsigned x = 0; x < w; ++x) { put(b, w, x, y, page(x + 500U, y + 500U)); } }
        for (unsigned y = 0; y < 300; ++y) { for (unsigned x = 0; x < 420; ++x) { put(b, w, wx + x, wy + y, page(x, y)); } }
        return b;
    };
    auto d = compose(100, 80);
    run.settle(d); run.assert_exact(d);
    bool video = false;
    unsigned x = 100, y = 80;
    for (unsigned n = 0; n < 40; ++n) {
        x += 9U; y += 4U; d = compose(x, y);
        run.step(d, 16U, 2000U);
        video = video || run.ui.motion_region().has_value();
    }
    assert(video && "an unaffordable strip must not wait outside the video crop");
    // A late strip at the edge cannot stop the interior from moving by copy.
    assert(run.viewer.copies >= 30U);
    const auto strips = run.viewer.unfenced;
    // Once motion stops the area leaves video well before the clip hold of one second.
    unsigned quiet = 0U;
    while (run.ui.motion_region() && quiet < 100U) { run.step(d, 20U, 2000U); ++quiet; }
    assert(quiet * 20U <= 500U);
    // Returning from video completes the motion: it is not fenced and uses the
    // idle path, so the whole area is exact well before a background refinement.
    unsigned finished = 0U;
    const auto exact_now = [&] {
        for (std::size_t i = 0; i < d.size(); i += 4) {
            if (run.viewer.rgba[i + 3] != 255 || run.viewer.rgba[i] != d[i + 2] || run.viewer.rgba[i + 1] != d[i + 1] || run.viewer.rgba[i + 2] != d[i]) { return false; }
        }
        return true;
    };
    while (!exact_now() && finished < 400U) { run.step(d, 20U, 2000U); ++finished; }
    assert(finished < 400U);
    assert(run.viewer.unfenced > strips);
    for (unsigned n = 0; n < 400U; ++n) { run.step(d, 50U, 2000U); }
    run.assert_exact(d);
}
static void test_input_response() {
    using std::chrono::milliseconds;
    const unsigned w = 640, h = 384;
    const auto square = [&](std::uint8_t v, unsigned size = 60U) {
        auto b = filled(w, h, 200);
        for (unsigned y = 100; y < 100U + size; ++y) { for (unsigned x = 100; x < 100U + size; ++x) { put(b, w, x, y, v); } }
        return b;
    };
    const auto bands = [](const Run& r, std::size_t from) {
        return r.viewer.unfenced - from;
    };
    Run run(w, h);
    auto d = square(10); run.settle(d); run.assert_exact(d);
    // Without an input event a dense change settles first.
    auto before = run.viewer.patch_bytes;
    d = square(20); run.step(d);
    assert(run.viewer.patch_bytes == before && run.viewer.unfenced == 0U);
    run.settle(d);
    // Repeated clicks on one button respond at once and stay exact.
    for (unsigned n = 1; n <= 6; ++n) {
        const auto from = run.viewer.unfenced;
        run.ui.input_event(n, run.now + milliseconds(300));
        d = square(static_cast<std::uint8_t>(30U + n * 5U));
        run.step(d, 100U);
        assert(bands(run, from) >= 1U);
        run.assert_exact(d);
        assert(!run.ui.motion_region());
    }
    // A second change during one event is motion: it follows the ordinary path.
    run.settle(d);
    run.ui.input_event(100, run.now + milliseconds(300));
    d = square(90); run.step(d, 16U);
    const auto served = run.viewer.unfenced;
    for (unsigned n = 0; n < 12U; ++n) { d = square(static_cast<std::uint8_t>(100U + n)); run.step(d, 16U); }
    assert(run.viewer.unfenced == served);
    assert(run.ui.motion_region());
    // An expired lease or a large repaint does not qualify.
    run.settle(d);
    run.ui.input_event(101, run.now - milliseconds(1));
    auto from = run.viewer.unfenced;
    d = square(140); run.step(d);
    assert(run.viewer.unfenced == from);
    run.settle(d);
    run.ui.input_event(102, run.now + milliseconds(300));
    from = run.viewer.unfenced;
    d = square(150, 380U); run.step(d);
    assert(run.viewer.unfenced == from);
    run.settle(d); run.assert_exact(d);
}
static void test_delivery_limit_sends_strips_to_video() {
    // The video target still claims 8 Mbit/s while the viewer's path delivers little.
    const unsigned w = 960, h = 640; Run run(w, h);
    const auto compose = [&](unsigned wx, unsigned wy) {
        auto b = filled(w, h, 90);
        for (unsigned y = 0; y < h; ++y) { for (unsigned x = 0; x < w; ++x) { put(b, w, x, y, page(x + 500U, y + 500U)); } }
        for (unsigned y = 0; y < 300; ++y) { for (unsigned x = 0; x < 420; ++x) { put(b, w, wx + x, wy + y, page(x, y)); } }
        return b;
    };
    auto d = compose(100, 80);
    run.settle(d); run.assert_exact(d);
    run.ui.delivery_limit(20000U);
    const auto before = run.viewer.patch_bytes;
    bool video = false;
    unsigned x = 100, y = 80;
    for (unsigned n = 0; n < 40; ++n) {
        x += 9U; y += 4U; d = compose(x, y);
        run.step(d, 16U, 8000U);
        video = video || run.ui.motion_region().has_value();
    }
    assert(video);
    assert(run.viewer.patch_bytes - before < 40000U && "the limit bounds exact bytes on the wire");
    run.ui.delivery_limit(0U);
    for (unsigned n = 0; n < 60U; ++n) { run.step(d, 50U, 8000U); }
    run.assert_exact(d);
}
static void test_noise_and_invalid_sources() {
    const unsigned w = 640, h = 384; Run run(w, h);
    std::mt19937 random(7);
    auto d = filled(w, h, 0);
    run.settle(d);
    // Random noise has no moved rectangle and stays on the ordinary path.
    for (unsigned n = 0; n < 8; ++n) {
        for (auto& v : d) { v = static_cast<std::uint8_t>(random()); }
        for (std::size_t i = 3; i < d.size(); i += 4) { d[i] = 255; }
        run.step(d);
    }
    assert(run.viewer.copies == 0U);
    // A source the viewer does not hold cannot produce a claim of exact pixels:
    // the page is new, not yet published, and immediately scrolls.
    Run fresh(w, h);
    auto a = filled(w, h, 200);
    fresh.settle(a);
    a = scrolled(w, h, 0, h, 0); fresh.step(a);
    a = scrolled(w, h, 0, h, 20); fresh.step(a);
    a = scrolled(w, h, 0, h, 40); fresh.step(a);
    fresh.settle(a); fresh.assert_exact(a);
}
static void test_clean_neighbours_survive() {
    // Two independent scrolling panes and a static column between them.
    const unsigned w = 960, h = 512; Run run(w, h);
    const auto compose = [&](unsigned a) {
        auto b = filled(w, h, 200);
        for (unsigned y = 0; y < h; ++y) {
            for (unsigned x = 0; x < 420; ++x) { put(b, w, x, y, page(x, y + a)); }
            for (unsigned x = 420; x < w; ++x) { put(b, w, x, y, page(x + 3000U, y)); }
        }
        return b;
    };
    auto d = compose(0);
    run.settle(d);
    for (unsigned n = 1; n <= 10; ++n) { d = compose(n * 40U); run.step(d, 32U); }
    assert(run.viewer.copies >= 5U);
    run.settle(d); run.assert_exact(d);
}
static void test_disabled_is_silent() {
    const unsigned w = 640, h = 384; Run run(w, h); run.ui.copies(false);
    auto d = scrolled(w, h, 0, h, 0); run.settle(d);
    for (unsigned n = 1; n <= 6; ++n) { d = scrolled(w, h, 0, h, n * 20U); run.step(d); }
    assert(run.viewer.copies == 0U && !run.ui.exact_mask());
}
// A record that references history round-trips, whatever repeats and however far back.
static void test_history_round_trip() {
    qmdp::PixelHistory encoder; encoder.reset();
    HistoryDecoder decoder;
    std::mt19937 random(7);
    std::vector<std::vector<std::uint32_t>> earlier;
    Bytes tokens;
    const auto send = [&](const std::vector<std::uint32_t>& tile) {
        encoder.encode(tile, tokens);
        // Tokens depend on the history, not on a discarded attempt: encoding twice is the same.
        Bytes again; encoder.encode(tile, again);
        assert(again == tokens);
        assert(decoder.decode(tokens, tile.size()) == tile);
        encoder.commit(tile);
        earlier.push_back(tile);
        return tokens.size();
    };
    // A uniform tile: one pixel, then a match overlapping itself.
    assert(send(std::vector<std::uint32_t>(4096, 0x332211U)) < 32U);
    std::vector<std::uint32_t> noise(4096);
    for (auto& v : noise) { v = random() & 0xffffffU; }
    assert(send(noise) >= 2U * 4096U);
    // The same tile again costs almost nothing.
    assert(send(noise) < 32U);
    // The second half of the last tile, shifted: most of it still matches.
    std::vector<std::uint32_t> shifted(noise.begin() + 2000, noise.end());
    shifted.resize(4096, 7U);
    assert(send(shifted) < 64U);
    // A small tile, and tiles shorter than one hash.
    assert(send({1U, 2U}) >= 8U); assert(send({}) == 8U); assert(send({1U, 2U, 3U, 1U, 2U, 3U, 1U}) > 8U);
    // Past the ring: references never reach pixels the viewer has overwritten.
    for (unsigned n = 0; n < 1100U; ++n) {
        std::vector<std::uint32_t> tile(4096);
        // Half new, half recent or very old, to place sources near the limit.
        for (auto& v : tile) { v = random() & 0xffffffU; }
        const auto& old = earlier[random() % earlier.size()];
        std::copy_n(old.begin(), std::min<std::size_t>(old.size(), 2048U), tile.begin());
        send(tile);
    }
    assert(encoder.position() > qmdp::PixelHistory::capacity);
    assert(decoder.position == encoder.position());
}
// Text from one font: a small bank of glyphs in lines that do not repeat.
static Bytes text_page(unsigned w, unsigned h, unsigned top, unsigned bottom, unsigned offset) {
    static const auto bank = [] {
        std::mt19937 random(11);
        std::vector<std::array<std::array<std::uint8_t, 3>, 9 * 15>> glyphs(60);
        for (auto& g : glyphs) {
            for (std::size_t n = 0; n < g.size(); ++n) {
                const auto col = n % 9U, row = n / 9U;
                const bool ink = col >= 1U && col < 8U && row >= 2U && row < 13U && random() % 3U == 0U;
                const auto edge = static_cast<std::uint8_t>(random() % 64U);
                // Subpixel-filtered ink: channels differ, as on an LCD-antialiased desktop.
                g[n] = ink ? std::array<std::uint8_t, 3> {static_cast<std::uint8_t>(20U + edge), static_cast<std::uint8_t>(40U + edge / 2U), static_cast<std::uint8_t>(30U)}
                           : std::array<std::uint8_t, 3> {255U, 255U, 255U};
            }
        }
        return glyphs;
    }();
    auto b = filled(w, h, 200);
    for (unsigned y = top; y < bottom; ++y) {
        const auto page_y = y - top + offset;
        const auto line = page_y / 19U, row = page_y % 19U;
        for (unsigned x = 40; x < w - 40; ++x) {
            const auto cell = (x - 40U) / 9U, col = (x - 40U) % 9U;
            const auto choice = ((line * 2654435761U) ^ (cell * 40503U)) >> 7U;
            auto* at = b.data() + (static_cast<std::size_t>(y) * w + x) * 4U;
            if (row >= 15U) { at[0] = at[1] = at[2] = 255U; continue; }
            const auto& pixel = bank[choice % bank.size()][row * 9U + col];
            at[2] = pixel[0]; at[1] = pixel[1]; at[0] = pixel[2];
        }
    }
    return b;
}
static void test_history_scroll_is_exact_and_much_cheaper() {
    const unsigned w = 960, h = 640;
    const auto run_scroll = [&](bool history) {
        Run run(w, h, history);
        auto d = text_page(w, h, 30, 610, 0);
        run.settle(d); run.assert_exact(d);
        for (unsigned n = 1; n <= 30; ++n) { d = text_page(w, h, 30, 610, n * 20U); run.step(d, 32U); }
        assert(run.viewer.copies >= 25U);
        run.settle(d); run.assert_exact(d);
        // Back to the first page: everything on it was sent.
        const auto before = run.viewer.patch_bytes;
        for (unsigned n = 30; n-- > 0;) { d = text_page(w, h, 30, 610, n * 20U); run.step(d, 32U); }
        run.settle(d); run.assert_exact(d);
        assert(history == (run.viewer.history_records > 0U));
        return std::pair {run.viewer.patch_bytes, run.viewer.patch_bytes - before};
    };
    const auto plain = run_scroll(false), history = run_scroll(true);
    std::cerr << "history scroll bytes " << history.first << " vs " << plain.first
              << ", back " << history.second << " vs " << plain.second << '\n';
    assert(history.first * 2U < plain.first);
    assert(history.second * 2U < plain.second);
}
static void test_idle_video_target_does_not_hold_strips() {
    // Copies leave video idle: its target says nothing about the path here, and 100
    // kbit/s is far below what these strips need. A reported delivery rate binds them.
    const unsigned w = 960, h = 640;
    const auto strips_reach_video = [&](bool history, unsigned limit) {
        Run run(w, h, history);
        auto d = text_page(w, h, 30, 610, 0);
        run.settle(d); run.assert_exact(d);
        run.ui.delivery_limit(limit);
        bool video = false;
        for (unsigned n = 1; n <= 40; ++n) {
            d = text_page(w, h, 30, 610, n * 20U);
            run.step(d, 32U, 100U);
            video = video || run.ui.motion_region().has_value();
        }
        run.ui.delivery_limit(0U);
        run.settle(d); run.assert_exact(d);
        return video;
    };
    assert(!strips_reach_video(true, 0U));
    assert(strips_reach_video(false, 0U) && "without history a low target still applies");
    assert(!strips_reach_video(true, 60000U) && "a strip a little late waits for bandwidth, fresh");
    assert(strips_reach_video(true, 2000U) && "copies stop being provable: this motion is beyond the path");
}
static void test_video_envelope_does_not_flip_on_a_drag() {
    using VideoEnvelope = qmdp::VideoEnvelope;
    using Clock = VideoEnvelope::Clock;
    using Region = UI::Region;
    const unsigned w = 1280, h = 768;
    const auto at = [start = Clock::now()](unsigned ms) { return start + std::chrono::milliseconds(ms); };
    {
        // A window moving across the screen: small motion, an envelope reaching all of it.
        VideoEnvelope e;
        std::optional<Region> last;
        unsigned changes = 0;
        for (unsigned n = 0; n < 200U; ++n) {
            const auto x = std::min(1024U, n * 8U);
            const auto r = e.select(at(n * 16U), n % 40U < 20U ? std::optional<Region>(Region {x / 64U * 64U, 64, 256, 640}) : std::nullopt, w, h, 192, 128);
            assert(r);
            if (!last || last->x != r->x || last->width != r->width) { ++changes; }
            last = r;
        }
        assert(last->x == 0U && last->width == w && "the envelope may reach the whole picture and stays a plane");
        assert(changes <= 25U);
    }
    {
        // Full-screen video from the start stays on the whole picture.
        VideoEnvelope e;
        for (unsigned n = 0; n < 100U; ++n) {
            assert(!e.select(at(n * 16U), Region {0, 0, w, h}, w, h, 192, 128));
        }
    }
    {
        // After a plane, only sustained motion over most of the desktop leaves it, and
        // it does not flip back at the first quiet tick.
        VideoEnvelope e;
        assert(e.select(at(0), Region {0, 0, 256, 256}, w, h, 192, 128));
        assert(e.select(at(100), Region {0, 0, w, h}, w, h, 192, 128) && "one busy tick is not full-screen video");
        assert(e.select(at(1000), Region {0, 0, w, h}, w, h, 192, 128));
        assert(!e.select(at(1100), Region {0, 0, w, h}, w, h, 192, 128));
        assert(!e.select(at(2000), std::nullopt, w, h, 192, 128));
        assert(!e.select(at(6000), Region {0, 0, 64, 64}, w, h, 192, 128));
        assert(e.select(at(6200), std::nullopt, w, h, 192, 128) && "the hold expired");
        // A short burst of most of the screen followed by quiet resets its timer.
        assert(e.select(at(6300), Region {0, 0, w, h}, w, h, 192, 128));
        assert(e.select(at(6400), Region {0, 0, 64, 64}, w, h, 192, 128));
        assert(e.select(at(7500), Region {0, 0, w, h}, w, h, 192, 128));
    }
    {
        // Too small to start a plane, then the encoder minimum applies.
        VideoEnvelope e;
        assert(!e.select(at(0), Region {0, 0, 64, 64}, w, h, 192, 128));
        const auto r = e.select(at(16), Region {1216, 704, 128, 128}, w, h, 192, 128);
        assert(r && r->width == 192U && r->x + r->width <= w);
        e.reset();
        assert(!e.select(at(32), std::nullopt, w, h, 192, 128));
    }
}
static void test_history_survives_budget_motion_and_reset() {
    // The worker encodes while tiles wait for credit: every record stays decodable.
    const unsigned w = 960, h = 640; Run run(w, h, true);
    const auto compose = [&](unsigned wx, unsigned wy) {
        auto b = filled(w, h, 90);
        for (unsigned y = 0; y < h; ++y) { for (unsigned x = 0; x < w; ++x) { put(b, w, x, y, page(x + 500U, y + 500U)); } }
        for (unsigned y = 0; y < 300; ++y) { for (unsigned x = 0; x < 420; ++x) { put(b, w, wx + x, wy + y, page(x, y)); } }
        return b;
    };
    auto d = compose(100, 80);
    run.settle(d); run.assert_exact(d);
    unsigned x = 100, y = 80;
    for (unsigned n = 0; n < 60; ++n) { x += 9U; y += 4U; d = compose(x, y); run.step(d, 16U, 2000U); }
    run.settle(d); run.assert_exact(d);
    // A new epoch restarts history on both sides.
    run.ui.resync(); run.step(d); run.settle(d); run.assert_exact(d);
    assert(run.viewer.history.position == 0U || run.viewer.history_records > 0U);
    // Stopping history restarts the epoch too: plain records follow.
    const auto records = run.viewer.history_records;
    run.ui.history(false); run.step(d); run.settle(d); run.assert_exact(d);
    assert(run.viewer.history_records == records);
}
} // namespace copy_oracle

int main() {
    copy_oracle::test_history_round_trip();
    copy_oracle::test_history_scroll_is_exact_and_much_cheaper();
    copy_oracle::test_idle_video_target_does_not_hold_strips();
    copy_oracle::test_video_envelope_does_not_flip_on_a_drag();
    copy_oracle::test_history_survives_budget_motion_and_reset();
    copy_oracle::test_scroll();
    copy_oracle::test_window_drag();
    copy_oracle::test_unaffordable_strips_fall_back_to_video();
    copy_oracle::test_input_response();
    copy_oracle::test_delivery_limit_sends_strips_to_video();
    copy_oracle::test_noise_and_invalid_sources();
    copy_oracle::test_clean_neighbours_survive();
    copy_oracle::test_disabled_is_silent();
    auto now = UI::Clock::now();
    UI ui; std::vector<Bytes> sent;
    auto sink = [&](std::span<const std::uint8_t> p) { sent.emplace_back(p.begin(), p.end()); return true; };
    auto b = pixels(65, 65);
    publish_stable(ui, std::make_shared<const Bytes>(b), 65, 65, now, 8000, sink);
    assert(sent.front()[4] == UI::reset);
    assert(ui.stats().patches == 4);
    for (const auto& p : sent) {
        if (p[4] != UI::patch) { continue; }
        const auto x = u16(p, 20), y = u16(p, 22), w = u16(p, 24), h = u16(p, 26);
        Bytes decoded(w * h * 4U); auto size = static_cast<uLongf>(decoded.size());
        assert(uncompress(decoded.data(), &size, p.data() + 28, static_cast<uLong>(p.size() - 28)) == Z_OK);
        assert(size == decoded.size());
        for (unsigned row = 0; row < h; ++row) {
            for (unsigned col = 0; col < w; ++col) {
                const auto src = ((y + row) * 65U + x + col) * 4U, dst = (row * w + col) * 4U;
                assert(decoded[dst] == b[src + 2] && decoded[dst + 1] == b[src + 1]);
                assert(decoded[dst + 2] == b[src] && decoded[dst + 3] == 255);
            }
        }
    }
    sent.clear();
    ui.update(std::make_shared<const Bytes>(b), 65, 65, now + std::chrono::milliseconds(210), 8000, sink);
    assert(sent.size() == 1 && sent[0][4] == UI::heartbeat);
    b[0] = 5; sent.clear();
    ui.update(std::make_shared<const Bytes>(b), 65, 65, now + std::chrono::milliseconds(300), 8000, sink);
    assert(ui.stats().patches == 5 && sent[0][4] == UI::patch);
    // Repeated motion relinquishes the overlay to video, then refines after settling.
    for (unsigned n = 1; n <= 8; ++n) {
        for (unsigned y = 0; y < 64; ++y) {
            for (unsigned x = 0; x < 64; ++x) { b[(y * 65 + x) * 4] = static_cast<std::uint8_t>(5 + n); }
        }
        ui.update(std::make_shared<const Bytes>(b), 65, 65, now + std::chrono::milliseconds(300 + n * 16), 8000, sink);
    }
    assert(ui.stats().clears > 0);
    const auto count = ui.stats().patches;
    ui.update(std::make_shared<const Bytes>(b), 65, 65, now + std::chrono::milliseconds(500), 8000, sink);
    assert(ui.stats().patches == count);
    ui.update(std::make_shared<const Bytes>(b), 65, 65, now + std::chrono::milliseconds(1500), 8000, sink);
    assert(ui.stats().patches == count + 1);
    sent.clear();
    ui.update(std::make_shared<const Bytes>(pixels(128, 64)), 128, 64, now + std::chrono::seconds(2), 8000, sink);
    assert(sent.front()[4] == UI::reset && u16(sent.front(), 16) == 128);
    // A lost local record forces a new snapshot rather than retaining an old tile.
    ui.resync(); bool dropped = false;
    publish_stable(ui, std::make_shared<const Bytes>(pixels(128, 64)), 128, 64, now + std::chrono::seconds(3), 8000,
        [&](std::span<const std::uint8_t> p) { if (p[4] == UI::patch && !dropped) { dropped = true; return false; } return sink(p); });
    assert(dropped);
    sent.clear();
    ui.update(std::make_shared<const Bytes>(pixels(128, 64)), 128, 64, now + std::chrono::seconds(4), 8000, sink);
    assert(sent.front()[4] == UI::reset);
    // Disable drops history; re-enable starts with a fresh geometry epoch.
    ui.disable(); sent.clear();
    ui.update(std::make_shared<const Bytes>(pixels(64, 64)), 64, 64, now + std::chrono::seconds(5), 500, sink);
    assert(sent.front()[4] == UI::reset && ui.stats().bytes > 0);
    // New input must bypass a still-incomplete desktop refinement.
    UI priority; auto large = pixels(1024, 1024); sent.clear();
    publish_stable(priority, std::make_shared<const Bytes>(large), 1024, 1024, now, 8000, sink);
    assert(priority.stats().patches == 16);
    priority.prioritize(UI::Region {960, 960, 64, 64}, now + std::chrono::seconds(1));
    large[large.size() - 4] = 7; sent.clear();
    priority.update(std::make_shared<const Bytes>(large), 1024, 1024,
                    now + std::chrono::milliseconds(20), 8000, sink);
    bool input_first = false;
    for (const auto& p : sent) {
        if (p[4] == UI::patch) { input_first = u16(p, 20) == 960 && u16(p, 22) == 960; break; }
    }
    assert(input_first);
    UI neutral; Bytes gray(64U * 64U * 4U, 255);
    for (std::size_t i = 0; i < gray.size(); i += 4) { gray[i] = gray[i + 1] = gray[i + 2] = 123; }
    sent.clear(); publish_stable(neutral, std::make_shared<const Bytes>(gray), 64, 64, now, 8000, sink);
    assert(sent.size() == 2 && sent[1][4] == UI::patch && sent[1][5] == 1);
    Bytes values(4096); auto count_gray = static_cast<uLongf>(values.size());
    assert(uncompress(values.data(), &count_gray, sent[1].data() + 28,
        static_cast<uLong>(sent[1].size() - 28)) == Z_OK);
    assert(count_gray == 4096 && std::all_of(values.begin(), values.end(), [](auto v) { return v == 123; }));
    // No capture polling is needed on an idle surface: only its lease is due.
    assert(neutral.next_wakeup() > now && neutral.next_wakeup() <= now + std::chrono::milliseconds(200));
    neutral.disable(); assert(!neutral.next_wakeup());
    // Large snapshots can continue bounded batches immediately, then sleep
    // until enough credit exists, even if no new capture damage arrives.
    UI scheduled; auto idle = std::make_shared<const Bytes>(pixels(1024, 1024));
    unsigned calls = 0;
    do {
        scheduled.update(idle, 1024, 1024, now, 500, sink);
        assert(scheduled.next_wakeup());
        assert(++calls < 30);
    } while (*scheduled.next_wakeup() <= now);
    assert(*scheduled.next_wakeup() < now + std::chrono::milliseconds(200));
    const auto before_refill = scheduled.stats().patches;
    scheduled.update(idle, 1024, 1024, *scheduled.next_wakeup(), 500, sink);
    assert(scheduled.stats().patches > before_refill);
    // Even after idle, a narrow-link snapshot must not emit 16 KiB at once
    // and stall the video producer for the duration of that burst.
    UI paced_snapshot; Bytes noise(512U * 64U * 4U, 255);
    std::minstd_rand random(123);
    for (std::size_t at = 0; at < noise.size(); at += 4) {
        const auto value = static_cast<std::uint8_t>(random());
        noise[at] = noise[at + 1] = noise[at + 2] = value;
    }
    auto snapshot = std::make_shared<const Bytes>(noise); sent.clear();
    publish_stable(paced_snapshot, snapshot, 512, 64, now, 1500, sink);
    std::size_t burst_bytes = 0;
    for (const auto& record : sent) { burst_bytes += record.size(); }
    assert(burst_bytes <= UI::header_size + 6190U);
    assert(paced_snapshot.stats().patches == 1);
    paced_snapshot.update(snapshot, 512, 64, now, 1500, sink);
    assert(paced_snapshot.stats().patches == 1);
    for (unsigned n = 1; n <= 8; ++n) {
        paced_snapshot.update(snapshot, 512, 64, now + std::chrono::milliseconds(n * 200), 1500, sink);
    }
    assert(paced_snapshot.stats().patches == 8);
    UI regions; auto moving = pixels(512, 256);
    for (unsigned frame = 0; frame < 6; ++frame) {
        for (unsigned y = 0; y < 128; ++y) {
            for (unsigned x = 256; x < 384; ++x) { moving[(y * 512 + x) * 4] = static_cast<std::uint8_t>(frame); }
        }
        regions.update(std::make_shared<const Bytes>(moving), 512, 256,
            now + std::chrono::milliseconds(frame * 16), 8000, sink);
    }
    assert(regions.motion_region());
    const auto area = *regions.motion_region();
    assert(area.x == 256 && area.y == 0 && area.width == 128 && area.height == 128);
    regions.update(std::make_shared<const Bytes>(moving), 512, 256,
        now + std::chrono::milliseconds(1200), 8000, sink);
    assert(!regions.motion_region());
    // Coverage hints must include a lone rapidly changing input tile and
    // full-screen motion; only the video planner may choose a full fallback.
    UI coverage; auto changes = pixels(512, 256);
    for (unsigned frame = 0; frame < 5; ++frame) {
        for (unsigned y = 0; y < 64; ++y) {
            for (unsigned x = 0; x < 64; ++x) { changes[(y * 512 + x) * 4] = static_cast<std::uint8_t>(frame); }
        }
        coverage.update(std::make_shared<const Bytes>(changes), 512, 256,
            now + std::chrono::milliseconds(frame * 16), 8000, sink);
    }
    assert(coverage.motion_region() && coverage.motion_region()->width == 64);
    for (unsigned frame = 5; frame < 10; ++frame) {
        changes = pixels(512, 256, frame);
        coverage.update(std::make_shared<const Bytes>(changes), 512, 256,
            now + std::chrono::milliseconds(frame * 16), 8000, sink);
    }
    assert(coverage.motion_region() && coverage.motion_region()->width == 512 &&
           coverage.motion_region()->height == 256);
    UI sparse_input; sparse_input.prioritize(UI::Region {0, 0, 64, 64}, now + std::chrono::seconds(1));
    Bytes letters(64U * 64U * 4U, 255);
    // A colored focus border does not turn the unchanged tile background
    // into video; colored subpixel font antialiasing remains on the fast path.
    letters[letters.size() - 4] = 0;
    for (unsigned frame = 0; frame < 12; ++frame) {
        letters[frame * 4] = static_cast<std::uint8_t>(frame);
        letters[frame * 4 + 1] = static_cast<std::uint8_t>(frame + 5);
        letters[frame * 4 + 2] = static_cast<std::uint8_t>(frame + 11);
        sparse_input.update(std::make_shared<const Bytes>(letters), 64, 64,
            now + std::chrono::milliseconds(frame * 16), 8000, sink);
    }
    assert(sparse_input.stats().patches == 12 && sparse_input.stats().clears == 0);
    assert(!sparse_input.motion_region());
    UI sticky_motion; sticky_motion.prioritize(UI::Region {0, 0, 64, 64}, now + std::chrono::seconds(1));
    Bytes monochrome(64U * 64U * 4U, 255);
    for (unsigned frame = 0; frame < 5; ++frame) {
        for (std::size_t at = 0; at < monochrome.size(); at += 4) {
            monochrome[at] = monochrome[at + 1] = monochrome[at + 2] = static_cast<std::uint8_t>(frame);
        }
        sticky_motion.update(std::make_shared<const Bytes>(monochrome), 64, 64,
            now + std::chrono::milliseconds(frame * 16), 8000, sink);
    }
    const auto motion_patches = sticky_motion.stats().patches;
    monochrome[0] = monochrome[1] = monochrome[2] = 30;
    sticky_motion.update(std::make_shared<const Bytes>(monochrome), 64, 64,
        now + std::chrono::milliseconds(96), 8000, sink);
    assert(sticky_motion.stats().patches == motion_patches && sticky_motion.motion_region());
    // A playing region larger than the clear batch must not discard the
    // neighboring static desktop/cache. Drain invalidations in bounded
    // immediate batches, preserving the original source epoch.
    UI large_motion;
    auto desktop = std::make_shared<const Bytes>(pixels(1024, 512));
    large_motion.update(desktop, 1024, 512, now - UI::settle_delay, 8000, sink);
    for (unsigned n = 0; n < 8; ++n) {
        large_motion.update(desktop, 1024, 512, now + std::chrono::milliseconds(n * 5), 8000, sink);
    }
    assert(large_motion.stats().patches == 128);
    sent.clear();
    for (unsigned frame = 1; frame <= 2; ++frame) {
        auto updated = *desktop;
        for (unsigned y = 0; y < 512; ++y) {
            for (unsigned x = 256; x < 1024; ++x) {
                updated[(y * 1024 + x) * 4] = static_cast<std::uint8_t>(frame);
            }
        }
        desktop = std::make_shared<const Bytes>(std::move(updated));
        large_motion.update(desktop, 1024, 512,
            now + std::chrono::milliseconds(40 + frame * 10), 8000, sink);
    }
    for (unsigned n = 0; n < 5; ++n) {
        const auto clears_before = large_motion.stats().clears;
        large_motion.update(desktop, 1024, 512, now + std::chrono::milliseconds(60), 8000, sink);
        assert(large_motion.stats().clears - clears_before <= 32);
    }
    assert(large_motion.stats().resets == 1 && large_motion.stats().clears == 96);
    assert(std::none_of(sent.begin(), sent.end(), [](const Bytes& p) {
        return p[4] == UI::reset || ((p[4] == UI::clear || p[4] == UI::patch) && u16(p, 20) < 256);
    }));
    // No exact picture of a moving tile may be published during startup or
    // 400ms decoder pauses. The neighboring static tile still refines once.
    UI temporal; sent.clear(); auto scene = pixels(128, 64);
    unsigned clock_frame = 0;
    for (unsigned ms = 0; ms < 3000; ms += 20) {
        if (ms % 1000 < 600) { ++clock_frame; }
        for (unsigned y = 0; y < 64; ++y) {
            for (unsigned x = 64; x < 128; ++x) {
                const auto at = (y * 128 + x) * 4;
                scene[at] = scene[at + 1] = scene[at + 2] = static_cast<std::uint8_t>((clock_frame % 2) * 255);
            }
        }
        temporal.update(std::make_shared<const Bytes>(scene), 128, 64,
                        now + std::chrono::milliseconds(ms), 8000, sink);
    }
    unsigned static_patches = 0, movie_patches = 0;
    for (const auto& record : sent) {
        if (record[4] != UI::patch) { continue; }
        (u16(record, 20) ? movie_patches : static_patches)++;
    }
    assert(static_patches == 1 && movie_patches == 0);
    temporal.update(std::make_shared<const Bytes>(scene), 128, 64, now + std::chrono::milliseconds(4100), 8000, sink);
    assert(temporal.stats().patches == 2); // A genuinely settled picture refines.
    // Unused alpha changes cannot invalidate opaque RGB glyphs.
    UI opaque; auto alpha = pixels(64, 64); sent.clear();
    publish_stable(opaque, std::make_shared<const Bytes>(alpha), 64, 64, now, 8000, sink);
    const auto patches_before_alpha = opaque.stats().patches;
    for (std::size_t at = 3; at < alpha.size(); at += 4) { alpha[at] = 0; }
    opaque.update(std::make_shared<const Bytes>(alpha), 64, 64, now + std::chrono::milliseconds(20), 8000, sink);
    assert(opaque.stats().patches == patches_before_alpha && opaque.stats().clears == 0);
    // A settled text window replacing video must regain its interactive
    // path before the complete movie hysteresis expires.
    UI replaced; auto text_over_movie = pixels(64, 64);
    for (unsigned n = 0; n < 5; ++n) {
        text_over_movie = pixels(64, 64, n);
        replaced.update(std::make_shared<const Bytes>(text_over_movie), 64, 64,
                        now + std::chrono::milliseconds(n * 20), 8000, sink);
    }
    assert(replaced.motion_region());
    replaced.prioritize(UI::Region {0, 0, 64, 64}, now + std::chrono::seconds(2));
    text_over_movie[0] = 90;
    replaced.update(std::make_shared<const Bytes>(text_over_movie), 64, 64,
                    now + std::chrono::milliseconds(250), 8000, sink);
    assert(replaced.stats().patches == 1 && !replaced.motion_region());
    // Slow scrolling can alter less than one eighth of a tile, but it is
    // not a caret-sized edit or keyboard foreground refinement.
    UI slow_scroll; auto sparse_scroll = pixels(64, 64); sent.clear();
    publish_stable(slow_scroll, std::make_shared<const Bytes>(sparse_scroll), 64, 64, now, 8000, sink);
    for (unsigned n = 1; n <= 6; ++n) {
        for (unsigned y = 0; y < 64; ++y) {
            for (unsigned x = 0; x < 2; ++x) { sparse_scroll[(y * 64 + x) * 4] = static_cast<std::uint8_t>(n); }
        }
        slow_scroll.update(std::make_shared<const Bytes>(sparse_scroll), 64, 64,
                           now + std::chrono::milliseconds(n * 200), 8000, sink);
        slow_scroll.update(std::make_shared<const Bytes>(sparse_scroll), 64, 64,
                           now + std::chrono::milliseconds(n * 200 + 120), 8000, sink);
        if (n >= 2) { assert(slow_scroll.motion_region()); }
    }
    assert(slow_scroll.stats().patches == 2); // initial and the first quiet interval only
    // Advanced peers may receive a small edit early, but first snapshots,
    // unknown layouts and dense motion retain ordinary capture fences.
    UI advanced; auto text = pixels(64,64); sent.clear();
    advanced.advanced(true); advanced.layout(42,true); advanced.capture(100);
    advanced.prioritize(UI::Region{0,0,64,64},now+std::chrono::seconds(10));
    publish_stable(advanced,std::make_shared<const Bytes>(text),64,64,now,8000,sink);
    assert(sent.front()[7]==16);
    for(const auto& p:sent) { assert(!(p[5]&2U)); }
    text[0]=33; sent.clear(); advanced.capture(200);
    advanced.update(std::make_shared<const Bytes>(text),64,64,now+std::chrono::milliseconds(20),8000,sink);
    assert(sent.size()==1 && sent[0][4]==UI::patch && (sent[0][5]&2U));
    advanced.prioritize(UI::Region{16,0,48,64},now+std::chrono::seconds(10));
    text[32*4]=55; sent.clear(); advanced.capture(210);
    advanced.update(std::make_shared<const Bytes>(text),64,64,now+std::chrono::milliseconds(25),8000,sink);
    assert(sent.size()==1 && (sent[0][5]&2U)); // unchanged pixels may be outside the focus
    text[4]=56; sent.clear(); advanced.capture(220);
    advanced.update(std::make_shared<const Bytes>(text),64,64,now+std::chrono::milliseconds(30),8000,sink);
    assert(sent.size()==1 && !(sent[0][5]&2U)); // a changed pixel outside it cannot lead video
    advanced.layout(0,false); advanced.capture(300); text[4]=34; sent.clear();
    advanced.update(std::make_shared<const Bytes>(text),64,64,now+std::chrono::milliseconds(40),8000,sink);
    assert(sent.front()[4]==UI::scene && sent.front()[7]==16);
    for(const auto& p:sent) { assert(!(p[5]&2U)); }
    // An old displayed tile gets a fresh, bounded grace period for a new
    // small edit. Its publication age must not cause an immediate clear
    // merely because background refinement spent the current byte credit.
    UI refill; Bytes budget_noise(128U*64U*4U,255); std::mt19937 budget_random(41);
    for(std::size_t i=0;i<budget_noise.size();i+=4) {
        const auto g=static_cast<std::uint8_t>(budget_random());budget_noise[i]=budget_noise[i+1]=budget_noise[i+2]=g;
    }
    refill.advanced(true);refill.layout(11,true);refill.capture(100);sent.clear();
    publish_stable(refill,std::make_shared<const Bytes>(budget_noise),128,64,now,500,sink);
    assert(refill.stats().patches==1);
    refill.update(std::make_shared<const Bytes>(budget_noise),128,64,now+std::chrono::seconds(1),500,sink);
    assert(refill.stats().patches==2);
    refill.prioritize(UI::Region{0,0,64,64},now+std::chrono::seconds(10));
    budget_noise[0]^=1;budget_noise[1]=budget_noise[2]=budget_noise[0];sent.clear();refill.capture(200);
    refill.update(std::make_shared<const Bytes>(budget_noise),128,64,now+std::chrono::milliseconds(1001),500,sink);
    assert(std::none_of(sent.begin(),sent.end(),[](const auto& p){return p[4]==UI::clear;}));
    sent.clear();refill.update(std::make_shared<const Bytes>(budget_noise),128,64,now+std::chrono::milliseconds(1100),500,sink);
    assert(std::any_of(sent.begin(),sent.end(),[](const auto& p){return p[4]==UI::patch&&(p[5]&2U);}));
    // Continuous new edits cannot renew an unserved tile forever.
    const auto cleared_before=refill.stats().clears;
    for(unsigned ms=1101;ms<=1250;++ms) {
        budget_noise[0]^=1;budget_noise[1]=budget_noise[2]=budget_noise[0];
        refill.update(std::make_shared<const Bytes>(budget_noise),128,64,now+std::chrono::milliseconds(ms),0,sink);
    }
    assert(refill.stats().clears>cleared_before);
    // A few changing pixels next to an active movie may be its counter or
    // an edge, not text input. Do not retain that old exact bitmap while
    // newer video advances the other half of the same picture. Unchanged
    // text and explicitly focused input must keep their exact images.
    UI movie_edge;auto edge_pixels=pixels(192,64);sent.clear();
    publish_stable(movie_edge,std::make_shared<const Bytes>(edge_pixels),192,64,now,8000,sink);
    for(unsigned frame=1;frame<=3;++frame) {
        for(unsigned y=0;y<64;++y)for(unsigned x=128;x<192;++x) {
            edge_pixels[(y*192+x)*4]=static_cast<std::uint8_t>(frame);
        }
        movie_edge.update(std::make_shared<const Bytes>(edge_pixels),192,64,
            now+std::chrono::milliseconds(frame*20),8000,sink);
    }
    assert(movie_edge.motion_region());sent.clear();edge_pixels[64*4]^=1;
    movie_edge.update(std::make_shared<const Bytes>(edge_pixels),192,64,
        now+std::chrono::milliseconds(80),8000,sink);
    assert(std::any_of(sent.begin(),sent.end(),[](const auto& p){return p[4]==UI::clear&&u16(p,20)==64;}));
    assert(std::none_of(sent.begin(),sent.end(),[](const auto& p){return (p[4]==UI::patch||p[4]==UI::clear)&&u16(p,20)==0;}));
    assert(std::none_of(sent.begin(),sent.end(),[](const auto& p){return p[4]==UI::patch&&u16(p,20)==64;}));
    movie_edge.prioritize(UI::Region{64,0,64,64},now+std::chrono::seconds(2));
    movie_edge.update(std::make_shared<const Bytes>(edge_pixels),192,64,
        now+std::chrono::milliseconds(250),8000,sink);
    sent.clear();edge_pixels[64*4]^=1;
    movie_edge.update(std::make_shared<const Bytes>(edge_pixels),192,64,
        now+std::chrono::milliseconds(270),8000,sink);
    assert(std::any_of(sent.begin(),sent.end(),[](const auto& p){return p[4]==UI::patch&&u16(p,20)==64;}));
    assert(std::none_of(sent.begin(),sent.end(),[](const auto& p){return p[4]==UI::clear&&u16(p,20)==64;}));
    // The tile centre can be inside an input clip while its changing video
    // edge is outside. That edge must relinquish its obsolete exact image.
    movie_edge.prioritize(UI::Region{64,0,40,64},now+std::chrono::seconds(2));
    sent.clear();edge_pixels[112*4]^=1;
    movie_edge.update(std::make_shared<const Bytes>(edge_pixels),192,64,
        now+std::chrono::milliseconds(280),8000,sink);
    assert(std::any_of(sent.begin(),sent.end(),[](const auto& p){return p[4]==UI::clear&&u16(p,20)==64;}));
    // A narrow field may occupy only the edge of a tile. Its actual changed
    // pixels, rather than the tile centre, establish text priority.
    UI thin;auto thin_pixels=pixels(64,64);sent.clear();
    thin.advanced(true);thin.layout(9,true);thin.capture(100);
    publish_stable(thin,std::make_shared<const Bytes>(thin_pixels),64,64,now,8000,sink);
    thin.prioritize(UI::Region{0,0,4,64},now+std::chrono::seconds(2));
    thin_pixels[0]^=1;sent.clear();thin.capture(200);
    thin.update(std::make_shared<const Bytes>(thin_pixels),64,64,now+std::chrono::milliseconds(20),8000,sink);
    assert(std::any_of(sent.begin(),sent.end(),[](const auto& p){return p[4]==UI::patch&&(p[5]&2U);}));
    // A layout boundary retires early images in the viewer. Even unchanged
    // pixels need a new fenced image; normal neighbouring tiles stay cached.
    UI layout_refresh; Bytes stable_pair(128U*64U*4U,255);
    layout_refresh.advanced(true);layout_refresh.layout(101,true);layout_refresh.capture(100);
    sent.clear();publish_stable(layout_refresh,std::make_shared<const Bytes>(stable_pair),128,64,now,8000,sink);
    layout_refresh.prioritize(UI::Region{0,0,64,64},now+std::chrono::seconds(10));
    stable_pair[0]=55;sent.clear();layout_refresh.capture(200);
    layout_refresh.update(std::make_shared<const Bytes>(stable_pair),128,64,now+std::chrono::milliseconds(20),8000,sink);
    assert(sent.size()==1 && sent[0][4]==UI::patch && (sent[0][5]&2U));
    // A later ordinary image may still be waiting for video while the
    // browser displays the earlier leading image in this same tile.
    layout_refresh.layout(101,false);stable_pair[4]=73;sent.clear();layout_refresh.capture(250);
    layout_refresh.update(std::make_shared<const Bytes>(stable_pair),128,64,now+std::chrono::milliseconds(25),8000,sink);
    assert(sent.size()==1 && sent[0][4]==UI::patch && !(sent[0][5]&2U));
    sent.clear();layout_refresh.layout(102,false);layout_refresh.capture(300);
    layout_refresh.update(std::make_shared<const Bytes>(stable_pair),128,64,now+std::chrono::milliseconds(40),8000,sink);
    assert(sent.front()[4]==UI::scene);
    sent.clear();layout_refresh.capture(400);
    layout_refresh.update(std::make_shared<const Bytes>(stable_pair),128,64,now+std::chrono::milliseconds(150),8000,sink);
    assert(std::count_if(sent.begin(),sent.end(),[](const auto& p){return p[4]==UI::patch;})==1);
    for(const auto& p:sent) { assert(!(p[5]&2U)); }
    assert(UI::greeting().size() == 28 && UI::greeting(7).size() == 32);
    std::cout << "QSM_LOSSLESS_UI_CORE_OK\n";
}
