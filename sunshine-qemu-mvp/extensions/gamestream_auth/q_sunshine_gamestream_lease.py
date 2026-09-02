#!/usr/bin/env python3
"""PIN-free GameStream client-certificate leases for q-sunshine authd.

The issuer deliberately sits behind the existing TLS/PAM login service.  It
accepts a valid, *in-memory* qsa1 ticket plus a public CSR and signs a
short-lived client-auth certificate for exactly the ticket's VM audience.
The private key remains with the client throughout: neither this module, the
authd process, nor the helper ever receives or serializes it.

This module is transport-neutral on purpose.  ``q_sunshine_auth_gateway``
owns TLS, request size limits, generic remote errors, and PAM; it calls the
strict parser and issuer below for the ``gamestream_lease`` operation.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from q_sunshine_auth import (AuthError, TICKET_KEY_BYTES,
                             TICKET_TTL_MAX_SECONDS,
                             TICKET_TTL_MIN_SECONDS, valid_audience,
                             verify_ticket)


LEASE_OPERATION = "gamestream_lease"
LEASE_PROTOCOL_VERSION = 1
MAX_CSR_PEM_BYTES = 16 * 1024
MAX_CERTIFICATE_PEM_BYTES = 16 * 1024
ISSUER_TIMEOUT_SECONDS = 5.0
_CERTIFICATE_BEGIN = "-----BEGIN CERTIFICATE-----\n"
_CERTIFICATE_END = "-----END CERTIFICATE-----\n"
_CSR_BEGIN = "-----BEGIN CERTIFICATE REQUEST-----\n"
_CSR_END = "-----END CERTIFICATE REQUEST-----\n"


@dataclass(frozen=True)
class LeaseIssuerConfig:
    """Root-local configuration for one VM's GameStream lease issuer."""

    issuer: Path
    ca_certificate: Path
    ca_private_key: Path
    sunshine_server_certificate: Path
    audience: str
    ttl_seconds: int
    require_root_owner: bool = True


def _ascii_pem(value: Any, *, maximum_bytes: int, begin: str, end: str) -> bytes:
    """Validate one bounded public PEM object without accepting a private key."""
    if not isinstance(value, str) or "\x00" in value:
        raise AuthError("authentication failed")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise AuthError("authentication failed") from error
    if (not encoded or len(encoded) > maximum_bytes or not value.startswith(begin) or
            not value.endswith(end) or value.count(begin) != 1 or
            value.count(end) != 1 or "PRIVATE KEY" in value):
        raise AuthError("authentication failed")
    return encoded


