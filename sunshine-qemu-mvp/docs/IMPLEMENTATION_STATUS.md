# Implementation status

Status date: 2026-09-02. The functional headless native stack, including the
standalone Qt desktop client and patched Moonlight Qt child, is accepted on this
host. Accepted means the documented E2E gates pass; it does not mean production
operations or a zero-copy encoder have been completed.

## Implemented and qualified

| Area | State | Evidence |
| --- | --- | --- |
| QEMU Display1 transport | Implemented | private D-Bus, RegisterListener FD handoff, inline/map callbacks, cursor metadata, input, resize and audio listener tests |
| CPU capture baseline | Implemented | validated pixman layouts, mapped/inline lifetimes, bounded latest-frame mailbox and software H.264 tests |
| DMA-BUF capture | Implemented with CPU readback | single-plane ScanoutDMABUF/UpdateDMABUF imports on GBM/EGL, BGRX readback, bounds/FD/failure counters |
| Sunshine integration | Implemented | pinned upstream patches 0001..0008, clean replay on upstream 4f39fc1, QEMU capture/input/audio source with ordered listener retirement and native lease enforcement |
| Headless deployment profile | Qualified | complete ldd deny gate rejects X11, Wayland, PulseAudio and ALSA; no host desktop/audio service is used |
| Moonlight video/input | Native system-auth KVM/VirGL passed | TLS/PAM ticket -> patched Moonlight CSR/mTLS lease -> pinned HTTPS, RTSP/RTP, H.264 decode, fullscreen/windowed presentation and raw guest evdev key/mouse evidence; no PIN/pairing fallback |
| Resolution | Native guest passed | SetUIInfo(1280x720), a new QEMU scanout, and ordered H.264 1280x800 -> 1280x720 evidence |
| Guest audio | Native KVM passed | QEMU AudioOutListener -> Sunshine Opus -> non-silent Moonlight-decoded 48 kHz stereo PCM |
| QSF clipboard/files | Native guest desktop passed | ticket-authenticated QSF gateway -> host-local token-protected virtio-serial bridge, actual Weston wl-copy/wl-paste, bidirectional hashes, constrained upload/download |
| QSF mTLS gateway | Protocol test passed | TLS 1.3 mutual authentication and local-token non-disclosure; production session binding remains deployer-owned |
| TLS/PAM system-auth + QSF ticket gateway | Protocol and Qt-client E2E passed | TLS 1.3 login, explicit VM audience, in-memory ticket injection, ticket-only QSF gateway/local broker/FakeAgent readiness, route-change revocation, logout teardown, and no QSettings secret persistence |
| Qt desktop shell | Native system-auth KVM/VirGL passed | standalone Qt/QML profile shell launches patched Moonlight Qt with explicit `--qsm-system-auth`; `run.qAMtPU` proves system login, ticket-to-lease media admission, disabled legacy pairing, windowed/fullscreen visible frames, controlled reconnect, guest input, ticket QSF clipboard/files/resize and post-reconnect download |

The QEMU DMA-BUF route is deliberately not called zero-copy: it imports into
headless EGL then synchronously reads CPU BGRX for Sunshine's libx264 software
encoder. The guest's VirGL renderer is native on the NVIDIA render node;
Sunshine does not currently use NVIDIA encoding.

## Native E2E evidence

