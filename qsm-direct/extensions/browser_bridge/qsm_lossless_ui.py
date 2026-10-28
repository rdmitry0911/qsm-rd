"""Bounded experimental exact-pixel layer, independent of video presentation.

The worker's ordered private records maintain one shared tile cache. Every
viewer has its own output epoch/serial and a replaceable pending tile map;
a slow viewer cannot accumulate old desktop history or block keyboard input.
"""
from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass, replace
import struct
import time
from typing import Callable
import zlib

HEADER = struct.Struct("!4sBBHIIHHHHHH")
MAGIC = b"QUI1"
HELLO, RESET, PATCH, CLEAR, HEARTBEAT, SCENE = range(6)
GAP = -1  # Internal invalidation, never a wire opcode.
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
    priority: bool = False
    capture: int = 0
    layout: int = 0
    early: bool = False

    def wire(self, epoch: int, serial: int, *, stamped: bool = True, advanced: bool = False) -> bytes:
        capture = struct.pack("!Q", self.capture) if stamped and self.capture else b""
        if advanced and capture:
            capture += struct.pack('!Q', self.layout)
        flags = self.encoding | (2 if advanced and self.early else 0)
        return HEADER.pack(MAGIC, self.op, flags, len(capture), epoch, serial,
                           self.width, self.height, self.x, self.y, self.w, self.h) + capture + self.payload


def decode(raw: bytes) -> Update:
    if not isinstance(raw, bytes) or not HEADER.size <= len(raw) <= HEADER.size + 16 + MAX_PAYLOAD:
        raise ValueError("invalid exact UI record size")
    magic, op, flags, reserved, epoch, serial, width, height, x, y, w, h = HEADER.unpack_from(raw)
    if magic != MAGIC or flags & ~3 or (flags and op != PATCH) or reserved not in (0, 8, 16) or op not in range(6):
        raise ValueError("unsupported exact UI record")
    if len(raw) < HEADER.size + reserved or (reserved and op == HELLO):
        raise ValueError("invalid exact UI timestamp")
    capture = struct.unpack_from("!Q", raw, HEADER.size)[0] if reserved else 0
    layout = struct.unpack_from('!Q', raw, HEADER.size + 8)[0] if reserved == 16 else 0
    early = bool(flags & 2)
    if layout >= 1 << 53 or (early and (reserved != 16 or not layout or not capture)) or (op == SCENE and reserved != 16):
        raise ValueError('invalid exact UI layout')
    encoding = flags & 1
    payload = raw[HEADER.size + reserved:]
    if op == HELLO:
        if any((epoch, serial, width, height, x, y, w, h)) or len(payload) not in (0, 4):
            raise ValueError("invalid exact UI greeting")
    else:
        if not epoch or not width or not height or width > 16384 or height > 16384 or width * height > MAX_PIXELS:
            raise ValueError("invalid exact UI geometry")
        if op in (RESET, HEARTBEAT, SCENE):
            if any((x, y, w, h)) or payload:
                raise ValueError("invalid exact UI state")
        else:
            if (x >= width or y >= height or x % EDGE or y % EDGE or
                    w != min(EDGE, width - x) or h != min(EDGE, height - y)):
                raise ValueError("invalid exact UI tile")
            if op == CLEAR and payload:
                raise ValueError("invalid exact UI clear")
            if op == PATCH:
                size = w * h * (1 if encoding else 4)
                try:
                    inflater = zlib.decompressobj()
                    pixels = inflater.decompress(payload, size + 1)
                except zlib.error:
                    raise ValueError("invalid exact UI compression") from None
                if len(pixels) != size or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                    raise ValueError("invalid exact UI pixel count")
                if not encoding and any(a != 255 for a in pixels[3::4]):
                    raise ValueError("exact UI tiles must be opaque")
    return Update(op, epoch, serial, width, height, x, y, w, h, payload, encoding, capture=capture, layout=layout, early=early)


