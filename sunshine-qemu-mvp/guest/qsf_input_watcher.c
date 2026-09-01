/*
 * qsf_input_watcher.c - tiny static guest-side evidence collector for a
 * composite Moonlight/Sunshine/QEMU test. It reads Linux evdev directly,
 * avoiding terminal buffering or desktop-toolkit event inference.
 */

#define _POSIX_C_SOURCE 200809L

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <poll.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

enum { max_inputs = 64 };

static bool write_all(int fd, const char *data, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        const ssize_t amount = write(fd, data + offset, size - offset);
        if (amount > 0) {
            offset += (size_t) amount;
            continue;
        }
        if (amount < 0 && errno == EINTR) {
            continue;
        }
        return false;
    }
    return true;
}

static void report(const char *telemetry_path, const char *message) {
    if (telemetry_path == NULL || message == NULL) {
        return;
    }
    /* Match the shell bootstrap/bridge behaviour: open, write one short
     * marker, close. This avoids holding the virtio-serial telemetry port
     * while another guest component reports an independent state change. */
    const int telemetry = open(telemetry_path, O_WRONLY | O_CLOEXEC | O_NOCTTY);
    if (telemetry < 0) {
        return;
    }
    (void) write_all(telemetry, message, strlen(message));
    (void) write_all(telemetry, "\n", 1U);
    close(telemetry);
}

static bool event_name(const char *name) {
    if (name == NULL || strncmp(name, "event", 5U) != 0 || name[5] == '\0') {
        return false;
    }
    for (const unsigned char *cursor = (const unsigned char *) name + 5U; *cursor; ++cursor) {
        if (*cursor < '0' || *cursor > '9') {
            return false;
        }
    }
    return true;
}

static int open_inputs(struct pollfd *descriptors, size_t maximum) {
    DIR *directory = opendir("/dev/input");
    if (directory == NULL) {
        return -1;
    }
    size_t count = 0;
    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL && count < maximum) {
        if (!event_name(entry->d_name)) {
            continue;
        }
        char path[128];
        if (snprintf(path, sizeof(path), "/dev/input/%s", entry->d_name) >= (int) sizeof(path)) {
            continue;
        }
        const int descriptor = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
        if (descriptor < 0) {
            continue;
        }
        descriptors[count++] = (struct pollfd) { .fd = descriptor, .events = POLLIN, .revents = 0 };
    }
    closedir(directory);
    return (int) count;
}

static bool write_ready_file(const char *path) {
    const int descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (descriptor < 0) {
        return false;
    }
    const bool written = write_all(descriptor, "ready\n", 6U);
    return written && close(descriptor) == 0;
}

int main(int argc, char **argv) {
    const char *telemetry_path = NULL;
    const char *ready_path = NULL;
    for (int index = 1; index < argc; index += 2) {
        if (index + 1 >= argc) {
            fprintf(stderr, "usage: %s --telemetry DEVICE --ready-file PATH\n", argv[0]);
            return 2;
        }
        if (strcmp(argv[index], "--telemetry") == 0) {
            telemetry_path = argv[index + 1];
        } else if (strcmp(argv[index], "--ready-file") == 0) {
            ready_path = argv[index + 1];
        } else {
            fprintf(stderr, "usage: %s --telemetry DEVICE --ready-file PATH\n", argv[0]);
            return 2;
        }
    }
    if (telemetry_path == NULL || ready_path == NULL) {
        fprintf(stderr, "usage: %s --telemetry DEVICE --ready-file PATH\n", argv[0]);
        return 2;
    }

    struct pollfd descriptors[max_inputs];
    const int input_count = open_inputs(descriptors, max_inputs);
    if (input_count <= 0) {
        report(telemetry_path, "QSF_VIRGL_WAYLAND_GUEST_E2E_FAILED=input_watcher_no_evdev_inputs");
        return 1;
    }
    unlink(ready_path);
    if (!write_ready_file(ready_path)) {
        report(telemetry_path, "QSF_VIRGL_WAYLAND_GUEST_E2E_FAILED=input_watcher_ready_file_failed");
        for (int index = 0; index < input_count; ++index) {
            close(descriptors[index].fd);
        }
        return 1;
    }
    report(telemetry_path, "QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCH_READY");

    bool key_a = false;
    bool mouse_abs = false;
    bool mouse_button = false;
    bool complete = false;
    for (;;) {
        const int ready = poll(descriptors, (nfds_t) input_count, -1);
        if (ready < 0 && errno == EINTR) {
            continue;
        }
        if (ready < 0) {
            report(telemetry_path, "QSF_VIRGL_WAYLAND_GUEST_E2E_FAILED=input_watcher_poll_failed");
            break;
        }
        for (int index = 0; index < input_count; ++index) {
            if (!(descriptors[index].revents & POLLIN)) {
                continue;
            }
            for (;;) {
                struct input_event event;
                const ssize_t amount = read(descriptors[index].fd, &event, sizeof(event));
                if (amount == (ssize_t) sizeof(event)) {
                    if (!key_a && event.type == EV_KEY && event.code == KEY_A && event.value == 1) {
                        key_a = true;
                        report(telemetry_path, "QSF_VIRGL_WAYLAND_GUEST_INPUT_KEY_A=observed");
                    }
                    if (!mouse_abs && event.type == EV_ABS &&
                        (event.code == ABS_X || event.code == ABS_Y)) {
                        mouse_abs = true;
                        report(telemetry_path, "QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_ABS=observed");
                    }
                    if (!mouse_button && event.type == EV_KEY && event.value == 1 &&
                        event.code >= BTN_LEFT && event.code <= BTN_TASK) {
                        mouse_button = true;
                        report(telemetry_path, "QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_BTN=observed");
                    }
                    if (!complete && key_a && mouse_abs && mouse_button) {
                        complete = true;
                        report(telemetry_path, "QSF_VIRGL_WAYLAND_GUEST_INPUT_E2E_OK");
                    }
                    continue;
                }
                if (amount < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
                    break;
                }
                break;
            }
        }
    }
    for (int index = 0; index < input_count; ++index) {
        close(descriptors[index].fd);
    }
    return 1;
}
