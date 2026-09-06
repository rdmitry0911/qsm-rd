/* qsf_state_watcher.c - event source for the QSM guest clipboard bridge.
 *
 * The Wayland bridge and qsf_guest_agent exchange clipboard state through an
 * atomically-renamed file.  Polling that file from a shell is wasteful, and
 * polling wl-paste as a proxy for it makes Plasma visibly touch its clipboard
 * on every pass.  This deliberately tiny Linux-only helper emits one line per
 * relevant directory event and has no desktop-library dependency.
 */

#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <unistd.h>

enum { event_buffer_size = 8192 };

static void usage(const char *program) {
  fprintf(stderr, "usage: %s --directory DIRECTORY --name FILE\n", program);
}

static bool selected_name(const struct inotify_event *event, const char *name) {
  return event->len > 0U && strcmp(event->name, name) == 0;
}

static bool report_change(void) {
  return fputs("changed\n", stdout) >= 0 && fflush(stdout) == 0;
}

int main(int argc, char **argv) {
  const char *directory = NULL;
  const char *name = NULL;
  for (int index = 1; index < argc; index += 2) {
    if (index + 1 >= argc) {
      usage(argv[0]);
      return 2;
    }
    if (strcmp(argv[index], "--directory") == 0) {
      directory = argv[index + 1];
    } else if (strcmp(argv[index], "--name") == 0) {
      name = argv[index + 1];
    } else {
      usage(argv[0]);
      return 2;
    }
  }
  if (directory == NULL || name == NULL || name[0] == '\0' || strchr(name, '/') != NULL) {
    usage(argv[0]);
    return 2;
  }

  const int descriptor = inotify_init1(IN_CLOEXEC);
  if (descriptor < 0) {
    perror("inotify_init1");
    return 1;
  }
  const uint32_t mask = IN_CLOSE_WRITE | IN_MOVED_TO | IN_ATTRIB | IN_DELETE_SELF | IN_MOVE_SELF;
  const int watch = inotify_add_watch(descriptor, directory, mask);
  if (watch < 0) {
    perror("inotify_add_watch");
    close(descriptor);
    return 1;
  }

  char buffer[event_buffer_size];
  for (;;) {
    const ssize_t bytes = read(descriptor, buffer, sizeof(buffer));
    if (bytes < 0 && errno == EINTR) {
      continue;
    }
    if (bytes <= 0) {
      if (bytes < 0) {
        perror("inotify read");
      }
      close(descriptor);
      return 1;
    }
    bool changed = false;
    for (size_t offset = 0U; offset + sizeof(struct inotify_event) <= (size_t) bytes;) {
      const struct inotify_event *event = (const struct inotify_event *) (buffer + offset);
      if ((event->mask & IN_Q_OVERFLOW) != 0U ||
          ((event->mask & (IN_CLOSE_WRITE | IN_MOVED_TO | IN_ATTRIB)) != 0U &&
           selected_name(event, name))) {
        changed = true;
      }
      if ((event->mask & (IN_IGNORED | IN_DELETE_SELF | IN_MOVE_SELF)) != 0U) {
        /* The state directory is not expected to move.  Reporting one change
         * lets the bridge make a final safe comparison before it restarts. */
        changed = true;
      }
      offset += sizeof(*event) + event->len;
    }
    if (changed && !report_change()) {
      close(descriptor);
      return 1;
    }
  }
}
