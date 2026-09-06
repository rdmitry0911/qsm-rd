# q-sunshine — headless QEMU/VirGL desktop source for Sunshine

This repository implements and qualifies a headless QEMU Display1 capture
backend for a pinned Sunshine build. The accepted native route is:

```text
Qt desktop shell -> patched Moonlight Qt (one-shot system-auth ticket)
  -> TLS/PAM authd -> ephemeral VM mTLS lease
  -> Sunshine GameStream (pinned HTTPS, encrypted RTSP/RTP; no pairing)
  -> QEMU Display1 D-Bus capture/input/audio
  -> KVM Q35 + virtio-vga-gl
  -> Alpine guest + VirGL on the host NVIDIA render node
```

The deployment-side Sunshine binary is deliberately free of X11, Wayland,
PulseAudio, and ALSA dependencies. Any short-lived Xvfb instance used by an
Embedded-Moonlight or Qt/Moonlight visual test is a **client harness only**;
it is never part of the q-sunshine host runtime.

An optional [`Qt desktop client`](docs/QT_DESKTOP_CLIENT.md) now provides a
portable Remmina/RDP-style client-side shell. It launches patched Moonlight Qt
as a separate media process for GameStream video/audio/input and owns saved
profiles, TLS/PAM sign-in, the system-authenticated QSF companion, clipboard,
constrained files, and safe presentation / guest-scanout handoffs. The native
media lease, created from the in-memory ticket by the child, is VM-bound and
has no PIN/pairing fallback. The retained client-certificate mTLS QSF gateway
is a separate legacy deployment mode, not a requirement for the desktop login.
The Qt client is off by default; enable it with
`-DQMDP_BUILD_QT_CLIENT=ON`.
It likewise adds no X11/Wayland dependency to the Sunshine host.

## QSM Direct and noVNC performance profile

The browser-native `qsm-pve-direct` package replaces the rendering backend of
the existing Proxmox VM **Console** card only for a VM with its private QEMU
Display1 configuration; other VM and LXC consoles remain stock noVNC. This
table compares the two transport models rather than inventing a universal
latency number: compare measurements only with the same guest resolution,
browser, workload, and network path.

| Performance property | QSM Direct | Stock PVE noVNC |
| --- | --- | --- |
| Browser video path | WebRTC H.264/Opus; browser H.264 hardware decode may be used | RFB rectangles over reliable WebSocket, decoded and painted by noVNC/Canvas |
| Host work for changing pixels | One low-latency H.264 encode per VM worker, shared by viewers; `Automatic` prefers a verified hardware encoder | QEMU VNC/RFB rectangle updates; no browser video-decoder path |
| Pointer during congestion | Latest pointer sample uses an unordered, non-retransmitted WebRTC channel, discarding stale motion | Pointer events use the ordered reliable WebSocket stream and wait behind older data |
| Video during packet loss | Media recovers at an H.264 intra frame while control remains separate | TCP/WebSocket head-of-line recovery delays later framebuffer updates |
| Resize | Requests a new even QEMU Display1 mode and sends an H.264 configuration/IDR boundary | Rescales or redraws the current RFB framebuffer; it does not request a Display1 mode |
| Best fit | Interactive desktop, animation, video, or high-latency/lossy links | Firmware text console, installation, recovery, zero-extra-transport baseline |

The QSM stress suite records first-video time, Chrome jitter-buffer delay,
key-to-pixel and hover latency, plus continuous-drag gaps. noVNC has no
equivalent PVE telemetry, so a hardware-specific comparison must collect the
same fixture and geometry on both paths. The packaged operational details are
in [`packaging/debian/README.qsm-pve-direct`](packaging/debian/README.qsm-pve-direct).

For artifact installation, per-VM system-auth setup, guest/QEMU wiring,
macOS Tahoe first launch, verification, removal, and signing caveats, see
[`docs/RELEASE_NOTES.md`](docs/RELEASE_NOTES.md). It describes a release
procedure but does not claim that a package has already been published.

## Accepted functionality

The current native acceptance suite has passed on QEMU 8.2.2, KVM, and an
NVIDIA RTX 3080 render node:

| Capability | What is exercised end to end |
| --- | --- |
| Video | Qt-controlled patched Moonlight native lease, pinned HTTPS launch, encrypted RTSP/RTP, H.264 encode/decode, QEMU `ScanoutDMABUF`/`UpdateDMABUF`, KVM, `virtio_gpu`, VirGL and non-black client images |
| Fullscreen and windowed client modes | Qt-controlled Moonlight `1280x800` windowed presentation and a physical `1600x900` fullscreen presentation at `(0,0)` |
| Resolution | client-selected QSF `1280x720` and `1600x900` profiles: visible Moonlight is quiesced, the prior Sunshine capture is allowed to retire, a profile-only authenticated QSF lease applies QEMU `Console.SetUIInfo` and waits for the exact Weston/VirGL acknowledgement, then a fresh Moonlight stream starts |
| Keyboard and mouse | Real patched-Moonlight keys, absolute motion and clicks observed as raw guest evdev `KEY_A` in windowed mode and fresh `KEY_B` + pointer/button evidence after fullscreen reconnect |
| Guest audio | QEMU D-Bus `AudioOutListener` -> Sunshine Opus -> Moonlight decoded non-silent 48 kHz stereo PCM, without a host sound server |
| Clipboard | QSF companion text reaches real guest Weston `wl-paste`; a guest `wl-copy` reaches the client side, with independent hashes |
| Files | QSF upload and download through QEMU virtio-serial, with independent guest/client SHA-256 assertions |

