#pragma once

#include "core/audio_fifo.hpp"
#include "core/latest_frame_mailbox.hpp"
#include "interfaces/qemu_display.hpp"
#include "interfaces/media_adapter.hpp"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <span>
#include <string>
#include <thread>
#include <vector>

namespace qmdp {

struct DesktopSessionOptions {
    std::chrono::milliseconds frame_wait {50};
    std::uint32_t audio_sample_rate {48000U};
    std::uint16_t audio_channels {2U};
    std::chrono::milliseconds audio_buffer {50};
    std::chrono::milliseconds audio_chunk {10};
};

// One-QEMU-console to one encoded-media worker. Display and audio callbacks
// never wait for the encoder or transport. Video uses a one-slot latest-frame
// mailbox; audio uses a bounded FIFO which drops oldest PCM on overflow.
class DesktopSession {
public:
    DesktopSession(IQemuDisplay& display,
                   IMediaAdapter& media,
                   DesktopSessionOptions options = {});
    ~DesktopSession();

    DesktopSession(const DesktopSession&) = delete;
    DesktopSession& operator=(const DesktopSession&) = delete;

    void start();
    void stop() noexcept;

    void set_ui_info(const ViewportRequest& request);
    void key(std::uint32_t qemu_key_number, bool pressed);
    void button(std::uint8_t qemu_button, bool pressed);
    [[nodiscard]] bool is_absolute_pointer();
    void absolute_pointer(std::uint32_t x, std::uint32_t y);
    void relative_pointer(std::int32_t dx, std::int32_t dy);
    // QEMU Display1 is the source of this session.  Once its event channel
    // fails, a direct-console worker cannot recover without a new VM console
    // launch, so expose that terminal condition to its owner.
    [[nodiscard]] bool display_failed() const noexcept;

    struct Stats {
        LatestFrameMailbox::Stats mailbox;
        AudioFifo::Stats audio_fifo;
        std::uint64_t encoded_frames {};
        std::uint64_t audio_callbacks {};
        std::uint64_t audio_submissions {};
        std::uint64_t rejected_audio_callbacks {};
        std::uint64_t idr_requests {};
        std::uint64_t errors {};
        bool running {};
        bool display_failed {};
        std::vector<std::string> recent_errors;
    };

    [[nodiscard]] Stats stats() const;

    // Audio from a source other than the display (an LXC container's desktop
    // session): converted and encoded like the display's own audio.
    void submit_audio(std::span<const float> samples,
                      std::uint32_t sample_rate,
                      std::uint16_t channels);

private:
    static std::size_t milliseconds_to_frames(
        std::uint32_t sample_rate,
        std::chrono::milliseconds duration);
    void encoder_loop() noexcept;
    void audio_loop() noexcept;
    void record_error(std::string message) noexcept;
    // QEMU's dbus audiodev defaults to 44.1 kHz; the session wants its own
    // rate and channel count (Opus: 48 kHz stereo).  Converts on the audio
    // callback thread; false for a format that cannot be converted.
    bool convert_audio(std::span<const float> samples,
                       std::uint32_t sample_rate,
                       std::uint16_t channels);

    IQemuDisplay& display_;
    IMediaAdapter& media_;
    DesktopSessionOptions options_;
    LatestFrameMailbox mailbox_;
    AudioFifo audio_fifo_;
    std::condition_variable audio_cv_;
    std::mutex audio_wait_mutex_;
    std::thread encoder_thread_;
    std::thread audio_thread_;
    std::atomic<bool> running_ {false};
    std::atomic<std::uint64_t> encoded_frames_ {};
    std::atomic<std::uint64_t> audio_callbacks_ {};
    std::atomic<std::uint64_t> audio_submissions_ {};
    std::atomic<std::uint64_t> rejected_audio_callbacks_ {};
    // Audio conversion state, serialised by audio_input_mutex_.
    std::mutex audio_input_mutex_;
    std::vector<float> converted_audio_;
    std::vector<float> previous_audio_frame_;
    std::uint32_t converter_rate_ {};
    double converter_position_ {};
    std::atomic<std::uint64_t> idr_requests_ {};
    std::atomic<std::uint64_t> errors_ {};
    std::atomic<bool> display_failed_ {false};
    mutable std::mutex errors_mutex_;
    std::vector<std::string> recent_errors_;
};

}  // namespace qmdp
