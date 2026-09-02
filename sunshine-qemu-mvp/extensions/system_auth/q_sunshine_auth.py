#!/usr/bin/env python3
"""Shared primitives for q-sunshine's short-lived system-auth tickets.

The ticket is deliberately not a replacement for GameStream on its own.  It
is a compact, VM-scoped credential for q-sunshine endpoints that explicitly
understand this protocol (the QSF gateway today, and a future native media
transport).  A stock Moonlight/Sunshine GameStream session continues to use
its own paired-client certificate until that media transport is replaced.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any


TICKET_VERSION = "qsa1"
TICKET_DOMAIN_SEPARATOR = b"q-sunshine-system-auth-ticket-v1\0"
TICKET_KEY_BYTES = 32
TICKET_TTL_MIN_SECONDS = 60
TICKET_TTL_MAX_SECONDS = 900
TICKET_CLOCK_SKEW_SECONDS = 30
MAX_TICKET_CHARACTERS = 1536
_SUBJECT_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}\Z")
_AUDIENCE_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_BASE64URL_PATTERN = re.compile(r"\A[A-Za-z0-9_-]+\Z")


class AuthError(RuntimeError):
    """A deliberately non-specific remote authentication failure."""


def valid_subject(subject: str) -> bool:
    """Return whether a PAM username is safe to encode in a ticket."""
    return bool(_SUBJECT_PATTERN.fullmatch(subject))


def valid_audience(audience: str) -> bool:
    """Return whether an instance-local ticket audience is well formed."""
    return bool(_AUDIENCE_PATTERN.fullmatch(audience))


def _encode_base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str, *, maximum_bytes: int) -> bytes:
    if (not isinstance(value, str) or not value or len(value) > MAX_TICKET_CHARACTERS or
            not _BASE64URL_PATTERN.fullmatch(value)):
        raise AuthError("authentication required")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise AuthError("authentication required") from error
    if len(decoded) > maximum_bytes or _encode_base64url(decoded) != value:
        raise AuthError("authentication required")
    return decoded


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _ticket_signature(key: bytes, encoded_payload: str) -> bytes:
    return hmac.new(key, TICKET_DOMAIN_SEPARATOR + encoded_payload.encode("ascii"),
                    hashlib.sha256).digest()


def load_ticket_key(path: Path, *, require_root_owner: bool = False) -> bytes:
    """Read exactly one root-local raw 256-bit signing key without symlinks.

    The installed systemd service runs as root and requests root ownership.
    Keeping the optional switch here lets an unprivileged developer exercise
    the protocol fixture with a temporary key; it is not used by deployment.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AuthError("cannot read system-auth ticket key") from error
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or
                (require_root_owner and metadata.st_uid != 0) or
                metadata.st_size != TICKET_KEY_BYTES):
            raise AuthError("system-auth ticket key must be a root-owned mode-0600 32-byte regular file")
        key = bytearray()
        while len(key) < TICKET_KEY_BYTES + 1:
            block = os.read(descriptor, TICKET_KEY_BYTES + 1 - len(key))
            if not block:
                break
            key.extend(block)
        if len(key) != TICKET_KEY_BYTES:
            raise AuthError("system-auth ticket key has an invalid size")
        return bytes(key)
    except OSError as error:
        raise AuthError("cannot read system-auth ticket key") from error
    finally:
        os.close(descriptor)


def _open_tls_material(path: Path, *, private_key: bool,
                       require_root_owner: bool) -> int:
    """Open one non-symlink TLS file and retain its checked descriptor.

    ``SSLContext.load_cert_chain()`` normally reopens pathnames after caller
    validation, leaving a root-service TOCTOU window. On supported Linux hosts
    it can instead read the checked descriptor through ``/proc/self/fd``.
    """
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None or not os.path.isdir("/proc/self/fd"):
        raise AuthError("secure TLS server material loading is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AuthError("cannot load TLS server material") from error
    try:
        metadata = os.fstat(descriptor)
        unsafe_mode = 0o077 if private_key else 0o022
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & unsafe_mode or
                (require_root_owner and metadata.st_uid != 0)):
            raise AuthError("TLS server material must be root-owned regular files with safe modes")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def load_tls_server_cert_chain(context: Any, certificate_path: Path,
                               private_key_path: Path, *,
                               require_root_owner: bool = False) -> None:
    """Load a TLS leaf/key only from checked Linux descriptors.

    A public certificate may be readable, but neither file may be group/world
    writable; the key also must be inaccessible to group/other. Root-run
    packaged services require root ownership of both files.
    """
    certificate_descriptor = _open_tls_material(
        certificate_path, private_key=False, require_root_owner=require_root_owner)
    private_key_descriptor = -1
    try:
        private_key_descriptor = _open_tls_material(
            private_key_path, private_key=True, require_root_owner=require_root_owner)
        context.load_cert_chain(
            certfile=f"/proc/self/fd/{certificate_descriptor}",
            keyfile=f"/proc/self/fd/{private_key_descriptor}",
        )
    except (OSError, ValueError) as error:
        raise AuthError("cannot load TLS server material") from error
    finally:
        if private_key_descriptor >= 0:
            os.close(private_key_descriptor)
        os.close(certificate_descriptor)


