#include "software/ffmpeg_software_adapter.hpp"

#include "core/pixel_format.hpp"

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <sys/wait.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace qmdp {

FfmpegSoftwareAdapter::FfmpegSoftwareAdapter(FfmpegSoftwareOptions options)
    : options_(std::move(options)) {
    if (options_.fps == 0U || options_.filename_prefix.empty() ||
        options_.ffmpeg_binary.empty() || options_.video_encoder.empty()) {
        throw std::invalid_argument("invalid ffmpeg software adapter options");
    }
}

FfmpegSoftwareAdapter::~FfmpegSoftwareAdapter() {
    stop();
}

void FfmpegSoftwareAdapter::start() {
    if (running_.exchange(true)) {
        throw std::logic_error("ffmpeg software adapter is already running");
    }
    std::filesystem::create_directories(options_.output_directory);
    // write(2) should report EPIPE rather than terminating the host when the
    // diagnostic encoder exits unexpectedly.
    (void) std::signal(SIGPIPE, SIG_IGN);
}

void FfmpegSoftwareAdapter::stop() noexcept {
    if (!running_.exchange(false)) {
        return;
    }
    std::lock_guard lock(process_mutex_);
    last_exit_status_ = close_segment();
}

std::filesystem::path FfmpegSoftwareAdapter::segment_path(
    std::uint64_t index) const {
    std::ostringstream filename;
    filename << options_.filename_prefix << '-' << std::setfill('0')
             << std::setw(3) << index << ".mkv";
    return options_.output_directory / filename.str();
}

void FfmpegSoftwareAdapter::open_segment(std::uint32_t width,
                                         std::uint32_t height) {
    if (child_pid_ >= 0 || input_pipe_) {
        throw std::logic_error("ffmpeg segment is already open");
    }

    int pipe_fds[2] {-1, -1};
    if (::pipe2(pipe_fds, O_CLOEXEC) < 0) {
        throw std::system_error(errno, std::generic_category(), "pipe2(ffmpeg)");
    }
    UniqueFd read_end(pipe_fds[0]);
    UniqueFd write_end(pipe_fds[1]);

    const auto path = segment_path(next_segment_++);
    const std::string dimensions = std::to_string(width) + "x" +
                                   std::to_string(height);
    const std::string fps = std::to_string(options_.fps);
    const std::string path_string = path.string();

    const pid_t pid = ::fork();
    if (pid < 0) {
        throw std::system_error(errno, std::generic_category(), "fork(ffmpeg)");
    }
    if (pid == 0) {
        if (::dup2(read_end.get(), STDIN_FILENO) < 0) {
            _exit(126);
        }
        read_end.reset();
        write_end.reset();

        std::vector<std::string> arguments {
            options_.ffmpeg_binary,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-f", "rawvideo",
            "-pixel_format", "bgra",
            "-video_size", dimensions,
            "-framerate", fps,
            "-i", "pipe:0",
            "-an",
            "-c:v", options_.video_encoder,
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-f", "matroska",
            path_string,
        };
        std::vector<char *> argv;
        argv.reserve(arguments.size() + 1U);
        for (auto& argument : arguments) {
            argv.push_back(argument.data());
        }
        argv.push_back(nullptr);
        ::execvp(argv.front(), argv.data());
        _exit(errno == ENOENT ? 127 : 126);
    }

    read_end.reset();
    child_pid_ = pid;
    input_pipe_ = std::move(write_end);
    width_ = width;
    height_ = height;
    ++segments_;
}

int FfmpegSoftwareAdapter::close_segment() noexcept {
    input_pipe_.reset();
    if (child_pid_ < 0) {
        return last_exit_status_.load();
    }

    int status = 0;
    pid_t result = -1;
    do {
        result = ::waitpid(child_pid_, &status, 0);
    } while (result < 0 && errno == EINTR);
    child_pid_ = -1;
    if (result < 0) {
        return -errno;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return status;
}

void FfmpegSoftwareAdapter::write_all(std::span<const std::uint8_t> bytes) {
    std::size_t offset = 0U;
    while (offset < bytes.size()) {
        const ssize_t written = ::write(input_pipe_.get(),
                                        bytes.data() + offset,
                                        bytes.size() - offset);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            throw std::system_error(errno,
                                    std::generic_category(),
                                    "write(ffmpeg rawvideo)");
        }
        if (written == 0) {
            throw std::runtime_error("ffmpeg rawvideo pipe closed");
        }
        offset += static_cast<std::size_t>(written);
    }
}

void FfmpegSoftwareAdapter::submit_frame(const FrameToken& frame) {
    if (!running_.load() || !frame.valid()) {
        throw std::logic_error("invalid ffmpeg frame submission");
    }
    const auto *cpu = frame.surface->cpu();
    if (cpu == nullptr || !cpu->bytes) {
        throw std::invalid_argument("ffmpeg software adapter requires CPU frames");
    }
    auto bgra = to_bgra(*cpu->bytes,
                        frame.surface->width,
                        frame.surface->height,
                        cpu->stride,
                        cpu->pixman_format);

    std::lock_guard lock(process_mutex_);
    if (child_pid_ < 0 || width_.load() != frame.surface->width ||
        height_.load() != frame.surface->height) {
        const int previous_status = close_segment();
        last_exit_status_ = previous_status;
        if (segments_.load() != 0U && previous_status != 0) {
            throw std::runtime_error("ffmpeg exited with status " +
                                     std::to_string(previous_status));
        }
        open_segment(frame.surface->width, frame.surface->height);
    }
    write_all(bgra);
    input_bytes_ += bgra.size();
    ++frames_;
}

void FfmpegSoftwareAdapter::submit_audio(
    std::span<const float> interleaved_samples,
    std::uint32_t,
    std::uint16_t channels) {
    if (!running_.load() || channels == 0U ||
        interleaved_samples.size() % channels != 0U) {
        throw std::logic_error("invalid ffmpeg audio submission");
    }
    // Video-only diagnostic for now. Count the PCM boundary so the production
    // Sunshine audio adapter can be added without changing DesktopSession.
    audio_frames_ += interleaved_samples.size() / channels;
}

void FfmpegSoftwareAdapter::request_idr() {
    // Starting a new segment already produces an IDR. An in-band force-IDR
    // control channel is intentionally left to the real Sunshine adapter.
    ++idr_requests_;
}

FfmpegSoftwareAdapter::Stats FfmpegSoftwareAdapter::stats() const noexcept {
    return {
        .frames = frames_.load(),
        .input_bytes = input_bytes_.load(),
        .audio_frames = audio_frames_.load(),
        .idr_requests = idr_requests_.load(),
        .segments = segments_.load(),
        .width = width_.load(),
        .height = height_.load(),
        .last_exit_status = last_exit_status_.load(),
    };
}

}  // namespace qmdp
