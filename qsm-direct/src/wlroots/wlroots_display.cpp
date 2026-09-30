#include "wlroots/wlroots_display.hpp"

#include "core/frame.hpp"
#include "core/pixel_format.hpp"

#include "virtual-keyboard-unstable-v1-client-protocol.h"
#include "wlr-output-management-unstable-v1-client-protocol.h"
#include "wlr-screencopy-unstable-v1-client-protocol.h"
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

#include <linux/input-event-codes.h>
#include <poll.h>
#include <sys/eventfd.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <ctime>
#include <optional>
#include <stdexcept>
#include <vector>

namespace qmdp {
namespace {

using Clock = std::chrono::steady_clock;

// wl_shm fourcc values not spelled out by wayland-client's enum.
constexpr std::uint32_t shm_xbgr8888 = 0x34324258U;  // bytes R,G,B,X
constexpr std::uint32_t shm_abgr8888 = 0x34324241U;  // bytes R,G,B,A
constexpr std::uint32_t shm_bgr888 = 0x34324742U;    // bytes R,G,B
constexpr std::uint32_t shm_rgb888 = 0x34324752U;    // bytes B,G,R

// Lower is better: 32-bit BGRX needs no swizzle for the encoder.
int format_rank(std::uint32_t format) noexcept {
    switch (format) {
    case WL_SHM_FORMAT_XRGB8888:
    case WL_SHM_FORMAT_ARGB8888: return 1;
    case shm_xbgr8888:
    case shm_abgr8888: return 2;
    case shm_bgr888: return 3;
    case shm_rgb888: return 4;
    default: return 0;  // unsupported
    }
}

std::uint32_t now_ms() noexcept {
    timespec ts {};
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<std::uint32_t>(static_cast<std::uint64_t>(ts.tv_sec) * 1000U +
                                      static_cast<std::uint64_t>(ts.tv_nsec) / 1'000'000U);
}

struct ShmBuffer {
    wl_buffer *buffer {};
    void *data {MAP_FAILED};
    std::size_t size {};
    std::uint32_t width {};
    std::uint32_t height {};
    std::uint32_t stride {};
    std::uint32_t format {};

    ShmBuffer() = default;
    ShmBuffer(const ShmBuffer&) = delete;
    ShmBuffer& operator=(const ShmBuffer&) = delete;
    ~ShmBuffer() {
        if (buffer != nullptr) { wl_buffer_destroy(buffer); }
        if (data != MAP_FAILED) { munmap(data, size); }
    }
};

}  // namespace

struct WlrootsDisplay::Impl {
    explicit Impl(WlrootsDisplayOptions opts) : options(std::move(opts)) {}

    WlrootsDisplayOptions options;
    QemuDisplayCallbacks callbacks;

    wl_display *display {};
    wl_registry *registry {};
    wl_output *output {};
    wl_shm *shm {};
    wl_seat *seat {};
    zwlr_screencopy_manager_v1 *screencopy {};
    std::uint32_t screencopy_version {};
    zwlr_virtual_pointer_manager_v1 *pointer_manager {};
    std::uint32_t pointer_manager_version {};
    zwp_virtual_keyboard_manager_v1 *keyboard_manager {};
    zwlr_output_manager_v1 *output_manager {};
    zwlr_virtual_pointer_v1 *pointer {};
    zwp_virtual_keyboard_v1 *keyboard {};

    // --- output management (resize) ---
    struct Head {
        zwlr_output_head_v1 *head {};
        std::string name;
        bool enabled {};
    };
    std::vector<Head> heads;
    std::uint32_t output_serial {};
    bool output_serial_valid {};
    zwlr_output_configuration_v1 *configuration {};
    std::mutex resize_mutex;
    std::optional<ViewportRequest> pending_resize;

    // --- keyboard state ---
    xkb_context *xkb_ctx {};
    xkb_keymap *keymap {};
    xkb_state *xkb {};
    std::array<std::uint32_t, 4> last_mods {};

    // --- threads ---
    int wake_fd {-1};
    std::thread thread;
    std::atomic<bool> stopping {false};
    bool started {false};
    std::mutex request_mutex;  // serialises multi-request input sequences

    // --- capture state (wayland thread only) ---
    struct Capture {
        zwlr_screencopy_frame_v1 *frame {};
        bool info_complete {};
        bool copy_sent {};
        bool ready {};
        bool failed {};
        std::uint32_t format {};
        int rank {};
        std::uint32_t width {};
        std::uint32_t height {};
        std::uint32_t stride {};
        std::uint32_t flags {};
        bool with_damage {};
        bool have_damage {};
        std::uint32_t dx0 {}, dy0 {}, dx1 {}, dy1 {};
    } capture;
    std::unique_ptr<ShmBuffer> shm_buffer;
    bool full_copy_next {true};
    Clock::time_point next_capture {};
    std::uint64_t sequence {};
    std::uint64_t generation {};
    Clock::time_point last_cursor_emit {};
    std::vector<std::shared_ptr<std::vector<std::uint8_t>>> pool;
    std::shared_ptr<const CursorShape> transparent_cursor;
    std::atomic<std::uint64_t> cursor_sequence {};

