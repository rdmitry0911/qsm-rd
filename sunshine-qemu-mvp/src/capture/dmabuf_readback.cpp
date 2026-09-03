#include "capture/dmabuf_readback.hpp"

#include "core/pixel_format.hpp"

#include <drm_fourcc.h>
#include <epoxy/egl.h>
#include <epoxy/gl.h>
#include <gbm.h>

#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

#include <unistd.h>

namespace qmdp {
namespace {

constexpr std::uint32_t max_dimension = 16384U;
constexpr std::uint64_t max_bytes = 512ULL * 1024ULL * 1024ULL;

[[nodiscard]] std::uint64_t checked_product(std::uint64_t left,
                                            std::uint64_t right,
                                            const char *label) {
    if (left != 0U && right > std::numeric_limits<std::uint64_t>::max() / left) {
        throw std::invalid_argument(std::string(label) + " overflows");
    }
    return left * right;
}

[[nodiscard]] std::vector<std::uint8_t> flip_rows(
    const std::uint8_t *source,
    std::uint32_t stride,
    std::uint32_t height) {
    const auto bytes = checked_product(stride, height, "DMA-BUF map size");
    if (bytes > std::numeric_limits<std::size_t>::max()) {
        throw std::invalid_argument("DMA-BUF map size exceeds address space");
    }
    std::vector<std::uint8_t> flipped(static_cast<std::size_t>(bytes));
    for (std::uint32_t row = 0U; row < height; ++row) {
        const auto source_offset = static_cast<std::size_t>(height - row - 1U) * stride;
        const auto destination_offset = static_cast<std::size_t>(row) * stride;
        std::memcpy(flipped.data() + destination_offset,
                    source + source_offset,
                    stride);
    }
    return flipped;
}

[[nodiscard]] std::string egl_error_message(const char *operation) {
    return std::string(operation) + " failed (EGL error 0x" +
           std::to_string(static_cast<unsigned int>(eglGetError())) + ")";
}

void check_gl(const char *operation) {
    const GLenum error = glGetError();
    if (error != GL_NO_ERROR) {
        throw std::runtime_error(std::string(operation) + " failed (GL error 0x" +
                                 std::to_string(static_cast<unsigned int>(error)) + ")");
    }
}

}  // namespace

// NVIDIA's GBM driver imports VirGL's DMA-BUF but intentionally returns EAGAIN
// for gbm_bo_map(). EGL_EXT_image_dma_buf_import is the interoperable readback
// path: import on the same render node, normalize through an ordinary RGBA FBO,
// then synchronously read the pixels. There is no host window system involved.
struct DmaBufReadback::EglReadback final {
    explicit EglReadback(gbm_device *device) {
        if (device == nullptr) {
            throw std::invalid_argument("EGL DMA-BUF readback has no GBM device");
        }

        if (epoxy_has_egl_extension(nullptr, "EGL_EXT_platform_base")) {
            display_ = eglGetPlatformDisplayEXT(
                EGL_PLATFORM_GBM_KHR,
                reinterpret_cast<EGLNativeDisplayType>(device),
                nullptr);
        }
        if (display_ == EGL_NO_DISPLAY) {
            display_ = eglGetDisplay(reinterpret_cast<EGLNativeDisplayType>(device));
        }
        if (display_ == EGL_NO_DISPLAY) {
            throw std::runtime_error(egl_error_message("eglGetDisplay(GBM)"));
        }

        EGLint major = 0;
        EGLint minor = 0;
        if (eglInitialize(display_, &major, &minor) != EGL_TRUE) {
            const auto message = egl_error_message("eglInitialize(GBM)");
            display_ = EGL_NO_DISPLAY;
            throw std::runtime_error(message);
        }
        initialized_ = true;

        if (!epoxy_has_egl_extension(display_, "EGL_EXT_image_dma_buf_import")) {
            throw std::runtime_error(
                "EGL render node lacks EGL_EXT_image_dma_buf_import");
        }
        if (eglBindAPI(EGL_OPENGL_API) != EGL_TRUE) {
            throw std::runtime_error(egl_error_message("eglBindAPI(OpenGL)"));
        }

        // Rendering targets explicit FBOs. Requiring a pbuffer excludes
        // otherwise valid GBM configurations exposed by nested VirGL, which
        // commonly publish only surfaceless OpenGL configs. This is the same
        // capability order as the validated QEMU/Sunshine capture backend.
        constexpr EGLint surfaceless_config_attributes[] = {
            EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
            EGL_NONE,
        };
        constexpr EGLint pbuffer_config_attributes[] = {
            EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
            EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
            EGL_NONE,
        };
        const auto choose_config = [this](const EGLint *attributes) {
            EGLint count = 0;
            return eglChooseConfig(display_, attributes, &config_, 1, &count) == EGL_TRUE && count != 0;
        };
        if (epoxy_has_egl_extension(display_, "EGL_KHR_surfaceless_context") &&
            choose_config(surfaceless_config_attributes)) {
            surfaceless_ = true;
        } else {
            if (!choose_config(pbuffer_config_attributes)) {
                throw std::runtime_error(
                    "EGL render node has no OpenGL configuration suitable for offscreen DMA-BUF readback");
            }
            constexpr EGLint pbuffer_attributes[] = {
                EGL_WIDTH, 1,
                EGL_HEIGHT, 1,
                EGL_NONE,
            };
            surface_ = eglCreatePbufferSurface(display_, config_, pbuffer_attributes);
            if (surface_ == EGL_NO_SURFACE) {
                throw std::runtime_error(egl_error_message("eglCreatePbufferSurface"));
            }
        }
        context_ = eglCreateContext(display_, config_, EGL_NO_CONTEXT, nullptr);
        if (context_ == EGL_NO_CONTEXT) {
            throw std::runtime_error(egl_error_message("eglCreateContext"));
        }
        make_current();
    }

