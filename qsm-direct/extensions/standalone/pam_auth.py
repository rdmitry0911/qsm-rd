#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-shot Linux-PAM helper. Credentials travel over stdin, never argv/env."""
import ctypes as C
import json
import os
import pwd
import re
import sys


def authenticate(service: str, username: str, password: str, remote: str = "") -> dict | None:
    if (not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", username) or not password or
            len(password.encode("utf-8")) > 1024 or "\0" in password):
        return None
    if os.geteuid() != 0:
        try:
            account = pwd.getpwnam(username)
        except KeyError:
            return None
        if account.pw_uid != os.geteuid():
            return None
    pam = C.CDLL("libpam.so.0")
    libc = C.CDLL(None)
    libc.calloc.argtypes, libc.calloc.restype = [C.c_size_t, C.c_size_t], C.c_void_p
    libc.strdup.argtypes, libc.strdup.restype = [C.c_char_p], C.c_void_p
    libc.free.argtypes = [C.c_void_p]

    class Message(C.Structure):
        _fields_ = [("style", C.c_int), ("text", C.c_char_p)]

    class Response(C.Structure):
        _fields_ = [("text", C.c_void_p), ("code", C.c_int)]

    callback_type = C.CFUNCTYPE(C.c_int, C.c_int, C.POINTER(C.POINTER(Message)),
                               C.POINTER(C.POINTER(Response)), C.c_void_p)

    @callback_type
    def conversation(count, messages, output, _data):
        if not 1 <= count <= 32:
            return 19  # PAM_CONV_ERR
        address = libc.calloc(count, C.sizeof(Response))
        if not address:
            return 5  # PAM_BUF_ERR
        responses = C.cast(address, C.POINTER(Response))
        try:
            for index in range(count):
                style = messages[index].contents.style
                if style in (1, 2):  # PAM_PROMPT_ECHO_OFF / PAM_PROMPT_ECHO_ON
                    value = password if style == 1 else username
                    responses[index].text = libc.strdup(value.encode("utf-8"))
                    if not responses[index].text:
                        raise MemoryError
                elif style not in (3, 4):  # PAM_ERROR_MSG / PAM_TEXT_INFO
                    raise ValueError
            output[0] = responses
            return 0
        except Exception:
            for index in range(count):
                if responses[index].text:
                    libc.free(responses[index].text)
            libc.free(address)
            return 19

    class Conversation(C.Structure):
        _fields_ = [("callback", callback_type), ("data", C.c_void_p)]

    pam.pam_start.argtypes = [C.c_char_p, C.c_char_p, C.POINTER(Conversation), C.POINTER(C.c_void_p)]
    pam.pam_authenticate.argtypes = [C.c_void_p, C.c_int]
    pam.pam_acct_mgmt.argtypes = [C.c_void_p, C.c_int]
    pam.pam_set_item.argtypes = [C.c_void_p, C.c_int, C.c_void_p]
    pam.pam_get_item.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_void_p)]
    pam.pam_end.argtypes = [C.c_void_p, C.c_int]
    handle = C.c_void_p()
    conv = Conversation(conversation, None)
    status = pam.pam_start(service.encode("ascii"), username.encode("utf-8"), C.byref(conv), C.byref(handle))
    if status != 0:
        return None
    try:
        if remote:
            pam.pam_set_item(handle, 4, C.c_char_p(remote.encode("ascii")))  # PAM_RHOST
        status = pam.pam_authenticate(handle, 1)  # PAM_DISALLOW_NULL_AUTHTOK
        if status != 0:
            return None
        status = pam.pam_acct_mgmt(handle, 1)
        if status != 0:  # includes expired account/password; no password changes over this API
            return None
        name = C.c_void_p()
        if pam.pam_get_item(handle, 2, C.byref(name)) != 0 or not name.value:  # PAM_USER
            return None
        account = pwd.getpwnam(C.cast(name, C.c_char_p).value.decode("utf-8"))
        if os.geteuid() != 0 and account.pw_uid != os.geteuid():
            return None
        return {"uid": account.pw_uid, "gid": account.pw_gid, "name": account.pw_name}
    finally:
        pam.pam_end(handle, status)


if __name__ == "__main__":
    try:
        payload = json.loads(sys.stdin.buffer.read(8193))
        result = authenticate(sys.argv[1], payload["username"], payload["password"], payload.get("remote", ""))
    except Exception:
        result = None
    print(json.dumps(result))