The current acceptance evidence is the native system-auth composite in
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.qAMtPU/`. It exercised the
patched Moonlight Qt child, TLS/PAM login, RAM-only `qsa1` ticket, native
CSR/mTLS lease, no-PIN GameStream launch, real Weston DRM clipboard bridge,
files, resize, and controlled fullscreen reconnect together. Its atomic
summary contains `QSUNSHINE_QT_SYSTEM_AUTH_GAMESTREAM_LEASE_OK=1` and
`QSUNSHINE_QT_SYSTEM_AUTH_NO_PIN_FALLBACK_OK=1` alongside the video, input,
clipboard, file, and guest-scanout assertions.

`run.FMrGhL`, `run.jxqPBO`, and `run.Jcmdij` remain ignored local historical
stock-Moonlight/legacy-pairing traces. They are useful regression diagnostics,
but are not evidence for the current system-auth media route.

The current safe profile-handoff trace is
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.qAMtPU/`. Its outer hook emits
`QSUNSHINE_QT_MOONLIGHT_VIRGL_QSF_HOOK_OK`; its nested trace records the
TLS/PAM-to-lease admission, two profile-only ticket-authenticated QSF leases,
exact guest acknowledgements for `1280x720` and `1600x900`, and new patched
Moonlight processes after each acknowledgement. Normal QSF is reactivated
only after replacement video is verified.

## Runtime topology

```text
                    client side only                         headless host
Qt shell -> patched Moonlight Qt ── mTLS GameStream ──> Sunshine qemu_dbus ─ private D-Bus -> QEMU
       │                  │                                 │                                │
       ├── TLS 1.3/PAM -> system-auth gateway -> ticket/lease ├─ Audio/Scanout/Input         ├─ KVM/Q35
       │                                                    │                                ├─ virtio-vga-gl
       └── TLS 1.3 QSF + ticket ───────────────────────> QSF ticket gateway ───────────────┴─ virtio-serial
                                                                                               │
                                                                                   Alpine + VirGL + Weston DRM
                                                                                   wl-copy / wl-paste + QSF agent
```

The QSF route is intentionally a separate authenticated companion protocol.
GameStream does not standardize bidirectional clipboard or file-transfer
messages, so those functions are not mislabeled as GameStream features. QSF
and media are separate authenticated channels. The system ticket is enforced
by QSF and feeds the child-owned mTLS lease sequence for GameStream; Sunshine
disables its pairing/PIN endpoints in this mode. See
[`docs/SYSTEM_AUTH.md`](docs/SYSTEM_AUTH.md) and
[`docs/GAMESTREAM_LEASE_AUTH.md`](docs/GAMESTREAM_LEASE_AUTH.md).

## Build the deployment artifact

The reproducible helper applies Sunshine patches `0001` through `0008` plus
the pinned nested Simple-Web-Server accessor patch to the upstream revision,
uses an isolated `libva 2.21` prefix only for the bundled FFmpeg ABI, and
rejects forbidden libraries across the full `ldd` closure.

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

## Re-run the gates

Build the project observer with DMA-BUF support. The two direct
Embedded-Moonlight commands below are retained **historical legacy-pairing**
codec/input regressions; they are not the accepted native system-auth media
gate:

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

For the accepted Qt/Moonlight + Weston clipboard/file/resize composite, do not
run a second GameStream test concurrently. The QEMU runner must be the user
that owns the private D-Bus session and has read/write access to both
`/dev/kvm` and the selected render node; using root solely to bypass KVM
permissions breaks that user-bus contract.

```bash
SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu/sunshine" \
QSUNSHINE_MOONLIGHT_QT_BINARY="$PWD/.upstream/moonlight-qt-clean/app/moonlight" \
VIRGL_QEMU_RUN_AS="$(id -un)" \
VIRGL_QSF_WAYLAND_QT_QSF_OWNER=qt \
VIRGL_QSF_WAYLAND_PROBE_DURATION_MS=390000 \
VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK=\
  "$PWD/scripts/run-qsunshine-qt-moonlight-virgl-qsf-wayland-hook.sh" \
  ./scripts/run-virgl-qsf-wayland-clipboard-e2e.sh
```

See [`docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md`](docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md),
[`docs/MOONLIGHT_AUDIO_E2E.md`](docs/MOONLIGHT_AUDIO_E2E.md), and
[`docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md`](docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md)
for prerequisites and assertions.