    std::atomic<std::uint32_t> output_width {1920U};
    std::atomic<std::uint32_t> output_height {1080U};

    mutable std::mutex stats_mutex;
    Stats stats;

    // ---------------------------------------------------------------- helpers
    void flush() noexcept { wl_display_flush(display); }

    void wake() const noexcept {
        if (wake_fd >= 0) {
            const std::uint64_t one = 1;
            [[maybe_unused]] const auto written = ::write(wake_fd, &one, sizeof one);
        }
    }

    void fatal(const std::string& message) noexcept {
        if (!stopping.exchange(true) && callbacks.on_error) {
            callbacks.on_error("wlroots display: " + message);
        }
    }

    // ------------------------------------------------------------- registry
    static void on_global(void *data, wl_registry *reg, std::uint32_t name, const char *iface, std::uint32_t version) {
        auto *self = static_cast<Impl *>(data);
        if (std::strcmp(iface, wl_output_interface.name) == 0 && self->output == nullptr) {
            self->output = static_cast<wl_output *>(wl_registry_bind(reg, name, &wl_output_interface, 1));
        } else if (std::strcmp(iface, wl_shm_interface.name) == 0) {
            self->shm = static_cast<wl_shm *>(wl_registry_bind(reg, name, &wl_shm_interface, 1));
        } else if (std::strcmp(iface, wl_seat_interface.name) == 0 && self->seat == nullptr) {
            self->seat = static_cast<wl_seat *>(wl_registry_bind(reg, name, &wl_seat_interface, 1));
        } else if (std::strcmp(iface, zwlr_screencopy_manager_v1_interface.name) == 0) {
            self->screencopy_version = std::min<std::uint32_t>(version, 3U);
            self->screencopy = static_cast<zwlr_screencopy_manager_v1 *>(
                wl_registry_bind(reg, name, &zwlr_screencopy_manager_v1_interface, self->screencopy_version));
        } else if (std::strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name) == 0) {
            self->pointer_manager_version = std::min<std::uint32_t>(version, 2U);
            self->pointer_manager = static_cast<zwlr_virtual_pointer_manager_v1 *>(
                wl_registry_bind(reg, name, &zwlr_virtual_pointer_manager_v1_interface, self->pointer_manager_version));
        } else if (std::strcmp(iface, zwp_virtual_keyboard_manager_v1_interface.name) == 0) {
            self->keyboard_manager = static_cast<zwp_virtual_keyboard_manager_v1 *>(
                wl_registry_bind(reg, name, &zwp_virtual_keyboard_manager_v1_interface, 1));
        } else if (std::strcmp(iface, zwlr_output_manager_v1_interface.name) == 0) {
            self->output_manager = static_cast<zwlr_output_manager_v1 *>(
                wl_registry_bind(reg, name, &zwlr_output_manager_v1_interface, std::min<std::uint32_t>(version, 2U)));
            zwlr_output_manager_v1_add_listener(self->output_manager, &output_manager_listener, self);
        }
    }
    static void on_global_remove(void *, wl_registry *, std::uint32_t) {}
    static constexpr wl_registry_listener registry_listener {on_global, on_global_remove};

    // ---------------------------------------------------- output management
    static void om_head(void *data, zwlr_output_manager_v1 *, zwlr_output_head_v1 *head) {
        auto *self = static_cast<Impl *>(data);
        self->heads.push_back(Head {head, {}, false});
        zwlr_output_head_v1_add_listener(head, &head_listener, self);
    }
    static void om_done(void *data, zwlr_output_manager_v1 *, std::uint32_t serial) {
        auto *self = static_cast<Impl *>(data);
        self->output_serial = serial;
        self->output_serial_valid = true;
    }
    static void om_finished(void *data, zwlr_output_manager_v1 *) {
        static_cast<Impl *>(data)->output_serial_valid = false;
    }
    static constexpr zwlr_output_manager_v1_listener output_manager_listener {om_head, om_done, om_finished};

