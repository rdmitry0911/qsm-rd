#pragma once

#include "capture/cpu_framebuffer.hpp"
#include "core/unix_fd.hpp"

#include <cstdint>
#include <memory>
#include <string>

struct gbm_bo;
struct gbm_device;

namespace qmdp {

// Imports QEMU Display1's single-plane ScanoutDMABUF into GBM and copies only
// the requested damage into CpuFramebuffer.  It is intentionally a bounded
// CPU readback boundary: later encoder layers may choose a native DMA-BUF
// path, but the current Sunshine software encoder receives ordinary pixels.
class DmaBufReadback final {
public:
    explicit DmaBufReadback(std::string render_node = "/dev/dri/renderD128");
    ~DmaBufReadback();

    DmaBufReadback(const DmaBufReadback&) = delete;
    DmaBufReadback& operator=(const DmaBufReadback&) = delete;

    [[nodiscard]] FrameToken scanout(CpuFramebuffer& framebuffer,
                                     UniqueFd fd,
                                     std::uint32_t width,
                                     std::uint32_t height,
                                     std::uint32_t stride,
                                     std::uint32_t drm_fourcc,
                                     std::uint64_t modifier,
                                     bool y0_top);

    [[nodiscard]] FrameToken update(CpuFramebuffer& framebuffer,
                                    std::int32_t x,
                                    std::int32_t y,
                                    std::int32_t width,
                                    std::int32_t height);

    void reset() noexcept;
    [[nodiscard]] bool active() const noexcept { return bo_ != nullptr; }

private:
    struct EglReadback;

    [[nodiscard]] static std::uint32_t pixman_format_for_fourcc(
        std::uint32_t drm_fourcc);
    void validate_scanout(std::uint32_t width,
                          std::uint32_t height,
                          std::uint32_t stride,
                          std::uint32_t drm_fourcc) const;
    [[nodiscard]] FrameToken copy_scanout_gbm(CpuFramebuffer& framebuffer);
    [[nodiscard]] FrameToken copy_scanout_egl(CpuFramebuffer& framebuffer);
    [[nodiscard]] FrameToken copy_update_gbm(CpuFramebuffer& framebuffer,
                                             std::int32_t x,
                                             std::int32_t y,
                                             std::int32_t width,
                                             std::int32_t height);
    [[nodiscard]] FrameToken copy_update_egl(CpuFramebuffer& framebuffer,
                                             std::int32_t x,
                                             std::int32_t y,
                                             std::int32_t width,
                                             std::int32_t height);
    void ensure_egl_readback();

    int render_fd_ {-1};
    gbm_device *device_ {};
    gbm_bo *bo_ {};
    UniqueFd backing_fd_;
    std::uint32_t width_ {};
    std::uint32_t height_ {};
    std::uint32_t stride_ {};
    std::uint32_t drm_fourcc_ {};
    std::uint64_t modifier_ {};
    std::uint32_t pixman_format_ {};
    bool y0_top_ {true};
    bool egl_fallback_ {false};
    std::unique_ptr<EglReadback> egl_readback_;
};

}  // namespace qmdp