class Source:
    def __init__(self) -> None:
        self.available = False
        self.features = 0
        self.capture = 0
        self.layout = 0
        self.seen: set[tuple[int, int]] = set()
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
            listener(Update(HELLO, payload=struct.pack("!I", self.features) if self.features else b""))

    def unsubscribe(self, listener: Callable[[Update], None]) -> None:
        self.listeners.discard(listener)

    def _notify(self, update: Update) -> None:
        for callback in tuple(self.listeners):
            callback(update)

    def fail(self) -> None:
        self.tiles.clear(); self.bytes = 0
        self.seen.clear()
        self.awaiting_reset = True
        self.gaps += 1
        self._notify(Update(GAP))

    def invalidate(self) -> None:
        """A new capture subscription must not replay a previous session."""
        self.tiles.clear(); self.bytes = 0
        self.seen.clear()
        self.awaiting_reset = True
        self._notify(Update(RESET))

    def feed(self, raw: bytes) -> None:
        try:
            update = decode(raw)
        except ValueError:
            self.fail()
            return
        self.last_record = time.monotonic()
        self.capture = max(self.capture, update.capture)
        if update.op == HELLO:
            self.layout = 0
            self.features = int.from_bytes(update.payload, "big")
            self.available = True
            self.geometry = None
            self.serial = None
            self.tiles.clear(); self.bytes = 0
            self.seen.clear()
            self.awaiting_reset = True
            self._notify(update)
            return
        if not self.available:
            self.fail(); return
        if (update.layout or update.early or update.op == SCENE) and not self.features & 8:
            self.fail(); return
        if update.op == RESET:
            self.layout = update.layout
            self.epoch, self.serial = update.epoch, update.serial
            self.geometry = update.width, update.height
            self.tiles.clear(); self.bytes = 0
            self.seen.clear()
            self.awaiting_reset = False
            self._notify(update)
            return
        if self.awaiting_reset:
            return
        if (update.epoch != self.epoch or self.geometry != (update.width, update.height) or
                self.serial is None or update.serial != (self.serial + 1) & 0xffff_ffff):
            self.fail(); return
        self.serial = update.serial
        if update.op == SCENE:
            self.layout = update.layout
            self._notify(update)
            return
        if update.op in (PATCH, CLEAR):
            key = update.x, update.y
            update = replace(update, priority=key in self.seen)
            self.seen.add(key)
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
                 default_enabled: bool = False,
                 demand: Callable[[int], object] = lambda _: None,
                 account: Callable[[int], object] = lambda _: None,
                 interactive: Callable[[], bool] = lambda: False) -> None:
        self.source, self.policy, self.resync = source, policy, resync
        self.demand = demand
        self.account = account
        self.interactive = interactive
        self.reserved = 0
        self.version = 1
        self.ack_serial = 0
        self.ack_pending = 0
        self.cache: OrderedDict[int, tuple[bytes, int, int, bool]] = OrderedDict()
        self.cache_keys: dict[tuple[int, int], int] = {}
        self.cache_next = 0
        self.cache_hits = self.scroll_copies = self.fills = 0
        self.probe = probe
        self.default_enabled = default_enabled
        self.bitrate = bitrate
        self.credit = float(MAX_BUFFERED)
        self.credit_at = time.monotonic()
        self.enabled = False
        self.channel = None
        self.task: asyncio.Task | None = None
        self.pending: OrderedDict[tuple[int, int], Update] = OrderedDict()
        self.invalidations: OrderedDict[tuple[int, int], Update] = OrderedDict()
        self.snapshot = True
        self.scene: Update | None = None
        self.epoch = self.serial = 0
        self.sent_patches = self.sent_bytes = 0
        self._closed = False
        self._resync_at = float("-inf")
        self._probe_at = float("-inf")
        self._wake = asyncio.Event()
        self.source.subscribe(self.receive)

    def wake(self) -> None:
        self._wake.set()

    def announce(self) -> None:
        self.policy({"op": "qsm_ui_policy", "version": 1,
                     "available": self.source.available, "enabled": self.enabled,
                     "features": self.source.features, "max_version": 5,
                     "default_enabled": self.default_enabled,
                     "patches": self.sent_patches, "bytes": self.sent_bytes,
                     "tiles": len(self.source.tiles), "gaps": self.source.gaps,
                     "cache_hits": self.cache_hits, "scroll_copies": self.scroll_copies, "fills": self.fills})

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
            self.pending.clear(); self.invalidations.clear(); self.snapshot = True
            self.scene = None
            if update.op == GAP:
                self._request_reset()
        elif self.enabled and update.op == SCENE:
            self.scene = update
        elif self.enabled:
            key = update.x, update.y
            self.pending.pop(key, None)
            if update.op == CLEAR:
                self.invalidations[key] = update
            else:
                self.pending[key] = update
                self.pending.move_to_end(key, last=False)
        self.wake()

    def enable(self, enabled: bool) -> None:
        self.enabled = enabled and self.source.available
        self.pending.clear(); self.invalidations.clear(); self.snapshot = True
        self.scene = None
        self.announce()
        self.wake()

    def opened(self, channel: object) -> None:
        self.channel = channel
        # A low-buffer notification replaces periodic probing of SCTP.
        channel.bufferedAmountLowThreshold = MAX_BUFFERED // 2
        callback = getattr(channel, "on", None)
        if callable(callback):
            callback("bufferedamountlow", self.wake)
        if self.task is None:
            self.task = asyncio.create_task(self.run())
        self.announce()
        self.wake()

    def confirm_cache(self, ids: list[int]) -> None:
        for cache_id in ids:
            old = self.cache.get(cache_id)
            if old is not None:
                self.cache[cache_id] = (*old[:3], True)

    def _command(self, update: Update) -> tuple[Update, tuple | None]:
        if self.version < 3 or update.op != PATCH:
            return update, None
        raw = zlib.decompress(update.payload)
        if update.encoding:
            rgba = bytearray(len(raw) * 4)
            rgba[0::4] = raw; rgba[1::4] = raw; rgba[2::4] = raw
            rgba[3::4] = b'\xff' * len(raw)
            raw = bytes(rgba)
        color = raw[:4]
        if raw == color * (update.w * update.h):
            return replace(update, op=6, payload=color, encoding=0), None
        for cache_id, (pixels, w, h, confirmed) in self.cache.items():
            if confirmed and (w, h) == (update.w, update.h) and pixels == raw:
                return replace(update, op=8, payload=struct.pack("!I", cache_id), encoding=0), None
        cache_id = self.cache_next + 1
        payload = struct.pack("!I", cache_id) + update.payload
        result = replace(update, op=7, payload=payload)
        # Scroll reads an immutable acknowledged bitmap. All overlapping
        # copies therefore observe the same old pixels, never prior writes.
        old = self.cache.get(self.cache_keys.get((update.x, update.y), 0))
        if old and old[3] and (old[1], old[2]) == (update.w, update.h):
            previous = old[0]; stride = update.w * 4
            for shift in range(1, update.h):
                count = (update.h - shift) * stride
                for delta in (shift, -shift):
                    dest = max(delta, 0) * stride; src = max(-delta, 0) * stride
                    if (raw[dest:dest + 32] != previous[src:src + 32] or
                            raw[dest:dest + count] != previous[src:src + count]):
                        continue
                    exposed = raw[:dest] if delta > 0 else raw[count:]
                    candidate = struct.pack("!IIh", cache_id, self.cache_keys[(update.x, update.y)], delta) + zlib.compress(exposed, 1)
                    if len(candidate) < len(payload):
                        payload = candidate; result = replace(update, op=9, payload=payload, encoding=0)
                    break
        return result, (cache_id, raw, update.w, update.h, (update.x, update.y))

    def _remember(self, entry: tuple | None) -> None:
        if entry is None:
            return
        cache_id, raw, w, h, key = entry
        self.cache_next = cache_id
        self.cache[cache_id] = (raw, w, h, False)
        self.cache_keys[key] = cache_id
        if len(self.cache) > 256:
            expired, _ = self.cache.popitem(last=False)
            self.cache_keys = {k: v for k, v in self.cache_keys.items() if v != expired}

    def _rate(self) -> float:
        # A snapshot or scroll repaint is not evidence of interactive typing.
        # Borrowing half the path for either caused visible video stalls.
        share = 0.5 if self.source.features & 1 and self.interactive() else 0.15
        return min(512 * 1024 if self.source.features & 1 else 128 * 1024,
                   max(0, self.bitrate()) * 125 * share)

    def _burst(self) -> float:
        # Bound low-rate SCTP bursts to about 20 ms of the complete path,
        # while allowing one maximum tile/cache command to make progress.
        return min(MAX_BUFFERED, max(HEADER.size + 16 + MAX_PAYLOAD + 10,
                                     max(0, self.bitrate()) * 125 * .02))

    def _reservation(self) -> None:
        channel = self.channel
        buffered = getattr(channel, "bufferedAmount", 0) if channel else 0
        outstanding = buffered + sum(HEADER.size + len(u.payload) for u in self.pending.values())
        outstanding += len(self.invalidations) * (HEADER.size + 8)
        capacity = max(1, self.bitrate() * 125)
        limit = min(50, math.ceil(self._rate() * 100 / capacity))
        percent = min(limit, math.ceil(outstanding * 100 / (capacity * .1)))
        percent = percent if self.enabled and self.source.features & 1 else 0
        if percent != self.reserved:
            self.reserved = percent
            self.demand(percent)
        if channel is not None:
            channel.bufferedAmountLowThreshold = MAX_BUFFERED // 2 if self.pending or self.invalidations or self.scene else 0

    def _delay(self, now: float, announced: float) -> float | None:
        deadlines = []
        if not self.source.available:
            deadlines.append(self._probe_at + 1)
        if self.enabled:
            deadlines.append(announced + 1)
            if self.source.awaiting_reset:
                deadlines.append(self._resync_at + 0.5)
            else:
                deadlines.append(self.source.last_record + 0.800001)
            channel = self.channel
            if (channel is not None and getattr(channel, "readyState", None) == "open"):
                buffered = getattr(channel, "bufferedAmount", 0)
                if self.snapshot and self.source.geometry is not None and buffered <= MAX_BUFFERED:
                    deadlines.append(now)
                rate = self._rate()
                credit = min(self._burst(), self.credit + max(0, now - self.credit_at) * rate)
                if self.invalidations and buffered + HEADER.size + 8 <= MAX_BUFFERED:
                    deadlines.append(now)
                if self.scene is not None and buffered + HEADER.size + 16 <= MAX_BUFFERED:
                    deadlines.append(now)
                for update in sorted(self.pending.values(), key=lambda u: not u.early):
                    # Do not inflate/scan bitmap caches for work which cannot
                    # enter the browser's decode window. Waiting is passive.
                    if self.version >= 2 and self.ack_pending >= (32 if update.early and self.version >= 5 else 24 if self.version >= 5 else 32):
                        break
                    if buffered >= MAX_BUFFERED // 2:
                        break
                    command, _ = self._command(update)
                    size = HEADER.size + (16 if self.version >= 5 and update.capture else 8 if self.version >= 2 and update.capture else 0) + len(command.payload)
                    if buffered + size > MAX_BUFFERED:
                        continue  # bufferedamountlow wakes the sender.
                    if update.op != PATCH or credit >= size:
                        deadlines.append(now)
                        break
                    if rate > 0:
                        deadlines.append(now + (size - credit) / rate)
                    break
        return max(0.0, min(deadlines) - now) if deadlines else None

    def drain(self) -> None:
        channel = self.channel
        if not self.enabled or channel is None or getattr(channel, "readyState", None) != "open":
            return
        self.source.poll(time.monotonic())
        now = time.monotonic()
        rate = self._rate()
        self.credit = min(self._burst(), self.credit + max(0, now - self.credit_at) * rate)
        self.credit_at = now
        if self.source.geometry is None:
            return
        if self.snapshot:
            if getattr(channel, "bufferedAmount", 0) > MAX_BUFFERED:
                return
            self.epoch = (self.epoch + 1) & 0xffff_ffff or 1
            self.serial = 1
            self.ack_pending = self.ack_serial = 0
            self.cache.clear(); self.cache_keys.clear(); self.cache_next = 0
            width, height = self.source.geometry
            record = Update(RESET, width=width, height=height, capture=self.source.capture, layout=self.source.layout).wire(
                self.epoch, self.serial, stamped=self.version >= 2, advanced=self.version >= 5)
            channel.send(record)
            self.account(len(record))
            self.credit -= HEADER.size
            self.pending = OrderedDict(self.source.tiles)
            self.invalidations.clear()
            self.snapshot = False
        if self.scene is not None:
            if self.version >= 5:
                record = self.scene.wire(self.epoch, (self.serial + 1) & 0xffff_ffff,
                                         stamped=True, advanced=True)
                if getattr(channel, 'bufferedAmount', 0) + len(record) > MAX_BUFFERED:
                    return
                channel.send(record)
                self.serial = (self.serial + 1) & 0xffff_ffff
                self.account(len(record)); self.credit -= len(record); self.sent_bytes += len(record)
            self.scene = None
        sent = 0
        # A replacement patch must never coalesce away the invalidation of
        # an older visible tile. Send bounded, cheap clears before preparing
        # any replacement, even when pixel inflation is backpressured.
        for key, update in tuple(self.invalidations.items())[:32]:
            record = update.wire(self.epoch, (self.serial + 1) & 0xffff_ffff,
                                 stamped=self.version >= 2, advanced=self.version >= 5)
            if getattr(channel, "bufferedAmount", 0) + len(record) > MAX_BUFFERED:
                return
            channel.send(record)
            self.serial = (self.serial + 1) & 0xffff_ffff
            self.invalidations.pop(key)
            self.account(len(record))
            self.credit -= len(record)
            self.sent_bytes += len(record)
            sent += len(record)
        if self.invalidations:
            return
        for key, update in sorted(self.pending.items(), key=lambda item: not item[1].early):
            if sent >= MAX_BUFFERED:
                break
            if self.version >= 2 and self.ack_pending >= (32 if update.early and self.version >= 5 else 24 if self.version >= 5 else 32):
                break
            if getattr(channel, "bufferedAmount", 0) >= MAX_BUFFERED // 2:
                break
            command, entry = self._command(update)
            size = HEADER.size + (16 if self.version >= 5 and update.capture else 8 if self.version >= 2 and update.capture else 0) + len(command.payload)
            if getattr(channel, "bufferedAmount", 0) + size > MAX_BUFFERED:
                break
            if update.op == PATCH and self.credit < size:
                break
            # Clears/reset may borrow a few bytes: stale pixels must disappear
            # even when a cached snapshot has exhausted its refinement share.
            self.pending.pop(key)
            self.serial = (self.serial + 1) & 0xffff_ffff
            channel.send(command.wire(self.epoch, self.serial, stamped=self.version >= 2, advanced=self.version >= 5))
            self.account(size)
            self._remember(entry)
            self.cache_hits += command.op == 8
            self.scroll_copies += command.op == 9
            self.fills += command.op == 6
            sent += size
            self.credit -= size
            self.sent_bytes += size
            self.sent_patches += update.op == PATCH

    async def run(self) -> None:
        announced = 0.0
        try:
            while not self._closed:
                self._wake.clear()
                failed = False
                now = time.monotonic()
                try:
                    if not self.source.available and now - self._probe_at >= 1:
                        self._probe_at = now
                        self.probe()
                    if self.enabled and self.source.awaiting_reset:
                        self._request_reset()
                    self.drain()
                    self._reservation()
                except Exception:
                    # Clear local history on a transient SCTP send failure.
                    self.pending.clear(); self.invalidations.clear(); self.snapshot = True
                    failed = True
                now = time.monotonic()
                if self.enabled and now - announced >= 1:
                    announced = now; self.announce()
                delay = 0.02 if failed else self._delay(now, announced)
                if delay == 0:
                    await asyncio.sleep(0)  # Fairness between bounded batches.
                else:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self._closed = True
        self.enabled = False
        self.pending.clear()
        self.invalidations.clear()
        self._reservation()
        self.wake()
        self.source.unsubscribe(self.receive)
        self.pending.clear()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