    Head *find_head(zwlr_output_head_v1 *head) {
        for (auto &entry : heads) {
            if (entry.head == head) { return &entry; }
        }
        return nullptr;
    }
    static void head_name(void *data, zwlr_output_head_v1 *h, const char *name) {
        if (auto *entry = static_cast<Impl *>(data)->find_head(h)) { entry->name = name; }
    }
    static void head_enabled(void *data, zwlr_output_head_v1 *h, std::int32_t enabled) {
        if (auto *entry = static_cast<Impl *>(data)->find_head(h)) { entry->enabled = enabled != 0; }
    }
    static void head_finished(void *data, zwlr_output_head_v1 *h) {
        auto *self = static_cast<Impl *>(data);
        std::erase_if(self->heads, [h](const Head &entry) { return entry.head == h; });
        zwlr_output_head_v1_destroy(h);
    }
    static void head_description(void *, zwlr_output_head_v1 *, const char *) {}
    static void head_physical_size(void *, zwlr_output_head_v1 *, std::int32_t, std::int32_t) {}
    static void head_mode(void *, zwlr_output_head_v1 *, zwlr_output_mode_v1 *) {}
    static void head_current_mode(void *, zwlr_output_head_v1 *, zwlr_output_mode_v1 *) {}
    static void head_position(void *, zwlr_output_head_v1 *, std::int32_t, std::int32_t) {}
    static void head_transform(void *, zwlr_output_head_v1 *, std::int32_t) {}
    static void head_scale(void *, zwlr_output_head_v1 *, wl_fixed_t) {}
    static void head_make(void *, zwlr_output_head_v1 *, const char *) {}
    static void head_model(void *, zwlr_output_head_v1 *, const char *) {}
    static void head_serial(void *, zwlr_output_head_v1 *, const char *) {}
    static void head_adaptive_sync(void *, zwlr_output_head_v1 *, std::uint32_t) {}
    static constexpr zwlr_output_head_v1_listener head_listener {
        head_name, head_description, head_physical_size, head_mode, head_enabled, head_current_mode,
        head_position, head_transform, head_scale, head_finished, head_make, head_model, head_serial,
        head_adaptive_sync,
    };

    static void cfg_succeeded(void *data, zwlr_output_configuration_v1 *cfg) {
        auto *self = static_cast<Impl *>(data);
        zwlr_output_configuration_v1_destroy(cfg);
        self->configuration = nullptr;
        std::lock_guard lock(self->stats_mutex);
        ++self->stats.resizes_applied;
    }
    static void cfg_failed(void *data, zwlr_output_configuration_v1 *cfg) {
        auto *self = static_cast<Impl *>(data);
        zwlr_output_configuration_v1_destroy(cfg);
        self->configuration = nullptr;
        std::lock_guard lock(self->stats_mutex);
        ++self->stats.resizes_failed;
    }
    static void cfg_cancelled(void *data, zwlr_output_configuration_v1 *cfg) {
        // The output layout changed under us (stale serial): retry once the
        // manager sends a fresh `done`, keeping the request pending.
        auto *self = static_cast<Impl *>(data);
        zwlr_output_configuration_v1_destroy(cfg);
        self->configuration = nullptr;
    }
    static constexpr zwlr_output_configuration_v1_listener configuration_listener {
        cfg_succeeded, cfg_failed, cfg_cancelled,
    };

    void apply_pending_resize() {
        if (output_manager == nullptr || configuration != nullptr || !output_serial_valid) { return; }
        std::optional<ViewportRequest> request;
        {
            std::lock_guard lock(resize_mutex);
            request.swap(pending_resize);
        }
        if (!request) { return; }
        Head *target = nullptr;
        for (auto &entry : heads) {
            if (entry.enabled) { target = &entry; break; }
        }
        if (target == nullptr) { return; }
        configuration = zwlr_output_manager_v1_create_configuration(output_manager, output_serial);
        zwlr_output_configuration_v1_add_listener(configuration, &configuration_listener, this);
        auto *head_config = zwlr_output_configuration_v1_enable_head(configuration, target->head);
        const auto refresh = request->refresh_millihz == 0U ? 60000U : request->refresh_millihz;
        zwlr_output_configuration_head_v1_set_custom_mode(
            head_config, static_cast<std::int32_t>(request->width), static_cast<std::int32_t>(request->height),
            static_cast<std::int32_t>(refresh));
        zwlr_output_configuration_v1_apply(configuration);
    }

