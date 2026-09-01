# Sunshine patch series

The first CPU display patch is exported and qualified against real QEMU. The
remaining series is intentionally split so each next layer remains reviewable
without a GPU.

## 0. `platform/linux: add QEMU Display1 CPU capture` — implemented

- opt-in `SUNSHINE_ENABLE_QEMU_DBUS` source selection;
- private message-bus address plus `RegisterListener` peer-FD handoff;
- inline CPU `Scanout`/`Update`, pixman normalization and safe stale-damage
  handling;
- software encoder `img_t` path and a real-QEMU/TCG Sunshine encoder-probe
  gate.

See `PINNED_UPSTREAM.md` and `docs/SUNSHINE_QEMU_INTEGRATION.md` for the exact
upstream base, patch and replay command.

## 1. `platform/linux: add Unix shared-map and DMA-BUF policy`

- Unix `ScanoutMap`/`UpdateMap`;
- explicit Map capability advertisement only after mmap lifetime is safe;
- keep the inline CPU fallback as the required baseline.

## 2. `platform/linux: route Moonlight input directly to QEMU`

- key/button state tracking;
- absolute and relative pointer paths;
- release-all on session teardown;
- no dependency on host desktop focus.

## 3. `platform/linux: add QEMU D-Bus audio source`

- `AudioOutListener` registration;
- PCM conversion;
- bounded FIFO;
- `platf::audio_control_t`/`mic_t` adapter;
- per-VM isolation.

## 4. `config: expose qemu_dbus diagnostics`

- capture selector documentation and structured counters;
- structured counters;
- clear software fallback reporting;
- configuration validation.

## 5. `platform/linux: add QEMU DMA-BUF fast path`

Deferred until the CPU path is merged and stable:

- single- and multi-plane DMA-BUF;
- modifier handling;
- EGL/Vulkan/VAAPI import;
- zero full-frame CPU readback;
- explicit fallback to the CPU implementation.
