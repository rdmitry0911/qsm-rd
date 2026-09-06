/*
 * qsf_guest_agent.c - minimal guest endpoint for the QSF companion channel.
 *
 * The agent deliberately accepts only a narrow line protocol over a QEMU
 * virtio-serial port.  It never executes data received from the client.  The
 * state files make the protocol useful on minimal guests; a desktop-specific adapter may mirror
 * qsf-clipboard.txt to the guest's native clipboard separately.  This agent
 * itself has no desktop-toolkit dependency.
 */

#define _POSIX_C_SOURCE 200809L

/* The retained QSF test endpoint has a constrained exchange-folder protocol.
 * qsm-desktop-agent is compiled clipboard-only: its browser transport has no
 * Files UI or file API, so the binary must not expose those commands either. */
#ifndef QSM_DESKTOP_CLIPBOARD_ONLY
#define QSM_DESKTOP_CLIPBOARD_ONLY 0
#endif

#include <ctype.h>
#if !QSM_DESKTOP_CLIPBOARD_ONLY
#include <dirent.h>
#endif
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

enum {
  max_clipboard_bytes = 1024 * 1024,
#if !QSM_DESKTOP_CLIPBOARD_ONLY
  max_file_bytes = 2 * 1024 * 1024,
  /* The exchange folders are deliberately shallow.  Bounding both the
   * number of names and their wire representation prevents a directory with
   * many tiny files from becoming a control-channel denial of service. */
  max_file_list_entries = 256,
  max_file_list_bytes = 64 * 1024,
#endif
  max_wire_bytes = 4 * 1024 * 1024,
  capability_protocol_version = 2,
  clipboard_apply_timeout_ms = 5000,
  /* A QEMU socket chardev deliberately reports HUP when the host-side
   * controller goes away.  Keep the guest endpoint alive and reopen its
   * virtio port after a short bounded pause instead of treating a terminal
   * worker restart as a guest shutdown. */
  agent_reconnect_pause_ns = 100L * 1000L * 1000L,
};

/* A generation makes a compositor acknowledgement unambiguously belong to
 * the profile the host has just committed.  In particular, a stale
 * acknowledgement for the same WxH must not release a later reconnect. */
struct connection_profile {
  uint64_t generation;
  long width;
  long height;
  long fps;
  long bitrate_kbps;
  const char *codec;
};

struct agent_state {
  int fd;
  char state_dir[512];
  char clipboard_path[640];
  char clipboard_generation_path[640];
  char clipboard_applied_path[640];
  char clipboard_ready_path[640];
#if !QSM_DESKTOP_CLIPBOARD_ONLY
  char incoming_dir[640];
  char outgoing_dir[640];
#endif
  struct timespec clipboard_mtime;
  off_t clipboard_size;
  bool clipboard_stamp_valid;
  long maximum_display_width;
  long maximum_display_height;
  long maximum_display_fps;
  bool require_profile_apply_ack;
  long profile_apply_timeout_ms;
  uint64_t next_profile_generation;
  uint64_t next_clipboard_generation;
  struct connection_profile pending_profile;
  char pending_profile_codec[5];
  bool pending_profile_valid;
  bool pending_profile_committed;
};

