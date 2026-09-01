# Implementation status

Status date: 2026-09-01. The functional headless native stack is accepted on
this host. Accepted means the documented E2E gates pass; it does not mean
production operations or a zero-copy encoder have been completed.

## Implemented and qualified

| Area | State | Evidence |
| --- | --- | --- |
| QEMU Display1 transport | Implemented | private D-Bus, RegisterListener FD handoff, inline/map callbacks, cursor metadata, input, resize and audio listener tests |
| CPU capture baseline | Implemented | validated pixman layouts, mapped/inline lifetimes, bounded latest-frame mailbox and software H.264 tests |
| DMA-BUF capture | Implemented with CPU readback | single-plane ScanoutDMABUF/UpdateDMABUF imports on GBM/EGL, BGRX readback, bounds/FD/failure counters |
| Sunshine integration | Implemented | pinned upstream patches 0001..0006, clean replay on upstream 4f39fc1, QEMU capture/input/audio source |
| Headless deployment profile | Qualified | complete ldd deny gate rejects X11, Wayland, PulseAudio and ALSA; no host desktop/audio service is used |
| Moonlight video/input | Native KVM/VirGL passed | private pairing, HTTPS, RTSP/RTP, H.264 decode, fullscreen/windowed presentation and raw guest evdev key/mouse evidence |
| Resolution | Native guest passed | SetUIInfo(1280x720), a new QEMU scanout, and ordered H.264 1280x800 -> 1280x720 evidence |
| Guest audio | Native KVM passed | QEMU AudioOutListener -> Sunshine Opus -> non-silent Moonlight-decoded 48 kHz stereo PCM |
| QSF clipboard/files | Native guest desktop passed | token-authenticated virtio-serial bridge, actual Weston wl-copy/wl-paste, bidirectional hashes, constrained upload/download |
| QSF mTLS gateway | Protocol test passed | TLS 1.3 mutual authentication and local-token non-disclosure; production session binding remains deployer-owned |

The QEMU DMA-BUF route is deliberately not called zero-copy: it imports into
headless EGL then synchronously reads CPU BGRX for Sunshine's libx264 software
encoder. The guest's VirGL renderer is native on the NVIDIA render node;
Sunshine does not currently use NVIDIA encoding.

## Native E2E evidence

| Gate | Fresh retained result | Required proof |
| --- | --- | --- |
| Moonlight fullscreen | artifacts/validation/moonlight-sunshine-virgl-e2e/run.vCQmck/trace.txt | 1280x720 at (0,0), non-black decode, KVM/VirGL, guest key/mouse and mode transition |
| Moonlight windowed | artifacts/validation/moonlight-sunshine-virgl-e2e/run.gNCula/trace.txt | 1280x720 client window in 1600x900 root with the same video/input/mode assertions |
| Decoded guest audio | artifacts/validation/moonlight-sunshine-qemu-audio-e2e/run.gki7t0/ | AUDIO_TONE_ON, 13.909 s non-silent 48 kHz stereo decoded client PCM |
| Combined video/input/desktop data | vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.KEqkVc/trace.txt | Moonlight hook, Weston clipboard both directions, files both directions, ordered live resize, actual installed guest packages |
| Combined Moonlight hook | run.KEqkVc/moonlight-sunshine-hook.480Wca/trace.txt | final no-X Sunshine binary, fullscreen H.264, guest KEY_A + absolute pointer + button |

The evidence directories are intentionally ignored because they contain
ephemeral pairing material and/or QSF capability tokens. They are local
verification artifacts, not release assets.

## Current topology

    Moonlight client (client-side SDL/Xvfb only)
      -> Sunshine qemu_dbus, software H.264 / Opus
      -> private QEMU Display1 D-Bus
      -> KVM Q35, virtio-vga-gl, virtio input, virtio serial
      -> Alpine: VirGL + Weston DRM + QSF agent/clipboard bridge

    QSF local or mTLS companion
      -> local 0600 token-protected broker
      -> QEMU virtio serial
      -> guest text state / files / Weston wl-copy and wl-paste

The Xvfb process in the native Moonlight harness belongs to the client only.
The Sunshine process has neither DISPLAY nor WAYLAND_DISPLAY, and the final
resolver closure has no X11/Wayland/PulseAudio/ALSA dependency.

## Deliberate boundaries

- Stock GameStream/Moonlight carries video, audio and input; it does not carry
  interoperable clipboard or file-transfer messages. QSF is a separate
  authenticated companion.
- Guest-to-client clipboard is currently safe broker event plus explicit
  clipboard-get pull/poll, not an unsolicited remote push protocol.
- QSF text is non-NUL UTF-8 up to 1 MiB; files are separate binary payloads
  up to 2 MiB with safe basenames.
- Weston 12 does not automatically reselect the requested virtio-gpu mode
  while live. The gate explicitly restarts Weston DRM after the resize and
  records the resulting scanout; it does not claim automatic hotplug.
- The remote mTLS code is protocol-tested, but end-to-end remote authorization,
  certificate lifecycle, user consent and per-VM session binding remain
  deployment work.

## Not complete for a production service

- GPU-native conversion/encoding, ScanoutDMABUF2, modifier coverage and
  measured latency/throughput targets;
- reconnect/release-all failure matrix, long soak, multi-VM supervision,
  systemd packaging and host-reboot recovery;
- Windows/UEFI/login coverage, microphone return, rich clipboard MIME,
  drag-and-drop, large/resumable files and consent UI;
- a public remote deployment security review, NAT traversal and key rotation.

The lower-level fake-QEMU, CPU/TCG, sanitiser and cross-process tests remain
part of the regression suite; see VALIDATION.md for the final command matrix.
