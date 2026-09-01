#pragma once

#include "core/unix_fd.hpp"

#include <cstddef>
#include <cstdint>
#include <span>

namespace qmdp {

// Read-only mmap() wrapper that accepts an arbitrary byte offset. The actual
// mapping is page-aligned while bytes() exposes exactly the requested window.
class MappedRegion {
public:
    MappedRegion() noexcept = default;
    MappedRegion(UniqueFd fd, std::uint64_t offset, std::size_t length);
    ~MappedRegion();

    MappedRegion(const MappedRegion&) = delete;
    MappedRegion& operator=(const MappedRegion&) = delete;
    MappedRegion(MappedRegion&& other) noexcept;
    MappedRegion& operator=(MappedRegion&& other) noexcept;

    [[nodiscard]] std::span<const std::uint8_t> bytes() const noexcept;
    [[nodiscard]] bool valid() const noexcept;
    void reset() noexcept;

private:
    UniqueFd fd_;
    void *mapping_ {nullptr};
    std::size_t mapping_length_ {};
    std::size_t data_delta_ {};
    std::size_t data_length_ {};
};

}  // namespace qmdp