    // ------------------------------------------------------------ screencopy
    static void f_buffer(void *data, zwlr_screencopy_frame_v1 *, std::uint32_t format, std::uint32_t width,
                         std::uint32_t height, std::uint32_t stride) {
        auto &c = static_cast<Impl *>(data)->capture;
        const int rank = format_rank(format);
        if (rank != 0 && (c.rank == 0 || rank < c.rank)) {
            c.rank = rank; c.format = format; c.width = width; c.height = height; c.stride = stride;
        }
        // screencopy v1/v2 send exactly one buffer event and no buffer_done.
        if (static_cast<Impl *>(data)->screencopy_version < 3U) { c.info_complete = true; }
    }
    static void f_flags(void *data, zwlr_screencopy_frame_v1 *, std::uint32_t flags) {
        static_cast<Impl *>(data)->capture.flags = flags;
    }
    static void f_ready(void *data, zwlr_screencopy_frame_v1 *, std::uint32_t, std::uint32_t, std::uint32_t) {
        static_cast<Impl *>(data)->capture.ready = true;
    }
    static void f_failed(void *data, zwlr_screencopy_frame_v1 *) {
        static_cast<Impl *>(data)->capture.failed = true;
    }
    static void f_damage(void *data, zwlr_screencopy_frame_v1 *, std::uint32_t x, std::uint32_t y,
                         std::uint32_t w, std::uint32_t h) {
        auto &c = static_cast<Impl *>(data)->capture;
        if (!c.have_damage) {
            c.dx0 = x; c.dy0 = y; c.dx1 = x + w; c.dy1 = y + h; c.have_damage = true;
        } else {
            c.dx0 = std::min(c.dx0, x); c.dy0 = std::min(c.dy0, y);
            c.dx1 = std::max(c.dx1, x + w); c.dy1 = std::max(c.dy1, y + h);
        }
    }
    static void f_linux_dmabuf(void *, zwlr_screencopy_frame_v1 *, std::uint32_t, std::uint32_t, std::uint32_t) {}
    static void f_buffer_done(void *data, zwlr_screencopy_frame_v1 *) {
        static_cast<Impl *>(data)->capture.info_complete = true;
    }
    static constexpr zwlr_screencopy_frame_v1_listener frame_listener {
        f_buffer, f_flags, f_ready, f_failed, f_damage, f_linux_dmabuf, f_buffer_done,
    };

    void begin_capture() {
        capture = Capture {};
        // overlay_cursor = 1: the session's own pointer image is composited in,
        // so the true guest cursor shape reaches the browser with the video.
        capture.frame = zwlr_screencopy_manager_v1_capture_output(screencopy, 1, output);
        zwlr_screencopy_frame_v1_add_listener(capture.frame, &frame_listener, this);
    }

    void end_capture() {
        if (capture.frame != nullptr) { zwlr_screencopy_frame_v1_destroy(capture.frame); }
        capture.frame = nullptr;
    }

