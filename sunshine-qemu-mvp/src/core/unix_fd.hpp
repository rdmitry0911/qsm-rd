#pragma once

#include <cerrno>
#include <system_error>
#include <unistd.h>

namespace qmdp {

class UniqueFd {
public:
    UniqueFd() noexcept = default;
    explicit UniqueFd(int fd) noexcept : fd_(fd) {}

    ~UniqueFd() { reset(); }

    UniqueFd(const UniqueFd&) = delete;
    UniqueFd& operator=(const UniqueFd&) = delete;

    UniqueFd(UniqueFd&& other) noexcept : fd_(other.release()) {}

    UniqueFd& operator=(UniqueFd&& other) noexcept {
        if (this != &other) {
            reset(other.release());
        }
        return *this;
    }

    [[nodiscard]] int get() const noexcept { return fd_; }
    [[nodiscard]] explicit operator bool() const noexcept { return fd_ >= 0; }

    [[nodiscard]] int release() noexcept {
        const int old = fd_;
        fd_ = -1;
        return old;
    }

    void reset(int replacement = -1) noexcept {
        if (fd_ >= 0) {
            while (::close(fd_) < 0 && errno == EINTR) {
            }
        }
        fd_ = replacement;
    }

    [[nodiscard]] UniqueFd duplicate() const {
        if (fd_ < 0) {
            return UniqueFd{};
        }
        const int copied = ::dup(fd_);
        if (copied < 0) {
            throw std::system_error(errno, std::generic_category(), "dup");
        }
        return UniqueFd{copied};
    }

private:
    int fd_ {-1};
};

}  // namespace qmdp
