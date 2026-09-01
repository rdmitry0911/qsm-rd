/*
 * qsf_guest_agent.c - minimal guest endpoint for the QSF companion channel.
 *
 * The agent deliberately accepts only a narrow line protocol over a QEMU
 * virtio-serial port.  It never interprets a filename as a path and it never
 * executes data received from the client.  The state files make the protocol
 * useful on minimal guests; a desktop-specific adapter may mirror
 * qsf-clipboard.txt to the guest's native clipboard separately.  This agent
 * itself has no desktop-toolkit dependency.
 */

#define _POSIX_C_SOURCE 200809L

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

enum {
  max_clipboard_bytes = 1024 * 1024,
  max_file_bytes = 2 * 1024 * 1024,
  max_wire_bytes = 4 * 1024 * 1024,
};

struct agent_state {
  int fd;
  char state_dir[512];
  char clipboard_path[640];
  char incoming_dir[640];
  char outgoing_dir[640];
  struct timespec clipboard_mtime;
  off_t clipboard_size;
  bool clipboard_stamp_valid;
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

static bool safe_name(const char *name) {
  if (name == NULL || name[0] == '\0' || strlen(name) > 128) {
    return false;
  }
  for (const unsigned char *cursor = (const unsigned char *) name; *cursor; ++cursor) {
    if (!(isalnum(*cursor) || *cursor == '.' || *cursor == '_' || *cursor == '-')) {
      return false;
    }
  }
  return strstr(name, "..") == NULL;
}

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
 * library. CLIP_SET is text-only; FILE_PUT intentionally remains binary-safe. */
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
    if (data == NULL || !valid_clipboard_utf8(data, size) ||
        write_file_atomic(state->clipboard_path, data, size) != 0) {
      free(data);
      write_line(state->fd, "ERR ", "BAD_CLIPBOARD");
      return;
    }
    free(data);
    update_clipboard_stamp(state);
    write_line(state->fd, "OK ", "CLIP_SET");
    return;
  }
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
    char *reply = NULL;
    const size_t reply_size = strlen(name) + strlen(wire_payload) + 7U;
    reply = malloc(reply_size);
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
      snprintf(state->incoming_dir, sizeof(state->incoming_dir), "%s/incoming", state_dir) >= (int) sizeof(state->incoming_dir) ||
      snprintf(state->outgoing_dir, sizeof(state->outgoing_dir), "%s/outgoing", state_dir) >= (int) sizeof(state->outgoing_dir)) {
    return -1;
  }
  strcpy(state->state_dir, state_dir);
  if (ensure_directory(state->state_dir) != 0 || ensure_directory(state->incoming_dir) != 0 || ensure_directory(state->outgoing_dir) != 0) {
    return -1;
  }
  if (access(state->clipboard_path, F_OK) != 0 && write_file_atomic(state->clipboard_path, (const uint8_t *) "", 0U) != 0) {
    return -1;
  }
  update_clipboard_stamp(state);
  state->fd = open(device, O_RDWR | O_CLOEXEC | O_NOCTTY);
  return state->fd < 0 ? -1 : 0;
}

int main(int argc, char **argv) {
  const char *device = "/dev/virtio-ports/org.q-sunshine.agent";
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
    fprintf(stderr, "qsf-guest-agent: initialization failed: %s\n", strerror(errno));
    return 1;
  }
  write_line(state.fd, "READY ", "QSF1");

  char *line = malloc(max_wire_bytes + 1U);
  size_t line_size = 0;
  if (line == NULL) {
    close(state.fd);
    return 1;
  }
  for (;;) {
    const struct pollfd descriptor = {.fd = state.fd, .events = POLLIN, .revents = 0};
    const int ready = poll((struct pollfd *) &descriptor, 1U, 250);
    if (ready < 0 && errno == EINTR) {
      continue;
    }
    if (ready < 0 || (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
      break;
    }
    if (ready > 0 && (descriptor.revents & POLLIN) != 0) {
      char buffer[4096];
      const ssize_t amount = read(state.fd, buffer, sizeof(buffer));
      if (amount <= 0) {
        break;
      }
      for (ssize_t index = 0; index < amount; ++index) {
        if (buffer[index] == '\n') {
          line[line_size] = '\0';
          handle_command(&state, line);
          line_size = 0;
        } else if (line_size < max_wire_bytes) {
          line[line_size++] = buffer[index];
        } else {
          line_size = 0;
          write_line(state.fd, "ERR ", "LINE_TOO_LONG");
        }
      }
    }
    poll_guest_clipboard(&state);
  }
  free(line);
  close(state.fd);
  return 0;
}
