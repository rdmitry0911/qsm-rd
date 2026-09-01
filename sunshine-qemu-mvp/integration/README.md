# Integration route

The standalone implementation completed the CPU-side QEMU transport, and the
first opt-in Sunshine CPU display patch is now exported, compiled, and run
against real QEMU. The remaining integration is deliberately staged so stock
Moonlight can be used before any client fork exists.

## Completed outside Sunshine

- message-bus and peer-to-peer D-Bus setup;
- `RegisterListener` socket transfer and ownership;
- inline and Unix shared-map framebuffers;
- cursor, `SetUIInfo`, direct keyboard/mouse and QEMU AudioOut;
- one-slot back-pressure;
- software H.264 diagnostic encoding;
- fake-QEMU cross-process test and sanitizer coverage.

## Current Sunshine boundary

`sunshine/patches/0001-platform-linux-add-QEMU-Display1-CPU-capture.patch`
implements `qemu_dbus_display_t : platf::display_t` for:

```text
capture = qemu_dbus
encoder = software
```

It is qualified through Sunshine's software/libx264 encoder probe and does not
require `/dev/dri`, VAAPI, NVENC or Vulkan. It is not yet a qualified Moonlight
network stream; direct QEMU input and audio are separate next boundaries.
See `sunshine/PINNED_UPSTREAM.md` for the patch's exact source base and
`docs/SUNSHINE_QEMU_INTEGRATION.md` for its real-QEMU gate.

## Execution contexts

The production backend has at least three independent contexts:

1. QEMU D-Bus dispatch;
2. Sunshine display capture/encoder scheduling;
3. Sunshine audio pull.

The D-Bus dispatch path may validate/copy a CPU update and publish a frame
sequence, but it must never wait for an encoder image or network packet. Video
back-pressure remains a one-slot latest-frame mailbox. Audio uses a bounded FIFO.

## Integration phases

### Phase A — Sunshine software stream

- compile the QEMU transport into Sunshine — complete;
- return CPU `img_t` frames from `platf::display_t` — complete;
- use Sunshine's existing software H.264 encoder probe — complete;
- use stock Moonlight;
- route Moonlight input to QEMU;
- expose QEMU PCM through Sunshine audio.

### Phase B — real-QEMU hardening

- UEFI, bootloader, Linux and Windows login testing;
- listener disconnect/reconnect;
- QEMU reboot and process restart;
- 8-hour soak and 100 reconnect cycles;
- per-VM D-Bus and service isolation.

### Phase C — GPU optimization

- `ScanoutDMABUF` and multi-plane `ScanoutDMABUF2`;
- modifier validation;
- existing Sunshine GPU conversion/encoder paths;
- measured fallback to the Phase A CPU path.

### Phase D — desktop extension

- Moonlight-Qt capability negotiation;
- debounced live resize;
- QEMU clipboard broker;
- local cursor;
- later microphone and files.

## Review evidence required for each patch

- changed-file list;
- ownership/lifetime statement for every FD and mapped surface;
- state transition and reconnect behavior;
- unit and integration tests;
- sanitizer result;
- p50/p95/p99 capture age when performance is relevant;
- explicit fallback and rollback behavior.