| Gate | Fresh retained result | Required proof |
| --- | --- | --- |
| Native system-auth Qt/Moonlight composite | vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.qAMtPU/trace.txt | TLS/PAM -> RAM-only ticket -> patched Moonlight native mTLS lease; explicit `--qsm-system-auth`; exact disabled legacy-pair endpoint result; video/input, ticket QSF clipboard/files, guest scanout handoff, KVM/VirGL |
| Moonlight fullscreen | artifacts/validation/moonlight-sunshine-virgl-e2e/run.vCQmck/trace.txt | 1280x720 at (0,0), non-black decode, KVM/VirGL, guest key/mouse and mode transition |
| Moonlight windowed | artifacts/validation/moonlight-sunshine-virgl-e2e/run.gNCula/trace.txt | 1280x720 client window in 1600x900 root with the same video/input/mode assertions |
| Decoded guest audio | artifacts/validation/moonlight-sunshine-qemu-audio-e2e/run.gki7t0/ | AUDIO_TONE_ON, 13.909 s non-silent 48 kHz stereo decoded client PCM |
| Historical embedded composite | vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.KEqkVc/trace.txt | legacy pairing diagnostic: Moonlight hook, Weston clipboard both directions, files, ordered live resize, actual installed guest packages |
| Historical embedded Moonlight hook | run.KEqkVc/moonlight-sunshine-hook.480Wca/trace.txt | legacy-pairing no-X Sunshine binary, fullscreen H.264, guest KEY_A + absolute pointer + button |
| Historical Qt composite, pass 1 | vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.FMrGhL/trace.txt | stock Moonlight Qt pair/list; 1280x800 windowed + 1600x900 physical fullscreen; KEY_A then post-reconnect KEY_B; mTLS QSF clipboard/files/resize |
| Historical Qt composite, pass 2 | vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.jxqPBO/trace.txt | independent repeat of the legacy complete gate; nested `qsunshine-qt-moonlight-hook/trace.txt` records non-black frames and input barriers |

The evidence directories are intentionally ignored because they contain
ephemeral tickets, leases, legacy pairing material, and/or QSF capability
tokens. They are local verification artifacts, not release assets.

## Current topology

    Qt desktop shell -> patched Moonlight Qt + one-shot ticket FD (client side only)
      -> authd CSR/mTLS lease -> Sunshine qemu_dbus, software H.264 / Opus
      -> private QEMU Display1 D-Bus
      -> KVM Q35, virtio-vga-gl, virtio input, virtio serial
      -> Alpine: VirGL + Weston DRM + QSF agent/clipboard bridge

    Qt TLS/PAM login -> per-VM system-auth gateway -> short-lived VM ticket
      -> ticket-only QSF TLS gateway -> local 0600 token-protected broker

    QSF local or legacy mTLS companion
      -> local 0600 token-protected broker
      -> QEMU virtio serial
      -> guest text state / files / Weston wl-copy and wl-paste

The disposable Xvfb process in native harnesses, including the Qt visual gate,
belongs to the client only. The Sunshine process has neither DISPLAY nor
WAYLAND_DISPLAY, and the final resolver closure has no
X11/Wayland/PulseAudio/ALSA dependency.

## Deliberate boundaries

- GameStream carries video, audio and input; it does not carry
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
- TLS/PAM system login is the admission authority for both current QSF and
  GameStream. The patched Moonlight child exchanges the RAM-only ticket for a
  VM-bound, ephemeral mTLS leaf; native Sunshine does not admit a legacy
  paired certificate or PIN route. `run.qAMtPU` is the password-only
  GameStream-media E2E claim; the retained pairing traces above are explicitly
  historical compatibility diagnostics.
- The current Qt shell trace proves windowed and physical fullscreen visible
  frames, controlled reconnect, native ticket QSF, and the KVM/VirGL/Weston
  guest in one run. Its visual gate selects Moonlight's software decoder only
  because `xwd` cannot read an NVIDIA VDPAU overlay on the disposable Xvfb;
  the normal client default remains `auto`.

## Not complete for a production service

- GPU-native conversion/encoding, ScanoutDMABUF2, modifier coverage and
  measured latency/throughput targets;
- reconnect/release-all failure matrix, long soak, audited multi-VM
  supervision and host-reboot recovery.  The Proxmox package provides
  conservative per-VM systemd templates, but deliberately does not create a
  QEMU endpoint or mutate VM configuration;
- Windows/UEFI/login coverage, microphone return, rich clipboard MIME,
  drag-and-drop, large/resumable files and consent UI;
- a public remote deployment security review, NAT traversal and key rotation.

The lower-level fake-QEMU, CPU/TCG, sanitiser and cross-process tests remain
part of the regression suite; see VALIDATION.md for the final command matrix.
