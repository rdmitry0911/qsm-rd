# Current status

Canonical code is `sunshine-qemu-mvp/`: the supplied `0.4.0` standalone
project plus unreleased work in this repository.

## Verified now

- The host has QEMU 8.2.2 with the D-Bus display/audio modules. `/dev/kvm` is
  absent, so all real-guest checks force TCG.
- The standalone matrix passes all 7 CTests, including real QEMU relative and
  absolute-pointer lanes.
- A pinned Alpine virt 3.24.1 reference ISO is installed outside Git and passes
  the real guest CPU-capture/H.264/absolute-tablet gate. Its verified ISO
  SHA-256 is `e73a6241bd5f3c5c2d4d38c02cc52c378c0415a7c888bd292066bf36e0f41a39`.
- An opt-in Sunshine `qemu_dbus` CPU-display patch is pinned to Sunshine
  `v2026.830.223700`. It compiles and its real-QEMU gate observes the adapter
  and Sunshine's software/libx264 encoder probe.

Generated evidence is retained locally at:

```text
sunshine-qemu-mvp/artifacts/validation/real-qemu/trace.txt
sunshine-qemu-mvp/artifacts/validation/alpine-reference-e2e/trace.txt
sunshine-qemu-mvp/artifacts/validation/upstream-sunshine-qemu-e2e/trace.txt
```

## Still open, in priority order

1. Route Moonlight keyboard/mouse into QEMU, track pressed state, and validate
   release-all on disconnect.
2. Add QEMU AudioOutListener through Sunshine's audio abstraction.
3. Validate a stock Moonlight session, then QEMU restart/reconnect behavior.
4. Add Unix shared-map/DMA-BUF only after the CPU fallback stays qualified.

No current result claims a production Moonlight stream, direct QEMU input,
QEMU audio, reconnect, release-all, DMA-BUF, or hardware encoding.
