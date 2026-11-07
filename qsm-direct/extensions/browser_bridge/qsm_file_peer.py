#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser's end of one file manager session, in a process of its own.

This parses what arrives from the network, so it runs without privileges and apart
from the console's media loop. The signal service has done the WebSocket handshake and
relays the bytes. Frames pass through unread: the guest helper validates them, and the
terminal has already fixed the session id and rights. See docs/FILE_MANAGER.md.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from collections import Counter, deque
import ctypes
import json
import os
import re
import resource
import select
import socket
import struct
import sys
import threading
import time

MAX_FRAME = 65536
MAX_LINE = 96 * 1024
MAX_TEXT = 128
READ_BUFFER = 1024 * 1024
MAX_ADDRESS_SPACE = 512 * 1024 * 1024
# A flood of tiny frames costs more memory than its bytes, so it pays as if larger.
MIN_FRAME_COST = 512
MAX_ECHOES = 64
# Credit bounds what an honest guest sends. Past this the peer stops reading, and the
# terminal's own limit then ends the session.
MAX_DOWNWARD_BYTES = 16 * 1024 * 1024
FRAME_ID = re.compile(rb'"id":\s*(\d+)')
# The browser connects, and pings ten times a second once the guest is ready.
CONNECT_SECONDS = 30.0
IDLE_SECONDS = 90.0
INITIAL_RATE = 256 * 1024
MIN_RATE = 8 * 1024
BURST_SECONDS = .02
MAX_WRITE_BYTES = 256 * 1024

OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10


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
        # A long listing goes first, but file data does not then wait out all of it.
        self._tokens = max(self._tokens - size, -self.rate)


def ws_frame(opcode: int, payload: bytes) -> bytes:
    size = len(payload)
    if size < 126:
        return bytes((0x80 | opcode, size)) + payload
    if size < 65536:
        return struct.pack('!BBH', 0x80 | opcode, 126, size) + payload
    return struct.pack('!BBQ', 0x80 | opcode, 127, size) + payload


