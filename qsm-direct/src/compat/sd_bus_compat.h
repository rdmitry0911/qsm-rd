#pragma once

// Minimal sd-bus ABI declarations used by this project.  Production builds
// prefer <systemd/sd-bus.h>; this fallback keeps the protocol tests buildable
// in lean containers that ship libsystemd.so but not libsystemd-dev.
//
// The declarations mirror the public systemd API (LGPL-2.1-or-later).

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct sd_bus sd_bus;
typedef struct sd_bus_message sd_bus_message;
typedef struct sd_bus_slot sd_bus_slot;
typedef struct sd_bus_creds sd_bus_creds;

typedef union sd_id128 {
    uint8_t bytes[16];
    uint64_t qwords[2];
} sd_id128_t;

typedef struct sd_bus_error {
    const char *name;
    const char *message;
    int _need_free;
} sd_bus_error;

typedef int (*sd_bus_message_handler_t)(sd_bus_message *message,
                                        void *userdata,
                                        sd_bus_error *ret_error);

int sd_id128_randomize(sd_id128_t *ret);

int sd_bus_new(sd_bus **ret);
int sd_bus_open_user(sd_bus **ret);
int sd_bus_set_address(sd_bus *bus, const char *address);
int sd_bus_set_fd(sd_bus *bus, int input_fd, int output_fd);
int sd_bus_set_bus_client(sd_bus *bus, int enabled);
int sd_bus_set_server(sd_bus *bus, int enabled, sd_id128_t server_id);
int sd_bus_set_anonymous(sd_bus *bus, int enabled);
int sd_bus_set_trusted(sd_bus *bus, int enabled);
int sd_bus_negotiate_fds(sd_bus *bus, int enabled);
int sd_bus_set_method_call_timeout(sd_bus *bus, uint64_t usec);
int sd_bus_start(sd_bus *bus);
int sd_bus_request_name(sd_bus *bus, const char *name, uint64_t flags);
int sd_bus_is_ready(sd_bus *bus);
int sd_bus_process(sd_bus *bus, sd_bus_message **ret);
int sd_bus_wait(sd_bus *bus, uint64_t timeout_usec);
int sd_bus_flush(sd_bus *bus);
void sd_bus_close(sd_bus *bus);
sd_bus *sd_bus_unref(sd_bus *bus);

int sd_bus_add_filter(sd_bus *bus,
                      sd_bus_slot **ret_slot,
                      sd_bus_message_handler_t callback,
                      void *userdata);
sd_bus_slot *sd_bus_slot_unref(sd_bus_slot *slot);

sd_bus *sd_bus_message_get_bus(sd_bus_message *message);
int sd_bus_message_send(sd_bus_message *message);

int sd_bus_message_is_method_call(sd_bus_message *message,
                                  const char *interface,
                                  const char *member);
int sd_bus_message_has_signature(sd_bus_message *message,
                                 const char *signature);
const char *sd_bus_message_get_path(sd_bus_message *message);
const char *sd_bus_message_get_interface(sd_bus_message *message);
const char *sd_bus_message_get_member(sd_bus_message *message);
const char *sd_bus_message_get_signature(sd_bus_message *message,
                                         int complete);
sd_bus *sd_bus_message_get_bus(sd_bus_message *message);
const char *sd_bus_message_get_sender(sd_bus_message *message);
uint8_t sd_bus_message_get_type(sd_bus_message *message);
int sd_bus_message_read(sd_bus_message *message, const char *types, ...);
int sd_bus_message_read_array(sd_bus_message *message,
                              char element_type,
                              const void **ret_ptr,
                              size_t *ret_size);
int sd_bus_message_read_basic(sd_bus_message *message,
                              char type,
                              void *ret);
int sd_bus_message_enter_container(sd_bus_message *message,
                                   char type,
                                   const char *contents);
int sd_bus_message_exit_container(sd_bus_message *message);
int sd_bus_message_at_end(sd_bus_message *message, int complete);
int sd_bus_message_rewind(sd_bus_message *message, int complete);

int sd_bus_message_new_method_call(sd_bus *bus,
                                   sd_bus_message **ret,
                                   const char *destination,
                                   const char *path,
                                   const char *interface,
                                   const char *member);
int sd_bus_message_new_method_return(sd_bus_message *call,
                                     sd_bus_message **ret);
int sd_bus_message_append(sd_bus_message *message,
                          const char *types,
                          ...);
int sd_bus_message_append_basic(sd_bus_message *message,
                                char type,
                                const void *value);
int sd_bus_message_append_array(sd_bus_message *message,
                                char element_type,
                                const void *data,
                                size_t size);
int sd_bus_message_open_container(sd_bus_message *message,
                                  char type,
                                  const char *contents);
int sd_bus_message_close_container(sd_bus_message *message);
int sd_bus_message_set_expect_reply(sd_bus_message *message, int enabled);
int sd_bus_send(sd_bus *bus, sd_bus_message *message, uint64_t *ret_cookie);
int sd_bus_call(sd_bus *bus,
                sd_bus_message *message,
                uint64_t timeout_usec,
                sd_bus_error *ret_error,
                sd_bus_message **ret_reply);
int sd_bus_call_method(sd_bus *bus,
                       const char *destination,
                       const char *path,
                       const char *interface,
                       const char *member,
                       sd_bus_error *ret_error,
                       sd_bus_message **ret_reply,
                       const char *types,
                       ...);
int sd_bus_reply_method_return(sd_bus_message *call,
                               const char *types,
                               ...);
int sd_bus_reply_method_errorf(sd_bus_message *call,
                               const char *name,
                               const char *format,
                               ...);
sd_bus_message *sd_bus_message_unref(sd_bus_message *message);

void sd_bus_error_free(sd_bus_error *error);
int sd_bus_error_is_set(const sd_bus_error *error);

#ifdef __cplusplus
}
#endif
