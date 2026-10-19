"""Shared, root-owned configuration for the standalone HTTPS endpoint."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
from urllib.parse import urlsplit

CONFIG_PATH = Path('/etc/qsm-pve-direct/signal.json')


@dataclass(frozen=True)
class SignalEndpoint:
    listen: str = '0.0.0.0'
    port: int = 8007  # Preserve existing add-on installations without a config.
    public_url: str = ''

    def __post_init__(self):
        if not isinstance(self.listen, str):
            raise ValueError('listen must be an IPv4 or IPv6 address')
        ipaddress.ip_address(self.listen)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('port must be between 1 and 65535')
        if not isinstance(self.public_url, str):
            raise ValueError('public_url must be an HTTPS origin or empty')
        if self.public_url:
            parsed = urlsplit(self.public_url)
            if (parsed.scheme != 'https' or not parsed.hostname or
                    parsed.username is not None or parsed.password is not None or
                    parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                    not re.fullmatch(r'https://[A-Za-z0-9.\-:\[\]]{1,255}/?', self.public_url)):
                raise ValueError('public_url must be an HTTPS origin without credentials or a path')
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError('public_url port must be between 1 and 65535')

    def browser_config(self) -> dict:
        return {'port': self.port, 'public_url': self.public_url.rstrip('/')}

    def as_dict(self) -> dict:
        return {'listen': self.listen, **self.browser_config()}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate endpoint configuration key')
        result[key] = value
    return result


def read_endpoint(path: Path = CONFIG_PATH, *, require_root_owner: bool = True) -> SignalEndpoint:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return SignalEndpoint()
    with os.fdopen(fd, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096 or
                metadata.st_mode & 0o022 or (require_root_owner and metadata.st_uid != 0)):
            raise ValueError('endpoint configuration must be a root-owned regular file, not writable by others')
        value = json.loads(source.read(4097), object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) - {'listen', 'port', 'public_url'}:
        raise ValueError('unknown endpoint configuration keys')
    return SignalEndpoint(**value)


def bind_socket(endpoint: SignalEndpoint) -> socket.socket:
    """Bind only; never displace another service. Used for preflight checks."""
    family = socket.AF_INET6 if ipaddress.ip_address(endpoint.listen).version == 6 else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind((endpoint.listen, endpoint.port))
        return listener
    except BaseException:
        listener.close()
        raise