def unmask(data: bytes, key: bytes) -> bytes:
    size = len(data)
    if not size:
        return b''
    mask = (key * (size // 4 + 1))[:size]
    return (int.from_bytes(data, 'little') ^ int.from_bytes(mask, 'little')).to_bytes(size, 'little')


class ProtocolError(ValueError):
    pass


class WebSocketReader:
    """Whole messages from a browser. Only a client's masked frames are valid."""

    def __init__(self, stream) -> None:
        self._stream = stream

    def _read(self, size: int) -> bytes:
        data = self._stream.read(size)
        if len(data) != size:
            raise EOFError
        return data

    def _frame(self) -> tuple[int, bool, bytes]:
        first, second = self._read(2)
        if first & 0x70 or not second & 0x80:
            raise ProtocolError('invalid frame')
        opcode, size = first & 0x0f, second & 0x7f
        if size == 126:
            size = struct.unpack('!H', self._read(2))[0]
        elif size == 127:
            size = struct.unpack('!Q', self._read(8))[0]
        if size > MAX_FRAME or (opcode >= OP_CLOSE and (size > 125 or not first & 0x80)):
            raise ProtocolError('invalid frame')
        key = self._read(4)
        return opcode, bool(first & 0x80), unmask(self._read(size), key)

    def message(self, on_ping) -> tuple[int, bytes] | None:
        """The next message, None at a close. Pings between its fragments are answered."""
        kind, parts, total = None, [], 0
        while True:
            opcode, final, payload = self._frame()
            if opcode == OP_CLOSE:
                return None
            if opcode == OP_PING:
                on_ping(payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CONTINUATION:
                if kind is None:
                    raise ProtocolError('invalid frame')
            elif opcode in (OP_TEXT, OP_BINARY):
                if kind is not None:
                    raise ProtocolError('invalid frame')
                kind = opcode
            else:
                raise ProtocolError('invalid frame')
            total += len(payload)
            if total > MAX_FRAME:
                raise ProtocolError('message too large')
            parts.append(payload)
            if final:
                return kind, parts[0] if len(parts) == 1 else b''.join(parts)


class FilePeer:
    def __init__(self, terminal: socket.socket, network: socket.socket) -> None:
        self._terminal = terminal
        self._network = network
        self._pacer = Pacer()
        self._done = threading.Event()
        self._condition = threading.Condition()
        # File data waits its turn at the pace. Anything else (a listing, an acknowledgement,
        # an error) goes first, unless it must stay behind data of its own request.
        self._urgent: deque[tuple[bytes, int]] = deque()
        self._bulk: deque[tuple[bytes, bytes | None]] = deque()
        self._bulk_ids: Counter[bytes] = Counter()
        self._queued = 0
        self._echoes = 0
        self._pong: bytes | None = None

    def _send_urgent(self, wire: bytes, cost: int) -> None:
        with self._condition:
            self._urgent.append((wire, cost))
            self._queued += cost
            self._condition.notify_all()

    def _on_ping(self, payload: bytes) -> None:
        # Only the latest needs an answer, or a client that does not read fills this process.
        with self._condition:
            self._pong = ws_frame(OP_PONG, payload)
            self._condition.notify_all()

    def _on_text(self, message: bytes) -> None:
        if len(message) > MAX_TEXT:
            return
        try:
            value = json.loads(message)
        except ValueError:
            return
        if not isinstance(value, dict):
            return
        if 'rate' in value:
            with self._condition:
                self._pacer.set_rate(value['rate'])
                self._condition.notify_all()
        elif 't' in value:
            # An echo that would wait behind many others says nothing new.
            with self._condition:
                if self._echoes >= MAX_ECHOES:
                    return
                self._echoes += 1
                self._urgent.appendleft((ws_frame(OP_TEXT, message), -1))
                self._condition.notify_all()

    def _from_browser(self) -> None:
        try:
            ready = select.select((self._network,), (), (), CONNECT_SECONDS)[0]
            if not ready:
                print('qsm-file-peer: no browser connected', file=sys.stderr, flush=True)
                return
            self._network.settimeout(IDLE_SECONDS)
            with self._network.makefile('rb', buffering=READ_BUFFER) as stream:
                reader = WebSocketReader(stream)
                while True:
                    message = reader.message(self._on_ping)
                    if message is None:
                        return
                    kind, payload = message
                    if kind == OP_TEXT:
                        self._on_text(payload)
                    elif 2 <= len(payload):
                        self._terminal.sendall(b'D ' + base64.b64encode(payload) + b'\n')
                    else:
                        return
        except (OSError, EOFError, ProtocolError):
            pass
        finally:
            self._done.set()

    def _status(self, state: str, reason: str | None = None) -> None:
        text = json.dumps({'op': 'status', 'state': state, 'reason': reason}, separators=(',', ':')).encode('ascii')
        self._send_urgent(ws_frame(OP_TEXT, text), 0)

    def _enqueue(self, frame: bytes) -> None:
        size = int.from_bytes(frame[:2], 'big')
        head = frame[2:2 + size]
        found = FRAME_ID.search(head)
        identifier = found.group(1) if found else None
        data = b'"o":' in head
        cost = max(len(frame), MIN_FRAME_COST)
        with self._condition:
            # An unreadable header takes the slower lane, where nothing can overtake it.
            if not data and identifier is not None and not self._bulk_ids[identifier]:
                self._urgent.append((ws_frame(OP_BINARY, frame), cost))
            else:
                self._bulk.append((frame, identifier))
                if identifier is not None:
                    self._bulk_ids[identifier] += 1
            self._queued += cost
            self._condition.notify_all()

    def _next_write(self) -> bytes:
        """What may go now: one urgent frame, or as much file data as the pace allows."""
        with self._condition:
            while True:
                if self._pong is not None:
                    wire, self._pong = self._pong, None
                    return wire
                if self._urgent:
                    wire, cost = self._urgent.popleft()
                    if cost < 0:
                        self._echoes -= 1
                    else:
                        # It goes now, and what it costs comes out of the pace of what follows.
                        self._pacer.charge(cost)
                        self._queued -= cost
                        self._condition.notify_all()
                    return wire
                if self._bulk:
                    wait = self._pacer.delay(max(len(self._bulk[0][0]), MIN_FRAME_COST))
                    if not wait:
                        break
                    # Short, so that an urgent frame does not wait out a slow pace.
                    self._condition.wait(min(wait, .01))
                    continue
                self._condition.wait()
            parts, total = [], 0
            while True:
                frame, identifier = self._bulk.popleft()
                if identifier is not None:
                    self._bulk_ids[identifier] -= 1
                    if not self._bulk_ids[identifier]:
                        del self._bulk_ids[identifier]
                self._queued -= max(len(frame), MIN_FRAME_COST)
                parts.append(ws_frame(OP_BINARY, frame))
                total += len(frame)
                if (not self._bulk or self._urgent or total >= MAX_WRITE_BYTES or
                        self._pacer.delay(max(len(self._bulk[0][0]), MIN_FRAME_COST))):
                    break
            self._condition.notify_all()
            return parts[0] if len(parts) == 1 else b''.join(parts)

    def _send_loop(self) -> None:
        try:
            while True:
                self._network.sendall(self._next_write())
        except OSError:
            pass
        finally:
            self._done.set()

    def _from_terminal(self) -> None:
        try:
            with self._terminal.makefile('rb', buffering=READ_BUFFER) as reader:
                while True:
                    with self._condition:
                        while self._queued > MAX_DOWNWARD_BYTES:
                            self._condition.wait()
                    line = reader.readline(MAX_LINE + 1)
                    if not line.endswith(b'\n') or len(line) > MAX_LINE:
                        return
                    if line.startswith(b'D '):
                        frame = base64.b64decode(line[2:-1], validate=True)
                        if not 2 <= len(frame) <= MAX_FRAME:
                            return
                        self._enqueue(frame)
                    elif line == b'R\n':
                        self._status('ready')
                    elif line.startswith(b'C '):
                        reason = line[2:-1].decode('ascii', 'replace')
                        self._status('closed', reason if reason in ('nohelper', 'limit', 'gone') else 'gone')
                    else:
                        return
        except (OSError, binascii.Error):
            pass
        finally:
            self._done.set()

    def run(self) -> int:
        for target in (self._from_browser, self._from_terminal, self._send_loop):
            threading.Thread(target=target, daemon=True).start()
        self._done.wait()
        # What is still queued goes nowhere useful: either end has gone.
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fd', type=int, required=True)
    parser.add_argument('--network-fd', type=int, required=True)
    arguments = parser.parse_args()
    drop_privileges()
    code = FilePeer(socket.socket(fileno=arguments.fd), socket.socket(fileno=arguments.network_fd)).run()
    sys.stderr.flush()
    # Threads may be blocked on a socket, and the kernel closes both for whoever waits at the far end.
    os._exit(code)


if __name__ == '__main__':
    sys.exit(main())
