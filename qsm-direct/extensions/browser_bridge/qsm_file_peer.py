#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The WebRTC end of one file manager session, in a process of its own.

aiortc costs a core at about 12 MiB/s, so this must not share the console's loop.
It forwards frames without reading them: the guest helper validates them, and the
terminal has already fixed the session id and rights. See docs/FILE_MANAGER.md.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from collections import Counter, deque
import ctypes
import json
import os
import re
import resource
import socket
import sys
import time

MAX_FRAME = 65536
MAX_LINE = 96 * 1024
MAX_OFFER = 256 * 1024
# The browser's own window, 4 MiB at most, bounds what it sends. Past this it is not honest.
MAX_UPWARD_BUFFER = 8 * 1024 * 1024
# What an idle peer maps (317 MiB) and then some. The terminal's other processes share a cgroup.
MAX_ADDRESS_SPACE = 1024 * 1024 * 1024
# aiortc keeps about 1 KB for each queued message, so a flood of tiny frames pays as if larger.
MIN_FRAME_COST = 512
MAX_ECHO_BUFFERED = 16 * 1024
MIN_WINDOW_DELAY = .05
MIN_WINDOW_BYTES = 8 * 1200
# Credit bounds what an honest guest sends. Past this the peer stops reading, and the
# terminal's own limit then ends the session.
MAX_DOWNWARD_BYTES = 16 * 1024 * 1024
FRAME_ID = re.compile(rb'"id":\s*(\d+)')
NEGOTIATION_SECONDS = 30.0
INITIAL_RATE = 256 * 1024
MIN_RATE = 8 * 1024
BURST_SECONDS = .02
# bufferedAmount adds queue delay for every later frame: keep about 50 ms of it.
QUEUE_SECONDS = .05
MIN_QUEUE_BYTES = 32 * 1024


def drop_privileges() -> None:
    # No PR_SET_PDEATHSIG: it follows the parent thread, and the request thread that
    # starts this process ends at once. End of file on the socket ends this process.
    PR_SET_NO_NEW_PRIVS = 38
    libc = ctypes.CDLL(None, use_errno=True)
    resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_SPACE, MAX_ADDRESS_SPACE))
    if os.geteuid() == 0:
        os.setgroups([])
        os.setresgid(65534, 65534, 65534)
        os.setresuid(65534, 65534, 65534)
    libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    # Media on this node comes first when the machine is short of CPU.
    os.nice(5)


class Pacer:
    """Token bucket for frames toward the browser, whose controller sets the rate."""

    def __init__(self, rate: float = INITIAL_RATE, clock=time.monotonic) -> None:
        self._clock = clock
        self.rate = float(rate)
        self._tokens = 0.
        self._updated = clock()

    def set_rate(self, value: object) -> None:
        if type(value) in (int, float) and value == value and 0 < value < 1 << 40:
            self.rate = max(float(MIN_RATE), float(value))

    def delay(self, size: int) -> float:
        """Seconds to wait before ``size`` bytes may go. Charges them when it returns 0."""
        now = self._clock()
        self._tokens = min(self.rate * BURST_SECONDS + size,
                           self._tokens + (now - self._updated) * self.rate)
        self._updated = now
        if self._tokens < size:
            return (size - self._tokens) / self.rate
        self._tokens -= size
        return 0.

    def charge(self, size: int) -> None:
        self._tokens -= size

    def queue_limit(self) -> int:
        return max(MIN_QUEUE_BYTES, int(self.rate * QUEUE_SECONDS))


def without_end_of_candidates(sdp: str) -> str:
    """As for the console: an unresolved mDNS offer must keep discovery open."""
    lines = sdp.splitlines()
    if not any(line.startswith("a=candidate:") and len(line.split()) > 4 and line.split()[4].endswith(".local")
               for line in lines):
        return sdp
    return "\r\n".join(line for line in lines if line != "a=end-of-candidates") + "\r\n"