    ~EglReadback() {
        destroy();
    }

    EglReadback(const EglReadback&) = delete;
    EglReadback& operator=(const EglReadback&) = delete;

    void import(int fd,
                std::uint32_t width,
                std::uint32_t height,
                std::uint32_t stride,
                std::uint32_t drm_fourcc,
                std::uint64_t modifier) {
        make_current();
        reset_image();

        // QEMU's DBus listener supplies a single-plane DMA-BUF. Keep this
        // attribute array local so the borrowed fd is never retained by EGL.
        std::vector<EGLint> attributes {
            EGL_WIDTH, static_cast<EGLint>(width),
            EGL_HEIGHT, static_cast<EGLint>(height),
            EGL_LINUX_DRM_FOURCC_EXT, static_cast<EGLint>(drm_fourcc),
            EGL_DMA_BUF_PLANE0_FD_EXT, fd,
            EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
            EGL_DMA_BUF_PLANE0_PITCH_EXT, static_cast<EGLint>(stride),
        };
        // Keep the exact modifier that QEMU exported.  In particular, zero is
        // DRM_FORMAT_MOD_LINEAR, not an omitted modifier.  QEMU itself only
        // omits these attributes for DRM_FORMAT_MOD_INVALID.  Omitting a
        // valid linear modifier happens to work on Mesa, but NVIDIA then
        // accepts the EGLImage and samples an all-black texture.
        if (modifier != DRM_FORMAT_MOD_INVALID) {
            attributes.push_back(EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT);
            attributes.push_back(static_cast<EGLint>(modifier & 0xffffffffULL));
            attributes.push_back(EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT);
            attributes.push_back(static_cast<EGLint>(modifier >> 32U));
        }
        attributes.push_back(EGL_NONE);

        EGLImageKHR image = eglCreateImageKHR(display_, EGL_NO_CONTEXT,
                                               EGL_LINUX_DMA_BUF_EXT, nullptr,
                                               attributes.data());
        if (image == EGL_NO_IMAGE_KHR) {
            throw std::runtime_error(egl_error_message("eglCreateImageKHR(DMA-BUF)"));
        }

        glGenTextures(1, &source_texture_);
        glBindTexture(GL_TEXTURE_2D, source_texture_);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glEGLImageTargetTexture2DOES(GL_TEXTURE_2D,
                                      reinterpret_cast<GLeglImageOES>(image));
        const GLenum image_target_error = glGetError();
        const EGLBoolean destroy_result = eglDestroyImageKHR(display_, image);
        if (image_target_error != GL_NO_ERROR) {
            throw std::runtime_error("glEGLImageTargetTexture2DOES failed (GL error 0x" +
                                     std::to_string(
                                         static_cast<unsigned int>(image_target_error)) + ")");
        }
        if (destroy_result != EGL_TRUE) {
            throw std::runtime_error(egl_error_message("eglDestroyImageKHR(DMA-BUF)"));
        }

        glGenFramebuffers(1, &source_framebuffer_);
        glBindFramebuffer(GL_FRAMEBUFFER, source_framebuffer_);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                               GL_TEXTURE_2D, source_texture_, 0);
        if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
            throw std::runtime_error("DMA-BUF source framebuffer is incomplete");
        }
        check_gl("configure DMA-BUF source framebuffer");