## Resolution behavior

The size selected by the user is a request for the **guest** VirGL scanout;
`fullscreen` is separately a Moonlight **client-presentation** mode. They may
have the same dimensions, but neither one is inferred from the other or from a
host GPU device node. The broker combines only three distinct inputs: verified
client decoder capability, a tested Sunshine host encoder envelope, and the
guest display envelope/current scanout capability.

Changing a selected guest size is deliberately not an in-place alteration of a
live Moonlight stream. The Qt coordinator follows this sequence:

1. It ends the normal QSF session, cancelling clipboard, file, and diagnostic
   resize operations, then stops the visible Moonlight process.
2. It waits for that process and a bounded Sunshine capture-retirement interval
   before opening a short **profile-only** authenticated QSF TLS 1.3 lease.
3. That lease can issue only `connection_optimize`. The broker resolves the
   three capability envelopes, sends QEMU `Console.SetUIInfo`, commits the
   profile, and waits for the generation-bound Weston/VirGL current-mode
   acknowledgement.
4. The temporary lease closes before a new patched Moonlight process is launched
   with the resolved stream profile. Normal QSF is activated again only after
   the replacement video is visibly verified.

After `connection_optimize` has crossed the encrypted client socket, **Cancel**
is deliberately a request rather than an immediate reconnect permission: the
client remains locked and Moonlight stays stopped until the authoritative
broker reply arrives. If that response path fails, the profile-only lease is
closed and the client holds a conservative 90-second remote-settlement guard
before it permits a manual reconnect. Closing a local TLS socket alone cannot
cancel a gateway worker that may still be applying `SetUIInfo` and waiting for
the guest acknowledgement. Before its encrypted profile request is written,
the client synchronously records a durable, per-Moonlight-profile recovery
marker. A quit or crash therefore keeps that profile's Moonlight and normal
QSF activation controls locked for at most 180 seconds after restart; a
terminal reply or proven settlement clears it.
The recovery/handoff lock also makes desktop-profile selection and saving,
Moonlight decoder/binary changes, and pairing immutable through the terminal
state. A direct in-process call therefore cannot select a clean profile to
bypass a marker or alter the launch inputs after the guest profile was chosen.

Thus an old Sunshine capture cannot replay its old display information while a
new guest mode is being committed, and clipboard/file traffic cannot race the
display transaction. `Console.SetUIInfo` remains only one step: the e2e gate
also requires the guest compositor acknowledgement and independent H.264
geometry evidence. See
[`docs/QSF_STREAM_NEGOTIATION.md`](docs/QSF_STREAM_NEGOTIATION.md).

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
- The Proxmox package ships conservative per-VM systemd templates, but it
  does not configure QEMU/VMs or credentials automatically and does not claim
  a cluster-wide supervisor, host-reboot recovery validation, NAT traversal,
  Windows-login coverage, or a long-term remote security review.
- The host is headless, but Moonlight itself remains a graphical client and
  needs its normal SDL platform on the client machine.
- QSF is not cryptographically bound to an already-admitted GameStream media
  session. The Qt shell requires manual activation after video is visible and
  cancels QSF on Moonlight teardown; the temporary profile-only lease never
  permits clipboard or file traffic. The native media route itself uses the
  ticket-to-lease identity rather than a legacy paired certificate. Use only a
  trusted, per-VM gateway.

## Documentation map

- [`docs/VALIDATION.md`](docs/VALIDATION.md) — recorded test matrix and exact evidence.
- [`docs/RELEASE_NOTES.md`](docs/RELEASE_NOTES.md) — Proxmox/macOS installation, launch, verification and removal order.
- [`docs/SUNSHINE_QEMU_INTEGRATION.md`](docs/SUNSHINE_QEMU_INTEGRATION.md) — pinned upstream patch/build contract.
- [`docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md`](docs/MOONLIGHT_SUNSHINE_VIRGL_E2E.md) — historical Embedded-Moonlight pairing video/input gate.
- [`docs/MOONLIGHT_AUDIO_E2E.md`](docs/MOONLIGHT_AUDIO_E2E.md) — decoded client-audio gate.
- [`docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md`](docs/VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md) — real guest desktop clipboard, files and resize.
- [`docs/QSF_STREAM_NEGOTIATION.md`](docs/QSF_STREAM_NEGOTIATION.md) — client/host/guest capability contract and scanout acknowledgement.
- [`extensions/qsf_control/README.md`](extensions/qsf_control/README.md) — local, ticket, and legacy-mTLS QSF companion operation.
- [`docs/SYSTEM_AUTH.md`](docs/SYSTEM_AUTH.md) — TLS/PAM login, VM-audience tickets, and the current GameStream boundary.
- [`docs/QT_DESKTOP_CLIENT.md`](docs/QT_DESKTOP_CLIENT.md) — portable Qt client shell, build, security and its QSF E2E gate.

## License

GPL-3.0-or-later. QEMU and Sunshine source are not vendored; the integration
patches are pinned and replayed against their documented upstream revision.
