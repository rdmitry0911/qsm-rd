"""Bounded transport-cc parsing and packet credit, independent of frame ACKs."""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import struct
import time
from typing import Callable

URI = 'http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01'
MAX_REPORTED_PACKETS = 4096


@dataclass(frozen=True)
class Report:
    media_ssrc: int
    reference: int
    received: tuple[tuple[int, int], ...]


def decode(data: bytes) -> Report:
    """Decode one complete RTCP transport-cc packet; times are receiver-local us."""
    if (len(data) < 20 or data[0] >> 6 != 2 or data[0] & 31 != 15 or
            data[1] != 205 or (struct.unpack_from('!H', data, 2)[0] + 1) * 4 != len(data)):
        raise ValueError('invalid transport feedback header')
    if data[0] & 32:
        padding = data[-1]
        if not padding or padding > len(data) - 20:
            raise ValueError('invalid RTCP padding')
        data = data[:-padding]
    base, count = struct.unpack_from('!HH', data, 12)
    if count > MAX_REPORTED_PACKETS:
        raise ValueError('excess transport packet count')
    reference = int.from_bytes(data[16:19], 'big')
    position, statuses = 20, []
    while len(statuses) < count:
        if position + 2 > len(data):
            raise ValueError('short transport status chunk')
        word = struct.unpack_from('!H', data, position)[0]
        position += 2
        if not word & 0x8000:
            symbol, run = word >> 13 & 3, word & 8191
            if not run or symbol == 3:
                raise ValueError('invalid transport status run')
            statuses.extend([symbol] * min(run, count - len(statuses)))
        elif not word & 0x4000:
            statuses.extend(word >> shift & 1 for shift in range(13, -1, -1))
        else:
            statuses.extend(word >> shift & 3 for shift in range(12, -1, -2))
    arrived, received = reference * 64000, []
    for i, symbol in enumerate(statuses[:count]):
        if symbol == 3:
            raise ValueError('reserved transport status')
        if symbol:
            size = 1 if symbol == 1 else 2
            if position + size > len(data):
                raise ValueError('short transport receive delta')
            arrived += 250 * int.from_bytes(data[position:position + size], 'big', signed=size == 2)
            position += size
            received.append(((base + i) & 65535, arrived))
    if len(data) - position > 3 or any(data[position:]):
        raise ValueError('invalid transport feedback tail')
    return Report(struct.unpack_from('!I', data, 8)[0], reference, tuple(received))


def separate(data: bytes) -> tuple[list[Report], bytes]:
    """Consume negotiated TWCC; forward every other RTCP block unchanged.

    No packet credit is applied from structurally malformed RTCP or bad TWCC. A bad TWCC
    block does not prevent an otherwise valid PLI/NACK/SR/RR reaching aiortc.
    """
    if len(data) > 65536:
        return [], data
    position, reports, remaining, bad, found = 0, [], [], False, False
    while position < len(data):
        if position + 4 > len(data) or data[position] >> 6 != 2:
            return [], data
        size = (struct.unpack_from('!H', data, position + 2)[0] + 1) * 4
        if size < 4 or position + size > len(data):
            return [], data
        block = data[position:position + size]
        if block[0] & 32 and (position + size != len(data) or
                             not block[-1] or block[-1] > size - 4):
            bad = True
        if block[1] == 205 and block[0] & 31 == 15:
            found = True
            try:
                reports.append(decode(block))
            except ValueError:
                bad = True
        else:
            remaining.append(block)
        position += size
    return ([] if bad else reports), (b''.join(remaining) if found else data)


@dataclass
class _Packet:
    sent: float
    end: int
    cost: int
    received: bool = False