def _read_public_certificate(path: Path, *, require_root_owner: bool) -> str:
    """Read one non-writable regular certificate file without following links."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AuthError("authentication failed") from error
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022 or
                (require_root_owner and metadata.st_uid != 0) or
                metadata.st_size <= 0 or metadata.st_size > MAX_CERTIFICATE_PEM_BYTES):
            raise AuthError("authentication failed")
        data = bytearray()
        while len(data) <= MAX_CERTIFICATE_PEM_BYTES:
            block = os.read(descriptor, MAX_CERTIFICATE_PEM_BYTES + 1 - len(data))
            if not block:
                break
            data.extend(block)
        if len(data) > MAX_CERTIFICATE_PEM_BYTES:
            raise AuthError("authentication failed")
        try:
            certificate = bytes(data).decode("ascii")
        finally:
            data[:] = b"\0" * len(data)
        _ascii_pem(certificate, maximum_bytes=MAX_CERTIFICATE_PEM_BYTES,
                   begin=_CERTIFICATE_BEGIN, end=_CERTIFICATE_END)
        return certificate
    except OSError as error:
        raise AuthError("authentication failed") from error
    finally:
        os.close(descriptor)


def _validate_issuer(path: Path, *, require_root_owner: bool) -> None:
    """Accept only a root-controlled regular executable, never a symlink."""
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise AuthError("authentication failed") from error
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022 or
            (require_root_owner and metadata.st_uid != 0) or
            not os.access(path, os.X_OK)):
        raise AuthError("authentication failed")


def validate_config(config: LeaseIssuerConfig) -> None:
    """Reject incomplete, unsafe, or cross-VM lease issuer configuration."""
    if (not isinstance(config, LeaseIssuerConfig) or
            not isinstance(config.audience, str) or
            type(config.ttl_seconds) is not int or
            not isinstance(config.require_root_owner, bool) or
            not all(isinstance(path, Path) for path in (
                config.issuer, config.ca_certificate, config.ca_private_key,
                config.sunshine_server_certificate,
            )) or
            not valid_audience(config.audience) or
            not TICKET_TTL_MIN_SECONDS <= config.ttl_seconds <= TICKET_TTL_MAX_SECONDS or
            not config.issuer.is_absolute() or not config.ca_certificate.is_absolute() or
            not config.ca_private_key.is_absolute() or
            not config.sunshine_server_certificate.is_absolute()):
        raise AuthError("authentication failed")
    _validate_issuer(config.issuer, require_root_owner=config.require_root_owner)


def parse_lease_request(payload: Any) -> tuple[str, bytes]:
    """Parse the exact JSON body accepted after a successful PAM login."""
    if (not isinstance(payload, dict) or
            set(payload) != {"csr_pem", "op", "session_token", "version"} or
            payload.get("op") != LEASE_OPERATION or
            payload.get("version") != LEASE_PROTOCOL_VERSION):
        raise AuthError("authentication failed")
    token = payload.get("session_token")
    if not isinstance(token, str):
        raise AuthError("authentication failed")
    csr = _ascii_pem(payload.get("csr_pem"), maximum_bytes=MAX_CSR_PEM_BYTES,
                     begin=_CSR_BEGIN, end=_CSR_END)
    return token, csr


def _run_issuer(config: LeaseIssuerConfig, *, subject: str, csr: bytes,
                ttl_seconds: int) -> str:
    """Sign a verified CSR with a root-local helper and return only its leaf."""
    arguments = [
        str(config.issuer),
        "--ca-cert", str(config.ca_certificate),
        "--ca-key", str(config.ca_private_key),
        "--subject", subject,
        "--audience", config.audience,
        "--ttl-seconds", str(ttl_seconds),
    ]
    if config.require_root_owner:
        arguments.append("--require-root-owner")
    try:
        result = subprocess.run(
            arguments, input=csr, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, timeout=ISSUER_TIMEOUT_SECONDS,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AuthError("authentication failed") from error
    if result.returncode != 0:
        raise AuthError("authentication failed")
    try:
        leaf = result.stdout.decode("ascii")
    except UnicodeDecodeError as error:
        raise AuthError("authentication failed") from error
    _ascii_pem(leaf, maximum_bytes=MAX_CERTIFICATE_PEM_BYTES,
               begin=_CERTIFICATE_BEGIN, end=_CERTIFICATE_END)
    return leaf


def issue_lease(ticket_key: bytes, request: Any, config: LeaseIssuerConfig,
                *, now: int | None = None) -> dict[str, Any]:
    """Issue a VM-scoped lease from one current qsa1 ticket and public CSR.

    The response expiry is deliberately conservative by one second: the C
    issuer reads its own clock while setting X.509 ``notAfter``.  A client can
    stop/renew before this advertised deadline but must regard the leaf's
    actual X.509 validity as authoritative.
    """
    validate_config(config)
    token, csr = parse_lease_request(request)
    if len(ticket_key) != TICKET_KEY_BYTES:
        raise AuthError("authentication failed")
    current_time = int(time.time()) if now is None else now
    subject, ticket_expires_at = verify_ticket(
        ticket_key, token, audience=config.audience, now=current_time)
    remaining_ticket_lifetime = ticket_expires_at - current_time
    lease_lifetime = min(config.ttl_seconds, remaining_ticket_lifetime)
    if lease_lifetime < TICKET_TTL_MIN_SECONDS:
        raise AuthError("authentication failed")
    server_certificate = _read_public_certificate(
        config.sunshine_server_certificate,
        require_root_owner=config.require_root_owner,
    )
    issuance_started = int(time.time()) if now is None else now
    leaf = _run_issuer(config, subject=subject, csr=csr, ttl_seconds=lease_lifetime)
    expires_at = issuance_started + lease_lifetime - 1
    return {
        "version": LEASE_PROTOCOL_VERSION,
        "audience": config.audience,
        "subject": subject,
        "expires_at_unix_ms": expires_at * 1000,
        "client_certificate_pem": leaf,
        "sunshine_server_certificate_pem": server_certificate,
    }
