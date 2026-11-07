#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""File manager helper of one desktop session. docs/FILE_MANAGER.md is the contract.

It serves QSF1 "FS" lines on a private unix socket with the permissions of the
logged-in user: from the host directly in a container, through qsm-desktop-agent
in a VM. The frame reader never blocks: filesystem calls run in worker threads,
so a hung mount costs only its own requests and a cancel still gets through.
Nothing runs on a timer: without requests the process sleeps in the event loop,
and the session's lock state is read only when a request needs it.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import ctypes
import errno
import json
import os
import pwd
import re
import secrets
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

MAX_FRAME = 65536
MAX_LINE = len(b'FS ') + 16 + len(b' D ') + (MAX_FRAME + 2) // 3 * 4
CHUNK = 32768
MIN_CHUNK = 1024
ACK_EVERY = 64 * 1024
READ_BATCH = 256 * 1024
MAX_PATH_BYTES = 4096
MAX_SESSIONS = 8
MAX_REQUESTS = 64
MAX_TRANSFERS = 16
MAX_LIST_ENTRIES = 200000
# The host holds about 16 MiB per session, base64 included, and a list is not paced by credit.
MAX_LIST_BYTES = 8 * 1024 * 1024
# A list part closes here. One more entry always fits below its 48 KiB limit.
PART_BYTES = 40 * 1024
SYNC_FROM = 1024 * 1024
PROGRESS_SECONDS = 0.2
LOCK_SECONDS = 2.0
COMMAND_SECONDS = 1.5
SHUTDOWN_SECONDS = 3.0
MAX_ID = 2**31 - 1
MAX_SIZE = 2**63 - 1
MAX_MTIME_MS = 2**63 // 10**6 - 1
MAX_CREDIT = 2**53
# The browser's window is at most 4 MiB. Far beyond it the sender is not
# waiting for acks, and a slow disk must not turn that into unbounded memory.
MAX_UNWRITTEN = 16 * 1024 * 1024
WRITE_HIGH, WRITE_LOW = 1024 * 1024, 256 * 1024
MAX_WRITE_BUFFER = 64 * 1024 * 1024
# A hung mount holds its worker for good, so the pool is sized for requests
# rather than for CPUs. Idle workers sleep on a queue without any timer.
WORKERS = ThreadPoolExecutor(max_workers=256, thread_name_prefix='qsm-files')

SID = re.compile(rb'[0-9a-f]{1,16}')
WRITE_OPS = frozenset({'mkdir', 'rename', 'delete', 'put'})
FOLLOW_UPS = frozenset({'data', 'end', 'credit', 'cancel'})
ERROR_NAMES = {getattr(errno, name): name for name in (
    'ENOENT', 'EACCES', 'EPERM', 'EEXIST', 'ENOTDIR', 'EISDIR', 'ENOTEMPTY', 'ENOSPC',
    'EDQUOT', 'EROFS', 'EINVAL', 'EBUSY', 'ENAMETOOLONG')}


class RequestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def os_error(error: OSError) -> tuple[str, str]:
    text = error.strerror or str(error)
    return ERROR_NAMES.get(error.errno, 'EIO'), text


def fs_path(value: object) -> bytes:
    if not isinstance(value, str):
        raise RequestError('EINVAL', 'a path must be a string')
    try:
        path = value.encode('utf-8', 'surrogateescape')
    except UnicodeEncodeError:
        raise RequestError('EINVAL', 'the path cannot be encoded') from None
    if b'\0' in path or not path.startswith(b'/'):
        raise RequestError('EINVAL', 'a path must be absolute and contain no NUL')
    if len(path) > MAX_PATH_BYTES:
        raise RequestError('ENAMETOOLONG', 'the path is too long')
    return path


def split_leaf(path: bytes) -> tuple[bytes, bytes]:
    """Parent and last component. A trailing slash would make the kernel follow
    a symlink that this call must act on itself, so it is removed here."""
    parent, _, name = path.rstrip(b'/').rpartition(b'/')
    if name in (b'', b'.', b'..'):
        raise RequestError('EINVAL', 'the path has no usable last component')
    return parent or b'/', name


