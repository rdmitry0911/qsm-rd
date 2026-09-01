# Implementation status

Status date: 2026-09-01. Project version: `0.4.0` plus unreleased real-QEMU
qualification work.

## Implemented and executable

| Area | State | Evidence |
|---|---|---|
| D-Bus connection | Implemented | message-bus address and inherited peer-FD paths compile and run |
| `Console.RegisterListener` | Implemented | real socket-FD handoff and peer D-Bus handshake in tests |
| Inline framebuffer | Implemented | `Scanout` + `Update` cross-process E2E |
| Shared CPU framebuffer | Implemented | `memfd` + `ScanoutMap` + `UpdateMap` E2E |
| Pixel validation/conversion | Implemented | eight 32-bit pixman layouts, bounds/stride/map checks, BGRA conversion |
| Video back-pressure | Implemented | queue depth one; stale complete frames are superseded |
| Resolution request | Implemented | `Console.SetUIInfo` observed by the fake QEMU process |
| Keyboard | Implemented | `Press`/`Release` observed cross-process |
| Mouse | Implemented | absolute, relative and button calls observed cross-process |
| Cursor metadata | Implemented | shape, hotspot, position and visibility received |
| QEMU audio output | Implemented | listener lifecycle and PCM-to-float integration test |
| Audio back-pressure | Implemented | callbacks enqueue into bounded 50 ms FIFO; encoder work is off the D-Bus thread |
| CPU diagnostic sink | Implemented | deterministic checksums and PPM snapshots |
| Software H.264 diagnostic sink | Implemented | FFmpeg/libx264 MKV segments without GPU |
| Resize during stream | Implemented in vertical slice | 640×360 → 854×480 creates a second encoder segment and IDR boundary |
| Real QEMU CPU transport | Implemented | TCG/SeaBIOS VGA fixture → private D-Bus → `RegisterListener` → inline CPU capture → H.264/`ffprobe` |
| Stale geometry damage | Implemented | incompatible old-mode update is acknowledged, counted and never copied into the current surface |
| Alpine reference guest | Passed | real Alpine 3.24.1 guest, `virtio-vga`, USB tablet, inline CPU capture and H.264 |
| Sunshine CPU display patch | Compiled and qualified | pinned Sunshine source selects `qemu_dbus`, receives real QEMU frames and passes its software/libx264 probe |
| Sanitizers | Passing | ASan + UBSan, including D-Bus FD passing and FFmpeg subprocess path |

## Test layers

1. Core unit tests: FD RAII, latest-frame mailbox, resize coalescing, audio FIFO,
   session state machine.
2. In-process QEMU peer test: real sd-bus, Unix FD passing, shared map, input,
   cursor, audio and resize.
3. No-GPU H.264 self-test: fake QEMU → D-Bus → CPU frames → `libx264`.
4. Cross-process message-bus test with shared memory.
5. Cross-process message-bus test with inline frame payloads.
6. Real-QEMU TCG E2E with a project-owned boot sector/floppy and retained
   H.264 trace.
7. Alpine reference-guest E2E with real guest HID selecting an absolute tablet.
8. Patched upstream Sunshine → real QEMU → software/libx264 encoder-probe
   E2E.

## Implemented as an integration contract

| Area | State |
|---|---|
| Sunshine `platf::display_t` backend | pinned opt-in CPU patch compiled and qualified against real QEMU; see `SUNSHINE_QEMU_INTEGRATION.md` |
| Sunshine `platf::mic_t`/audio integration | QEMU listener and FIFO work; Sunshine object adapter remains |
| Per-VM packaging | configuration and launch model prepared; production service template remains |
| Real QEMU execution | inline CPU lane passed against QEMU 8.2 under TCG; see `REAL_QEMU_E2E.md` |

## Not implemented yet

- an actual Sunshine/Moonlight network session from the QEMU capture source;
- QEMU `ScanoutDMABUF`/`UpdateDMABUF` fast path;
- automatic reconnect to a restarted real QEMU process;
- pressed-key/button tracking with release-all on abnormal disconnect;
- QEMU clipboard bridge;
- Moonlight-Qt live-resize, clipboard and local-cursor side channel;
- multi-console and multi-VM broker;
- guest-agent features such as DPI, IME and file transfer.

## Current vertical slice

```text
fake QEMU process
  → real message-bus D-Bus
  → RegisterListener socket FD
  → real peer-to-peer D-Bus
  → inline bytes or memfd framebuffer
  → latest-frame mailbox
  → CPU frame sink or FFmpeg/libx264

real QEMU under TCG
  → patched Sunshine `qemu_dbus` `display_t`
  → Sunshine software/libx264 encoder probe

QEMU AudioOutListener
  → PCM conversion
  → bounded AudioFifo
  → independent audio consumer thread
```

The same `QemuDbusDisplay` class is used by `qemu-display-probe`; replacing the
fake service with `qemu-system-* -display dbus` does not change the listener
implementation.

QEMU 8.2 on Linux selects the inline `Scanout`/`Update` lane. Its Unix
shared-map transport is not available, so `ScanoutMap`/`UpdateMap` remain
protocol-tested with the fake service until qualification on QEMU 9.2 or newer.

The Sunshine patch deliberately advertises no shared-map capability and requires
`capture=qemu_dbus`, `encoder=software`, and QEMU `gl=off`. Its real gate proves
the capture/encoder boundary, not a Moonlight session, input, audio, or
reconnect.