def issue_ticket(key: bytes, *, subject: str, audience: str, ttl_seconds: int,
                 now: int | None = None) -> tuple[str, int]:
    """Issue a signed, short-lived ticket and return it with its UTC expiry."""
    if len(key) != TICKET_KEY_BYTES or not valid_subject(subject) or not valid_audience(audience):
        raise AuthError("cannot issue system-auth ticket")
    if not TICKET_TTL_MIN_SECONDS <= ttl_seconds <= TICKET_TTL_MAX_SECONDS:
        raise AuthError("cannot issue system-auth ticket")
    issued_at = int(time.time()) if now is None else now
    expires_at = issued_at + ttl_seconds
    payload = {
        "aud": audience,
        "exp": expires_at,
        "iat": issued_at,
        "jti": _encode_base64url(secrets.token_bytes(16)),
        "sub": subject,
        "v": 1,
    }
    encoded_payload = _encode_base64url(_canonical_payload(payload))
    signature = _encode_base64url(_ticket_signature(key, encoded_payload))
    return f"{TICKET_VERSION}.{encoded_payload}.{signature}", expires_at


def verify_ticket(key: bytes, ticket: str, *, audience: str,
                  now: int | None = None) -> tuple[str, int]:
    """Verify an opaque bearer ticket and return its subject and UTC expiry.

    The verifier intentionally has only a generic failure result.  Callers
    must not use it as a username or ticket validity oracle.
    """
    if len(key) != TICKET_KEY_BYTES or not valid_audience(audience):
        raise AuthError("authentication required")
    if not isinstance(ticket, str) or len(ticket) > MAX_TICKET_CHARACTERS:
        raise AuthError("authentication required")
    pieces = ticket.split(".")
    if len(pieces) != 3 or pieces[0] != TICKET_VERSION:
        raise AuthError("authentication required")
    encoded_payload, encoded_signature = pieces[1:]
    signature = _decode_base64url(encoded_signature, maximum_bytes=hashlib.sha256().digest_size)
    expected = _ticket_signature(key, encoded_payload)
    if len(signature) != len(expected) or not hmac.compare_digest(signature, expected):
        raise AuthError("authentication required")
    try:
        payload_bytes = _decode_base64url(encoded_payload, maximum_bytes=1024)
        payload = json.loads(payload_bytes.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError, AuthError) as error:
        raise AuthError("authentication required") from error
    if not isinstance(payload, dict) or set(payload) != {"aud", "exp", "iat", "jti", "sub", "v"}:
        raise AuthError("authentication required")
    subject = payload.get("sub")
    payload_audience = payload.get("aud")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    ticket_id = payload.get("jti")
    if (payload.get("v") != 1 or not isinstance(subject, str) or
            not isinstance(payload_audience, str) or type(issued_at) is not int or
            type(expires_at) is not int or not isinstance(ticket_id, str) or
            not valid_subject(subject) or payload_audience != audience):
        raise AuthError("authentication required")
    try:
        if len(_decode_base64url(ticket_id, maximum_bytes=16)) != 16:
            raise AuthError("authentication required")
    except AuthError as error:
        raise AuthError("authentication required") from error
    current_time = int(time.time()) if now is None else now
    if (expires_at <= current_time or issued_at > current_time + TICKET_CLOCK_SKEW_SECONDS or
            expires_at <= issued_at or expires_at - issued_at > TICKET_TTL_MAX_SECONDS):
        raise AuthError("authentication required")
    return subject, expires_at