def integer(value: object, low: int, high: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise RequestError('EINVAL', f'{name} is invalid')
    return value


def flag(head: dict, name: str) -> bool:
    value = head.get(name)
    if value is not None and not isinstance(value, bool):
        raise RequestError('EINVAL', f'{name} is invalid')
    return bool(value)


def _no_constant(name: str) -> None:
    raise ValueError(name)


def file_type(mode: int) -> str:
    return 'f' if stat.S_ISREG(mode) else 'd' if stat.S_ISDIR(mode) else 'l' if stat.S_ISLNK(mode) else 'o'


def link_target(path: bytes) -> str:
    try:
        return file_type(os.stat(path).st_mode)
    except OSError:  # dangling or looping
        return 'x'


def describe(path: bytes, info: os.stat_result) -> tuple[str, int, int, int, str | None]:
    kind = file_type(info.st_mode)
    return (kind, info.st_size, info.st_mtime_ns // 10**6, stat.S_IMODE(info.st_mode),
            link_target(path) if kind == 'l' else None)


def describe_path(path: bytes) -> tuple[str, int, int, int, str | None]:
    return describe(path, os.lstat(path))


def read_entries(directory, room: int) -> tuple[list[bytes], bool]:
    """Encoded entries for one list part, and whether the directory is exhausted."""
    entries: list[bytes] = []
    size = 0
    while len(entries) < room and size < PART_BYTES:
        entry = next(directory, None)
        if entry is None:
            return entries, True
        name = entry.name.decode('utf-8', 'surrogateescape')
        try:
            value = [name, *describe(entry.path, entry.stat(follow_symlinks=False))]
        except FileNotFoundError:
            continue
        except OSError:  # a directory readable but not searchable still shows its names
            value = [name, 'o', 0, 0, 0, None]
        entries.append(json.dumps(value, separators=(',', ':')).encode('ascii'))
        size += len(entries[-1]) + 1
    return entries, False


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


_libc = ctypes.CDLL(None, use_errno=True)
AT_FDCWD = -100
RENAME_NOREPLACE = 1


def _renameat2(source: bytes, target: bytes, dir_fd: int) -> None:
    try:
        call = _libc.renameat2
    except AttributeError:
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS)) from None
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if call(dir_fd, source, dir_fd, target, RENAME_NOREPLACE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def rename_noreplace(source: bytes, target: bytes, dir_fd: int | None = None) -> None:
    try:
        return _renameat2(source, target, AT_FDCWD if dir_fd is None else dir_fd)
    except OSError as error:
        if error.errno not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise
    # No RENAME_NOREPLACE on this kernel or filesystem. Taking the name first
    # fails if anything holds it, so the rename can only replace our placeholder.
    if stat.S_ISDIR(os.lstat(source, dir_fd=dir_fd).st_mode):
        os.mkdir(target, 0o700, dir_fd=dir_fd)
        remove = os.rmdir
    else:
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd))
        remove = os.unlink
    try:
        os.rename(source, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        remove(target, dir_fd=dir_fd)
        raise


def open_directory_nofollow(name, dir_fd: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def delete_path(parent: bytes, name: bytes, recursive: bool, stop: threading.Event, report) -> int:
    """Remove one path without following a link at any depth. Every level is
    opened O_NOFOLLOW and acted on through its descriptor, so swapping a
    directory for a symlink during the walk cannot lead outside the tree."""
    removed = 0
    reported_at = time.monotonic()

    def gone() -> None:
        nonlocal removed, reported_at
        removed += 1
        now = time.monotonic()
        if now - reported_at >= PROGRESS_SECONDS:
            reported_at = now
            report(removed)
        if stop.is_set():
            raise RequestError('ECANCELED', 'cancelled')

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    levels: list[tuple[int, str | bytes]] = []
    try:
        info = os.lstat(name, dir_fd=parent_fd)
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=parent_fd)
            gone()
            return removed
        if not recursive:
            os.rmdir(name, dir_fd=parent_fd)
            gone()
            return removed
        levels.append((open_directory_nofollow(name, parent_fd), name))
        while levels:
            fd, own_name = levels[-1]
            descended = False
            with os.scandir(fd) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        levels.append((open_directory_nofollow(entry.name, fd), entry.name))
                        descended = True
                        break
                    os.unlink(entry.name, dir_fd=fd)
                    gone()
            if not descended:
                levels.pop()
                os.close(fd)
                os.rmdir(own_name, dir_fd=levels[-1][0] if levels else parent_fd)
                gone()
        return removed
    finally:
        for fd, _ in levels:
            os.close(fd)
        os.close(parent_fd)


def replaced_mode(name: bytes, dir_fd: int) -> int | None:
    """The mode a replacing upload keeps, or None if nothing is there to keep.
    A link to a file counts as that file; the link itself is what gets replaced."""
    try:
        info = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        try:
            info = os.stat(name, dir_fd=dir_fd)
        except OSError:
            return None
    if stat.S_ISDIR(info.st_mode):
        raise RequestError('EISDIR', 'the target is a directory')
    if not stat.S_ISREG(info.st_mode):
        raise RequestError('ENOTREG', 'the target is not a regular file')
    return stat.S_IMODE(info.st_mode) & 0o777


class Request:
    """One browser request, its final response and its current worker call."""

    transfer = False

    def __init__(self, session: Session, rid: int, op: str, head: dict, payload: bytes) -> None:
        self.session, self.rid, self.op, self.head = session, rid, op, head
        self.task: asyncio.Task | None = None
        self.job: asyncio.Future | None = None
        self.done = False
        self.stop = threading.Event()

    async def blocking(self, function, *args):
        # Shielded: cancelling the request returns here at once, while the
        # worker may still use its descriptor until cleanup() has seen it end.
        self.job = asyncio.get_running_loop().run_in_executor(WORKERS, function, *args)
        # Nobody reads a cancelled request's result, and it may well be an error.
        self.job.add_done_callback(lambda job: job.cancelled() or job.exception())
        return await asyncio.shield(self.job)

    async def settle(self) -> None:
        if self.job is not None and not self.job.done():
            await asyncio.wait([self.job])

    async def run(self) -> None:
        try:
            try:
                await self.execute()
            finally:
                await self.cleanup()
        except asyncio.CancelledError:
            pass  # abort() has answered, or the session no longer listens
        except RequestError as error:
            self.fail(error.code, error.message)
        except OSError as error:
            self.fail(*os_error(error))
        except Exception as error:
            print(f'qsm-desktop-files: {self.op} failed: {error!r}', file=sys.stderr, flush=True)
            self.fail('EIO', str(error))

    async def execute(self) -> None:
        if self.op != 'hello':
            await self.refuse_if_locked()
        await getattr(self, 'op_' + self.op)()

    async def cleanup(self) -> None:
        await self.settle()

    async def refuse_if_locked(self) -> None:
        if await self.session.connection.app.lock.locked():
            raise RequestError('ELOCKED', 'the session is locked')

    def release(self) -> None:
        self.done = True
        self.session.forget(self)

    def finish(self, **fields) -> None:
        if not self.done:
            self.release()
            self.session.send({'id': self.rid, 'ok': True, **fields})

    def fail(self, code: str, message: str) -> None:
        if not self.done:
            self.release()
            self.session.send({'id': self.rid, 'ok': False, 'err': code, 'msg': message})

    def abort(self, code: str | None = None, message: str = '') -> None:
        """End now. The answer never waits for a worker, which may be hung;
        its cleanup follows whenever that worker returns."""
        if self.done:
            return
        if code is None:
            self.release()
        else:
            self.fail(code, message)
        self.stop.set()
        self.task.cancel()

    def on_data(self, head: dict, payload: bytes) -> None:
        raise RequestError('EINVAL', 'this request takes no data')

    def on_end(self, head: dict, payload: bytes) -> None:
        raise RequestError('EINVAL', 'this request takes no end')

    def on_credit(self, head: dict, payload: bytes) -> None:
        raise RequestError('EINVAL', 'this request takes no credit')

    async def op_hello(self) -> None:
        app = self.session.connection.app
        self.finish(v=1, home=app.home, user=app.user, rights=self.session.rights,
                    locked=await app.lock.locked(), chunk=CHUNK)

    async def op_list(self) -> None:
        path = self.head.get('path')
        directory = await self.blocking(os.scandir, fs_path(path))
        try:
            total, sent, exhausted = 0, 0, False
            while not exhausted and total < MAX_LIST_ENTRIES and sent < MAX_LIST_BYTES:
                await self.session.connection.writable.wait()
                entries, exhausted = await self.blocking(read_entries, directory, MAX_LIST_ENTRIES - total)
                if entries:
                    total += len(entries)
                    part = b'{"id":%d,"part":[%s]}' % (self.rid, b','.join(entries))
                    sent += len(part)
                    self.session.send_frame(part)
            truncated = not exhausted and not await self.blocking(lambda: next(directory, None) is None)
            self.finish(path=path, total=total, truncated=truncated)
        finally:
            await self.settle()
            await asyncio.get_running_loop().run_in_executor(WORKERS, directory.close)

    async def op_stat(self) -> None:
        path = fs_path(self.head.get('path'))
        kind, size, mtime, mode, target = await self.blocking(describe_path, path)
        self.finish(type=kind, size=size, mtime=mtime, mode=mode, target=target)

    async def op_mkdir(self) -> None:
        path = fs_path(self.head.get('path'))
        if flag(self.head, 'parents'):
            await self.blocking(os.makedirs, path, 0o777, True)
        else:
            await self.blocking(os.mkdir, path)
        self.finish()

    async def op_rename(self) -> None:
        source = os.path.join(*split_leaf(fs_path(self.head.get('from'))))
        target = os.path.join(*split_leaf(fs_path(self.head.get('to'))))
        await self.blocking(rename_noreplace, source, target)
        self.finish()

    async def op_delete(self) -> None:
        parent, name = split_leaf(fs_path(self.head.get('path')))
        recursive = flag(self.head, 'recursive')
        loop = asyncio.get_running_loop()

        def report(count: int) -> None:
            loop.call_soon_threadsafe(self.send_progress, count)

        self.finish(removed=await self.blocking(delete_path, parent, name, recursive, self.stop, report))

    def send_progress(self, count: int) -> None:
        if not self.done:
            self.session.send({'id': self.rid, 'progress': count})


class Upload(Request):
    transfer = True

    def __init__(self, session: Session, rid: int, op: str, head: dict, payload: bytes) -> None:
        super().__init__(session, rid, op, head, payload)
        self.parent, self.name = split_leaf(fs_path(head.get('path')))
        self.size = integer(head.get('size'), 0, MAX_SIZE, 'size')
        mtime, mode = head.get('mtime'), head.get('mode')
        self.mtime = None if mtime is None else integer(mtime, -MAX_MTIME_MS, MAX_MTIME_MS, 'mtime')
        self.mode = None if mode is None else integer(mode, 0, 0o7777, 'mode') & 0o777
        self.overwrite = head.get('overwrite')
        if self.overwrite not in ('fail', 'replace'):
            raise RequestError('EINVAL', 'overwrite is invalid')
        last = flag(head, 'last')
        if len(payload) > self.size or (last and len(payload) != self.size):
            raise RequestError('EINVAL', 'the data does not match the size')
        self.queue: deque[bytes] = deque()
        self.queued = 0
        self.received = 0
        self.written = 0
        self.end_size = self.size if last else None
        self.wake = asyncio.Event()
        self.dir_fd = self.fd = -1
        self.part: bytes | None = None
        self.take(payload)

    def take(self, payload: bytes) -> None:
        if payload:
            self.queue.append(payload)
            self.queued += len(payload)
            self.received += len(payload)
            self.session.unwritten += len(payload)
            self.wake.set()

    def on_data(self, head: dict, payload: bytes) -> None:
        if self.end_size is not None:
            raise RequestError('EINVAL', 'data after the end of the upload')
        if integer(head.get('o'), 0, MAX_SIZE, 'o') != self.received:
            raise RequestError('EINVAL', 'data is not at the next offset')
        if self.received + len(payload) > self.size:
            raise RequestError('EINVAL', 'more data than the size')
        self.take(payload)
        if self.session.unwritten > MAX_UNWRITTEN:
            raise RequestError('EBUSY', 'too much data waiting for the disk')

    def on_end(self, head: dict, payload: bytes) -> None:
        if self.end_size is not None:
            raise RequestError('EINVAL', 'the upload has already ended')
        self.end_size = integer(head.get('size'), 0, MAX_SIZE, 'size')
        self.wake.set()

    def release(self) -> None:
        super().release()
        self.session.unwritten -= self.queued
        self.queued = 0
        self.queue.clear()

    def next_batch(self) -> bytes:
        batch = [self.queue.popleft()]
        size = len(batch[0])
        while self.queue and size + len(self.queue[0]) <= ACK_EVERY:
            batch.append(self.queue.popleft())
            size += len(batch[-1])
        return b''.join(batch)

    async def execute(self) -> None:
        await self.refuse_if_locked()
        await self.blocking(self.open_part)
        while True:
            if self.queue:
                batch = self.next_batch()
                await self.blocking(write_all, self.fd, batch)
                self.written += len(batch)
                self.queued -= len(batch)
                self.session.unwritten -= len(batch)
                self.session.send({'id': self.rid, 'ack': self.written})
            elif self.end_size is not None:
                break
            else:
                self.wake.clear()
                await self.wake.wait()
        if self.end_size != self.size or self.written != self.size:
            raise RequestError('EINVAL', 'the size does not match the data')
        await self.blocking(self.commit)
        self.finish(size=self.written)

    def open_part(self) -> None:
        self.dir_fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY)
        if self.overwrite == 'replace':
            replaced_mode(self.name, self.dir_fd)
        else:
            try:
                info = os.lstat(self.name, dir_fd=self.dir_fd)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISDIR(info.st_mode):
                    raise RequestError('EISDIR', 'the target is a directory')
                raise RequestError('EEXIST', 'the target exists')
        # NAME_MAX still holds for a target name near the limit.
        stem = self.name[:255 - len(b'..qsm-part-') - 8]
        for _ in range(16):
            part = b'.%s.qsm-part-%s' % (stem, secrets.token_hex(4).encode('ascii'))
            try:
                self.fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=self.dir_fd)
            except FileExistsError:
                continue
            self.part = part
            return
        raise OSError(errno.EEXIST, 'no free temporary name')

    def commit(self) -> None:
        if self.written >= SYNC_FROM:
            os.fsync(self.fd)
        mode = replaced_mode(self.name, self.dir_fd) if self.overwrite == 'replace' else None
        if mode is None:
            mode = self.session.connection.app.default_mode if self.mode is None else self.mode
        try:
            os.fchmod(self.fd, mode)
        except OSError as error:  # vfat and the like have no modes to set
            if error.errno not in (errno.EPERM, errno.ENOTSUP):
                raise
        if self.mtime is not None:
            os.utime(self.fd, ns=(self.mtime * 10**6, self.mtime * 10**6))
        fd, self.fd = self.fd, -1
        os.close(fd)
        if self.stop.is_set():  # cancelled during a long fsync: answered, so not published
            return
        if self.overwrite == 'replace':
            os.rename(self.part, self.name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        else:
            rename_noreplace(self.part, self.name, self.dir_fd)
        self.part = None

    def discard(self) -> None:
        with suppress(OSError):
            if self.fd >= 0:
                os.close(self.fd)
        with suppress(OSError):
            if self.part is not None:
                os.unlink(self.part, dir_fd=self.dir_fd)
        with suppress(OSError):
            if self.dir_fd >= 0:
                os.close(self.dir_fd)
        self.fd = self.dir_fd = -1
        self.part = None

    async def cleanup(self) -> None:
        await self.settle()
        await asyncio.get_running_loop().run_in_executor(WORKERS, self.discard)


class Download(Request):
    transfer = True

    def __init__(self, session: Session, rid: int, op: str, head: dict, payload: bytes) -> None:
        super().__init__(session, rid, op, head, payload)
        self.path = fs_path(head.get('path'))
        self.credit = integer(head.get('credit'), 0, MAX_CREDIT, 'credit')
        self.frame = self.frame_size(head, CHUNK)
        offset = head.get('offset')
        self.position = 0 if offset is None else integer(offset, 0, MAX_SIZE, 'offset')
        self.granted = asyncio.Event()
        self.fd = -1

    def on_credit(self, head: dict, payload: bytes) -> None:
        self.credit += integer(head.get('n'), 0, MAX_CREDIT, 'n')
        self.frame = self.frame_size(head, self.frame)
        self.granted.set()

    def open_regular(self) -> os.stat_result:
        # Merely opening a FIFO wakes whoever waits to write to it, so look first.
        # O_NONBLOCK still covers a swap in between, which must not hang.
        self.refuse_irregular(os.stat(self.path))
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        info = os.fstat(self.fd)
        self.refuse_irregular(info)
        os.set_blocking(self.fd, True)
        return info

    @staticmethod
    def frame_size(head: dict, default: int) -> int:
        # On a narrow path a full frame is a burst that delays the console behind it.
        return integer(head['chunk'], MIN_CHUNK, CHUNK, 'chunk') if 'chunk' in head else default

    @staticmethod
    def refuse_irregular(info: os.stat_result) -> None:
        if stat.S_ISDIR(info.st_mode):
            raise RequestError('EISDIR', 'a directory cannot be downloaded')
        if not stat.S_ISREG(info.st_mode):
            raise RequestError('ENOTREG', 'not a regular file')

    async def execute(self) -> None:
        await self.refuse_if_locked()
        first = await self.blocking(self.open_regular)
        self.session.send({'id': self.rid, 'meta': True, 'size': first.st_size,
                           'mtime': first.st_mtime_ns // 10**6, 'mode': stat.S_IMODE(first.st_mode)})
        while True:
            if self.credit <= 0:
                # A file that ends exactly here must finish without waiting
                # for credit nobody would have a reason to send.
                if not await self.blocking(os.pread, self.fd, 1, self.position):
                    break
                if self.credit <= 0:
                    self.granted.clear()
                    await self.granted.wait()
                continue
            await self.session.connection.writable.wait()
            data = await self.blocking(os.pread, self.fd, min(self.credit, READ_BATCH), self.position)
            if not data:
                break
            for start in range(0, len(data), self.frame):
                self.session.send({'id': self.rid, 'o': self.position + start}, data[start:start + self.frame])
            self.position += len(data)
            self.credit -= len(data)
        last = await self.blocking(os.fstat, self.fd)
        self.finish(size=self.position,
                    changed=(last.st_size, last.st_mtime_ns) != (first.st_size, first.st_mtime_ns))

    async def cleanup(self) -> None:
        await self.settle()
        if self.fd >= 0:
            await asyncio.get_running_loop().run_in_executor(WORKERS, os.close, self.fd)
            self.fd = -1


REQUEST_KINDS = {'hello': Request, 'list': Request, 'stat': Request, 'mkdir': Request, 'rename': Request,
                 'delete': Request, 'put': Upload, 'get': Download}


class Session:
    def __init__(self, connection: Connection, sid: bytes, rights: str) -> None:
        self.connection, self.rights = connection, rights
        self.prefix = b'FS ' + sid + b' D '
        self.requests: dict[int, Request] = {}
        self.transfers = 0
        self.unwritten = 0
        self.closed = False

    def send_frame(self, header: bytes, payload: bytes = b'') -> None:
        if not self.closed:
            frame = struct.pack('>H', len(header)) + header + payload
            self.connection.write_line(self.prefix + base64.b64encode(frame))

    def send(self, head: dict, payload: bytes = b'') -> None:
        self.send_frame(json.dumps(head, separators=(',', ':')).encode('ascii'), payload)

    def forget(self, request: Request) -> None:
        if self.requests.get(request.rid) is request:
            del self.requests[request.rid]
            if request.transfer:
                self.transfers -= 1

    def refuse(self, rid: int, code: str, message: str) -> None:
        """One id gets one final response, also when a browser reuses it."""
        request = self.requests.get(rid)
        if request is not None:
            request.abort(code, message)
        else:
            self.send({'id': rid, 'ok': False, 'err': code, 'msg': message})

    def on_frame(self, encoded: bytes) -> None:
        try:
            frame = base64.b64decode(encoded, validate=True)
            length = int.from_bytes(frame[:2], 'big')
            if len(frame) < 2 or 2 + length > len(frame):
                return
            head = json.loads(frame[2:2 + length].decode('ascii'), parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return
        if not isinstance(head, dict):
            return
        rid, op = head.get('id'), head.get('op')
        if not isinstance(rid, int) or isinstance(rid, bool) or not 1 <= rid <= MAX_ID:
            return
        is_follow_up = isinstance(op, str) and op in FOLLOW_UPS
        if is_follow_up and rid not in self.requests:
            # Normal for the data of a put that failed and the credit of a get
            # that ended: the one failure has already been sent.
            return
        if len(frame) > MAX_FRAME:
            self.refuse(rid, 'EINVAL', 'the frame is too long')
        elif is_follow_up:
            self.follow_up(rid, op, head, frame[2 + length:])
        elif isinstance(op, str) and op in REQUEST_KINDS:
            self.start(rid, op, head, frame[2 + length:])
        else:
            self.refuse(rid, 'EINVAL', 'unknown operation')

    def follow_up(self, rid: int, op: str, head: dict, payload: bytes) -> None:
        request = self.requests[rid]
        if op == 'cancel':
            request.abort('ECANCELED', 'cancelled')
            return
        try:
            getattr(request, 'on_' + op)(head, payload)
        except RequestError as error:
            request.abort(error.code, error.message)

    def start(self, rid: int, op: str, head: dict, payload: bytes) -> None:
        if rid in self.requests:
            self.refuse(rid, 'EINVAL', 'this id is in use')
            return
        kind = REQUEST_KINDS[op]
        try:
            if op in WRITE_OPS and self.rights != 'rw':
                raise RequestError('ERIGHTS', 'this session may not write')
            if len(self.requests) >= MAX_REQUESTS or (kind.transfer and self.transfers >= MAX_TRANSFERS):
                raise RequestError('EBUSY', 'too many requests in progress')
            request = kind(self, rid, op, head, payload)
        except RequestError as error:
            self.send({'id': rid, 'ok': False, 'err': error.code, 'msg': error.message})
            return
        self.requests[rid] = request
        if kind.transfer:
            self.transfers += 1
        request.task = self.connection.app.spawn(request.run())

    def close(self) -> None:
        """Abort everything; a closed session sends nothing more."""
        self.closed = True
        for request in list(self.requests.values()):
            request.abort()


class Connection(asyncio.Protocol):
    def __init__(self, app: App) -> None:
        self.app = app
        self.sessions: dict[bytes, Session] = {}
        self.buffer = bytearray()
        self.writable = asyncio.Event()
        self.writable.set()
        self.transport: asyncio.Transport | None = None

    def connection_made(self, transport: asyncio.Transport) -> None:
        self.transport = transport
        transport.set_write_buffer_limits(high=WRITE_HIGH, low=WRITE_LOW)
        self.app.connections.add(self)
        self.write_line(b'READY QSF1')

    def connection_lost(self, exc: Exception | None) -> None:
        self.app.connections.discard(self)
        self.close_sessions()
        self.writable.set()

    def pause_writing(self) -> None:
        self.writable.clear()

    def resume_writing(self) -> None:
        self.writable.set()

    def write_line(self, line: bytes) -> None:
        if not self.transport.is_closing():
            self.transport.write(line + b'\n')
            # Transfers wait for this buffer. Only a peer that keeps asking
            # without ever reading the answers can get here.
            if self.transport.get_write_buffer_size() > MAX_WRITE_BUFFER:
                self.drop()

    def close_sessions(self) -> None:
        sessions, self.sessions = self.sessions, {}
        for session in sessions.values():
            session.close()

    def drop(self) -> None:
        self.close_sessions()
        self.transport.abort()

    def data_received(self, data: bytes) -> None:
        self.buffer += data
        start = 0
        while (end := self.buffer.find(b'\n', start)) >= 0:
            if end - start > MAX_LINE:
                self.drop()
                return
            self.on_line(bytes(self.buffer[start:end]))
            if self.transport.is_closing():
                return
            start = end + 1
        del self.buffer[:start]
        if len(self.buffer) > MAX_LINE:
            self.drop()

    def on_line(self, line: bytes) -> None:
        if line == b'FS_CAPS':
            self.write_line(b'OK FS_CAPS 1')
            return
        fields = line.split(b' ')
        if fields[0] != b'FS':
            self.write_line(b'ERR UNKNOWN_COMMAND')
            return
        if len(fields) < 3 or not SID.fullmatch(fields[1]):
            return
        sid, verb, arguments = fields[1], fields[2], fields[3:]
        if verb == b'D' and len(arguments) == 1:
            session = self.sessions.get(sid)
            if session is not None:
                session.on_frame(arguments[0])
        elif verb == b'O' and len(arguments) == 1 and arguments[0] in (b'r', b'rw'):
            self.open_session(sid, arguments[0].decode('ascii'))
        elif verb == b'C' and not arguments:
            session = self.sessions.pop(sid, None)
            if session is not None:
                session.close()

    def open_session(self, sid: bytes, rights: str) -> None:
        # Reopening replaces the old session, so its rights can never linger.
        previous = self.sessions.pop(sid, None)
        if previous is not None:
            previous.close()
        if len(self.sessions) >= MAX_SESSIONS:
            self.write_line(b'FS ' + sid + b' C limit')
            return
        self.sessions[sid] = Session(self, sid, rights)
        self.write_line(b'FS ' + sid + b' R')


def _command(*command: str) -> subprocess.CompletedProcess | None:
    """None if the tool is not installed. Every argument here is a constant."""
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors='replace', timeout=COMMAND_SECONDS, env={**os.environ, 'LC_ALL': 'C'})
    except FileNotFoundError:
        return None


SCREEN_SAVERS = (
    ('org.freedesktop.ScreenSaver', '/ScreenSaver', 'org.freedesktop.ScreenSaver'),
    ('org.freedesktop.ScreenSaver', '/org/freedesktop/ScreenSaver', 'org.freedesktop.ScreenSaver'),
    ('org.gnome.ScreenSaver', '/org/gnome/ScreenSaver', 'org.gnome.ScreenSaver'),
)
# GNOME's settings daemon owns org.freedesktop.ScreenSaver for inhibition only.
# An owner that lacks the call is not a locker, one that fails while running it is.
NOT_A_LOCKER = ('unknown ', 'no such ', 'does not exist', "doesn't exist", 'does not understand')


def session_locked() -> bool:
    """A locker that exists but fails counts as locked; none at all as not locked."""
    try:
        names = _command('busctl', '--user', '--timeout=1', 'call', 'org.freedesktop.DBus',
                         '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'ListNames')
        # No bus at all means no screen saver on it, and that is the tools' one clear answer.
        # A bus that does not answer may have a locker on it.
        if names is not None and names.returncode != 0 and not any(
                text in names.stderr.lower() for text in ('failed to connect', 'no such file')):
            return True
        owned = names.stdout if names is not None and names.returncode == 0 else ''
        for name, path, interface in SCREEN_SAVERS:
            if f'"{name}"' not in owned:
                continue
            reply = _command('busctl', '--user', '--timeout=1', '--auto-start=no', 'call',
                             name, path, interface, 'GetActive')
            if reply is None:
                continue
            if reply.returncode == 0:
                return reply.stdout.strip() != 'b false'
            if not any(text in reply.stderr.lower() for text in NOT_A_LOCKER):
                return True
        hint = _command('loginctl', 'show-session', 'auto', '-p', 'LockedHint', '--value')
        return hint is not None and hint.returncode == 0 and hint.stdout.strip() == 'yes'
    except (subprocess.SubprocessError, OSError):
        return True


class LockState:
    """Read on demand and cached, so a session without requests costs nothing."""

    def __init__(self, check, interval: float) -> None:
        self.check, self.interval = check, interval
        self.value = False
        self.read_at: float | None = None
        self.reading: asyncio.Future | None = None

    def read(self) -> bool:
        try:
            return bool(self.check())
        except Exception:
            return True

    def settled(self, future: asyncio.Future) -> None:
        self.value = future.result()
        self.read_at = asyncio.get_running_loop().time()
        self.reading = None

    async def locked(self) -> bool:
        loop = asyncio.get_running_loop()
        if self.read_at is not None and loop.time() - self.read_at < self.interval:
            return self.value
        if self.reading is None:
            self.reading = loop.run_in_executor(WORKERS, self.read)
            self.reading.add_done_callback(self.settled)
        return await asyncio.shield(self.reading)


class App:
    def __init__(self, lock: LockState, default_mode: int) -> None:
        self.lock, self.default_mode = lock, default_mode
        self.connections: set[Connection] = set()
        self.tasks: set[asyncio.Task] = set()
        try:
            account = pwd.getpwuid(os.getuid())
            self.user, self.home = account.pw_name, account.pw_dir
        except KeyError:
            self.user, self.home = str(os.getuid()), os.environ.get('HOME', '/')

    def spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


def listen(path: str) -> tuple[socket.socket, int, int]:
    """Bound 0600 from the start. Returns the socket, its inode and the umask."""
    os.makedirs(os.path.dirname(path) or '.', mode=0o700, exist_ok=True)
    try:
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise SystemExit(f'qsm-desktop-files: {path} exists and is not a socket')
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    umask = os.umask(0o177)
    try:
        server.bind(path)
    finally:
        os.umask(umask)
    os.chmod(path, 0o600)
    server.listen(16)
    server.setblocking(False)
    return server, os.stat(path).st_ino, umask


async def serve(path: str, check, interval: float, home: str | None = None) -> None:
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, stopping.set)
    listener, inode, umask = listen(path)
    app = App(LockState(check, interval), 0o666 & ~umask)
    app.home = home or app.home
    server = await loop.create_unix_server(lambda: Connection(app), sock=listener)
    await stopping.wait()
    server.close()
    with suppress(OSError):
        if os.stat(path).st_ino == inode:  # a newer helper may own the name by now
            os.unlink(path)
    for connection in list(app.connections):
        connection.drop()
    if app.tasks:
        await asyncio.wait(app.tasks, timeout=SHUTDOWN_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='QSM Direct file manager helper of one desktop session.')
    parser.add_argument('--listen', required=True, metavar='PATH', help='unix socket to create')
    # For the tests. The file holds 1 while "locked" and replaces the D-Bus and logind checks.
    parser.add_argument('--lock-state-file', help=argparse.SUPPRESS)
    parser.add_argument('--lock-interval', type=float, default=LOCK_SECONDS, help=argparse.SUPPRESS)
    parser.add_argument('--home', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    check = session_locked
    if args.lock_state_file is not None:
        def check() -> bool:
            try:
                with open(args.lock_state_file, encoding='ascii') as state:
                    return state.read().strip() == '1'
            except FileNotFoundError:
                return False
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(serve(args.listen, check, args.lock_interval, args.home))
    sys.stdout.flush()
    sys.stderr.flush()
    # Not a normal exit: that would join workers, and one may be stuck on a hung mount.
    os._exit(0)


if __name__ == '__main__':
    sys.exit(main())
