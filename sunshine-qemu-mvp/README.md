# q-sunshine — headless QEMU/VirGL desktop source for Sunshine

This repository implements and qualifies a headless QEMU Display1 capture
backend for a pinned Sunshine build. The accepted native route is:

```text
Moonlight Embedded client
  -> Sunshine GameStream (private pairing, HTTPS, RTSP/RTP)
  -> QEMU Display1 D-Bus capture/input/audio
  -> KVM Q35 + virtio-vga-gl
  -> Alpine guest + VirGL on the host NVIDIA render node
```

The deployment-side Sunshine binary is deliberately free of X11, Wayland,
PulseAudio, and ALSA dependencies. The short-lived Xvfb instance used by the
Moonlight Embedded test client is a **client harness only**; it is never part
of the q-sunshine host runtime.

## Accepted functionality

The current native acceptance suite has passed on QEMU 8.2.2, KVM, and an
NVIDIA RTX 3080 render node:

| Capability | What is exercised end to end |
| --- | --- |
| Video | Moonlight pairing, HTTPS launch, RTSP/RTP, H.264 encode/decode, QEMU `ScanoutDMABUF`/`UpdateDMABUF`, KVM, `virtio_gpu`, VirGL and a non-black client image |
| Fullscreen and windowed client modes | Moonlight's `1280x720` fullscreen presentation at `(0,0)` and a `1280x720` window inside a `1600x900` root |
| Resolution | Sunshine/QSF `Console.SetUIInfo(1280x720)`, a new guest scanout, and independently encoded `1280x800 -> 1280x720` H.264 segments |
| Keyboard and mouse | A real Moonlight SDL key, relative/absolute motion and click, observed as raw guest evdev `KEY_A`, absolute pointer and `BTN_LEFT` events |
| Guest audio | QEMU D-Bus `AudioOutListener` -> Sunshine Opus -> Moonlight decoded non-silent 48 kHz stereo PCM, without a host sound server |
| Clipboard | QSF companion text reaches real guest Weston `wl-paste`; a guest `wl-copy` reaches the client side, with independent hashes |
| Files | QSF upload and download through QEMU virtio-serial, with independent guest/client SHA-256 assertions |

The strongest combined evidence is the one-VM native composite in
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.KEqkVc/` (ignored because it
contains ephemeral credentials). It ran the final no-desktop-dependency
Sunshine binary, Moonlight input, a real Weston DRM clipboard bridge, files,
and the mode transition together.

## Runtime topology

```text
                 client side only                         headless host
Moonlight SDL/Xvfb ── GameStream ──> Sunshine qemu_dbus ── private D-Bus ──> QEMU
       │                                    │                                     │
       │                                    ├─ QEMU AudioOutListener               ├─ KVM/Q35
       │                                    ├─ ScanoutDMABUF                       ├─ virtio-vga-gl
       │                                    └─ Keyboard/Mouse/SetUIInfo            └─ virtio-serial
       │                                                                          │
       └── QSF mTLS/local companion ───────── QSF control ───────────────────────┘
                                                                                  │
                                                                       Alpine + VirGL + Weston DRM
                                                                       wl-copy / wl-paste + QSF agent
```

The QSF route is intentionally a separate authenticated companion protocol.
Stock Moonlight/GameStream does not standardize bidirectional clipboard or
file-transfer messages, so those functions are not mislabeled as GameStream
features.

## Build the deployment artifact

The reproducible helper applies Sunshine patches `0001` through `0006` to the
pinned upstream revision, uses an isolated `libva 2.21` prefix only for the
bundled FFmpeg ABI, and rejects forbidden libraries across the full `ldd`
closure.

```bash
./scripts/build-isolated-libva-2.21.sh
SUNSHINE_BUILD_DIR="$PWD/.upstream/build-sunshine-qemu-no-x11" \
  ./scripts/build-upstream-sunshine-qemu.sh
