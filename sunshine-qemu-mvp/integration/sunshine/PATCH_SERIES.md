# Sunshine patch series

The CPU display and keyboard/mouse patches are exported and qualified against
real QEMU. The remaining series is intentionally split so each next layer
remains reviewable without a GPU.

## 0. `platform/linux: add QEMU Display1 CPU capture` — implemented

- opt-in `SUNSHINE_ENABLE_QEMU_DBUS` source selection;
- private message-bus address plus `RegisterListener` peer-FD handoff;
- inline CPU `Scanout`/`Update`, pixman normalization and safe stale-damage
  handling;
- software encoder `img_t` path and a real-QEMU/TCG Sunshine encoder-probe
  gate.

See `PINNED_UPSTREAM.md` and `docs/SUNSHINE_QEMU_INTEGRATION.md` for the exact
upstream base, patch and replay command.

## 1. `platform/linux: route QEMU Display1 input` — implemented

- QEMU `Keyboard`/`Mouse` capability probe and Windows-VK to QEMU-qnum mapping;
- relative Moonlight deltas bridge to a bounded, scanout-scaled virtual cursor
  through `SetAbsPosition` when QEMU reports `Mouse.IsAbsolute=true`; native
  `RelMotion` is retained only for relative QEMU devices;
- geometry/reset handling plus stable final input counters distinguish
  client-no-motion from a rejected QEMU method;
- `input::reset()` release-all reaches QEMU through the existing wrappers;
- no dependency on host desktop focus and no host virtual-input fallback;
- real-QEMU guest acknowledgement of both `A` make and break bytes.

## 2. `platform/linux: add QEMU D-Bus audio source` — implemented and decoded-client E2E passed

- `AudioOutListener` registration;
- PCM conversion;
- bounded FIFO;
- `platf::audio_control_t`/`mic_t` adapter;
- per-VM isolation.

The QEMU protocol is qualified against a headless KVM BIOS tone fixture and a
real GameStream client. The full gate uses QEMU `-audiodev dbus`, registers
`AudioOutListener`, encodes the accepted 48 kHz stereo guest PCM as Opus, and
requires Moonlight Embedded's SDL disk output to contain finite non-silent
decoded S16LE PCM. It does not use a host sound server. The final strict
no-X11/no-Pulse replay is
`artifacts/validation/moonlight-sunshine-qemu-audio-e2e/run.gki7t0/` and
records 13.909 seconds of decoded PCM (`max=0.0 dB`, `mean=-3.3 dB`). Exact
invocation and assertions are in `docs/MOONLIGHT_AUDIO_E2E.md`.

## 3. `config: expose qemu_dbus diagnostics`

- capture selector documentation and structured counters;
- structured counters;
- clear software fallback reporting;
- configuration validation.

## 4. `platform/linux: add Unix shared-map and DMA-BUF policy`

- Unix `ScanoutMap`/`UpdateMap`;
- explicit Map capability advertisement only after mmap lifetime is safe;
- keep the inline CPU fallback as the required baseline.

## 5. `platform/linux: add QEMU DMA-BUF EGL readback` — implemented

- opt-in `SUNSHINE_ENABLE_QEMU_DBUS_DMABUF` capability, detected only when
  `gbm` and `libdrm` development packages are available;
- QEMU Display1 single-plane `ScanoutDMABUF`/`UpdateDMABUF` with a retained,
  CLOEXEC duplicate of the received D-Bus FD-list entry;
- headless GBM/EGL `EGL_EXT_image_dma_buf_import` + FBO `glReadPixels` into
  tight BGRX CPU frames, including QEMU's `y0_top` orientation;
- prefers `EGL_KHR_surfaceless_context` for FBO-only readback, retaining a
  one-pixel pbuffer only as a compatibility fallback; this supports nested
  VirGL drivers that advertise window-only EGL configurations;
- bounded 16,384-pixel / 512-MiB validation and four XRGB/ARGB/XBGR/ABGR
  formats; modifiers are required only when QEMU supplies a nonzero modifier;
