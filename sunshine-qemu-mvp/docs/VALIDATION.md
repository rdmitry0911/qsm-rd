# Validation

Validation date: 2026-09-01 UTC.

## Environment

```text
cmake version 3.28.3
GNU C++ 13.3.0
FFmpeg 6.1.1
Ubuntu 24.04 container, x86-64
GPU: not used
QEMU: 8.2.2, `-display dbus` and `-audiodev dbus` available
KVM device: absent; real guest test forced `-accel tcg`
```

The tests use the real system D-Bus ABI, Unix socket FD passing, peer-to-peer
D-Bus, `memfd_create()`, `mmap()`, threads and FFmpeg. The fake services retain
coverage for deterministic fault injection and Unix shared maps; the real-QEMU
lane qualifies the installed QEMU D-Bus implementation itself.

## Release/RelWithDebInfo matrix

Executed directly through:

```bash
cmake --build build-runtime -j2
ctest --test-dir build-runtime --output-on-failure
```

CTest result:

```text
100% tests passed, 0 tests failed out of 7
Total Test time (real) = 7.69 sec
```

### 1. Core tests

```text
qmdp_core_tests: passed
```

Covers geometry, pixel formats, mapped-region bounds, latest-frame mailbox,
audio FIFO, resize coalescing and session lifecycle.

### 2. In-process peer-D-Bus integration

One recorded run:

```text
frames published/encoded/dropped: 41/26/15
map scanouts/updates: 2/39
final mode: 426x242
input keyboard/mouse: 2/4
audio writes/frames: 67/32160
session errors: 0
```

This test uses a real `socketpair`, D-Bus authentication, Unix FD transfer and
`memfd` framebuffer. A deliberately slow CPU sink verifies that stale frames are
superseded instead of queued.

### 3. Cross-process shared-map E2E

```text
FAKE_QEMU_RESULT frames=30 ui_info=1 keyboard=2 mouse=4
requested=320x180 listener=1 peer_completed=1

frames published/encoded/dropped: 30/30/0
scanout inline/map: 0/1
updates inline/map: 0/29
cursor definitions/moves: 1/1
session errors: 0
```

A separate `qmdp-fake-qemu` process owns `org.qemu` on a real session bus. The
probe calls `RegisterListener`; the framebuffer is shared by `memfd` over the peer
connection.

### 4. Cross-process inline E2E

```text
FAKE_QEMU_RESULT frames=30 ui_info=1 keyboard=2 mouse=4
requested=320x180 listener=1 peer_completed=1

frames published/encoded/dropped: 30/30/0
scanout inline/map: 1/0
updates inline/map: 29/0
cursor definitions/moves: 1/1
session errors: 0
```

This exercises the byte-array fallback used when shared maps or DMA-BUF are not
available.

### 5. No-GPU software-encode self-test

A 1.2-second recorded run:

```text
encoded frames: 36
output segments: 2
final mode: 854x480
shared-map scanouts/updates: 2/39
mailbox dropped: 5
audio callbacks/frames: 132/63360
ffmpeg exit status: 0
display audio registered/failures/writes: 1/0/132
session errors: 0
```

`ffprobe` verified:

```text
qmdp-cpu-000.mkv
  codec: h264
  size: 640x360
  pixel format: yuv420p
  rate: 30 fps
  duration: 0.7 s

qmdp-cpu-001.mkv
  codec: h264
  size: 854x480
  pixel format: yuv420p
  rate: 30 fps
  duration: 0.5 s
```

The two files prove encoder restart on a display geometry change. They are test
segments, not a benchmark or a Moonlight stream.

### 6. Real-QEMU TCG inline CPU E2E

Executed through:

```bash
./scripts/run-real-qemu-selftest.sh
```

The test generated and verified a `512`-byte boot sector with signature `55 aa`,
embedded it in a `1474560`-byte floppy image, started a private session D-Bus,
and ran a real QEMU 8.2 process with `-accel tcg -display dbus,gl=off`.

```text
frames published/encoded/dropped: 42/41/1
scanout inline/map: 42/0
ffmpeg exit status: 0
session errors: 0

qemu-probe-001.mkv
  codec: h264
  size: 640x400
  pixel format: yuv420p
  rate: 30 fps
```