    bool ensure_shm_buffer() {
        const auto &c = capture;
        if (shm_buffer && shm_buffer->width == c.width && shm_buffer->height == c.height &&
            shm_buffer->stride == c.stride && shm_buffer->format == c.format) {
            return true;
        }
        auto buffer = std::make_unique<ShmBuffer>();
        buffer->width = c.width; buffer->height = c.height; buffer->stride = c.stride; buffer->format = c.format;
        buffer->size = static_cast<std::size_t>(c.stride) * c.height;
        const int fd = memfd_create("qsm-wlroots-screencopy", MFD_CLOEXEC);
        if (fd < 0) { return false; }
        if (ftruncate(fd, static_cast<off_t>(buffer->size)) < 0) { ::close(fd); return false; }
        buffer->data = mmap(nullptr, buffer->size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (buffer->data == MAP_FAILED) { ::close(fd); return false; }
        wl_shm_pool *pool_object = wl_shm_create_pool(shm, fd, static_cast<std::int32_t>(buffer->size));
        buffer->buffer = wl_shm_pool_create_buffer(pool_object, 0, static_cast<std::int32_t>(c.width),
                                                   static_cast<std::int32_t>(c.height),
                                                   static_cast<std::int32_t>(c.stride), c.format);
        wl_shm_pool_destroy(pool_object);
        ::close(fd);
        const bool resized = !shm_buffer || shm_buffer->width != c.width || shm_buffer->height != c.height;
        shm_buffer = std::move(buffer);
        full_copy_next = true;
        if (resized) { ++generation; }
        output_width = c.width;
        output_height = c.height;
        std::lock_guard lock(stats_mutex);
        ++stats.reallocations;
        stats.width = c.width; stats.height = c.height; stats.shm_format = c.format;
        return true;
    }

    std::shared_ptr<std::vector<std::uint8_t>> pooled_bytes(std::size_t size) {
        for (auto &entry : pool) {
            if (entry.use_count() == 1 && entry->size() == size) { return entry; }
        }
        auto fresh = std::make_shared<std::vector<std::uint8_t>>(size);
        if (pool.size() >= 4U) {
            auto it = std::find_if(pool.begin(), pool.end(), [](const auto &e) { return e.use_count() == 1; });
            if (it != pool.end()) { *it = fresh; return fresh; }
            return fresh;  // every pooled buffer still in flight: allocate, don't retain
        }
        pool.push_back(fresh);
        return fresh;
    }

    // Convert the captured SHM image into tightly packed x8r8g8b8 (bytes
    // B,G,R,X) with y0 at the top, the encoder's native layout.
    void publish_frame() {
        const auto begin = Clock::now();
        const auto &c = capture;
        const std::uint32_t w = c.width, h = c.height;
        const std::size_t out_stride = static_cast<std::size_t>(w) * 4U;
        auto bytes = pooled_bytes(out_stride * h);
        const auto *src_base = static_cast<const std::uint8_t *>(shm_buffer->data);
        const bool invert = (c.flags & ZWLR_SCREENCOPY_FRAME_V1_FLAGS_Y_INVERT) != 0U;
        for (std::uint32_t y = 0; y < h; ++y) {
            const std::uint8_t *src = src_base + static_cast<std::size_t>(invert ? h - 1U - y : y) * c.stride;
            std::uint8_t *dst = bytes->data() + static_cast<std::size_t>(y) * out_stride;
            switch (c.format) {
            case WL_SHM_FORMAT_XRGB8888:
            case WL_SHM_FORMAT_ARGB8888:
                std::memcpy(dst, src, out_stride);
                for (std::uint32_t x = 0; x < w; ++x) { dst[x * 4U + 3U] = 0xffU; }
                break;
            case shm_xbgr8888:
            case shm_abgr8888:
                for (std::uint32_t x = 0; x < w; ++x) {
                    dst[x * 4U] = src[x * 4U + 2U]; dst[x * 4U + 1U] = src[x * 4U + 1U];
                    dst[x * 4U + 2U] = src[x * 4U]; dst[x * 4U + 3U] = 0xffU;
                }
                break;
            case shm_bgr888:  // bytes R,G,B
                for (std::uint32_t x = 0; x < w; ++x) {
                    dst[x * 4U] = src[x * 3U + 2U]; dst[x * 4U + 1U] = src[x * 3U + 1U];
                    dst[x * 4U + 2U] = src[x * 3U]; dst[x * 4U + 3U] = 0xffU;
                }
                break;
            case shm_rgb888:  // bytes B,G,R
                for (std::uint32_t x = 0; x < w; ++x) {
                    dst[x * 4U] = src[x * 3U]; dst[x * 4U + 1U] = src[x * 3U + 1U];
                    dst[x * 4U + 2U] = src[x * 3U + 2U]; dst[x * 4U + 3U] = 0xffU;
                }
                break;
            default:
                return;
            }
        }
        auto surface = std::make_shared<FrameSurface>();
        surface->width = w;
        surface->height = h;
        surface->y0_top = true;
        surface->generation = generation;
        surface->debug_name = "wlroots-screencopy";
        surface->storage = CpuPixels {static_cast<std::uint32_t>(out_stride), pixman_x8r8g8b8,
                                      std::shared_ptr<const std::vector<std::uint8_t>>(bytes)};
        FrameToken token;
        token.surface = std::move(surface);
        if (c.with_damage && c.have_damage) {
            const auto x0 = std::min(c.dx0, w), y0 = std::min(c.dy0, h);
            token.damage = DamageRect {x0, y0, std::min(c.dx1, w) - x0, std::min(c.dy1, h) - y0};
        } else {
            token.damage = DamageRect {0U, 0U, w, h};
        }
        token.sequence = ++sequence;
        token.produced_at = Clock::now();
        const auto convert_us = static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(token.produced_at - begin).count());
        {
            std::lock_guard lock(stats_mutex);
            ++stats.frames;
            stats.convert_total_microseconds += convert_us;
            stats.convert_max_microseconds = std::max(stats.convert_max_microseconds, convert_us);
        }
        if (callbacks.on_frame) { callbacks.on_frame(std::move(token)); }
    }

    // The compositor draws the real cursor into the video.  Give the browser a
    // fully transparent cursor so it does not paint a second (local) arrow on
    // top; re-announced periodically so a late-joining console learns it too.
    void emit_transparent_cursor() {
        if (!callbacks.on_cursor) { return; }
        CursorState state;
        state.visible = true;
        state.x = 0;
        state.y = 0;
        state.shape = transparent_cursor;
        state.sequence = ++cursor_sequence;
        callbacks.on_cursor(std::move(state));
        last_cursor_emit = Clock::now();
    }

    void service_capture(Clock::time_point now) {
        if (capture.frame == nullptr) {
            if (now >= next_capture) { begin_capture(); }
            return;
        }
        if (capture.failed) {
            end_capture();
            full_copy_next = true;
            next_capture = now + std::chrono::milliseconds(100);
            std::lock_guard lock(stats_mutex);
            ++stats.capture_failures;
            return;
        }
        if (capture.info_complete && !capture.copy_sent) {
            if (capture.rank == 0 || !ensure_shm_buffer()) {
                capture.failed = true;
                return;
            }
            capture.with_damage = !full_copy_next && screencopy_version >= 2U;
            if (capture.with_damage) {
                zwlr_screencopy_frame_v1_copy_with_damage(capture.frame, shm_buffer->buffer);
            } else {
                zwlr_screencopy_frame_v1_copy(capture.frame, shm_buffer->buffer);
            }
            capture.copy_sent = true;
            full_copy_next = false;
            return;
        }
        if (capture.ready) {
            publish_frame();
            end_capture();
            const auto interval = std::chrono::microseconds(1'000'000U / std::max(1U, options.max_fps));
            next_capture = now + interval;
            if (now - last_cursor_emit > std::chrono::seconds(2)) { emit_transparent_cursor(); }
        }
    }

