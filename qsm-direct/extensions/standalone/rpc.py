# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded JSON records over an inherited private Unix socket pair."""
import json
import struct

MAX_RECORD = 256 * 1024


async def receive(reader):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 1 <= size <= MAX_RECORD:
        raise ValueError("invalid agent record size")
    value = json.loads(await reader.readexactly(size))
    if not isinstance(value, dict):
        raise ValueError("invalid agent record")
    return value


async def send(writer, value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    if not 1 <= len(data) <= MAX_RECORD:
        raise ValueError("invalid agent record size")
    writer.write(struct.pack("!I", len(data)) + data)
    await writer.drain()