- full-frame reread on `UpdateDMABUF` for correctness. This is not a
  zero-copy encoder path and does not implement multi-plane `ScanoutDMABUF2`.

The build and linker workaround for the pinned upstream FFmpeg/libva ABI is
documented in `ISOLATED_LIBVA.md`.

## 6. `0005-cmake-avoid-X11-FFmpeg-glue-for-software-builds.patch` — implemented

- bundled static FFmpeg still requires core `libva`/`libva-drm`, but
  `va-x11`/`X11` are linked only when Sunshine VAAPI is enabled;
- the reproducible QEMU builder disables libvirtualhid's optional XTest host
  fallback and rejects direct X11/XTest/Xi/Xext/Xrender/Xrandr/Wayland ELF
  dependencies after linking.

## 7. `0006-platform-linux-add-QEMU-guest-audio-only-build.patch` — implemented

- opt-in `SUNSHINE_ENABLE_QEMU_DBUS_AUDIO_ONLY` replaces the PulseAudio audio
  implementation with the existing QEMU `AudioOutListener` controller;
- non-QEMU capture intentionally has no audio in this deployment;
- the reproducible builder rejects PulseAudio, ALSA, X11, and Wayland across
  the complete `ldd` dependency closure.

## 8. `0007-platform-linux-close-QEMU-listener-transport-before-teardown.patch` — implemented

- retain a CLOEXEC duplicate of each private Display1 and AudioOutListener
  peer socket;
- force `shutdown(SHUT_RDWR)` before quitting the peer loop or unregistering
  the exported D-Bus object;
- prevent QEMU from retaining a live listener connection after its object was
  removed, which otherwise produces repeated `UnknownMethod` display updates
  during capture retirement.

## 9. `0008-nvhttp-require-system-auth-media-leases.patch` — transport-only PVE profile

- `QSUNSHINE_TRANSPORT_ONLY=ON` replaces Sunshine's application launcher with
  one immutable `QEMU Console` entry (`appid=1255037302`); it never reads an
  app manifest, starts a command, runs a prep command, or owns a child process;
- retains only the authenticated GameStream endpoints required by Moonlight:
  `/serverinfo`, `/applist`, `/appasset`, `/launch`, `/resume`, and `/cancel`.
  `launch`, supplied `resume`, and `appasset` IDs are parsed strictly and an
  invalid ID is rejected before RTSP state is created;
- makes the media plane mTLS-only: an exact VM audience URI SAN, constrained
  CA:false/clientAuth leaf profile, certificate validity, and a conservative
  login subject are required; an encrypted RTSP `rikey` possession proof is
  required before native session admission. The legacy plaintext HTTP listener
  is not compiled into this profile, so no unauthenticated GameStream bootstrap
  port exists;
- loads only absolute, root-owned, non-group/world-writable trust paths using
  `O_NOFOLLOW`; a relative certificate or media-CA setting fails before any
  legacy appdata migration can create a second configuration plane;
- removes the host control plane from the transport target: Web UI, PIN
  pairing, paired-certificate store, discovery, UPnP, tray, display device
  manager, persistent state, `apps.json`, Boost.Process, and host command/
  process-group helpers. The package consequently carries no X11, Wayland,
  PulseAudio, ALSA, curl, miniupnpc, or Qt runtime dependency;
- locks the remaining video/audio capture graph to QEMU Display1 with DMA-BUF
  readback and guest-only audio. Conflicting CMake cache inputs cannot
  re-enable a host-desktop capture or display-control backend; encoder choice
  remains a root-owned VM policy and is accepted only after runtime capability
  probing;
- preserves the normal Sunshine build behind the CMake switch. In the PVE
  package Proxmox owns identity, ACLs, VM selection, and VM lifecycle; Sunshine
  is solely the VM-specific media transport.

This outer patch depends on the nested
`simple-web-server/0001-server-http-expose-request-tls-native-handle.patch`.
Apply that patch inside Sunshine's
`third-party/Simple-Web-Server` worktree before compiling the replay.
