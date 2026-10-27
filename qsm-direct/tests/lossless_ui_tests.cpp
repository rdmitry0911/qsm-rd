// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/lossless_ui.hpp"
#include <algorithm>
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
int main() {
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
    assert(UI::greeting().size() == 28 && UI::greeting(7).size() == 32);
    std::cout << "QSM_LOSSLESS_UI_CORE_OK\n";
}
