/* SPDX-License-Identifier: GPL-3.0-or-later */
/*
 * Minimal stdin-only PAM conversation for q-sunshine-auth.
 *
 * The Python TLS gateway passes a two-field length-prefixed packet on stdin
 * and the kernel-observed remote IP with --rhost. The password is never placed
 * in argv, an environment variable, a log, or a temporary file. This helper
 * intentionally produces no diagnostic output: remote callers get one
 * non-enumerating authentication failure either way.
 *
 * The wire protocol carries exactly a username and password. It deliberately
 * rejects a second interactive PAM prompt (for example an OTP challenge)
 * rather than accidentally treating the password as an answer to it.
 */

#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <security/pam_appl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef __linux__
#include <sys/prctl.h>
#endif

#define MAX_USERNAME_BYTES 64U
#define MAX_PASSWORD_BYTES 4096U

struct credential_data {
    char username[MAX_USERNAME_BYTES + 1U];
    char password[MAX_PASSWORD_BYTES + 1U];
    unsigned int username_prompt_count;
    unsigned int password_prompt_count;
};

static void secure_zero(void *pointer, size_t length) {
    volatile unsigned char *bytes = pointer;
    while (length-- != 0U) {
        *bytes++ = 0U;
    }
}

static int read_exact(int descriptor, void *destination, size_t length) {
    unsigned char *cursor = destination;
    while (length != 0U) {
        ssize_t count = read(descriptor, cursor, length);
        if (count == 0) {
            return -1;
        }
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        cursor += (size_t) count;
        length -= (size_t) count;
    }
    return 0;
}

static int valid_service(const char *service) {
    size_t length = 0U;
    if (service == NULL) {
        return 0;
    }
    for (; service[length] != '\0'; ++length) {
        const unsigned char character = (unsigned char) service[length];
        if (!((character >= 'A' && character <= 'Z') ||
              (character >= 'a' && character <= 'z') ||
              (character >= '0' && character <= '9') ||
              character == '_' || character == '.' || character == '-')) {
            return 0;
        }
        if (length >= 64U) {
            return 0;
        }
    }
    return length != 0U;
}

static int valid_username(const char *username, size_t length) {
    size_t index;
    if (length == 0U || length > MAX_USERNAME_BYTES) {
        return 0;
    }
    for (index = 0U; index < length; ++index) {
        const unsigned char character = (unsigned char) username[index];
        const int is_alnum = (character >= 'A' && character <= 'Z') ||
                             (character >= 'a' && character <= 'z') ||
                             (character >= '0' && character <= '9');
        if (!(is_alnum || character == '_' || character == '.' ||
              character == '@' || character == '-')) {
            return 0;
        }
    }
    return 1;
}

static int valid_remote_host(const char *remote_host) {
    struct in_addr address_v4;
    struct in6_addr address_v6;

    if (remote_host == NULL || remote_host[0] == '\0') {
        return 0;
    }
    return inet_pton(AF_INET, remote_host, &address_v4) == 1 ||
           inet_pton(AF_INET6, remote_host, &address_v6) == 1;
}

static int read_credentials(struct credential_data *credentials) {
    uint16_t encoded_length;
    size_t username_length;
    size_t password_length;

    if (read_exact(STDIN_FILENO, &encoded_length, sizeof(encoded_length)) != 0) {
        return -1;
    }
    username_length = (size_t) ntohs(encoded_length);
    if (username_length == 0U || username_length > MAX_USERNAME_BYTES ||
        read_exact(STDIN_FILENO, credentials->username, username_length) != 0 ||
        !valid_username(credentials->username, username_length)) {
        return -1;
    }
    credentials->username[username_length] = '\0';
    if (read_exact(STDIN_FILENO, &encoded_length, sizeof(encoded_length)) != 0) {
        return -1;
    }
    password_length = (size_t) ntohs(encoded_length);
    if (password_length == 0U || password_length > MAX_PASSWORD_BYTES ||
        read_exact(STDIN_FILENO, credentials->password, password_length) != 0 ||
        memchr(credentials->password, '\0', password_length) != NULL) {
        return -1;
    }
    credentials->password[password_length] = '\0';
    return 0;
}