        ensure_destination(width, height);
        width_ = width;
        height_ = height;
    }

    [[nodiscard]] std::vector<std::uint8_t> read_full(bool y0_top) {
        if (source_framebuffer_ == 0U || width_ == 0U || height_ == 0U) {
            throw std::logic_error("EGL DMA-BUF image is not active");
        }
        make_current();
        glBindFramebuffer(GL_READ_FRAMEBUFFER, source_framebuffer_);
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, destination_framebuffer_);
        // glReadPixels emits bottom-up rows.  QEMU VirGL advertises
        // y0_top=false, which means that copying source bottom-to-top into a
        // normal destination makes row zero of the CPU result logical top.
        // Inverting both source and destination would cancel that correction
        // and vertically mirror the browser image while input remains normal.
        const GLint source_y0 = y0_top ? static_cast<GLint>(height_) : 0;
        const GLint source_y1 = y0_top ? 0 : static_cast<GLint>(height_);
        glBlitFramebuffer(0, source_y0,
                          static_cast<GLint>(width_), source_y1,
                          0, 0,
                          static_cast<GLint>(width_), static_cast<GLint>(height_),
                          GL_COLOR_BUFFER_BIT, GL_NEAREST);
        check_gl("blit DMA-BUF into readback framebuffer");

        const auto bytes = checked_product(checked_product(width_, 4U,
                                                            "EGL readback row"),
                                           height_, "EGL readback size");
        if (bytes > max_bytes || bytes > std::numeric_limits<std::size_t>::max()) {
            throw std::invalid_argument("EGL DMA-BUF readback exceeds byte limit");
        }
        std::vector<std::uint8_t> pixels(static_cast<std::size_t>(bytes));
        glBindFramebuffer(GL_READ_FRAMEBUFFER, destination_framebuffer_);
        glReadBuffer(GL_COLOR_ATTACHMENT0);
        glPixelStorei(GL_PACK_ALIGNMENT, 1);
        glReadPixels(0, 0, static_cast<GLsizei>(width_), static_cast<GLsizei>(height_),
                     GL_BGRA, GL_UNSIGNED_BYTE, pixels.data());
        glPixelStorei(GL_PACK_ALIGNMENT, 4);
        check_gl("read DMA-BUF framebuffer");
        return pixels;
    }

    void reset_image() noexcept {
        if (!make_current_noexcept()) {
            source_texture_ = 0U;
            source_framebuffer_ = 0U;
            width_ = 0U;
            height_ = 0U;
            return;
        }
        if (source_framebuffer_ != 0U) {
            glDeleteFramebuffers(1, &source_framebuffer_);
            source_framebuffer_ = 0U;
        }
        if (source_texture_ != 0U) {
            glDeleteTextures(1, &source_texture_);
            source_texture_ = 0U;
        }
        width_ = 0U;
        height_ = 0U;
    }

