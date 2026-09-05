#include "pipeline/desktop_session.hpp"

#include <algorithm>
#include <exception>
#include <limits>
#include <stdexcept>
#include <utility>

namespace qmdp {

std::size_t DesktopSession::milliseconds_to_frames(
    std::uint32_t sample_rate,
    std::chrono::milliseconds duration) {
    if (sample_rate == 0U || duration.count() <= 0) {
        throw std::invalid_argument("invalid audio timing options");
    }
    const auto milliseconds = static_cast<std::uint64_t>(duration.count());
    const auto frames = (static_cast<std::uint64_t>(sample_rate) * milliseconds +
                         999U) /
                        1000U;
    if (frames == 0U || frames > std::numeric_limits<std::size_t>::max()) {
        throw std::invalid_argument("audio timing options exceed platform limits");
    }
    return static_cast<std::size_t>(frames);
}

DesktopSession::DesktopSession(IQemuDisplay& display,
                               IMediaAdapter& media,
                               DesktopSessionOptions options)
    : display_(display),
      media_(media),
      options_(options),
      audio_fifo_(options.audio_sample_rate,
                  options.audio_channels,
                  milliseconds_to_frames(options.audio_sample_rate,
                                         options.audio_buffer)) {
    if (options_.frame_wait.count() <= 0 ||
        options_.audio_channels == 0U ||
        options_.audio_chunk.count() <= 0 ||
        options_.audio_chunk > options_.audio_buffer) {
        throw std::invalid_argument("invalid desktop session options");
    }
}

DesktopSession::~DesktopSession() {
    stop();
}

void DesktopSession::start() {
    if (running_.exchange(true)) {
        throw std::logic_error("desktop session is already running");
    }
    display_failed_ = false;

    try {
        media_.start();
        encoder_thread_ = std::thread(&DesktopSession::encoder_loop, this);
        audio_thread_ = std::thread(&DesktopSession::audio_loop, this);
        display_.start({
            .on_frame = [this](FrameToken frame) {
                (void) mailbox_.publish(std::move(frame));
            },
            .on_audio = [this](std::span<const float> samples,
                               std::uint32_t sample_rate,
                               std::uint16_t channels) {
                if (sample_rate != options_.audio_sample_rate ||
                    channels != options_.audio_channels) {
                    ++rejected_audio_callbacks_;
                    record_error("audio format changed without session reconfiguration");
                    return;
                }
                try {
                    audio_fifo_.push(samples);
                    ++audio_callbacks_;
                    audio_cv_.notify_one();
                } catch (const std::exception& ex) {
                    record_error(std::string("audio enqueue: ") + ex.what());
                }
            },
            .on_error = [this](std::string message) {
                record_error(std::move(message));
                // QemuDbusDisplay reports an event-channel close here. The
                // producer worker polls this flag and tears down its WebRTC
                // bridge, rather than continuing to encode a stale frame.
                display_failed_ = true;
            },
        });
    } catch (...) {
        running_ = false;
        mailbox_.close();
        audio_cv_.notify_all();
        if (encoder_thread_.joinable()) {
            encoder_thread_.join();
        }
        if (audio_thread_.joinable()) {
            audio_thread_.join();
        }
        media_.stop();
        throw;
    }
}

void DesktopSession::stop() noexcept {
    if (!running_.exchange(false)) {
        return;
    }
    display_.stop();
    mailbox_.close();
    audio_cv_.notify_all();
    if (encoder_thread_.joinable()) {
        encoder_thread_.join();
    }
    if (audio_thread_.joinable()) {
        audio_thread_.join();
    }
    media_.stop();
}

void DesktopSession::set_ui_info(const ViewportRequest& request) {
    if (!running_.load()) {
        throw std::logic_error("desktop session is not running");
    }
    display_.set_ui_info(request);
}

void DesktopSession::key(std::uint32_t qemu_key_number, bool pressed) {
    if (!running_.load()) {
        throw std::logic_error("desktop session is not running");
    }
    display_.key(qemu_key_number, pressed);
}

void DesktopSession::button(std::uint8_t qemu_button, bool pressed) {
    if (!running_.load()) {
        throw std::logic_error("desktop session is not running");
    }
    display_.button(qemu_button, pressed);
}

bool DesktopSession::is_absolute_pointer() {
    if (!running_.load()) {
        throw std::logic_error("desktop session is not running");
    }
    return display_.is_absolute_pointer();
}

void DesktopSession::absolute_pointer(std::uint32_t x, std::uint32_t y) {
    if (!running_.load()) {
        throw std::logic_error("desktop session is not running");
    }
    display_.absolute_pointer(x, y);
}

void DesktopSession::relative_pointer(std::int32_t dx, std::int32_t dy) {
    if (!running_.load()) {
        throw std::logic_error("desktop session is not running");
    }
    display_.relative_pointer(dx, dy);
}

bool DesktopSession::display_failed() const noexcept {
    return display_failed_.load();
}

void DesktopSession::encoder_loop() noexcept {
    std::uint32_t previous_width = 0U;
    std::uint32_t previous_height = 0U;

    while (running_.load()) {
        auto frame = mailbox_.wait_pop(options_.frame_wait);
        if (!frame) {
            continue;
        }
        try {
            // A QEMU D-Bus ScanoutMap generation identifies the backing
            // buffer, not necessarily a new guest display mode.  VirGL can
            // replace that buffer at the same geometry while a desktop is
            // being drawn. Treating every replacement as a mode switch makes
            // a hardware encoder restart on ordinary mouse/keyboard damage,
            // which discards its in-flight output and creates seconds of
            // interactive latency.  A fresh H.264 configuration/IDR is
            // needed only when the visible luma geometry actually changes.
            const bool mode_changed = previous_width != 0U &&
                (frame->surface->width != previous_width ||
                 frame->surface->height != previous_height);
            media_.submit_frame(*frame);
            ++encoded_frames_;
            if (mode_changed) {
                media_.request_idr();
                ++idr_requests_;
            }
            previous_width = frame->surface->width;
            previous_height = frame->surface->height;
        } catch (const std::exception& ex) {
            record_error(std::string("frame submission: ") + ex.what());
        } catch (...) {
            record_error("frame submission: unknown exception");
        }
    }
}

void DesktopSession::audio_loop() noexcept {
    const auto chunk_frames = milliseconds_to_frames(options_.audio_sample_rate,
                                                     options_.audio_chunk);
    while (running_.load() || audio_fifo_.stats().queued_frames != 0U) {
        {
            std::unique_lock lock(audio_wait_mutex_);
            audio_cv_.wait_for(lock, options_.audio_chunk, [this] {
                return !running_.load() ||
                       audio_fifo_.stats().queued_frames != 0U;
            });
        }

        auto samples = audio_fifo_.pop(chunk_frames);
        if (samples.empty()) {
            continue;
        }
        try {
            media_.submit_audio(samples,
                                options_.audio_sample_rate,
                                options_.audio_channels);
            ++audio_submissions_;
        } catch (const std::exception& ex) {
            record_error(std::string("audio submission: ") + ex.what());
        } catch (...) {
            record_error("audio submission: unknown exception");
        }
    }
}

void DesktopSession::record_error(std::string message) noexcept {
    ++errors_;
    try {
        std::lock_guard lock(errors_mutex_);
        constexpr std::size_t max_recent_errors = 16U;
        if (recent_errors_.size() == max_recent_errors) {
            recent_errors_.erase(recent_errors_.begin());
        }
        recent_errors_.push_back(std::move(message));
    } catch (...) {
    }
}

DesktopSession::Stats DesktopSession::stats() const {
    std::vector<std::string> recent;
    {
        std::lock_guard lock(errors_mutex_);
        recent = recent_errors_;
    }
    return {
        .mailbox = mailbox_.stats(),
        .audio_fifo = audio_fifo_.stats(),
        .encoded_frames = encoded_frames_.load(),
        .audio_callbacks = audio_callbacks_.load(),
        .audio_submissions = audio_submissions_.load(),
        .rejected_audio_callbacks = rejected_audio_callbacks_.load(),
        .idr_requests = idr_requests_.load(),
        .errors = errors_.load(),
        .running = running_.load(),
        .display_failed = display_failed_.load(),
        .recent_errors = std::move(recent),
    };
}

}  // namespace qmdp
