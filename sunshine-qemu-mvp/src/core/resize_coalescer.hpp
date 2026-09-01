#pragma once

#include <chrono>
#include <cstdint>
#include <optional>

namespace qmdp {

struct ViewportRequest {
    std::uint64_t request_id {};
    std::uint32_t width {};
    std::uint32_t height {};
    std::uint32_t refresh_millihz {60000U};
    std::uint16_t remote_scale_percent {100U};
};

struct ResizePolicy {
    std::uint32_t min_width {640U};
    std::uint32_t min_height {480U};
    std::uint32_t max_width {7680U};
    std::uint32_t max_height {4320U};
    std::uint32_t width_alignment {2U};
    std::uint32_t height_alignment {2U};
    std::chrono::milliseconds debounce {250};
};

class ResizeCoalescer {
public:
    explicit ResizeCoalescer(ResizePolicy policy = {});

    void submit(ViewportRequest request,
                std::chrono::steady_clock::time_point now);

    [[nodiscard]] std::optional<ViewportRequest> poll(
        std::chrono::steady_clock::time_point now);

    [[nodiscard]] std::optional<ViewportRequest> pending() const;
    [[nodiscard]] std::uint64_t superseded_count() const noexcept;

private:
    [[nodiscard]] ViewportRequest normalize(ViewportRequest request) const;

    ResizePolicy policy_;
    std::optional<ViewportRequest> pending_;
    std::optional<ViewportRequest> last_emitted_;
    std::chrono::steady_clock::time_point last_submit_ {};
    std::uint64_t superseded_ {};
};

}  // namespace qmdp