private:
    void make_current() {
        if (eglMakeCurrent(display_, surface_, surface_, context_) != EGL_TRUE) {
            throw std::runtime_error(egl_error_message("eglMakeCurrent"));
        }
    }

    [[nodiscard]] bool make_current_noexcept() noexcept {
        return display_ != EGL_NO_DISPLAY && (surfaceless_ || surface_ != EGL_NO_SURFACE) &&
               context_ != EGL_NO_CONTEXT &&
               eglMakeCurrent(display_, surface_, surface_, context_) == EGL_TRUE;
    }

    void ensure_destination(std::uint32_t width, std::uint32_t height) {
        if (destination_texture_ != 0U && destination_width_ == width &&
            destination_height_ == height) {
            return;
        }
        if (destination_framebuffer_ != 0U) {
            glDeleteFramebuffers(1, &destination_framebuffer_);
            destination_framebuffer_ = 0U;
        }
        if (destination_texture_ != 0U) {
            glDeleteTextures(1, &destination_texture_);
            destination_texture_ = 0U;
        }
        glGenTextures(1, &destination_texture_);
        glBindTexture(GL_TEXTURE_2D, destination_texture_);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8,
                     static_cast<GLsizei>(width), static_cast<GLsizei>(height),
                     0, GL_BGRA, GL_UNSIGNED_BYTE, nullptr);
        glGenFramebuffers(1, &destination_framebuffer_);
        glBindFramebuffer(GL_FRAMEBUFFER, destination_framebuffer_);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                               GL_TEXTURE_2D, destination_texture_, 0);
        if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
            throw std::runtime_error("DMA-BUF readback framebuffer is incomplete");
        }
        check_gl("configure DMA-BUF readback framebuffer");
        destination_width_ = width;
        destination_height_ = height;
    }

    void destroy() noexcept {
        reset_image();
        if (make_current_noexcept()) {
            if (destination_framebuffer_ != 0U) {
                glDeleteFramebuffers(1, &destination_framebuffer_);
                destination_framebuffer_ = 0U;
            }
            if (destination_texture_ != 0U) {
                glDeleteTextures(1, &destination_texture_);
                destination_texture_ = 0U;
            }
        }
        if (display_ != EGL_NO_DISPLAY) {
            eglMakeCurrent(display_, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
            if (context_ != EGL_NO_CONTEXT) {
                eglDestroyContext(display_, context_);
                context_ = EGL_NO_CONTEXT;
            }
            if (surface_ != EGL_NO_SURFACE) {
                eglDestroySurface(display_, surface_);
                surface_ = EGL_NO_SURFACE;
            }
            if (initialized_) {
                eglTerminate(display_);
                initialized_ = false;
            }
            display_ = EGL_NO_DISPLAY;
        }
    }

    EGLDisplay display_ {EGL_NO_DISPLAY};
    EGLConfig config_ {};
    EGLSurface surface_ {EGL_NO_SURFACE};
    EGLContext context_ {EGL_NO_CONTEXT};
    GLuint source_texture_ {};
    GLuint source_framebuffer_ {};
    GLuint destination_texture_ {};
    GLuint destination_framebuffer_ {};
    std::uint32_t width_ {};
    std::uint32_t height_ {};
    std::uint32_t destination_width_ {};
    std::uint32_t destination_height_ {};
    bool initialized_ {false};
    bool surfaceless_ {false};
};

DmaBufReadback::DmaBufReadback(std::string render_node) {
    if (render_node.empty()) {
        throw std::invalid_argument("DMA-BUF render node is empty");
    }
    render_fd_ = ::open(render_node.c_str(), O_RDWR | O_CLOEXEC);
    if (render_fd_ < 0) {
        throw std::system_error(errno, std::generic_category(),
                                "open DMA-BUF render node " + render_node);
    }
    device_ = gbm_create_device(render_fd_);
    if (device_ == nullptr) {
        const int saved_errno = errno;
        ::close(render_fd_);
        render_fd_ = -1;
        throw std::system_error(saved_errno == 0 ? ENODEV : saved_errno,
                                std::generic_category(),
                                "gbm_create_device for " + render_node);
    }
}

DmaBufReadback::~DmaBufReadback() {
    // EGLDisplay was created from device_, so release it while the GBM device
    // and render-node fd are still valid.
    egl_readback_.reset();
    reset();
    if (device_ != nullptr) {
        gbm_device_destroy(device_);
    }
    if (render_fd_ >= 0) {
        ::close(render_fd_);
    }
}

void DmaBufReadback::reset() noexcept {
    if (egl_readback_) {
        egl_readback_->reset_image();
    }
    if (bo_ != nullptr) {
        gbm_bo_destroy(bo_);
        bo_ = nullptr;
    }
    backing_fd_.reset();
    width_ = 0U;
    height_ = 0U;
    stride_ = 0U;
    drm_fourcc_ = 0U;
    modifier_ = 0U;
    pixman_format_ = 0U;
    y0_top_ = true;
    egl_fallback_ = false;
}

std::uint32_t DmaBufReadback::pixman_format_for_fourcc(
    std::uint32_t drm_fourcc) {
    switch (drm_fourcc) {
        case DRM_FORMAT_XRGB8888: return pixman_x8r8g8b8;
        case DRM_FORMAT_ARGB8888: return pixman_a8r8g8b8;
        case DRM_FORMAT_XBGR8888: return pixman_x8b8g8r8;
        case DRM_FORMAT_ABGR8888: return pixman_a8b8g8r8;
        default:
            throw std::invalid_argument("unsupported DMA-BUF fourcc");
    }
}

