# Implementation status

Status date: 2026-08-31. Project version: `0.4.0`.

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
| Sanitizers | Passing | ASan + UBSan, including D-Bus FD passing and FFmpeg subprocess path |

## Test layers

1. Core unit tests: FD RAII, latest-frame mailbox, resize coalescing, audio FIFO,
   session state machine.
2. In-process QEMU peer test: real sd-bus, Unix FD passing, shared map, input,
   cursor, audio and resize.
3. No-GPU H.264 self-test: fake QEMU → D-Bus → CPU frames → `libx264`.
4. Cross-process message-bus test with shared memory.
5. Cross-process message-bus test with inline frame payloads.

## Implemented as an integration contract

| Area | State |
|---|---|
| Sunshine `platf::display_t` backend | exact CPU backend contract and patch split prepared; not yet compiled in Sunshine |
| Sunshine `platf::mic_t`/audio integration | QEMU listener and FIFO work; Sunshine object adapter remains |
| Per-VM packaging | configuration and launch model prepared; production service template remains |
| Real QEMU execution | probe and launch command ready; `qemu-system-*` absent in this build container |

## Not implemented yet

- actual Sunshine/Moonlight network stream from the QEMU capture source;
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

QEMU AudioOutListener
  → PCM conversion
  → bounded AudioFifo
  → independent audio consumer thread
```

The same `QemuDbusDisplay` class is used by `qemu-display-probe`; replacing the
fake service with `qemu-system-* -display dbus` does not change the listener
implementation.
