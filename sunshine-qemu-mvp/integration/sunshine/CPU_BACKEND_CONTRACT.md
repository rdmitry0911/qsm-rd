# Sunshine CPU capture backend contract

## Purpose

This document began as the first upstream-facing integration slice.  The
current exported series retains its safe inline CPU baseline and additionally
implements QEMU Display1 input, guest audio, and single-plane DMA-BUF import.
DMA-BUF is imported on a headless GBM/EGL render node and read back to ordinary
CPU-resident BGRX memory for Sunshine's software encoder; it is not a native
encoder or zero-copy implementation.

The standalone implementation already proves the QEMU side of the contract in
`QemuDbusDisplay`, `CpuFramebuffer`, and `DesktopSession`. The exported patch
moves the inline CPU lifecycle into Sunshine's Linux platform layer.

## Implemented Sunshine files

```text
src/platform/linux/qemu_dbus.h
src/platform/linux/qemu_dbus.cpp
```

Minimal registry changes:

```text
src/platform/linux/misc.cpp
cmake/compile_definitions/linux.cmake
```

Opt-in CMake option:

```text
SUNSHINE_ENABLE_QEMU_DBUS
```

Suggested configuration:

```ini
capture = qemu_dbus
encoder = software
```

```text
SUNSHINE_QEMU_DBUS_ADDRESS=unix:path=/run/qmdp/vm42/bus
SUNSHINE_QEMU_DBUS_DESTINATION=org.qemu  # optional
```

## `platf::display_t` mapping

Create `qemu_dbus_display_t : platf::display_t`.

### Construction

1. Connect to the configured D-Bus address.
2. Resolve `/org/qemu/Display1/Console_<id>`.
3. Create a Unix `socketpair()`.
4. Call `Console.RegisterListener()` with QEMU's end before opening the
   synchronous peer client. This prevents an authentication deadlock.
5. Serve `org.qemu.Display1.Listener` on the local peer end and report an
   empty `Interfaces` property, deliberately declining Unix shared maps.
6. Wait for the first scanout before returning a usable display.
7. Set `width`, `height`, `logical_width`, and `logical_height` from the active
   surface.

### `alloc_img()`

Return a CPU image object derived from `platf::img_t`:

```cpp
struct qemu_cpu_img_t : platf::img_t {
  std::vector<std::uint8_t> storage;
};
```

For the software encoder path:

- `data` points to `storage.data()`;
- `row_pitch` is `width * 4` unless Sunshine's converter requires alignment;
- `pixel_pitch = 4`;
- pixel order is normalized once to Sunshine's expected BGR0 format.

### `capture()`

The D-Bus dispatch thread never calls Sunshine callbacks and never waits for an
encoder image. It updates the current CPU framebuffer and publishes a generation
number into a one-entry latest-frame mailbox.

The Sunshine capture thread:

1. waits for a generation newer than the last delivered generation;
2. calls `pull_free_image_cb()`;
3. copies the current normalized surface to that image;
4. stamps `frame_timestamp` at receipt/update time;
5. calls `push_captured_image_cb(..., true)` from the capture thread;
6. returns `capture_e::reinit` when geometry or pixel format changes;
7. emits a timeout callback when no new generation arrives before the capture
   interval.

This design intentionally performs one CPU copy in the MVP. It is slower than
DMA-BUF but deterministic and compatible with `encoder = software`. A closed
peer marks capture for `capture_e::reinit`; it does not keep publishing a stale
frame.

### `dummy_img()`

Fill the provided image with black using the active geometry. Do not call back
into QEMU from this method.

### Surface changes

A new `Scanout` with changed geometry creates a new surface generation.
`Update` mutates the existing generation's pixels and increments the frame
sequence. An update that refers to unavailable or out-of-bounds geometry is
acknowledged and dropped. The capture method reports `capture_e::reinit` for a
new surface generation.

### Back-pressure

The queue depth is exactly one. If QEMU publishes N frames while the software
encoder is busy, only the newest complete framebuffer is delivered next. The
backend records dropped/superseded frame count but never blocks the QEMU peer
D-Bus connection.

## Input mapping — implemented

Sunshine's Linux input backend should be made selectable independently from the
capture backend. In QEMU mode:

- keyboard make/break → `org.qemu.Display1.Keyboard.Press/Release`;
- mouse button make/break → `org.qemu.Display1.Mouse.Press/Release`;
- absolute position → `SetAbsPosition`;
- relative motion → `RelMotion`;
- disconnect/error → synthesize releases for every locally tracked pressed key
  and button before closing the connection when possible.

The exported Sunshine input wrappers route Moonlight keyboard and mouse events
to these methods.  For QEMU absolute devices, Moonlight's relative deltas are
scaled and accumulated into bounded `SetAbsPosition` calls; `RelMotion` is
used only for relative QEMU devices.  A native Moonlight E2E additionally
requires raw guest evdev evidence for `KEY_A`, pointer movement, and
`BTN_LEFT`, rather than treating a D-Bus reply as input proof.

## Audio mapping — implemented

`qemu_dbus_audio_control_t : platf::audio_control_t` and
`qemu_dbus_mic_t : platf::mic_t` (the Sunshine name `mic_t` represents the host
playback capture source) are selected by the QEMU guest-audio-only build.

1. Register `org.qemu.Display1.AudioOutListener`.
2. Convert QEMU integer or float PCM to interleaved normalized `float`.
3. Resample only when QEMU's rate differs from the active Sunshine stream.
4. Put samples into a bounded FIFO, defaulting to 50 ms.
5. `mic_t::sample()` pops exactly the frame count requested by Sunshine.
6. On overflow, discard the oldest complete sample frames.
7. On underflow, return silence when continuous audio is requested.

## Failure semantics

| Failure | Result |
|---|---|
| Initial bus/name unavailable | backend construction fails with actionable log |
| Listener peer disconnect | capture returns `reinit`; reconnect policy runs outside callback |
| Invalid dimensions/stride/payload size | reject message and preserve previous valid surface |
| Unsupported pixman format | report backend error; never reinterpret bytes silently |
| Audio interface absent | video remains available unless audio was configured as required |
| Encoder slower than source | supersede old frame; never grow queue |

## Acceptance for the current Sunshine patch

- The pinned upstream replay builds with `SUNSHINE_ENABLE_QEMU_DBUS=ON`,
  `SUNSHINE_ENABLE_QEMU_DBUS_DMABUF=ON`, and
  `SUNSHINE_ENABLE_QEMU_DBUS_AUDIO_ONLY=ON`.
- The final deployment artifact has no X11, Wayland, PulseAudio, or ALSA
  dependency in its complete `ldd` closure.  It is still a headless GBM/EGL
  consumer and requires its configured render node for the DMA-BUF lane.
- The CPU/TCG gate observes `Screencasting with QEMU Display1 D-Bus`, creates
  `libx264`, and selects `software`; the native KVM/VirGL gate additionally
  proves Moonlight pairing, input, DMA-BUF, guest mode change, and decoded
  client video.
- The audio gate proves QEMU `AudioOutListener` through Sunshine Opus to a
  non-silent Moonlight-decoded client artifact.
- Unix shared maps, cursor composition, reconnect/soak, release-all under an
  unexpected disconnect, `ScanoutDMABUF2`, native GPU conversion, and hardware
  encoding remain outside this contract.
