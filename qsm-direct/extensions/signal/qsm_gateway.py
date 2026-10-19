"""Private TURN credentials and a fail-closed lease for actual QSM ICE sockets.

Copied next to both the signalling service and browser bridge by the package.
The optional gateway never grants its TURN user general UDP access to the node.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
import time

GATEWAY_PATH = Path('/etc/qsm-pve-direct/gateway.json')
TABLE = 'qsm_direct_gateway'


def read_gateway(path=GATEWAY_PATH):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('gateway configuration must be a private root-owned regular file')
        config = json.loads(source.read(4097))
    if (not isinstance(config, dict) or set(config) != {'secret', 'relay_ipv4'} or
            not isinstance(config['secret'], str) or len(config['secret']) != 64 or
            any(c not in '0123456789abcdef' for c in config['secret'])):
        raise ValueError('invalid gateway configuration')
    address = ipaddress.IPv4Address(config['relay_ipv4'])
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
        raise ValueError('relay_ipv4 must be a non-loopback local IPv4 address')
    return config


def ice_credentials(config, *, now=None):
    # Bound only to this node, expires after 10 minutes. Random suffix avoids
    # disclosing a PVE username, guest id, ticket or stable tracking identifier.
    username = f'{int(time.time() if now is None else now) + 600}:{secrets.token_hex(12)}'
    credential = base64.b64encode(hmac.new(config['secret'].encode(), username.encode(), hashlib.sha1).digest()).decode()
    return {'username': username, 'credential': credential, 'ttl': 600}


class IcePeerLease:
    """Authorize only bound server ICE sockets, never arbitrary TURN peers.

    Elements expire in 15 seconds even if the terminal is killed. Refreshes
    are atomic; a failed refresh closes this peer instead of opening access.
    No firewall change occurs in the ordinary direct-UDP profile.
    """
    def __init__(self, address, ports):
        self.address = str(ipaddress.IPv4Address(address))
        self.ports = sorted(set(ports))
        if not self.ports or any(type(p) is not int or not 1 <= p <= 65535 for p in self.ports):
            raise ValueError('no usable gateway ICE sockets')
        self.task = None
        self.registered = False

    async def _change(self, *, remove=False):
        rows = []
        for port in self.ports:
            item = f'{self.address} . {port}'
            if self.registered:
                rows.append(f'delete element inet {TABLE} peers {{ {item} }}')
            if not remove:
                rows.append(f'add element inet {TABLE} peers {{ {item} timeout 15s }}')
        if not rows:
            return
        process = await asyncio.create_subprocess_exec('/usr/sbin/nft', '-f', '-',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.communicate(('\n'.join(rows) + '\n').encode()), 3)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise RuntimeError('gateway ICE firewall lease unavailable')
        self.registered = not remove

    async def start(self, close):
        await self._change()
        async def refresh():
            try:
                while True:
                    await asyncio.sleep(5)
                    await self._change()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not await close here: close cancels this refresh task.
                asyncio.create_task(close())
        self.task = asyncio.create_task(refresh())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.registered:
            try:
                await self._change(remove=True)
            except Exception:
                pass  # Expiring kernel entries remain fail-closed after 15s.
