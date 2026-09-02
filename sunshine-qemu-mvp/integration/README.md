# Integration route

The pinned Sunshine patch series now implements the functional QEMU Display1
route, not just a CPU capture contract:

```text
Moonlight client -> Sunshine qemu_dbus -> QEMU Display1 -> KVM/VirGL guest
                  -> QEMU AudioOutListener -> Sunshine Opus -> Moonlight
                  -> QEMU keyboard/mouse/SetUIInfo
```

The deployment profile is explicitly headless. It has no X11, Wayland,
PulseAudio, or ALSA runtime dependency; its full resolved `ldd` closure is
checked by `scripts/build-upstream-sunshine-qemu.sh`. A disposable Xvfb
process belongs only to the Moonlight SDL test client.

## Completed boundaries

- private message-bus and peer-to-peer D-Bus setup with
  `Console.RegisterListener` FD handoff;
- inline/map CPU capture plus validated single-plane
  `ScanoutDMABUF`/`UpdateDMABUF` import;
- headless GBM/EGL DMA-BUF -> CPU BGRX readback for the existing
  `libx264` software encoder;
- Sunshine keyboard/mouse routing to QEMU Display1, including bounded virtual
  absolute-pointer conversion for Moonlight-relative motion;
- guest PCM through `AudioOutListener`, a bounded FIFO, Sunshine Opus, and
  Moonlight decoded PCM;
- real KVM Q35 + `virtio-vga-gl` + VirGL on an NVIDIA render node;
- Moonlight fullscreen/windowed presentation, real guest evdev input,
  `SetUIInfo` mode evidence, real guest Weston clipboard bridging, and
  constrained QSF file transfer.

## What is deliberately separate

QSF is the authenticated companion for UTF-8 clipboard and files. It is not an
extension of the stock Moonlight/GameStream protocol. Its local control socket
is token protected; the optional mTLS gateway authenticates a remote companion
without sending that local token over TCP.

The native graphics path is functional but currently performs CPU readback
before `libx264`. It must not be described as zero-copy or hardware encoded.
Production service supervision, reconnect/soak coverage, latency targets,
multi-plane DMA-BUF, and guest OS coverage beyond the test image remain future
operational work.

## Entry points

- [`sunshine/PINNED_UPSTREAM.md`](sunshine/PINNED_UPSTREAM.md) — exact upstream base and replay.
- [`sunshine/PATCH_SERIES.md`](sunshine/PATCH_SERIES.md) — six patch layers and constraints.
- [`moonlight/PINNED_UPSTREAM.md`](moonlight/PINNED_UPSTREAM.md) — pinned native
  PIN-free GameStream lease client and replay.
- [`sunshine/QEMU_INPUT_AND_DATA_SCOPE.md`](sunshine/QEMU_INPUT_AND_DATA_SCOPE.md) — input and companion protocol boundary.
- [`../docs/SUNSHINE_QEMU_INTEGRATION.md`](../docs/SUNSHINE_QEMU_INTEGRATION.md) — build profile.
- [`../docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md`](../docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md) — native video/input gate.
- [`../docs/MOONLIGHT_AUDIO_E2E.md`](../docs/MOONLIGHT_AUDIO_E2E.md) — decoded-audio gate.
- [`../docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md`](../docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md) — desktop clipboard/file/resize gate.