class PacketHistory:
    """Credit only a received video prefix or a host-matched complete frame.

    Audio shares sequence numbering but cannot acknowledge video. Lost video
    packets remain holes until packet delivery or a complete-frame ACK proves
    repair. Evicting history fails closed instead of treating loss as delivery.
    """
    MAX_PACKETS = 16384  # Less than half the 16-bit sequence space.
    MAX_AGE = 8.
    FRESH_AGE = 1.5

    def __init__(self, media_ssrcs: set[int], clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._media_ssrcs = {0, *media_ssrcs}
        self._packets: OrderedDict[int, _Packet] = OrderedDict()
        self._pending: deque[int] = deque()
        self._lookup: dict[int, int] = {}
        self._next = self._sent = self._frontier = self._blocked = 0
        self._reference: int | None = None
        self._offsets: list[tuple[float, float]] = []
        self._last_video = float('-inf')
        self._delivery: tuple[tuple[float, float, int], ...] = ()

    @property
    def next_sequence(self) -> int:
        return self._next & 65535

    def _remove_first(self, *, evicted: bool = False) -> None:
        absolute, packet = self._packets.popitem(last=False)
        if self._lookup.get(absolute & 65535) == absolute:
            del self._lookup[absolute & 65535]
        if evicted and packet.cost and packet.end > self._frontier:
            self._blocked = max(self._blocked, packet.end)

    def _prune(self) -> None:
        now = self._clock()
        while self._packets and (len(self._packets) > self.MAX_PACKETS or
                                 now - next(iter(self._packets.values())).sent >= self.MAX_AGE):
            self._remove_first(evicted=True)
        while self._pending and self._pending[0] not in self._packets:
            self._pending.popleft()

    def sent(self, video_cost: int) -> int:
        if type(video_cost) is not int or not 0 <= video_cost <= 65599:
            raise ValueError('invalid video packet cost')
        sequence = self.next_sequence
        self._sent += video_cost
        self._packets[self._next] = _Packet(self._clock(), self._sent, video_cost)
        if video_cost:
            self._pending.append(self._next)
        self._lookup[sequence] = self._next
        self._next += 1
        self._prune()
        return sequence

    def _advance(self, completed: int) -> None:
        self._frontier = max(self._frontier, completed)
        if self._frontier < self._blocked:
            return
        while self._pending:
            packet = self._packets.get(self._pending[0])
            if packet is None:
                self._pending.popleft()
                continue
            if packet.end > self._frontier and not packet.received:
                break
            self._frontier = max(self._frontier, packet.end)
            # Keep bounded send timing until TWCC itself reports arrival.
            # A faster complete-frame ACK must not erase delivery samples.
            self._pending.popleft()

    def receive(self, report: Report) -> bool:
        self._delivery = ()
        self._prune()
        if report.media_ssrc not in self._media_ssrcs or not report.received:
            return False
        reference = report.reference
        if self._reference is not None:
            reference = self._reference + ((reference - self._reference + (1 << 23)) % (1 << 24) - (1 << 23))
            if (self._reference - reference) * .064 > self.FRESH_AGE:
                return False
        adjustment = (reference - report.reference) * .064
        now, known = self._clock(), []
        for sequence, arrived_us in report.received:
            absolute = self._lookup.get(sequence)
            packet = self._packets.get(absolute)
            if packet is not None and not packet.received and 0 <= now - packet.sent < self.MAX_AGE:
                known.append((packet, arrived_us / 1e6 + adjustment))
        if not known:
            return False
        offsets = [(t, o) for t, o in self._offsets if now - t < 60][-127:]
        offsets.append((now, now - max(arrived for _, arrived in known)))
        upper = min(offset for _, offset in offsets)
        # Receiver arrival plus this upper clock-offset bound cannot precede
        # its actual send. This also rejects an old feedback payload reused
        # after the transport sequence wraps onto a newly sent packet.
        if any(packet.sent > arrived + upper + .01 for packet, arrived in known):
            return False
        self._offsets = offsets
        self._reference = reference if self._reference is None else max(reference, self._reference)
        self._delivery = tuple((packet.sent, arrived, packet.cost) for packet, arrived in known if packet.cost)
        video = False
        for packet, _ in known:
            packet.received = True
            video = video or bool(packet.cost)
        before = self._frontier
        if video:
            self._last_video = now
        self._advance(0)
        return self._frontier > before

    def credit(self, completed_frame_bytes: int) -> int:
        if type(completed_frame_bytes) is not int or not 0 <= completed_frame_bytes <= self._sent:
            return 0
        self._prune()
        self._advance(completed_frame_bytes)
        return self._frontier if self._clock() - self._last_video < self.FRESH_AGE else completed_frame_bytes

    def take_delivery(self) -> tuple[tuple[float, float, int], ...]:
        """Take new video (server send s, receiver arrival s, wire cost) samples.

        The two clocks have different origins; only relative transit changes
        and within-clock intervals can be used without clock calibration.
        """
        delivery, self._delivery = self._delivery, ()
        return delivery

    def clear(self) -> None:
        self._packets.clear()
        self._pending.clear()
        self._lookup.clear()
        self._offsets.clear()
        self._last_video = float('-inf')
        self._delivery = ()