The probe performed `Console.RegisterListener` over the real message bus and
peer socket, captured real inline CPU scanouts, sent keyboard/relative-pointer/
button input, and encoded the result through FFmpeg. The exact raw trace is
`artifacts/validation/real-qemu/trace.txt`.

This QEMU version does not expose Unix `Listener.Unix.Map`, so the real test
correctly records only the inline lane. Standard VGA also rejects optional
`Console.SetUIInfo`; the test does not misrepresent that as a resize pass.

### 7. Alpine reference-guest E2E

Executed through:

```bash
BUILD_DIR="$PWD/build-runtime" BOOT_WAIT_SECONDS=12 \
  ./scripts/run-alpine-reference-e2e.sh
```

The pinned Alpine virt 3.24.1 ISO has SHA-256
`e73a6241bd5f3c5c2d4d38c02cc52c378c0415a7c888bd292066bf36e0f41a39`.
Its TCG guest selected the USB tablet through the Linux HID stack; the probe
recorded absolute input, 5/5 encoded frames, inline scanouts, H.264 output and
zero session errors. The compact trace is
`artifacts/validation/alpine-reference-e2e/trace.txt`.

### 8. Patched upstream Sunshine real-QEMU gate

The exported patch was applied to Sunshine `v2026.830.223700`
(`4f39fc116294abf8241bcd30e1b1e23d371e6e7b`), built with only the QEMU
capture source, and run through:

```bash
SUNSHINE_BINARY=/tmp/q-sunshine-upstream-qemu/sunshine \
  ./scripts/run-upstream-sunshine-qemu-e2e.sh
```

The gate passed against QEMU 8.2.2 under TCG. Its trace records
`capture=qemu_dbus`, `encoder=software`, QEMU Display1 screencasting,
`libx264` creation, and `Found H.264 encoder: libx264 [software]`.
The bounded `timeout` exit status is intentional after startup probing. See
`SUNSHINE_QEMU_INTEGRATION.md`; this is not a Moonlight, input, or audio test.

## Sanitizers

A separate Debug build was configured with:

```text
-fsanitize=address,undefined
-fno-omit-frame-pointer
ASAN_OPTIONS=detect_leaks=1:halt_on_error=1
UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1
```

Result:

```text
100% tests passed, 0 tests failed out of 5
Total Test time (real) = 4.41 sec
```

No AddressSanitizer, leak sanitizer or UndefinedBehaviorSanitizer error was
reported.

## Harmless container warning

`dbus-run-session` prints:

```text
Failed to set fd limit to 65536: Operation not permitted
```

The restricted container does not allow increasing its hard descriptor limit.
The D-Bus sessions and all E2E assertions still complete successfully. This is
not emitted by the project code.

## Evidence files

```text
artifacts/validation/no-gpu-selftest/environment.txt
artifacts/validation/no-gpu-selftest/ctest.log
artifacts/validation/no-gpu-selftest/selftest.log
artifacts/validation/no-gpu-selftest/ffprobe.txt
artifacts/validation/no-gpu-selftest/encoded/*.mkv
artifacts/validation/no-gpu-selftest/*-final.ppm
artifacts/validation/sanitizers-ctest.log
artifacts/validation/real-qemu/environment.txt
artifacts/validation/real-qemu/ctest.log
artifacts/validation/real-qemu/trace.txt
artifacts/validation/real-qemu/probe.log
artifacts/validation/real-qemu/qemu.log
artifacts/validation/real-qemu/ffprobe.txt
artifacts/validation/real-qemu/encoded/*.mkv
artifacts/validation/alpine-reference-e2e/trace.txt
artifacts/validation/alpine-reference-e2e/encoded/*.mkv
artifacts/validation/upstream-sunshine-qemu-e2e/trace.txt
artifacts/validation/upstream-sunshine-qemu-e2e/sunshine.log
```

## Not validated here

The following claims are intentionally not made by this package:

- an actual Sunshine/Moonlight network session;
- Linux login or Windows login behavior;
- QEMU restart/reconnect soak;
- clipboard;
- DMA-BUF or hardware encoding;
- latency or CPU-performance targets.

The real-QEMU result is a narrow, reproducible transport/capture/input/H.264
qualification. Its scope and exclusions are also documented in
`REAL_QEMU_E2E.md`.