static int conversation(int message_count, const struct pam_message **messages,
                        struct pam_response **responses, void *application_data) {
    struct credential_data *credentials = application_data;
    struct pam_response *answer;
    int index;

    if (message_count <= 0 || messages == NULL || responses == NULL || credentials == NULL) {
        return PAM_CONV_ERR;
    }
    answer = calloc((size_t) message_count, sizeof(*answer));
    if (answer == NULL) {
        return PAM_BUF_ERR;
    }
    for (index = 0; index < message_count; ++index) {
        if (messages[index] == NULL) {
            goto fail;
        }
        switch (messages[index]->msg_style) {
        case PAM_PROMPT_ECHO_ON:
            if (credentials->username_prompt_count != 0U) {
                goto fail;
            }
            ++credentials->username_prompt_count;
            answer[index].resp = strdup(credentials->username);
            break;
        case PAM_PROMPT_ECHO_OFF:
            if (credentials->password_prompt_count != 0U) {
                goto fail;
            }
            ++credentials->password_prompt_count;
            answer[index].resp = strdup(credentials->password);
            break;
        case PAM_ERROR_MSG:
        case PAM_TEXT_INFO:
            answer[index].resp = NULL;
            break;
        default:
            goto fail;
        }
        if ((messages[index]->msg_style == PAM_PROMPT_ECHO_ON ||
             messages[index]->msg_style == PAM_PROMPT_ECHO_OFF) && answer[index].resp == NULL) {
            goto fail;
        }
    }
    *responses = answer;
    return PAM_SUCCESS;

fail:
    for (index = 0; index < message_count; ++index) {
        if (answer[index].resp != NULL) {
            secure_zero(answer[index].resp, strlen(answer[index].resp));
            free(answer[index].resp);
        }
    }
    free(answer);
    return PAM_CONV_ERR;
}

int main(int argc, char **argv) {
    const char *service = NULL;
    const char *remote_host = NULL;
    struct credential_data credentials;
    struct pam_conv pam_conversation;
    pam_handle_t *handle = NULL;
    int result = EXIT_FAILURE;
    int pam_result = PAM_SYSTEM_ERR;

    memset(&credentials, 0, sizeof(credentials));
#ifdef __linux__
    (void) prctl(PR_SET_DUMPABLE, 0, 0, 0, 0);
#endif
    if (argc == 5 && strcmp(argv[1], "--service") == 0 &&
        strcmp(argv[3], "--rhost") == 0) {
        service = argv[2];
        remote_host = argv[4];
    }
    if (!valid_service(service) || !valid_remote_host(remote_host) ||
        read_credentials(&credentials) != 0) {
        goto done;
    }
    pam_conversation.conv = conversation;
    pam_conversation.appdata_ptr = &credentials;
    pam_result = pam_start(service, credentials.username, &pam_conversation, &handle);
    if (pam_result != PAM_SUCCESS) {
        goto done;
    }
    pam_result = pam_set_item(handle, PAM_RHOST, remote_host);
    if (pam_result == PAM_SUCCESS) {
        pam_result = pam_authenticate(handle, PAM_DISALLOW_NULL_AUTHTOK);
    }
    if (pam_result == PAM_SUCCESS) {
        pam_result = pam_acct_mgmt(handle, 0);
    }

done:
    if (handle != NULL) {
        const int end_result = pam_end(handle, pam_result);
        if (pam_result == PAM_SUCCESS && end_result != PAM_SUCCESS) {
            pam_result = end_result;
        }
    }
    if (pam_result == PAM_SUCCESS) {
        result = EXIT_SUCCESS;
    }
    secure_zero(&credentials, sizeof(credentials));
    return result;
}