class FilePeer:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from aiortc import RTCPeerConnection
        self._reader = reader
        self._writer = writer
        self._pc = RTCPeerConnection()
        self._files = None
        self._pacer = Pacer()
        self._done = asyncio.Event()
        self._opened = asyncio.Event()
        self._pending_status: list[str] = []
        # File data waits its turn at the pace. Anything else (a listing, an acknowledgement,
        # an error) goes first, unless it must stay behind data of its own request.
        self._urgent: deque[bytes] = deque()
        self._bulk: deque[tuple[bytes, bytes | None]] = deque()
        self._bulk_ids: Counter[bytes] = Counter()
        self._queued = 0
        self._queued_changed = asyncio.Event()
        self._pc.on("datachannel", self._on_datachannel)
        self._pc.on("connectionstatechange", self._on_state)

    def _on_state(self) -> None:
        if self._pc.connectionState in ("failed", "closed"):
            self._done.set()

    def _on_datachannel(self, channel) -> None:
        files = (channel.label == "qsm-files" and self._files is None and channel.ordered is True and
                 channel.maxRetransmits is None and channel.maxPacketLifeTime is None)
        # Without a limit on retransmission: see the browser's side for what a FORWARD-TSN does.
        rtt = (channel.label == "qsm-files-rtt" and channel.ordered is False and
               channel.maxRetransmits is None and channel.maxPacketLifeTime is None)
        if files:
            self._files = channel
            channel.on("message", self._from_browser)
            channel.on("close", self._done.set)
            self._opened.set()
            for status in self._pending_status:
                channel.send(status)
            self._pending_status.clear()
        elif rtt:
            channel.on("message", lambda message: self._on_rtt(channel, message))
        else:
            channel.close()

    def _on_rtt(self, channel, message) -> None:
        if not isinstance(message, str) or len(message) > 128:
            return
        try:
            value = json.loads(message)
        except ValueError:
            return
        if isinstance(value, dict) and "rate" in value:
            self._pacer.set_rate(value["rate"])
        elif (isinstance(value, dict) and "t" in value and channel.readyState == "open" and
                channel.bufferedAmount <= MAX_ECHO_BUFFERED):
            channel.send(message)

    def _from_browser(self, message) -> None:
        if not isinstance(message, bytes) or not 2 <= len(message) <= MAX_FRAME:
            self._done.set()
            return
        self._writer.write(b"D " + base64.b64encode(message) + b"\n")
        if self._writer.transport.get_write_buffer_size() > MAX_UPWARD_BUFFER:
            self._done.set()

    def _status(self, state: str, reason: str | None = None) -> None:
        text = json.dumps({"op": "status", "state": state, "reason": reason}, separators=(",", ":"))
        channel = self._files
        if channel is not None and channel.readyState == "open":
            channel.send(text)
        else:
            self._pending_status.append(text)

    def _enqueue(self, frame: bytes) -> None:
        size = int.from_bytes(frame[:2], 'big')
        head = frame[2:2 + size]
        found = FRAME_ID.search(head)
        identifier = found.group(1) if found else None
        data = b'"o":' in head
        # An unreadable header takes the slower lane, where nothing can overtake it.
        if not data and identifier is not None and not self._bulk_ids[identifier]:
            self._urgent.append(frame)
        else:
            self._bulk.append((frame, identifier))
            if identifier is not None:
                self._bulk_ids[identifier] += 1
        self._queued += len(frame)
        self._queued_changed.set()

    def _sent(self, frame: bytes) -> None:
        self._queued -= len(frame)
        self._queued_changed.set()

    def _keep_window_open(self) -> None:
        """The browser's controller sets the pace, so SCTP's window must not be the limit.

        aiortc grows its window only when it is full, and a paced sender seldom fills it. After
        a retransmission timeout it starts again from one packet and can stay there for good,
        with echoes waiting in the same queue and reading as congestion. Loss recovery is
        unchanged. These are aiortc's private fields, so a version without them keeps its own.
        """
        sctp = self._pc.sctp
        window = getattr(sctp, '_cwnd', None)
        if type(window) is int:
            delay = getattr(sctp, '_srtt', None) or MIN_WINDOW_DELAY
            wanted = int(self._pacer.rate * max(delay, MIN_WINDOW_DELAY) * 2) + MIN_WINDOW_BYTES
            if window < wanted:
                sctp._cwnd = wanted

    async def _send_loop(self) -> None:
        await self._opened.wait()
        channel = self._files
        while not self._done.is_set():
            if channel.readyState != 'open':
                return
            self._keep_window_open()
            if not self._urgent and not self._bulk:
                self._queued_changed.clear()
                await self._queued_changed.wait()
                continue
            if channel.bufferedAmount > self._pacer.queue_limit():
                await asyncio.sleep(.002)
                continue
            if self._urgent:
                frame = self._urgent.popleft()
                # It goes now, and what it costs comes out of the pace of what follows.
                self._pacer.charge(max(len(frame), MIN_FRAME_COST))
                channel.send(frame)
                self._sent(frame)
                continue
            frame, identifier = self._bulk[0]
            wait = self._pacer.delay(max(len(frame), MIN_FRAME_COST))
            if wait:
                # Short, so that an urgent frame does not wait out a slow pace.
                await asyncio.sleep(min(wait, .01))
                continue
            self._bulk.popleft()
            if identifier is not None:
                self._bulk_ids[identifier] -= 1
                if not self._bulk_ids[identifier]:
                    del self._bulk_ids[identifier]
            channel.send(frame)
            self._sent(frame)

    async def _from_terminal(self) -> None:
        reader = self._reader
        while not self._done.is_set():
            while self._queued > MAX_DOWNWARD_BYTES and not self._done.is_set():
                self._queued_changed.clear()
                await self._queued_changed.wait()
            try:
                line = await reader.readuntil(b"\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                break
            if line.startswith(b"D "):
                try:
                    frame = base64.b64decode(line[2:-1], validate=True)
                except binascii.Error:
                    break
                if not 2 <= len(frame) <= MAX_FRAME:
                    break
                self._enqueue(frame)
            elif line == b"R\n":
                self._status("ready")
            elif line.startswith(b"C "):
                reason = line[2:-1].decode("ascii", "replace")
                self._status("closed", reason if reason in ("nohelper", "limit", "gone") else "gone")
            else:
                break
        self._done.set()

    async def negotiate(self) -> None:
        from aiortc import RTCSessionDescription
        request = json.loads(await asyncio.wait_for(self._reader.readline(), NEGOTIATION_SECONDS))
        sdp = request.get("offer") if isinstance(request, dict) else None
        if not isinstance(sdp, str) or not sdp.isascii() or not 1 <= len(sdp) <= MAX_OFFER:
            raise ValueError("invalid offer")
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=without_end_of_candidates(sdp), type="offer"))
        await self._pc.setLocalDescription(await self._pc.createAnswer())
        local = self._pc.localDescription
        candidates = [[c.ip, c.port, c.protocol]
                      for c in self._pc.sctp.transport.transport.iceGatherer.getLocalCandidates()]
        self._writer.write(json.dumps({"answer": local.sdp, "candidates": candidates},
                                      separators=(",", ":")).encode("ascii") + b"\n")
        await self._writer.drain()

    async def run(self) -> int:
        try:
            await self.negotiate()
        except Exception as error:  # noqa: BLE001 - the offer is untrusted; its text is never logged
            print(f"qsm-file-peer: negotiation failed: {type(error).__name__}", file=sys.stderr, flush=True)
            await self._pc.close()
            return 1
        relay = asyncio.ensure_future(self._from_terminal())
        sending = asyncio.ensure_future(self._send_loop())
        try:
            await asyncio.wait_for(self._opened.wait(), NEGOTIATION_SECONDS)
            await self._done.wait()
        except asyncio.TimeoutError:
            print("qsm-file-peer: no browser connected", file=sys.stderr, flush=True)
        relay.cancel()
        sending.cancel()
        await self._pc.close()
        return 0


async def main_async(descriptor: int) -> int:
    connection = socket.socket(fileno=descriptor)
    connection.setblocking(False)
    reader, writer = await asyncio.open_unix_connection(sock=connection, limit=max(MAX_LINE, MAX_OFFER * 2))
    return await FilePeer(reader, writer).run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fd", type=int, required=True)
    arguments = parser.parse_args()
    drop_privileges()
    return asyncio.run(main_async(arguments.fd))


if __name__ == "__main__":
    sys.exit(main())
