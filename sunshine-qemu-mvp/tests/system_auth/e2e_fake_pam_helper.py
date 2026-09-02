#!/usr/bin/env python3
"""Narrow deterministic PAM-helper fixture for the native visual E2E hook.

It implements only the stdin packet contract used by ``PamAuthenticator``.
The fixed account is deliberately an E2E fixture, not a production login:
``alice`` / ``e2e-system-password``.  Keeping the password out of command
arguments and the environment is what the surrounding hook verifies.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--service", required=True)
    parser.add_argument("--rhost", required=True)
    try:
        parser.parse_args()
    except SystemExit:
        return 1

    packet = sys.stdin.buffer.read(4096 + 128)
    valid = False
    if len(packet) >= 4:
        username_length = int.from_bytes(packet[:2], "big")
        username_end = 2 + username_length
        if username_end + 2 <= len(packet):
            password_length = int.from_bytes(packet[username_end:username_end + 2], "big")
            password_start = username_end + 2
            password_end = password_start + password_length
            valid = (
                password_end == len(packet)
                and packet[2:username_end] == b"alice"
                and packet[password_start:password_end] == b"e2e-system-password"
            )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