```

The resulting binary is:

```text
.upstream/build-sunshine-qemu-no-x11/sunshine
```

It uses a GBM/EGL render node for QEMU DMA-BUF import but no host display
server. Its current renderer path imports a DMA-BUF into headless EGL and
reads it back to CPU BGRX for Sunshine's `libx264` software encoder. It is a
functional native VirGL path, **not** zero-copy capture or hardware encoding.

## Re-run the native gates

Build the project observer with DMA-BUF support and point every Sunshine test
at the exact deployment artifact:

```bash
cmake -S . -B .build-dmabuf -G Ninja -DQMDP_ENABLE_DMABUF_READBACK=ON
cmake --build .build-dmabuf --target qemu-display-probe

SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine" \
MOONLIGHT_BINARY="$PWD/.upstream/build-moonlight-embedded/moonlight" \
QEMU_DISPLAY_PROBE="$PWD/.build-dmabuf/qemu-display-probe" \
QEMU_ACCEL=kvm STREAM_SECONDS=12 \
  ./scripts/run-moonlight-sunshine-virgl-e2e.sh

SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine" \
MOONLIGHT_BINARY="$PWD/.upstream/build-moonlight-embedded/moonlight" \
QEMU_DISPLAY_PROBE="$PWD/.build-dmabuf/qemu-display-probe" \
QEMU_ACCEL=kvm MOONLIGHT_WINDOW_MODE=windowed STREAM_SECONDS=12 \
  ./scripts/run-moonlight-sunshine-virgl-e2e.sh
```

For the full Moonlight + Weston clipboard/file/resize composite, do not run a
second GameStream test concurrently:

```bash
SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine" \
VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK=\
  "$PWD/scripts/run-moonlight-sunshine-virgl-qsf-wayland-hook.sh" \
VIRGL_QSF_WAYLAND_PROBE_DURATION_MS=60000 \
  ./scripts/run-virgl-qsf-wayland-clipboard-e2e.sh
```

See [`docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md`](docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md),
[`docs/MOONLIGHT_AUDIO_E2E.md`](docs/MOONLIGHT_AUDIO_E2E.md), and
[`docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md`](docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md)
for prerequisites and assertions.

## Resolution behavior

Fullscreen is a Moonlight client-presentation mode. Sunshine also requests
the stream geometry through `Console.SetUIInfo`; a successful reply alone is
not treated as a completed mode switch. The native tests require a new QEMU
scanout and H.264 evidence of the requested `1280x720` mode.

The guest's Weston 12 DRM backend does not automatically reselect a new
preferred virtio-gpu mode after it is running. The QSF/Wayland gate therefore
uses and records an explicit Weston DRM restart after the resize request. It
is a documented guest-desktop fallback, not a claim of automatic hotplug.

## Intentional limits

- No GPU-native encoder, zero-copy end-to-end path, multi-plane
  `ScanoutDMABUF2`, performance target, soak test, or reconnect policy is
  claimed.
- QSF has 1 MiB UTF-8 clipboard and 2 MiB file limits with safe basenames; it
  is a session companion, not a stock Moonlight protocol extension.
- The repository does not provide a production systemd supervisor, NAT
  traversal, Windows-login coverage, or a long-term remote security review.
- The host is headless, but Moonlight itself remains a graphical client and
  needs its normal SDL platform on the client machine.

## Documentation map

- [`docs/VALIDATION.md`](docs/VALIDATION.md) — recorded test matrix and exact evidence.
- [`docs/SUNSHINE_QEMU_INTEGRATION.md`](docs/SUNSHINE_QEMU_INTEGRATION.md) — pinned upstream patch/build contract.
- [`docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md`](docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md) — native video/input/fullscreen gate.
- [`docs/MOONLIGHT_AUDIO_E2E.md`](docs/MOONLIGHT_AUDIO_E2E.md) — decoded client-audio gate.
- [`docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md`](docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md) — real guest desktop clipboard, files and resize.
- [`extensions/qsf_control/README.md`](extensions/qsf_control/README.md) — local and mTLS QSF companion operation.

## License

GPL-3.0-or-later. QEMU and Sunshine source are not vendored; the integration
patches are pinned and replayed against their documented upstream revision.