    // ------------------------------------------------------------ main loop
    void loop() noexcept {
        try {
            emit_transparent_cursor();
            while (!stopping.load()) {
                const auto now = Clock::now();
                apply_pending_resize();
                service_capture(now);

                while (wl_display_prepare_read(display) != 0) {
                    if (wl_display_dispatch_pending(display) < 0) { fatal("dispatch failed"); return; }
                }
                if (wl_display_flush(display) < 0 && errno != EAGAIN) {
                    wl_display_cancel_read(display);
                    fatal("connection lost (flush)");
                    return;
                }
                int timeout = -1;
                if (capture.frame == nullptr) {
                    const auto wait = std::chrono::duration_cast<std::chrono::milliseconds>(next_capture - Clock::now());
                    timeout = static_cast<int>(std::clamp<std::int64_t>(wait.count(), 0, 1000));
                } else {
                    timeout = 1000;  // bounded: re-check stop/resize even while a copy is pending
                }
                pollfd fds[2] = {{wl_display_get_fd(display), POLLIN, 0}, {wake_fd, POLLIN, 0}};
                const int ready = ::poll(fds, 2, timeout);
                if (ready < 0) {
                    wl_display_cancel_read(display);
                    if (errno == EINTR) { continue; }
                    fatal("poll failed");
                    return;
                }
                if ((fds[0].revents & POLLIN) != 0) {
                    if (wl_display_read_events(display) < 0) { fatal("connection lost (read)"); return; }
                } else {
                    wl_display_cancel_read(display);
                }
                if ((fds[0].revents & (POLLHUP | POLLERR)) != 0) { fatal("connection closed by compositor"); return; }
                if (wl_display_dispatch_pending(display) < 0) { fatal("dispatch failed"); return; }
                if ((fds[1].revents & POLLIN) != 0) {
                    std::uint64_t value = 0;
                    [[maybe_unused]] const auto got = ::read(wake_fd, &value, sizeof value);
                }
            }
        } catch (const std::exception &error) {
            fatal(error.what());
        }
    }

