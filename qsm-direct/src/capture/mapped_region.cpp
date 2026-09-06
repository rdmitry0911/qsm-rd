#include "capture/mapped_region.hpp"

#include <cerrno>
#include <limits>
#include <stdexcept>
#include <system_error>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace qmdp {
namespace {

std::size_t checked_add(std::size_t left, std::size_t right) {
    if (right > std::numeric_limits<std::size_t>::max() - left) {
        throw std::overflow_error("mapped region size overflow");
    }
    return left + right;
}

}  // namespace

MappedRegion::MappedRegion(UniqueFd fd,
                           std::uint64_t offset,
                           std::size_t length)
    : fd_(std::move(fd)), data_length_(length) {
    if (!fd_) {
        throw std::invalid_argument("cannot map an invalid file descriptor");
    }
    if (length == 0U) {
        throw std::invalid_argument("cannot map an empty region");
    }

    const long raw_page_size = ::sysconf(_SC_PAGESIZE);
    if (raw_page_size <= 0) {
        throw std::runtime_error("sysconf(_SC_PAGESIZE) failed");
    }
    const auto page_size = static_cast<std::uint64_t>(raw_page_size);
    const std::uint64_t aligned_offset = offset - (offset % page_size);
    const std::uint64_t delta64 = offset - aligned_offset;
    if (delta64 > std::numeric_limits<std::size_t>::max() ||
        aligned_offset > static_cast<std::uint64_t>(std::numeric_limits<off_t>::max())) {
        throw std::overflow_error("mapping offset is not representable");
    }
    data_delta_ = static_cast<std::size_t>(delta64);
    mapping_length_ = checked_add(data_delta_, length);

    struct stat metadata {};
    if (::fstat(fd_.get(), &metadata) == 0 && S_ISREG(metadata.st_mode) &&
        metadata.st_size >= 0) {
        const auto file_size = static_cast<std::uint64_t>(metadata.st_size);
        if (offset > file_size || length > file_size - offset) {
            throw std::invalid_argument("shared map is smaller than the advertised framebuffer");
        }
    }

    mapping_ = ::mmap(nullptr,
                      mapping_length_,
                      PROT_READ,
                      MAP_SHARED,
                      fd_.get(),
                      static_cast<off_t>(aligned_offset));
    if (mapping_ == MAP_FAILED) {
        mapping_ = nullptr;
        throw std::system_error(errno, std::generic_category(), "mmap framebuffer");
    }
}

MappedRegion::~MappedRegion() {
    reset();
}

MappedRegion::MappedRegion(MappedRegion&& other) noexcept
    : fd_(std::move(other.fd_)),
      mapping_(other.mapping_),
      mapping_length_(other.mapping_length_),
      data_delta_(other.data_delta_),
      data_length_(other.data_length_) {
    other.mapping_ = nullptr;
    other.mapping_length_ = 0U;
    other.data_delta_ = 0U;
    other.data_length_ = 0U;
}

MappedRegion& MappedRegion::operator=(MappedRegion&& other) noexcept {
    if (this != &other) {
        reset();
        fd_ = std::move(other.fd_);
        mapping_ = other.mapping_;
        mapping_length_ = other.mapping_length_;
        data_delta_ = other.data_delta_;
        data_length_ = other.data_length_;
        other.mapping_ = nullptr;
        other.mapping_length_ = 0U;
        other.data_delta_ = 0U;
        other.data_length_ = 0U;
    }
    return *this;
}

std::span<const std::uint8_t> MappedRegion::bytes() const noexcept {
    if (mapping_ == nullptr || data_length_ == 0U) {
        return {};
    }
    const auto *begin = static_cast<const std::uint8_t *>(mapping_) + data_delta_;
    return {begin, data_length_};
}

bool MappedRegion::valid() const noexcept {
    return mapping_ != nullptr && data_length_ > 0U;
}

void MappedRegion::reset() noexcept {
    if (mapping_ != nullptr) {
        (void) ::munmap(mapping_, mapping_length_);
        mapping_ = nullptr;
    }
    mapping_length_ = 0U;
    data_delta_ = 0U;
    data_length_ = 0U;
    fd_.reset();
}

}  // namespace qmdp