static const char base64_chars[] =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static bool write_all(int fd, const void *data, size_t size) {
  const uint8_t *cursor = data;
  size_t offset = 0;
  while (offset < size) {
    const ssize_t amount = write(fd, cursor + offset, size - offset);
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

static void write_line(int fd, const char *prefix, const char *value) {
  if (prefix == NULL) {
    return;
  }
  if (!write_all(fd, prefix, strlen(prefix))) {
    return;
  }
  if (value != NULL) {
    if (!write_all(fd, value, strlen(value))) {
      return;
    }
  }
  (void) write_all(fd, "\n", 1U);
}

#if !QSM_DESKTOP_CLIPBOARD_ONLY
static bool safe_name(const char *name) {
  if (name == NULL || name[0] == '\0' || !isalnum((unsigned char) name[0]) || strlen(name) > 128) {
    return false;
  }
  for (const unsigned char *cursor = (const unsigned char *) name; *cursor; ++cursor) {
    if (!(isalnum(*cursor) || *cursor == '.' || *cursor == '_' || *cursor == '-')) {
      return false;
    }
  }
  return strstr(name, "..") == NULL;
}
#endif

static int ensure_directory(const char *path) {
  if (mkdir(path, 0700) == 0 || errno == EEXIST) {
    return 0;
  }
  return -1;
}

static char *base64_encode(const uint8_t *data, size_t size) {
  const size_t output_size = ((size + 2U) / 3U) * 4U;
  char *output = malloc(output_size + 1U);
  if (output == NULL) {
    return NULL;
  }
  size_t source = 0;
  size_t destination = 0;
  while (source < size) {
    const uint32_t first = data[source++];
    const bool second_present = source < size;
    const uint32_t second = second_present ? data[source++] : 0U;
    const bool third_present = source < size;
    const uint32_t third = third_present ? data[source++] : 0U;
    const uint32_t value = (first << 16U) | (second << 8U) | third;
    output[destination++] = base64_chars[(value >> 18U) & 0x3FU];
    output[destination++] = base64_chars[(value >> 12U) & 0x3FU];
    output[destination++] = second_present ? base64_chars[(value >> 6U) & 0x3FU] : '=';
    output[destination++] = third_present ? base64_chars[value & 0x3FU] : '=';
  }
  output[destination] = '\0';
  return output;
}

static int base64_value(unsigned char character) {
  const char *found = strchr(base64_chars, (int) character);
  return found == NULL ? -1 : (int) (found - base64_chars);
}

static uint8_t *base64_decode(const char *input, size_t *decoded_size, size_t maximum) {
  const size_t input_size = strlen(input);
  if (strcmp(input, "-") == 0) {
    uint8_t *empty = malloc(1U);
    if (empty != NULL) {
      *decoded_size = 0U;
    }
    return empty;
  }
  if (input_size == 0 || (input_size % 4U) != 0U || input_size > ((maximum + 2U) / 3U) * 4U + 4U) {
    return NULL;
  }
  size_t padding = 0;
  if (input[input_size - 1U] == '=') {
    ++padding;
  }
  if (input[input_size - 2U] == '=') {
    ++padding;
  }
  const size_t size = (input_size / 4U) * 3U - padding;
  if (size > maximum) {
    return NULL;
  }
  uint8_t *output = malloc(size == 0U ? 1U : size);
  if (output == NULL) {
    return NULL;
  }
  size_t destination = 0;
  for (size_t source = 0; source < input_size; source += 4U) {
    const int a = base64_value((unsigned char) input[source]);
    const int b = base64_value((unsigned char) input[source + 1U]);
    const int c = input[source + 2U] == '=' ? 0 : base64_value((unsigned char) input[source + 2U]);
    const int d = input[source + 3U] == '=' ? 0 : base64_value((unsigned char) input[source + 3U]);
    if (a < 0 || b < 0 || c < 0 || d < 0 ||
        (input[source + 2U] == '=' && (input[source + 3U] != '=' || source + 4U != input_size)) ||
        (input[source + 3U] == '=' && source + 4U != input_size)) {
      free(output);
      return NULL;
    }
    const uint32_t value = ((uint32_t) a << 18U) | ((uint32_t) b << 12U) |
      ((uint32_t) c << 6U) | (uint32_t) d;
    if (destination < size) {
      output[destination++] = (uint8_t) ((value >> 16U) & 0xFFU);
    }
    if (destination < size) {
      output[destination++] = (uint8_t) ((value >> 8U) & 0xFFU);
    }
    if (destination < size) {
      output[destination++] = (uint8_t) (value & 0xFFU);
    }
  }
  *decoded_size = size;
  return output;
}

static bool utf8_continuation(uint8_t byte) {
  return byte >= 0x80U && byte <= 0xBFU;
}

/* Validate non-NUL Unicode scalar-value UTF-8 without a locale or desktop
 * library. CLIP_SET is text-only. */
static bool valid_clipboard_utf8(const uint8_t *data, size_t size) {
  if (data == NULL) {
    return false;
  }
  for (size_t index = 0U; index < size;) {
    const uint8_t first = data[index++];
    if (first <= 0x7FU) {
      if (first == 0U) {
        return false;
      }
      continue;
    }
    if (first >= 0xC2U && first <= 0xDFU) {
      if (size - index < 1U || !utf8_continuation(data[index])) {
        return false;
      }
      ++index;
      continue;
    }
    if (first >= 0xE0U && first <= 0xEFU) {
      if (size - index < 2U ||
          !utf8_continuation(data[index]) ||
          !utf8_continuation(data[index + 1U]) ||
          (first == 0xE0U && data[index] < 0xA0U) ||
          (first == 0xEDU && data[index] > 0x9FU)) {
        return false;
      }
      index += 2U;
      continue;
    }
    if (first >= 0xF0U && first <= 0xF4U) {
      if (size - index < 3U ||
          !utf8_continuation(data[index]) ||
          !utf8_continuation(data[index + 1U]) ||
          !utf8_continuation(data[index + 2U]) ||
          (first == 0xF0U && data[index] < 0x90U) ||
          (first == 0xF4U && data[index] > 0x8FU)) {
        return false;
      }
      index += 3U;
      continue;
    }
    return false;
  }
  return true;
}

static int read_file(const char *path, uint8_t **data, size_t *size, size_t maximum) {
  struct stat metadata;
  if (stat(path, &metadata) != 0 || metadata.st_size < 0 || (uintmax_t) metadata.st_size > maximum) {
    return -1;
  }
  const size_t expected = (size_t) metadata.st_size;
  uint8_t *buffer = malloc(expected == 0U ? 1U : expected);
  if (buffer == NULL) {
    return -1;
  }
  int file = open(path, O_RDONLY | O_CLOEXEC);
  if (file < 0) {
    free(buffer);
    return -1;
  }
  size_t offset = 0;
  while (offset < expected) {
    const ssize_t amount = read(file, buffer + offset, expected - offset);
    if (amount > 0) {
      offset += (size_t) amount;
      continue;
    }
    if (amount < 0 && errno == EINTR) {
      continue;
    }
    {
      close(file);
      free(buffer);
      return -1;
    }
  }
  close(file);
  *data = buffer;
  *size = expected;
  return 0;
}

static int write_file_atomic(const char *path, const uint8_t *data, size_t size) {
  char temporary[768];
  if (snprintf(temporary, sizeof(temporary), "%s.new", path) >= (int) sizeof(temporary)) {
    return -1;
  }
  int file = open(temporary, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
  if (file < 0) {
    return -1;
  }
  size_t offset = 0;
  while (offset < size) {
    const ssize_t amount = write(file, data + offset, size - offset);
    if (amount > 0) {
      offset += (size_t) amount;
      continue;
    }
    if (amount < 0 && errno == EINTR) {
      continue;
    }
    {
      close(file);
      unlink(temporary);
      return -1;
    }
  }
  if (fsync(file) != 0 || close(file) != 0 || rename(temporary, path) != 0) {
    unlink(temporary);
    return -1;
  }
  return 0;
}

static bool clipboard_generation_text(uint64_t generation, char *text, size_t text_size) {
  return generation != 0U && text != NULL &&
    snprintf(text, text_size, "%" PRIu64 "\n", generation) < (int) text_size;
}

static bool clipboard_bridge_ready(const struct agent_state *state) {
  struct stat metadata;
  return state != NULL && stat(state->clipboard_ready_path, &metadata) == 0 &&
    S_ISREG(metadata.st_mode);
}

static bool clipboard_is_applied(const struct agent_state *state, uint64_t generation) {
  char expected[64];
  uint8_t *actual = NULL;
  size_t actual_size = 0U;
  if (state == NULL || !clipboard_generation_text(generation, expected, sizeof(expected)) ||
      read_file(state->clipboard_applied_path, &actual, &actual_size, sizeof(expected)) != 0) {
    return false;
  }
  const bool matches = actual_size == strlen(expected) &&
    memcmp(actual, expected, actual_size) == 0;
  free(actual);
  return matches;
}

static bool clipboard_applied_event(const struct inotify_event *event,
                                    const struct agent_state *state) {
  return event != NULL && state != NULL && event->len > 0U &&
    strcmp(event->name, "qsf-clipboard-applied") == 0;
}

static bool await_clipboard_applied(const struct agent_state *state, uint64_t generation) {
  if (clipboard_is_applied(state, generation)) {
    return true;
  }
  const int descriptor = inotify_init1(IN_CLOEXEC);
  if (descriptor < 0) {
    return false;
  }
  const int watch = inotify_add_watch(descriptor, state->state_dir,
                                      IN_CLOSE_WRITE | IN_MOVED_TO | IN_ATTRIB);
  if (watch < 0) {
    close(descriptor);
    return false;
  }
  /* The bridge may have completed between the initial read and installing the
   * watch.  Check once more so this is a completion event, never a sleep. */
  if (clipboard_is_applied(state, generation)) {
    inotify_rm_watch(descriptor, watch);
    close(descriptor);
    return true;
  }
  struct timespec started;
  if (clock_gettime(CLOCK_MONOTONIC, &started) != 0) {
    inotify_rm_watch(descriptor, watch);
    close(descriptor);
    return false;
  }
  bool applied = false;
  char buffer[8192];
  while (!applied) {
    struct timespec current;
    if (clock_gettime(CLOCK_MONOTONIC, &current) != 0) {
      break;
    }
    const long long elapsed_ms = ((long long) current.tv_sec - (long long) started.tv_sec) * 1000LL +
      ((long long) current.tv_nsec - (long long) started.tv_nsec) / 1000000LL;
    if (elapsed_ms >= clipboard_apply_timeout_ms) {
      break;
    }
    const int remaining_ms = (int) (clipboard_apply_timeout_ms - elapsed_ms);
    const struct pollfd waiter = {.fd = descriptor, .events = POLLIN, .revents = 0};
    const int ready = poll((struct pollfd *) &waiter, 1U, remaining_ms);
    if (ready <= 0) {
      continue;
    }
    const ssize_t bytes = read(descriptor, buffer, sizeof(buffer));
    if (bytes <= 0) {
      break;
    }
    bool relevant = false;
    for (size_t offset = 0U; offset + sizeof(struct inotify_event) <= (size_t) bytes;) {
      const struct inotify_event *event = (const struct inotify_event *) (buffer + offset);
      if ((event->mask & IN_Q_OVERFLOW) != 0U || clipboard_applied_event(event, state)) {
        relevant = true;
      }
      offset += sizeof(*event) + event->len;
    }
    if (relevant && clipboard_is_applied(state, generation)) {
      applied = true;
    }
  }
  inotify_rm_watch(descriptor, watch);
  close(descriptor);
  return applied;
}

static uint64_t next_clipboard_generation(struct agent_state *state) {
  if (state->next_clipboard_generation == UINT64_MAX) {
    state->next_clipboard_generation = 1U;
  } else {
    ++state->next_clipboard_generation;
  }
  return state->next_clipboard_generation;
}

static bool write_clipboard_request(struct agent_state *state, const uint8_t *data,
                                    size_t size, uint64_t generation) {
  char generation_text[64];
  return state != NULL && clipboard_generation_text(generation, generation_text, sizeof(generation_text)) &&
    write_file_atomic(state->clipboard_generation_path, (const uint8_t *) generation_text,
                      strlen(generation_text)) == 0 &&
    write_file_atomic(state->clipboard_path, data, size) == 0;
}

#if !QSM_DESKTOP_CLIPBOARD_ONLY
/* Return a compact, line-oriented manifest of regular files in one QSF
 * exchange folder. Names are constrained to one safe component and symlinks
 * are invisible to the host controller. */
static int list_exchange_files(const char *directory, char **listing) {
  if (directory == NULL || listing == NULL) {
    return -1;
  }
  *listing = NULL;
  DIR *stream = opendir(directory);
  if (stream == NULL) {
    return -1;
  }
  char *result = calloc(max_file_list_bytes + 1U, 1U);
  if (result == NULL) {
    closedir(stream);
    return -1;
  }
  size_t used = 0;
  size_t entries = 0;
  const int directory_fd = dirfd(stream);
  struct dirent *entry = NULL;
  while ((entry = readdir(stream)) != NULL) {
    if (!safe_name(entry->d_name)) {
      continue;
    }
    struct stat metadata;
    if (fstatat(directory_fd, entry->d_name, &metadata, AT_SYMLINK_NOFOLLOW) != 0 ||
        !S_ISREG(metadata.st_mode) || metadata.st_size < 0) {
      continue;
    }
    if (entries >= max_file_list_entries) {
      break;
    }
    const int length = snprintf(result + used, max_file_list_bytes + 1U - used,
                                "%s\t%jd\n", entry->d_name, (intmax_t) metadata.st_size);
    if (length < 0 || (size_t) length >= max_file_list_bytes + 1U - used) {
      free(result);
      closedir(stream);
      return -1;
    }
    used += (size_t) length;
    entries += 1U;
  }
  closedir(stream);
  *listing = result;
  return 0;
}
#endif

/* The capability exchange intentionally uses a short fixed-field ASCII
 * message instead of unbounded JSON. This keeps the guest endpoint usable in
 * a minimal initramfs and makes every received limit explicit. */
static bool parse_bounded_decimal(const char *value, long minimum, long maximum, long *parsed) {
  if (value == NULL || value[0] == '\0' || parsed == NULL) {
    return false;
  }
  for (const unsigned char *cursor = (const unsigned char *) value; *cursor; ++cursor) {
    if (!isdigit(*cursor)) {
      return false;
    }
  }
  errno = 0;
  char *end = NULL;
  const long number = strtol(value, &end, 10);
  if (errno != 0 || end == value || *end != '\0' || number < minimum || number > maximum) {
    return false;
  }
  *parsed = number;
  return true;
}

static bool parse_profile_generation(const char *value, uint64_t *parsed) {
  if (value == NULL || value[0] == '\0' || parsed == NULL) {
    return false;
  }
  for (const unsigned char *cursor = (const unsigned char *) value; *cursor; ++cursor) {
    if (!isdigit(*cursor)) {
      return false;
    }
  }
  errno = 0;
  char *end = NULL;
  const unsigned long long number = strtoull(value, &end, 10);
  if (errno != 0 || end == value || *end != '\0' || number == 0U) {
    return false;
  }
  *parsed = (uint64_t) number;
  return true;
}

static bool supported_video_codec(const char *codec) {
  return codec != NULL &&
    (strcmp(codec, "H264") == 0 || strcmp(codec, "HEVC") == 0 || strcmp(codec, "AV1") == 0);
}

static bool connection_profile_text(const struct connection_profile *profile,
                                    char *text, size_t text_size) {
  return profile != NULL && text != NULL &&
    snprintf(text, text_size,
             "version=%d\ngeneration=%" PRIu64 "\nresolution=%ldx%ld\nfps=%ld\nbitrate_kbps=%ld\nvideo_codec=%s\n",
             capability_protocol_version, profile->generation, profile->width,
             profile->height, profile->fps, profile->bitrate_kbps,
             profile->codec) < (int) text_size;
}

static bool connection_profile_reply(const struct connection_profile *profile,
                                     char *reply, size_t reply_size) {
  return profile != NULL && reply != NULL &&
    snprintf(reply, reply_size, "%d %" PRIu64 " %ld %ld %ld %ld %s",
             capability_protocol_version, profile->generation, profile->width,
             profile->height, profile->fps, profile->bitrate_kbps,
             profile->codec) < (int) reply_size;
}

static bool equal_connection_profiles(const struct connection_profile *first,
                                      const struct connection_profile *second) {
  return first != NULL && second != NULL && first->generation == second->generation &&
    first->width == second->width && first->height == second->height &&
    first->fps == second->fps && first->bitrate_kbps == second->bitrate_kbps &&
    first->codec != NULL && second->codec != NULL && strcmp(first->codec, second->codec) == 0;
}

static bool write_connection_profile(struct agent_state *state,
                                     const struct connection_profile *profile) {
  char profile_path[768];
  char resolution_path[768];
  char profile_text[256];
  char resolution_text[64];
  if (snprintf(profile_path, sizeof(profile_path), "%s/connection-profile", state->state_dir) >= (int) sizeof(profile_path) ||
      snprintf(resolution_path, sizeof(resolution_path), "%s/resolution", state->state_dir) >= (int) sizeof(resolution_path) ||
      !connection_profile_text(profile, profile_text, sizeof(profile_text)) ||
      snprintf(resolution_text, sizeof(resolution_text), "%ldx%ld\n", profile->width, profile->height) >= (int) sizeof(resolution_text)) {
    return false;
  }
  /* Keep the previous acknowledgement in place until the desktop adapter
   * atomically replaces it with an acknowledgement for this generation.
   * Removing it before publishing the new profile creates a visible
   * old-profile/no-ack state: an adapter watching both files can then restart
   * the compositor once for the artificial gap and once more for the actual
   * requested profile.  Generation makes a retained acknowledgement unable
   * to satisfy AWAIT_CONNECTION_PROFILE for the new profile. */
  return write_file_atomic(profile_path, (const uint8_t *) profile_text, strlen(profile_text)) == 0 &&
    write_file_atomic(resolution_path, (const uint8_t *) resolution_text, strlen(resolution_text)) == 0;
}

static bool profile_is_applied(const struct agent_state *state,
                               const struct connection_profile *profile) {
  char applied_path[768];
  char expected[256];
  uint8_t *actual = NULL;
  size_t actual_size = 0U;
  if (snprintf(applied_path, sizeof(applied_path), "%s/connection-profile-applied", state->state_dir) >= (int) sizeof(applied_path) ||
      !connection_profile_text(profile, expected, sizeof(expected)) ||
      read_file(applied_path, &actual, &actual_size, sizeof(expected)) != 0) {
    return false;
  }
  const bool matches = actual_size == strlen(expected) &&
    memcmp(actual, expected, actual_size) == 0;
  free(actual);
  return matches;
}

static bool await_profile_applied(const struct agent_state *state,
                                  const struct connection_profile *profile) {
  if (!state->require_profile_apply_ack) {
    return true;
  }
  struct timespec started;
  if (clock_gettime(CLOCK_MONOTONIC, &started) != 0) {
    return false;
  }
  const struct timespec pause = {.tv_sec = 0, .tv_nsec = 50L * 1000L * 1000L};
  for (;;) {
    if (profile_is_applied(state, profile)) {
      return true;
    }
    struct timespec current;
    if (clock_gettime(CLOCK_MONOTONIC, &current) != 0) {
      return false;
    }
    const long long elapsed_ms = ((long long) current.tv_sec - (long long) started.tv_sec) * 1000LL +
      ((long long) current.tv_nsec - (long long) started.tv_nsec) / 1000000LL;
    if (elapsed_ms >= state->profile_apply_timeout_ms) {
      return false;
    }
    (void) nanosleep(&pause, NULL);
  }
}

static uint64_t next_profile_generation(struct agent_state *state) {
  if (state->next_profile_generation == UINT64_MAX) {
    state->next_profile_generation = 1U;
  } else {
    ++state->next_profile_generation;
  }
  return state->next_profile_generation;
}

static bool update_clipboard_stamp(struct agent_state *state) {
  struct stat metadata;
  if (stat(state->clipboard_path, &metadata) == 0) {
    state->clipboard_mtime = metadata.st_mtim;
    state->clipboard_size = metadata.st_size;
    state->clipboard_stamp_valid = true;
    return true;
  }
  state->clipboard_stamp_valid = false;
  return false;
}

static void emit_clipboard(struct agent_state *state, const char *prefix, bool report_errors) {
  uint8_t *data = NULL;
  size_t size = 0;
  if (read_file(state->clipboard_path, &data, &size, max_clipboard_bytes) != 0) {
    if (report_errors) {
      write_line(state->fd, "ERR ", "CLIPBOARD_UNAVAILABLE");
    }
    return;
  }
  if (!valid_clipboard_utf8(data, size)) {
    free(data);
    if (report_errors) {
      write_line(state->fd, "ERR ", "CLIPBOARD_NOT_UTF8");
    }
    return;
  }
  char *encoded = base64_encode(data, size);
  free(data);
  if (encoded == NULL) {
    write_line(state->fd, "ERR ", "OUT_OF_MEMORY");
    return;
  }
  write_line(state->fd, prefix, encoded[0] == '\0' ? "-" : encoded);
  free(encoded);
}

static void poll_guest_clipboard(struct agent_state *state) {
  struct stat metadata;
  if (stat(state->clipboard_path, &metadata) != 0) {
    return;
  }
  if (!state->clipboard_stamp_valid ||
      metadata.st_mtim.tv_sec != state->clipboard_mtime.tv_sec ||
      metadata.st_mtim.tv_nsec != state->clipboard_mtime.tv_nsec ||
      metadata.st_size != state->clipboard_size) {
    update_clipboard_stamp(state);
    /* A guest-local writer can bypass CLIP_SET. Do not leak malformed bytes
     * as EVENT_CLIP; a later synchronous CLIP_GET reports the rejection. */
    emit_clipboard(state, "EVENT_CLIP ", false);
  }
}

static void handle_command(struct agent_state *state, char *line) {
  char *save = NULL;
  char *command = strtok_r(line, " ", &save);
  if (command == NULL) {
    return;
  }
  if (strcmp(command, "PING") == 0 && strtok_r(NULL, " ", &save) == NULL) {
    write_line(state->fd, "OK ", "PONG");
    return;
  }
  if (strcmp(command, "CLIP_GET") == 0 && strtok_r(NULL, " ", &save) == NULL) {
    emit_clipboard(state, "CLIP ", true);
    return;
  }
  if (strcmp(command, "CLIP_SET") == 0) {
    char *encoded = strtok_r(NULL, " ", &save);
    if (encoded == NULL || strtok_r(NULL, " ", &save) != NULL) {
      write_line(state->fd, "ERR ", "BAD_CLIP_SET");
      return;
    }
    size_t size = 0;
    uint8_t *data = base64_decode(encoded, &size, max_clipboard_bytes);
    const uint64_t generation = next_clipboard_generation(state);
    if (data == NULL || !valid_clipboard_utf8(data, size) ||
        !write_clipboard_request(state, data, size, generation)) {
      free(data);
      write_line(state->fd, "ERR ", "BAD_CLIPBOARD");
      return;
    }
    free(data);
    update_clipboard_stamp(state);
    if (!clipboard_bridge_ready(state)) {
      /* A minimal/headless guest retains the narrow state-file protocol, but
       * cannot claim that a desktop clipboard was updated.  The browser uses
       * this distinct reply to avoid emitting Ctrl+V against stale native
       * clipboard contents. */
      write_line(state->fd, "OK ", "CLIP_SET");
      return;
    }
    if (!await_clipboard_applied(state, generation)) {
      write_line(state->fd, "ERR ", "CLIPBOARD_NOT_APPLIED");
      return;
    }
    char reply[96];
    if (snprintf(reply, sizeof(reply), "CLIP_SET %" PRIu64, generation) >= (int) sizeof(reply)) {
      write_line(state->fd, "ERR ", "CLIPBOARD_NOT_APPLIED");
      return;
    }
    write_line(state->fd, "OK ", reply);
    return;
  }
#if !QSM_DESKTOP_CLIPBOARD_ONLY
  if (strcmp(command, "FILE_PUT") == 0) {
    char *name = strtok_r(NULL, " ", &save);
    char *encoded = strtok_r(NULL, " ", &save);
    if (!safe_name(name) || encoded == NULL || strtok_r(NULL, " ", &save) != NULL) {
      write_line(state->fd, "ERR ", "BAD_FILE_PUT");
      return;
    }
    size_t size = 0;
    uint8_t *data = base64_decode(encoded, &size, max_file_bytes);
    char path[768];
    if (data == NULL || snprintf(path, sizeof(path), "%s/%s", state->incoming_dir, name) >= (int) sizeof(path) ||
        write_file_atomic(path, data, size) != 0) {
      free(data);
      write_line(state->fd, "ERR ", "BAD_FILE_DATA");
      return;
    }
    free(data);
    write_line(state->fd, "OK FILE_PUT ", name);
    return;
  }
  if (strcmp(command, "FILE_GET") == 0) {
    char *name = strtok_r(NULL, " ", &save);
    if (!safe_name(name) || strtok_r(NULL, " ", &save) != NULL) {
      write_line(state->fd, "ERR ", "BAD_FILE_GET");
      return;
    }
    char path[768];
    uint8_t *data = NULL;
    size_t size = 0;
    if (snprintf(path, sizeof(path), "%s/%s", state->outgoing_dir, name) >= (int) sizeof(path) ||
        read_file(path, &data, &size, max_file_bytes) != 0) {
      write_line(state->fd, "ERR ", "FILE_NOT_FOUND");
      return;
    }
    char *encoded = base64_encode(data, size);
    free(data);
    if (encoded == NULL) {
      write_line(state->fd, "ERR ", "OUT_OF_MEMORY");
      return;
    }
    const char *wire_payload = encoded[0] == '\0' ? "-" : encoded;
    const size_t reply_size = strlen(name) + strlen(wire_payload) + 7U;
    char *reply = malloc(reply_size);
    if (reply == NULL) {
      free(encoded);
      write_line(state->fd, "ERR ", "OUT_OF_MEMORY");
      return;
    }
    (void) snprintf(reply, reply_size, "%s %s", name, wire_payload);
    write_line(state->fd, "FILE ", reply);
    free(reply);
    free(encoded);
    return;
  }
  if (strcmp(command, "FILE_LIST") == 0) {
    char *area = strtok_r(NULL, " ", &save);
    if (area == NULL || strtok_r(NULL, " ", &save) != NULL) {
      write_line(state->fd, "ERR ", "BAD_FILE_LIST");
      return;
    }
    const char *directory = NULL;
    if (strcmp(area, "incoming") == 0) {
      directory = state->incoming_dir;
    } else if (strcmp(area, "outgoing") == 0) {
      directory = state->outgoing_dir;
    } else {
      write_line(state->fd, "ERR ", "BAD_FILE_LIST");
      return;
    }
    char *listing = NULL;
    if (list_exchange_files(directory, &listing) != 0) {
      write_line(state->fd, "ERR ", "FILE_LIST_FAILED");
      return;
    }
    char *encoded = base64_encode((const uint8_t *) listing, strlen(listing));
    free(listing);
    if (encoded == NULL) {
      write_line(state->fd, "ERR ", "OUT_OF_MEMORY");
      return;
    }
    const char *wire_payload = encoded[0] == '\0' ? "-" : encoded;
    const size_t reply_size = strlen(area) + strlen(wire_payload) + 2U;
    char *reply = malloc(reply_size);
    if (reply == NULL) {
      free(encoded);
      write_line(state->fd, "ERR ", "OUT_OF_MEMORY");
      return;
    }
    (void) snprintf(reply, reply_size, "%s %s", area, wire_payload);
    write_line(state->fd, "FILES ", reply);
    free(reply);
    free(encoded);
    return;
  }
#endif
  if (strcmp(command, "CONNECTION_OPTIMIZE") == 0) {
    if (strtok_r(NULL, " ", &save) != NULL) {
      write_line(state->fd, "ERR ", "BAD_CONNECTION_OPTIMIZE");
      return;
    }
    /* The VirGL guest asks for the pair configuration only after declaring
     * its display envelope. It owns scanout geometry; it is not a media
     * video decoder endpoint. */
    char reply[128];
    if (snprintf(reply, sizeof(reply), "%d %ld %ld %ld", capability_protocol_version, state->maximum_display_width,
                 state->maximum_display_height, state->maximum_display_fps) >= (int) sizeof(reply)) {
      write_line(state->fd, "ERR ", "GUEST_CAPABILITIES_REPLY");
      return;
    }
    write_line(state->fd, "GUEST_CAPABILITIES_REQUEST ", reply);
    return;
  }
  if (strcmp(command, "PAIR_CAPABILITIES") == 0) {
    char *version = strtok_r(NULL, " ", &save);
    char *width = strtok_r(NULL, " ", &save);
    char *height = strtok_r(NULL, " ", &save);
    char *fps = strtok_r(NULL, " ", &save);
    char *bitrate_kbps = strtok_r(NULL, " ", &save);
    char *codec = strtok_r(NULL, " ", &save);
    long parsed_version = 0L;
    long parsed_width = 0L;
    long parsed_height = 0L;
    long parsed_fps = 0L;
    long parsed_bitrate_kbps = 0L;
    if (version == NULL || width == NULL || height == NULL || fps == NULL ||
        bitrate_kbps == NULL || codec == NULL || strtok_r(NULL, " ", &save) != NULL ||
        !parse_bounded_decimal(version, capability_protocol_version, capability_protocol_version, &parsed_version) ||
        !parse_bounded_decimal(width, 64L, state->maximum_display_width, &parsed_width) ||
        !parse_bounded_decimal(height, 64L, state->maximum_display_height, &parsed_height) ||
        !parse_bounded_decimal(fps, 10L, state->maximum_display_fps, &parsed_fps) ||
        !parse_bounded_decimal(bitrate_kbps, 500L, 500000L, &parsed_bitrate_kbps) ||
        !supported_video_codec(codec)) {
      write_line(state->fd, "ERR ", "BAD_PAIR_CAPABILITIES");
      return;
    }
    (void) parsed_version;
    const struct connection_profile profile = {
      .generation = next_profile_generation(state),
      .width = parsed_width,
      .height = parsed_height,
      .fps = parsed_fps,
      .bitrate_kbps = parsed_bitrate_kbps,
      .codec = codec,
    };
    if (strlen(codec) >= sizeof(state->pending_profile_codec)) {
      write_line(state->fd, "ERR ", "BAD_PAIR_CAPABILITIES");
      return;
    }
    (void) strcpy(state->pending_profile_codec, codec);
    state->pending_profile = profile;
    state->pending_profile.codec = state->pending_profile_codec;
    state->pending_profile_valid = true;
    state->pending_profile_committed = false;
    char reply[128];
    if (!connection_profile_reply(&state->pending_profile, reply, sizeof(reply))) {
      write_line(state->fd, "ERR ", "PROFILE_REPLY");
      return;
    }
    /* Do not write state yet.  The host must successfully request QEMU's
     * virtual mode first; COMMIT_CONNECTION_PROFILE starts the guest desktop
     * reconfiguration only after that happens. */
    write_line(state->fd, "CONNECTION_PROFILE_ACCEPTED ", reply);
    return;
  }
  if (strcmp(command, "COMMIT_CONNECTION_PROFILE") == 0 ||
      strcmp(command, "AWAIT_CONNECTION_PROFILE") == 0) {
    const bool await = strcmp(command, "AWAIT_CONNECTION_PROFILE") == 0;
    char *version = strtok_r(NULL, " ", &save);
    char *generation = strtok_r(NULL, " ", &save);
    char *width = strtok_r(NULL, " ", &save);
    char *height = strtok_r(NULL, " ", &save);
    char *fps = strtok_r(NULL, " ", &save);
    char *bitrate_kbps = strtok_r(NULL, " ", &save);
    char *codec = strtok_r(NULL, " ", &save);
    long parsed_version = 0L;
    long parsed_width = 0L;
    long parsed_height = 0L;
    long parsed_fps = 0L;
    long parsed_bitrate_kbps = 0L;
    uint64_t parsed_generation = 0U;
    const struct connection_profile requested = {
      .generation = 0U,
      .width = 0L,
      .height = 0L,
      .fps = 0L,
      .bitrate_kbps = 0L,
      .codec = codec,
    };
    if (version == NULL || generation == NULL || width == NULL || height == NULL ||
        fps == NULL || bitrate_kbps == NULL || codec == NULL || strtok_r(NULL, " ", &save) != NULL ||
        !parse_bounded_decimal(version, capability_protocol_version, capability_protocol_version, &parsed_version) ||
        !parse_profile_generation(generation, &parsed_generation) ||
        !parse_bounded_decimal(width, 64L, state->maximum_display_width, &parsed_width) ||
        !parse_bounded_decimal(height, 64L, state->maximum_display_height, &parsed_height) ||
        !parse_bounded_decimal(fps, 10L, state->maximum_display_fps, &parsed_fps) ||
        !parse_bounded_decimal(bitrate_kbps, 500L, 500000L, &parsed_bitrate_kbps) ||
        !supported_video_codec(codec)) {
      write_line(state->fd, "ERR ", await ? "BAD_AWAIT_CONNECTION_PROFILE" : "BAD_COMMIT_CONNECTION_PROFILE");
      return;
    }
    (void) parsed_version;
    struct connection_profile expected = requested;
    expected.generation = parsed_generation;
    expected.width = parsed_width;
    expected.height = parsed_height;
    expected.fps = parsed_fps;
    expected.bitrate_kbps = parsed_bitrate_kbps;
    if (!state->pending_profile_valid || (await && !state->pending_profile_committed) ||
        !equal_connection_profiles(&expected, &state->pending_profile)) {
      write_line(state->fd, "ERR ", await ? "NO_PENDING_CONNECTION_PROFILE" : "BAD_CONNECTION_PROFILE_COMMIT");
      return;
    }
    if (!await) {
      if (!write_connection_profile(state, &state->pending_profile)) {
        write_line(state->fd, "ERR ", "PROFILE_STATE");
        return;
      }
      state->pending_profile_committed = true;
      char reply[128];
      if (!connection_profile_reply(&state->pending_profile, reply, sizeof(reply))) {
        write_line(state->fd, "ERR ", "PROFILE_REPLY");
        return;
      }
      write_line(state->fd, "CONNECTION_PROFILE_PENDING ", reply);
      return;
    }
    if (!await_profile_applied(state, &state->pending_profile)) {
      state->pending_profile_valid = false;
      state->pending_profile_committed = false;
      write_line(state->fd, "ERR ", "PROFILE_NOT_APPLIED");
      return;
    }
    char reply[128];
    if (!connection_profile_reply(&state->pending_profile, reply, sizeof(reply))) {
      write_line(state->fd, "ERR ", "PROFILE_REPLY");
      return;
    }
    state->pending_profile_valid = false;
    state->pending_profile_committed = false;
    write_line(state->fd, "CONNECTION_PROFILE ", reply);
    return;
  }
  if (strcmp(command, "RESIZE") == 0) {
    char *width = strtok_r(NULL, " ", &save);
    char *height = strtok_r(NULL, " ", &save);
    char *end_width = NULL;
    char *end_height = NULL;
    const long parsed_width = width == NULL ? 0L : strtol(width, &end_width, 10);
    const long parsed_height = height == NULL ? 0L : strtol(height, &end_height, 10);
    if (width == NULL || height == NULL || strtok_r(NULL, " ", &save) != NULL ||
        *end_width != '\0' || *end_height != '\0' || parsed_width < 64L || parsed_width > 16384L ||
        parsed_height < 64L || parsed_height > 16384L) {
      write_line(state->fd, "ERR ", "BAD_RESIZE");
      return;
    }
    char path[768];
    char dimensions[64];
    if (snprintf(path, sizeof(path), "%s/resolution", state->state_dir) >= (int) sizeof(path) ||
        snprintf(dimensions, sizeof(dimensions), "%ldx%ld\n", parsed_width, parsed_height) >= (int) sizeof(dimensions) ||
        write_file_atomic(path, (const uint8_t *) dimensions, strlen(dimensions)) != 0) {
      write_line(state->fd, "ERR ", "RESIZE_STATE");
      return;
    }
    char reply[64];
    (void) snprintf(reply, sizeof(reply), "RESIZE %ld %ld", parsed_width, parsed_height);
    write_line(state->fd, "OK ", reply);
    return;
  }
  write_line(state->fd, "ERR ", "UNKNOWN_COMMAND");
}

static int initialize_state(struct agent_state *state, const char *device, const char *state_dir) {
  memset(state, 0, sizeof(*state));
  if (strlen(state_dir) >= sizeof(state->state_dir) ||
      snprintf(state->clipboard_path, sizeof(state->clipboard_path), "%s/qsf-clipboard.txt", state_dir) >= (int) sizeof(state->clipboard_path) ||
      snprintf(state->clipboard_generation_path, sizeof(state->clipboard_generation_path), "%s/qsf-clipboard-generation", state_dir) >= (int) sizeof(state->clipboard_generation_path) ||
      snprintf(state->clipboard_applied_path, sizeof(state->clipboard_applied_path), "%s/qsf-clipboard-applied", state_dir) >= (int) sizeof(state->clipboard_applied_path) ||
      snprintf(state->clipboard_ready_path, sizeof(state->clipboard_ready_path), "%s/wayland-clipboard-bridge.ready", state_dir) >= (int) sizeof(state->clipboard_ready_path)
#if !QSM_DESKTOP_CLIPBOARD_ONLY
      || snprintf(state->incoming_dir, sizeof(state->incoming_dir), "%s/incoming", state_dir) >= (int) sizeof(state->incoming_dir)
      || snprintf(state->outgoing_dir, sizeof(state->outgoing_dir), "%s/outgoing", state_dir) >= (int) sizeof(state->outgoing_dir)
#endif
      ) {
    return -1;
  }
  strcpy(state->state_dir, state_dir);
  state->maximum_display_width = 16384L;
  state->maximum_display_height = 16384L;
  state->maximum_display_fps = 240L;
  state->require_profile_apply_ack = true;
  state->profile_apply_timeout_ms = 30000L;
  const char *maximum_width = getenv("QSM_DESKTOP_AGENT_MAX_WIDTH");
  const char *maximum_height = getenv("QSM_DESKTOP_AGENT_MAX_HEIGHT");
  const char *maximum_fps = getenv("QSM_DESKTOP_AGENT_MAX_FPS");
  const char *require_profile_apply_ack = getenv("QSM_DESKTOP_AGENT_REQUIRE_PROFILE_APPLY_ACK");
  const char *profile_apply_timeout_ms = getenv("QSM_DESKTOP_AGENT_PROFILE_APPLY_TIMEOUT_MS");
  long require_apply_ack = 1L;
  if ((maximum_width != NULL &&
       !parse_bounded_decimal(maximum_width, 64L, 16384L, &state->maximum_display_width)) ||
      (maximum_height != NULL &&
       !parse_bounded_decimal(maximum_height, 64L, 16384L, &state->maximum_display_height)) ||
      (maximum_fps != NULL &&
       !parse_bounded_decimal(maximum_fps, 10L, 240L, &state->maximum_display_fps)) ||
      (require_profile_apply_ack != NULL &&
       !parse_bounded_decimal(require_profile_apply_ack, 0L, 1L, &require_apply_ack)) ||
      (profile_apply_timeout_ms != NULL &&
       !parse_bounded_decimal(profile_apply_timeout_ms, 1000L, 60000L,
                              &state->profile_apply_timeout_ms))) {
    errno = EINVAL;
    return -1;
  }
  state->require_profile_apply_ack = require_apply_ack != 0L;
  struct timespec generation_clock;
  if (clock_gettime(CLOCK_REALTIME, &generation_clock) != 0) {
    return -1;
  }
  state->next_profile_generation = ((uint64_t) generation_clock.tv_sec << 32U) ^
    (uint64_t) generation_clock.tv_nsec ^ (uint64_t) getpid();
  if (state->next_profile_generation == UINT64_MAX) {
    state->next_profile_generation = 0U;
  }
  state->next_clipboard_generation = ((uint64_t) generation_clock.tv_nsec << 32U) ^
    (uint64_t) generation_clock.tv_sec ^ ((uint64_t) getpid() << 16U);
  if (state->next_clipboard_generation == UINT64_MAX) {
    state->next_clipboard_generation = 0U;
  }
  if (ensure_directory(state->state_dir) != 0
#if !QSM_DESKTOP_CLIPBOARD_ONLY
      || ensure_directory(state->incoming_dir) != 0 || ensure_directory(state->outgoing_dir) != 0
#endif
      ) {
    return -1;
  }
  if (access(state->clipboard_path, F_OK) != 0 && write_file_atomic(state->clipboard_path, (const uint8_t *) "", 0U) != 0) {
    return -1;
  }
  update_clipboard_stamp(state);
  state->fd = open(device, O_RDWR | O_CLOEXEC | O_NOCTTY);
  return state->fd < 0 ? -1 : 0;
}

static void reset_transport_state(struct agent_state *state) {
  /* A profile exchange is a single host-controller transaction.  Do not let
   * a reconnect complete a partially received transaction from a previous
   * controller; the durable profile files remain available to the desktop
   * adapter, while this connection-local protocol state is discarded. */
  state->pending_profile_valid = false;
  state->pending_profile_committed = false;
  memset(&state->pending_profile, 0, sizeof(state->pending_profile));
  memset(state->pending_profile_codec, 0, sizeof(state->pending_profile_codec));
}

static void reconnect_pause(void) {
  struct timespec remaining = {
    .tv_sec = 0,
    .tv_nsec = agent_reconnect_pause_ns,
  };
  while (nanosleep(&remaining, &remaining) != 0 && errno == EINTR) {
  }
}

static bool serve_transport(struct agent_state *state) {
  write_line(state->fd, "READY ", "QSF1");

  char *line = malloc(max_wire_bytes + 1U);
  size_t line_size = 0;
  if (line == NULL) {
    return false;
  }
  for (;;) {
    const struct pollfd descriptor = {.fd = state->fd, .events = POLLIN, .revents = 0};
    const int ready = poll((struct pollfd *) &descriptor, 1U, 250);
    if (ready < 0 && errno == EINTR) {
      continue;
    }
    if (ready < 0 || (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
      break;
    }
    if (ready > 0 && (descriptor.revents & POLLIN) != 0) {
      char buffer[4096];
      const ssize_t amount = read(state->fd, buffer, sizeof(buffer));
      if (amount <= 0) {
        break;
      }
      for (ssize_t index = 0; index < amount; ++index) {
        if (buffer[index] == '\n') {
          line[line_size] = '\0';
          handle_command(state, line);
          line_size = 0;
        } else if (line_size < max_wire_bytes) {
          line[line_size++] = buffer[index];
        } else {
          line_size = 0;
          write_line(state->fd, "ERR ", "LINE_TOO_LONG");
        }
      }
    }
    poll_guest_clipboard(state);
  }
  free(line);
  return true;
}

int main(int argc, char **argv) {
  const char *device = "/dev/virtio-ports/org.qsm.direct.agent";
  const char *state_dir = "/tmp/qsf";
  if (argc == 3 && strcmp(argv[1], "--device") == 0) {
    device = argv[2];
  } else if (argc == 3 && strcmp(argv[1], "--state-dir") == 0) {
    state_dir = argv[2];
  } else if (argc == 5 && strcmp(argv[1], "--device") == 0 && strcmp(argv[3], "--state-dir") == 0) {
    device = argv[2];
    state_dir = argv[4];
  } else if (argc != 1) {
    fprintf(stderr, "usage: %s [--device DEVICE] [--state-dir DIRECTORY]\n", argv[0]);
    return 2;
  }

  struct agent_state state;
  if (initialize_state(&state, device, state_dir) != 0) {
    fprintf(stderr, "qsm-desktop-agent: initialization failed: %s\n", strerror(errno));
    return 1;
  }
  /* A disconnected Unix-socket chardev may otherwise raise SIGPIPE while the
   * guest is reporting READY or an unsolicited clipboard event.  Treat it as
   * the ordinary reconnect condition handled below. */
  (void) signal(SIGPIPE, SIG_IGN);
  for (;;) {
    if (!serve_transport(&state)) {
      close(state.fd);
      return 1;
    }
    close(state.fd);
    state.fd = -1;
    reset_transport_state(&state);

    /* The terminal worker is intentionally created and retired separately
     * from the VM.  A host-side QSF controller can therefore disappear while
     * the guest stays up, and a VM reboot can make the path briefly absent
     * while the controller remains alive.  Reopen the same fixed virtio port
     * until the peer is present again; no data or controller identity crosses
     * this boundary during the retry. */
    do {
      reconnect_pause();
      state.fd = open(device, O_RDWR | O_CLOEXEC | O_NOCTTY);
    } while (state.fd < 0);
  }
}