void DmaBufReadback::validate_scanout(std::uint32_t width,
                                      std::uint32_t height,
                                      std::uint32_t stride,
                                      std::uint32_t drm_fourcc) const {
    if (width == 0U || height == 0U || width > max_dimension ||
        height > max_dimension) {
        throw std::invalid_argument("DMA-BUF dimensions are outside limits");
    }
    if (stride < checked_product(width, 4U, "DMA-BUF pixel row") ||
        stride > static_cast<std::uint32_t>(std::numeric_limits<int>::max())) {
        throw std::invalid_argument("DMA-BUF stride is invalid");
    }
    if (checked_product(stride, height, "DMA-BUF size") > max_bytes) {
        throw std::invalid_argument("DMA-BUF exceeds configured byte limit");
    }
    (void) pixman_format_for_fourcc(drm_fourcc);
}

FrameToken DmaBufReadback::scanout(CpuFramebuffer& framebuffer,
                                   UniqueFd fd,
                                   std::uint32_t width,
                                   std::uint32_t height,
                                   std::uint32_t stride,
                                   std::uint32_t drm_fourcc,
                                   std::uint64_t modifier,
                                   bool y0_top) {
    if (!fd) {
        throw std::invalid_argument("DMA-BUF scanout has no file descriptor");
    }
    validate_scanout(width, height, stride, drm_fourcc);

    reset();
    backing_fd_ = std::move(fd);
    width_ = width;
    height_ = height;
    stride_ = stride;
    drm_fourcc_ = drm_fourcc;
    modifier_ = modifier;
    pixman_format_ = pixman_format_for_fourcc(drm_fourcc);
    y0_top_ = y0_top;
    // Do not first probe gbm_bo_map().  On this exact VirGL path it can wait
    // for GPU completion and reduce interactive capture to a few frames per
    // second. EGL_EXT_image_dma_buf_import is already required, is the
    // Sunshine QEMU backend's production path, and has deterministic full
    // readback semantics for Display1 damage callbacks.
    egl_fallback_ = true;
    ensure_egl_readback();
    return copy_scanout_egl(framebuffer);
}

FrameToken DmaBufReadback::copy_scanout_gbm(CpuFramebuffer& framebuffer) {
    if (bo_ == nullptr) {
        throw std::logic_error("DMA-BUF scanout is not active");
    }
    std::uint32_t map_stride = 0U;
    void *map_data = nullptr;
    void *mapped = gbm_bo_map(bo_, 0U, 0U, width_, height_,
                              GBM_BO_TRANSFER_READ, &map_stride, &map_data);
    if (mapped == nullptr || map_data == nullptr) {
        throw std::system_error(errno == 0 ? EIO : errno,
                                std::generic_category(), "gbm_bo_map scanout");
    }
    try {
        const auto bytes = checked_product(map_stride, height_, "DMA-BUF mapped size");
        if (map_stride < checked_product(width_, 4U, "DMA-BUF mapped row") ||
            bytes > max_bytes || bytes > std::numeric_limits<std::size_t>::max()) {
            throw std::invalid_argument("DMA-BUF mapped geometry is invalid");
        }
        const auto *source = static_cast<const std::uint8_t *>(mapped);
        FrameToken frame = y0_top_
            ? framebuffer.scanout_inline(width_, height_, map_stride, pixman_format_,
                                         {source, static_cast<std::size_t>(bytes)},
                                         "qemu-dmabuf-gbm")
            : framebuffer.scanout_inline(width_, height_, map_stride, pixman_format_,
                                         flip_rows(source, map_stride, height_),
                                         "qemu-dmabuf-gbm-flipped");
        gbm_bo_unmap(bo_, map_data);
        return frame;
    } catch (...) {
        gbm_bo_unmap(bo_, map_data);
        throw;
    }
}

void DmaBufReadback::ensure_egl_readback() {
    if (!egl_readback_) {
        egl_readback_ = std::make_unique<EglReadback>(device_);
    }
}

FrameToken DmaBufReadback::copy_scanout_egl(CpuFramebuffer& framebuffer) {
    if (!backing_fd_) {
        throw std::logic_error("EGL DMA-BUF scanout has no backing fd");
    }
    ensure_egl_readback();
    egl_readback_->import(backing_fd_.get(), width_, height_, stride_, drm_fourcc_, modifier_);
    const auto pixels = egl_readback_->read_full(y0_top_);
    // The RGBA FBO normalizes every supported source fourcc to XRGB pixels.
    pixman_format_ = pixman_x8r8g8b8;
    return framebuffer.scanout_inline(width_, height_, width_ * 4U, pixman_format_,
                                      pixels, "qemu-dmabuf-egl");
}

