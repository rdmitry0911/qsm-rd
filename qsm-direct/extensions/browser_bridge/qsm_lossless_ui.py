"""Bounded experimental exact-pixel layer, independent of video presentation.

The worker's ordered private records maintain one shared tile cache. Every
viewer has its own output epoch/serial and a replaceable pending tile map;
a slow viewer cannot accumulate old desktop history or block keyboard input.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import struct
import time
from typing import Callable
import zlib

HEADER = struct.Struct("!4sBBHIIHHHHHH")
MAGIC = b"QUI1"
HELLO, RESET, PATCH, CLEAR, HEARTBEAT, GAP = range(6)
EDGE = 64
MAX_PAYLOAD = 6144
MAX_PIXELS = 3840 * 2160
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_BUFFERED = 16 * 1024


@dataclass(frozen=True)
class Update:
    op: int
    epoch: int = 0
    serial: int = 0
    width: int = 0
    height: int = 0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    payload: bytes = b""
    encoding: int = 0

    def wire(self, epoch: int, serial: int) -> bytes:
        return HEADER.pack(MAGIC, self.op, self.encoding, 0, epoch, serial,
                           self.width, self.height, self.x, self.y, self.w, self.h) + self.payload


def decode(raw: bytes) -> Update:
    if not isinstance(raw, bytes) or not HEADER.size <= len(raw) <= HEADER.size + MAX_PAYLOAD:
        raise ValueError("invalid exact UI record size")
    magic, op, flags, reserved, epoch, serial, width, height, x, y, w, h = HEADER.unpack_from(raw)
    if magic != MAGIC or flags & ~1 or (flags and op != PATCH) or reserved or op not in range(5):
        raise ValueError("unsupported exact UI record")
    payload = raw[HEADER.size:]
    if op == HELLO:
        if any((epoch, serial, width, height, x, y, w, h)) or payload:
            raise ValueError("invalid exact UI greeting")
    else:
        if not epoch or not width or not height or width > 16384 or height > 16384 or width * height > MAX_PIXELS:
            raise ValueError("invalid exact UI geometry")
        if op in (RESET, HEARTBEAT):
            if any((x, y, w, h)) or payload:
                raise ValueError("invalid exact UI state")
        else:
            if (x >= width or y >= height or x % EDGE or y % EDGE or
                    w != min(EDGE, width - x) or h != min(EDGE, height - y)):
                raise ValueError("invalid exact UI tile")
            if op == CLEAR and payload:
                raise ValueError("invalid exact UI clear")
            if op == PATCH:
                size = w * h * (1 if flags else 4)
                try:
                    inflater = zlib.decompressobj()
                    pixels = inflater.decompress(payload, size + 1)
                except zlib.error:
                    raise ValueError("invalid exact UI compression") from None
                if len(pixels) != size or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                    raise ValueError("invalid exact UI pixel count")
                if not flags and any(a != 255 for a in pixels[3::4]):
                    raise ValueError("exact UI tiles must be opaque")
    return Update(op, epoch, serial, width, height, x, y, w, h, payload, flags)


class Source:
    def __init__(self) -> None:
        self.available = False
        self.geometry: tuple[int, int] | None = None
        self.epoch = 0
        self.serial: int | None = None
        self.awaiting_reset = True
        self.tiles: dict[tuple[int, int], Update] = {}
        self.bytes = 0
        self.last_record = 0.0
        self.listeners: set[Callable[[Update], None]] = set()
        self.gaps = 0

    def subscribe(self, listener: Callable[[Update], None]) -> None:
        self.listeners.add(listener)
        if self.available:
            listener(Update(HELLO))

    def unsubscribe(self, listener: Callable[[Update], None]) -> None:
        self.listeners.discard(listener)

    def _notify(self, update: Update) -> None:
        for callback in tuple(self.listeners):
            callback(update)

    def fail(self) -> None:
        self.tiles.clear(); self.bytes = 0
        self.awaiting_reset = True
        self.gaps += 1
        self._notify(Update(GAP))

    def invalidate(self) -> None:
        """A new capture subscription must not replay a previous session."""
        self.tiles.clear(); self.bytes = 0
        self.awaiting_reset = True
        self._notify(Update(RESET))

    def feed(self, raw: bytes) -> None:
        try:
            update = decode(raw)
        except ValueError:
            self.fail()
            return
        self.last_record = time.monotonic()
        if update.op == HELLO:
            self.available = True
            self.geometry = None
            self.serial = None
            self.tiles.clear(); self.bytes = 0
            self.awaiting_reset = True
            self._notify(update)
            return
        if not self.available:
            self.fail(); return
        if update.op == RESET:
            self.epoch, self.serial = update.epoch, update.serial
            self.geometry = update.width, update.height
            self.tiles.clear(); self.bytes = 0
            self.awaiting_reset = False
            self._notify(update)
            return
        if self.awaiting_reset:
            return
        if (update.epoch != self.epoch or self.geometry != (update.width, update.height) or
                self.serial is None or update.serial != (self.serial + 1) & 0xffff_ffff):
            self.fail(); return
        self.serial = update.serial
        if update.op in (PATCH, CLEAR):
            key = update.x, update.y
            old = self.tiles.pop(key, None)
            if old:
                self.bytes -= len(old.payload)
            if update.op == PATCH:
                if self.bytes + len(update.payload) > MAX_CACHE_BYTES:
                    self.fail(); return
                self.tiles[key] = update
                self.bytes += len(update.payload)
            self._notify(update)

    def poll(self, now: float) -> None:
        # Missing worker invalidations must not leave frozen exact pixels
        # covering otherwise healthy video indefinitely.
        if self.available and not self.awaiting_reset and now - self.last_record > 0.8:
            self.fail()


class Peer:
    def __init__(self, source: Source, policy: Callable[[dict], object], resync: Callable[[], object],
                 probe: Callable[[], object] = lambda: None,
                 bitrate: Callable[[], int] = lambda: 8000,
                 default_enabled: bool = False) -> None:
        self.source, self.policy, self.resync = source, policy, resync
        self.probe = probe
        self.default_enabled = default_enabled
        self.bitrate = bitrate
        self.credit = float(MAX_BUFFERED)
        self.credit_at = time.monotonic()
        self.enabled = False
        self.channel = None
        self.task: asyncio.Task | None = None
        self.pending: OrderedDict[tuple[int, int], Update] = OrderedDict()
        self.snapshot = True
        self.epoch = self.serial = 0
        self.sent_patches = self.sent_bytes = 0
        self._closed = False
        self._resync_at = float("-inf")
        self.source.subscribe(self.receive)

    def announce(self) -> None:
        self.policy({"op": "qsm_ui_policy", "version": 1,
                     "available": self.source.available, "enabled": self.enabled,
                     "default_enabled": self.default_enabled,
                     "patches": self.sent_patches, "bytes": self.sent_bytes,
                     "tiles": len(self.source.tiles), "gaps": self.source.gaps})

    def _request_reset(self) -> None:
        now = time.monotonic()
        if self.enabled and now - self._resync_at >= 0.5:
            self._resync_at = now
            self.resync()

    def receive(self, update: Update) -> None:
        if update.op == HELLO:
            self.snapshot = True
            self.announce()
            self._request_reset()
        elif update.op in (RESET, GAP):
            self.pending.clear(); self.snapshot = True
            if update.op == GAP:
                self._request_reset()
        elif self.enabled:
            key = update.x, update.y
            self.pending.pop(key, None)
            self.pending[key] = update
            # Fresh input/invalidations outrank an unfinished cached snapshot.
            self.pending.move_to_end(key, last=False)

    def enable(self, enabled: bool) -> None:
        self.enabled = enabled and self.source.available
        self.pending.clear(); self.snapshot = True
        self.announce()

    def opened(self, channel: object) -> None:
        self.channel = channel
        if self.task is None:
            self.task = asyncio.create_task(self.run())
        self.announce()

    def drain(self) -> None:
        channel = self.channel
        if not self.enabled or channel is None or getattr(channel, "readyState", None) != "open":
            return
        self.source.poll(time.monotonic())
        now = time.monotonic()
        rate = min(128 * 1024, max(0, self.bitrate()) * 125 * 0.15)
        self.credit = min(float(MAX_BUFFERED), self.credit + max(0, now - self.credit_at) * rate)
        self.credit_at = now
        if self.source.geometry is None:
            return
        if self.snapshot:
            if getattr(channel, "bufferedAmount", 0) > MAX_BUFFERED:
                return
            self.epoch = (self.epoch + 1) & 0xffff_ffff or 1
            self.serial = 1
            width, height = self.source.geometry
            channel.send(Update(RESET, width=width, height=height).wire(self.epoch, self.serial))
            self.credit -= HEADER.size
            self.pending = OrderedDict(self.source.tiles)
            self.snapshot = False
        sent = 0
        for key, update in tuple(self.pending.items()):
            if sent >= MAX_BUFFERED:
                break
            size = HEADER.size + len(update.payload)
            if getattr(channel, "bufferedAmount", 0) + size > MAX_BUFFERED:
                break
            if update.op == PATCH and self.credit < size:
                continue
            # Clears/reset may borrow a few bytes: stale pixels must disappear
            # even when a cached snapshot has exhausted its refinement share.
            self.pending.pop(key)
            self.serial = (self.serial + 1) & 0xffff_ffff
            channel.send(update.wire(self.epoch, self.serial))
            sent += size
            self.credit -= size
            self.sent_bytes += size
            self.sent_patches += update.op == PATCH

    async def run(self) -> None:
        announced = 0.0
        try:
            while not self._closed:
                try:
                    if not self.source.available:
                        self.probe()
                    if self.enabled and self.source.awaiting_reset:
                        self._request_reset()
                    self.drain()
                except Exception:
                    # Clear local history on a transient SCTP send failure.
                    self.pending.clear(); self.snapshot = True
                now = time.monotonic()
                if self.enabled and now - announced >= 1:
                    announced = now; self.announce()
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self._closed = True
        self.source.unsubscribe(self.receive)
        self.pending.clear()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
