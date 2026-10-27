// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/lossless_ui.hpp"
#include <algorithm>
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <iostream>
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
int main() {
    auto now = UI::Clock::now();
    UI ui; std::vector<Bytes> sent;
    auto sink = [&](std::span<const std::uint8_t> p) { sent.emplace_back(p.begin(), p.end()); return true; };
    auto b = pixels(65, 65);
    ui.update(std::make_shared<const Bytes>(b), 65, 65, now, 8000, sink);
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
        b[0] = static_cast<std::uint8_t>(5 + n);
        ui.update(std::make_shared<const Bytes>(b), 65, 65, now + std::chrono::milliseconds(300 + n * 16), 8000, sink);
    }
    assert(ui.stats().clears > 0);
    const auto count = ui.stats().patches;
    ui.update(std::make_shared<const Bytes>(b), 65, 65, now + std::chrono::milliseconds(500), 8000, sink);
    assert(ui.stats().patches == count);
    ui.update(std::make_shared<const Bytes>(b), 65, 65, now + std::chrono::milliseconds(800), 8000, sink);
    assert(ui.stats().patches == count + 1);
    sent.clear();
    ui.update(std::make_shared<const Bytes>(pixels(128, 64)), 128, 64, now + std::chrono::seconds(1), 8000, sink);
    assert(sent.front()[4] == UI::reset && u16(sent.front(), 16) == 128);
    // A lost local record forces a new snapshot rather than retaining an old tile.
    ui.resync(); bool dropped = false;
    ui.update(std::make_shared<const Bytes>(pixels(128, 64)), 128, 64, now + std::chrono::seconds(2), 8000,
        [&](std::span<const std::uint8_t> p) { if (p[4] == UI::patch && !dropped) { dropped = true; return false; } return sink(p); });
    assert(dropped);
    sent.clear();
    ui.update(std::make_shared<const Bytes>(pixels(128, 64)), 128, 64, now + std::chrono::seconds(3), 8000, sink);
    assert(sent.front()[4] == UI::reset);
    // Disable drops history; re-enable starts with a fresh geometry epoch.
    ui.disable(); sent.clear();
    ui.update(std::make_shared<const Bytes>(pixels(64, 64)), 64, 64, now + std::chrono::seconds(4), 500, sink);
    assert(sent.front()[4] == UI::reset && ui.stats().bytes > 0);
    // New input must bypass a still-incomplete desktop refinement.
    UI priority; auto large = pixels(1024, 1024); sent.clear();
    priority.update(std::make_shared<const Bytes>(large), 1024, 1024, now, 8000, sink);
    assert(priority.stats().patches == 16);
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
    sent.clear(); neutral.update(std::make_shared<const Bytes>(gray), 64, 64, now, 8000, sink);
    assert(sent.size() == 2 && sent[1][4] == UI::patch && sent[1][5] == 1);
    Bytes values(4096); auto count_gray = static_cast<uLongf>(values.size());
    assert(uncompress(values.data(), &count_gray, sent[1].data() + 28,
        static_cast<uLong>(sent[1].size() - 28)) == Z_OK);
    assert(count_gray == 4096 && std::all_of(values.begin(), values.end(), [](auto v) { return v == 123; }));
    std::cout << "QSM_LOSSLESS_UI_CORE_OK\n";
}