    // ------------------------------------------------------------ keyboard
    void setup_keyboard() {
        if (keyboard_manager == nullptr || seat == nullptr) { return; }
        xkb_ctx = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
        if (xkb_ctx == nullptr) { return; }
        xkb_rule_names names {};
        names.layout = options.xkb_layout.c_str();
        keymap = xkb_keymap_new_from_names(xkb_ctx, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
        if (keymap == nullptr) { return; }
        xkb = xkb_state_new(keymap);
        char *text = xkb_keymap_get_as_string(keymap, XKB_KEYMAP_FORMAT_TEXT_V1);
        if (text == nullptr) { return; }
        const std::size_t length = std::strlen(text) + 1U;
        const int fd = memfd_create("qsm-wlroots-keymap", MFD_CLOEXEC);
        bool ok = fd >= 0 && ftruncate(fd, static_cast<off_t>(length)) == 0;
        if (ok) {
            void *map = mmap(nullptr, length, PROT_WRITE, MAP_SHARED, fd, 0);
            ok = map != MAP_FAILED;
            if (ok) { std::memcpy(map, text, length); munmap(map, length); }
        }
        std::free(text);
        if (!ok) { if (fd >= 0) { ::close(fd); } return; }
        keyboard = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(keyboard_manager, seat);
        zwp_virtual_keyboard_v1_keymap(keyboard, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1, fd,
                                       static_cast<std::uint32_t>(length));
        ::close(fd);
    }
};

// ======================================================================== API

WlrootsDisplay::WlrootsDisplay(WlrootsDisplayOptions options)
    : impl_(std::make_unique<Impl>(std::move(options))) {}

WlrootsDisplay::~WlrootsDisplay() {
    stop();
    if (impl_->options.socket_fd >= 0) { ::close(impl_->options.socket_fd); }
}

void WlrootsDisplay::start(QemuDisplayCallbacks callbacks) {
    auto &d = *impl_;
    if (d.started) { throw std::logic_error("wlroots display already started"); }
    d.callbacks = std::move(callbacks);
    if (d.options.socket_fd >= 0) {
        d.display = wl_display_connect_to_fd(d.options.socket_fd);  // takes ownership of the fd
        d.options.socket_fd = -1;
    } else {
        d.display = wl_display_connect(d.options.socket_path.c_str());
    }
    if (d.display == nullptr) {
        throw std::runtime_error("cannot connect to the container's wlroots compositor: " +
                                 std::string(std::strerror(errno)));
    }
    d.registry = wl_display_get_registry(d.display);
    wl_registry_add_listener(d.registry, &Impl::registry_listener, &d);
    wl_display_roundtrip(d.display);
    if (d.output == nullptr || d.shm == nullptr || d.screencopy == nullptr) {
        throw std::runtime_error("wlroots compositor lacks wl_output/wl_shm/zwlr_screencopy_manager_v1");
    }
    if (d.pointer_manager != nullptr && d.seat != nullptr) {
        d.pointer = d.pointer_manager_version >= 2U
            ? zwlr_virtual_pointer_manager_v1_create_virtual_pointer_with_output(d.pointer_manager, d.seat, d.output)
            : zwlr_virtual_pointer_manager_v1_create_virtual_pointer(d.pointer_manager, d.seat);
    }
    d.setup_keyboard();
    // A second roundtrip delivers the output manager's heads and serial.
    wl_display_roundtrip(d.display);

    auto shape = std::make_shared<CursorShape>();
    shape->width = 1U;
    shape->height = 1U;
    shape->argb = {0U, 0U, 0U, 0U};
    d.transparent_cursor = std::move(shape);

    d.wake_fd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    if (d.wake_fd < 0) { throw std::runtime_error("eventfd failed"); }
    d.stopping = false;
    d.next_capture = Clock::now();
    d.started = true;
    d.thread = std::thread([&d] { d.loop(); });
}

void WlrootsDisplay::stop() noexcept {
    auto &d = *impl_;
    if (!d.started) { return; }
    d.stopping = true;
    d.wake();
    if (d.thread.joinable()) { d.thread.join(); }
    d.end_capture();
    d.shm_buffer.reset();
    if (d.configuration != nullptr) { zwlr_output_configuration_v1_destroy(d.configuration); }
    for (auto &head : d.heads) { zwlr_output_head_v1_destroy(head.head); }
    d.heads.clear();
    if (d.pointer != nullptr) { zwlr_virtual_pointer_v1_destroy(d.pointer); }
    if (d.keyboard != nullptr) { zwp_virtual_keyboard_v1_destroy(d.keyboard); }
    if (d.display != nullptr) {
        wl_display_flush(d.display);
        wl_display_disconnect(d.display);
    }
    d.display = nullptr;
    if (d.xkb != nullptr) { xkb_state_unref(d.xkb); }
    if (d.keymap != nullptr) { xkb_keymap_unref(d.keymap); }
    if (d.xkb_ctx != nullptr) { xkb_context_unref(d.xkb_ctx); }
    d.xkb = nullptr; d.keymap = nullptr; d.xkb_ctx = nullptr;
    if (d.wake_fd >= 0) { ::close(d.wake_fd); }
    d.wake_fd = -1;
    d.started = false;
}

void WlrootsDisplay::set_ui_info(const ViewportRequest& request) {
    auto &d = *impl_;
    ViewportRequest clamped = request;
    clamped.width = std::clamp<std::uint32_t>(request.width & ~1U, 320U, 7680U);
    clamped.height = std::clamp<std::uint32_t>(request.height & ~1U, 200U, 4320U);
    {
        std::lock_guard lock(d.resize_mutex);
        d.pending_resize = clamped;
    }
    d.wake();
}

std::uint32_t WlrootsDisplay::evdev_from_qemu_key(std::uint32_t k) noexcept {
    switch (k) {
    // The browser console sends the Meta keys as 0x5b/0x5c.
    case 0x5bU: case 0xdbU: return KEY_LEFTMETA;
    case 0x5cU: case 0xdcU: return KEY_RIGHTMETA;
    case 0x54U: return KEY_SYSRQ;
    case 0x9cU: return KEY_KPENTER;
    case 0x9dU: return KEY_RIGHTCTRL;
    case 0xb5U: return KEY_KPSLASH;
    case 0xb7U: return KEY_SYSRQ;
    case 0xb8U: return KEY_RIGHTALT;
    case 0xc6U: return KEY_PAUSE;
    case 0xc7U: return KEY_HOME;
    case 0xc8U: return KEY_UP;
    case 0xc9U: return KEY_PAGEUP;
    case 0xcbU: return KEY_LEFT;
    case 0xcdU: return KEY_RIGHT;
    case 0xcfU: return KEY_END;
    case 0xd0U: return KEY_DOWN;
    case 0xd1U: return KEY_PAGEDOWN;
    case 0xd2U: return KEY_INSERT;
    case 0xd3U: return KEY_DELETE;
    case 0xddU: return KEY_COMPOSE;
    default: break;
    }
    // Plain set-1 numbers 1..88 coincide with Linux evdev codes (Esc..F12).
    if (k >= 1U && k <= 0x58U) { return k; }
    return 0U;
}

void WlrootsDisplay::key(std::uint32_t qemu_key_number, bool pressed) {
    auto &d = *impl_;
    const auto code = evdev_from_qemu_key(qemu_key_number);
    if (code == 0U || d.keyboard == nullptr) { return; }
    std::lock_guard lock(d.request_mutex);
    zwp_virtual_keyboard_v1_key(d.keyboard, now_ms(), code,
                                pressed ? WL_KEYBOARD_KEY_STATE_PRESSED : WL_KEYBOARD_KEY_STATE_RELEASED);
    // wlroots does not derive modifiers from a virtual keyboard's keys; track
    // them with xkb and announce each change explicitly.
    if (d.xkb != nullptr) {
        xkb_state_update_key(d.xkb, code + 8U, pressed ? XKB_KEY_DOWN : XKB_KEY_UP);
        const std::array<std::uint32_t, 4> mods {
            xkb_state_serialize_mods(d.xkb, XKB_STATE_MODS_DEPRESSED),
            xkb_state_serialize_mods(d.xkb, XKB_STATE_MODS_LATCHED),
            xkb_state_serialize_mods(d.xkb, XKB_STATE_MODS_LOCKED),
            xkb_state_serialize_layout(d.xkb, XKB_STATE_LAYOUT_EFFECTIVE),
        };
        if (mods != d.last_mods) {
            d.last_mods = mods;
            zwp_virtual_keyboard_v1_modifiers(d.keyboard, mods[0], mods[1], mods[2], mods[3]);
        }
    }
    d.flush();
    std::lock_guard stats_lock(d.stats_mutex);
    ++d.stats.key_events;
}

void WlrootsDisplay::button(std::uint8_t qemu_button, bool pressed) {
    auto &d = *impl_;
    if (d.pointer == nullptr) { return; }
    std::lock_guard lock(d.request_mutex);
    const auto time = now_ms();
    switch (qemu_button) {
    case 0U: case 1U: case 2U: case 5U: case 6U: {
        static constexpr std::array<std::uint32_t, 7> codes {BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, 0U, 0U, BTN_SIDE, BTN_EXTRA};
        zwlr_virtual_pointer_v1_button(d.pointer, time, codes[qemu_button],
                                       pressed ? WL_POINTER_BUTTON_STATE_PRESSED : WL_POINTER_BUTTON_STATE_RELEASED);
        break;
    }
    case 3U: case 4U: {  // QEMU wheel up / down: one notch per press
        if (!pressed) { return; }
        const int direction = qemu_button == 3U ? -1 : 1;
        zwlr_virtual_pointer_v1_axis_source(d.pointer, WL_POINTER_AXIS_SOURCE_WHEEL);
        zwlr_virtual_pointer_v1_axis_discrete(d.pointer, time, WL_POINTER_AXIS_VERTICAL_SCROLL,
                                              wl_fixed_from_int(15 * direction), direction);
        break;
    }
    default:
        return;
    }
    zwlr_virtual_pointer_v1_frame(d.pointer);
    d.flush();
    std::lock_guard stats_lock(d.stats_mutex);
    ++d.stats.pointer_events;
}

void WlrootsDisplay::absolute_pointer(std::uint32_t x, std::uint32_t y) {
    auto &d = *impl_;
    if (d.pointer == nullptr) { return; }
    const std::uint32_t w = d.output_width.load(), h = d.output_height.load();
    std::lock_guard lock(d.request_mutex);
    zwlr_virtual_pointer_v1_motion_absolute(d.pointer, now_ms(), std::min(x, w - 1U), std::min(y, h - 1U), w, h);
    zwlr_virtual_pointer_v1_frame(d.pointer);
    d.flush();
    std::lock_guard stats_lock(d.stats_mutex);
    ++d.stats.pointer_events;
}

void WlrootsDisplay::relative_pointer(std::int32_t dx, std::int32_t dy) {
    auto &d = *impl_;
    if (d.pointer == nullptr) { return; }
    std::lock_guard lock(d.request_mutex);
    zwlr_virtual_pointer_v1_motion(d.pointer, now_ms(), wl_fixed_from_int(dx), wl_fixed_from_int(dy));
    zwlr_virtual_pointer_v1_frame(d.pointer);
    d.flush();
}

WlrootsDisplay::Stats WlrootsDisplay::stats() const {
    std::lock_guard lock(impl_->stats_mutex);
    return impl_->stats;
}

}  // namespace qmdp
