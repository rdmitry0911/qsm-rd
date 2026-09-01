# Sunshine–QEMU Desktop MVP

CPU-first implementation of a QEMU/KVM remote-desktop source for a future
Sunshine capture backend. Sunshine runs on the host; the guest only exposes its
QEMU display, input and audio interfaces. A GPU is **not** required for the
current development and test path.

## Current status

Version `0.4.0` contains an executable QEMU-side vertical slice, not only a
plan:

- real `org.qemu.Display1.Console.RegisterListener` client;
- peer-to-peer D-Bus listener over a passed Unix FD;
- CPU framebuffer capture through both `Scanout`/`Update` and Unix
  `ScanoutMap`/`UpdateMap`;
- validated pixman formats, dimensions, stride, damage rectangles and map
  lifetime;
- one-slot `latest frame wins` back-pressure;
- direct QEMU keyboard, absolute/relative mouse and button calls;
- `Console.SetUIInfo` resolution requests;
- cursor shape, hotspot, position and visibility reception;
- QEMU `AudioOutListener`, PCM-to-float conversion and a non-blocking bounded
  50 ms audio FIFO;
- a no-GPU H.264 diagnostic path using FFmpeg `libx264`;
- a standalone fake QEMU service for real cross-process D-Bus tests;
- a real-QEMU/TCG CPU E2E: project-owned BIOS/VGA fixture → private D-Bus →
  `RegisterListener` peer socket → H.264/`ffprobe` trace;
- normal and ASan/UBSan test suites.

The first in-tree Sunshine adapter is now available as a pinned, opt-in CPU
patch. It compiles in Sunshine and has a real-QEMU/TCG gate that exercises
Sunshine's `display_t` boundary and its software/libx264 encoder probe. It
does not yet produce a validated Moonlight network stream, direct QEMU input,
or QEMU audio; see `docs/SUNSHINE_QEMU_INTEGRATION.md`.

## Architecture implemented now

```text
fake or real QEMU
  │
  │ org.qemu.Display1 on a Unix D-Bus address
  ▼
QemuDbusDisplay
  ├── RegisterListener → peer D-Bus socket
  ├── Scanout / Update
  ├── ScanoutMap / UpdateMap
  ├── CursorDefine / MouseSet
  ├── Keyboard / Mouse / SetUIInfo
  └── AudioOutListener → float PCM
  │
  ├── LatestFrameMailbox (depth 1)
  └── AudioFifo (bounded, drops oldest)
  │
  ▼
DesktopSession
  │
  ├── CpuFrameSink → PPM/checksum
  └── FfmpegSoftwareAdapter → H.264 MKV (libx264, no GPU)
```

Target integration:

```text
QEMU D-Bus CPU framebuffer
  → Sunshine qemu_dbus display_t
  → Sunshine software encoder
  → stock Moonlight client
```

Later, only the capture storage changes:

```text
QEMU ScanoutDMABUF → Sunshine native GPU conversion/encoder
```

## Build and test without a GPU

Required on Linux:

- CMake 3.20 or newer;
- C++20 compiler;
- Ninja;
- runtime `libsystemd.so.0`;
- `dbus-run-session`;
- FFmpeg with `libx264` for the encoded self-test.

Run the complete reproducible path:

```bash
./scripts/run-no-gpu-selftest.sh
```

It performs a clean build, the fake-QEMU CTest matrix, a software H.264
self-test and `ffprobe` verification. When the host has QEMU and the toolchain
for the boot fixture, CTest also includes the real-QEMU E2E. Outputs are
written to:

```text
artifacts/validation/no-gpu-selftest/
```

Manual build:

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build -j2
ctest --test-dir build --output-on-failure
```

Sanitizers:

```bash
./scripts/run-sanitizers.sh
```

## Qualify against a real QEMU process

The CPU runtime gate uses a generated 512-byte VGA boot sector inside a
1.44-MiB floppy image. It does not need KVM, a GPU, a guest OS, an ISO, or a
network connection from the guest:

```bash
./scripts/install-qemu-debian.sh  # Debian/Ubuntu only
./scripts/run-real-qemu-selftest.sh
```

The retained E2E trace is written to `artifacts/validation/real-qemu/`.
See `docs/REAL_QEMU_E2E.md` for its exact coverage and limits. On QEMU 8.2
the real Linux lane is inline `Scanout`/`Update`; the Unix shared-map path
requires a newer QEMU and remains independently covered by the fake service.

For a small bootable Linux reference guest and a longer guest-HID E2E, see
[`docs/REFERENCE_VM.md`](docs/REFERENCE_VM.md).  The provisioner pins and
SHA-256-verifies an Alpine virt ISO outside Git, then the runner retains a
separate CPU-capture/H.264 trace.

## Qualify the patched Sunshine boundary

The pinned upstream patch and a reproducible real-QEMU gate are documented in
[`docs/SUNSHINE_QEMU_INTEGRATION.md`](docs/SUNSHINE_QEMU_INTEGRATION.md).
The gate proves the CPU `display_t` adapter plus Sunshine's software/libx264
encoder probing; it is intentionally narrower than a Moonlight session.

## Attach the probe to real QEMU

A CPU-only launch example is provided:

```bash
DISK_IMAGE=/path/to/guest.qcow2 \
  ./scripts/launch-qemu-dbus-cpu-example.sh
```

In another terminal, use the address file printed by the launcher:

```bash
./scripts/run-qemu-display-probe.sh \
  /tmp/qmdp-qmdp-cpu-demo/dbus.address
```

Set `ENCODE=1` to create H.264 segments. `REQUEST_SIZE` is intentionally empty
by default because `Console.SetUIInfo` is optional; set it only for a guest
display device that supports resize.

## Entry points

- `tools/qemu_display_probe.cpp` — attach to real QEMU and capture CPU frames;
- `tools/fake_qemu_dbus.cpp` — cross-process synthetic QEMU service;
- `tools/qmdp_dbus_selftest.cpp` — full no-GPU video/audio/resize self-test;
- `scripts/run-real-qemu-selftest.sh` — self-contained real-QEMU/TCG E2E;
- `docs/REAL_QEMU_E2E.md` — scope, artifacts and limits of that E2E;
- `scripts/run-upstream-sunshine-qemu-e2e.sh` — real QEMU → patched Sunshine
  CPU display/encoder gate;
- `docs/SUNSHINE_QEMU_INTEGRATION.md` — pinned patch, build and scope;
- `config/qemu-vm.cpu.example.toml` — explicit software-only baseline;
- `scripts/run-sanitizers.sh` — reproducible ASan/UBSan test matrix;
- `docs/PROJECT_TREE.md` — full repository tree and module ownership;
- `docs/IMPLEMENTATION_STATUS.md` — implemented versus pending functionality;
- `integration/sunshine/CPU_BACKEND_CONTRACT.md` — next in-tree Sunshine slice;
- `plan/mvp-backlog.yaml` — dependency-ordered engineering backlog.

## License

GPL-3.0-or-later. No QEMU or Sunshine source is vendored in this repository.