FrameToken DmaBufReadback::update(CpuFramebuffer& framebuffer,
                                  std::int32_t x,
                                  std::int32_t y,
                                  std::int32_t width,
                                  std::int32_t height) {
    if (!backing_fd_) {
        throw std::logic_error("DMA-BUF update arrived before scanout");
    }
    return copy_update_egl(framebuffer, x, y, width, height);
}

FrameToken DmaBufReadback::copy_update_gbm(CpuFramebuffer& framebuffer,
                                           std::int32_t x,
                                           std::int32_t y,
                                           std::int32_t width,
                                           std::int32_t height) {
    if (x < 0 || y < 0 || width <= 0 || height <= 0 ||
        static_cast<std::uint64_t>(x) + static_cast<std::uint64_t>(width) > width_ ||
        static_cast<std::uint64_t>(y) + static_cast<std::uint64_t>(height) > height_) {
        throw std::invalid_argument("DMA-BUF damage is outside scanout");
    }
    const auto source_y = y0_top_
        ? static_cast<std::uint32_t>(y)
        : height_ - static_cast<std::uint32_t>(y) - static_cast<std::uint32_t>(height);
    std::uint32_t map_stride = 0U;
    void *map_data = nullptr;
    void *mapped = gbm_bo_map(bo_, static_cast<std::uint32_t>(x), source_y,
                              static_cast<std::uint32_t>(width),
                              static_cast<std::uint32_t>(height),
                              GBM_BO_TRANSFER_READ, &map_stride, &map_data);
    if (mapped == nullptr || map_data == nullptr) {
        throw std::system_error(errno == 0 ? EIO : errno,
                                std::generic_category(), "gbm_bo_map update");
    }
    try {
        const auto bytes = checked_product(map_stride, static_cast<std::uint32_t>(height),
                                           "DMA-BUF mapped damage size");
        if (map_stride < checked_product(static_cast<std::uint32_t>(width), 4U,
                                         "DMA-BUF mapped damage row") ||
            bytes > max_bytes || bytes > std::numeric_limits<std::size_t>::max()) {
            throw std::invalid_argument("DMA-BUF mapped damage geometry is invalid");
        }
        const auto *source = static_cast<const std::uint8_t *>(mapped);
        FrameToken frame = y0_top_
            ? framebuffer.update_inline(x, y, width, height, map_stride, pixman_format_,
                                        {source, static_cast<std::size_t>(bytes)})
            : framebuffer.update_inline(x, y, width, height, map_stride, pixman_format_,
                                        flip_rows(source, map_stride,
                                                  static_cast<std::uint32_t>(height)));
        gbm_bo_unmap(bo_, map_data);
        return frame;
    } catch (...) {
        gbm_bo_unmap(bo_, map_data);
        throw;
    }
}

FrameToken DmaBufReadback::copy_update_egl(CpuFramebuffer& framebuffer,
                                           std::int32_t x,
                                           std::int32_t y,
                                           std::int32_t width,
                                           std::int32_t height) {
    if (x < 0 || y < 0 || width <= 0 || height <= 0 ||
        static_cast<std::uint64_t>(x) + static_cast<std::uint64_t>(width) > width_ ||
        static_cast<std::uint64_t>(y) + static_cast<std::uint64_t>(height) > height_) {
        throw std::invalid_argument("DMA-BUF damage is outside scanout");
    }
    if (!egl_readback_) {
        throw std::logic_error("EGL DMA-BUF fallback is not initialized");
    }
    // Read a complete normalized image before applying it. QEMU's Display1
    // damage coordinates describe the producer's origin, whereas glReadPixels
    // exposes bottom-up rows. A full-frame update avoids mixing those origins
    // and is deliberately bounded by max_bytes; native zero-copy encoding can
    // replace this compatibility path later.
    const auto pixels = egl_readback_->read_full(y0_top_);
    return framebuffer.update_inline(0, 0,
                                     static_cast<std::int32_t>(width_),
                                     static_cast<std::int32_t>(height_),
                                     width_ * 4U, pixman_format_, pixels);
}

}  // namespace qmdp
