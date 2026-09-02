# Validation

Validation date: 2026-09-02 UTC.

## Result

The current full native system-auth acceptance run is:

    vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.qAMtPU/

Its trace records
`.upstream/build-sunshine-qemu/sunshine`, patched Moonlight Qt, and a host GUI
of `none`; the disposable Xvfb exists solely in the client visual lane. It
proves TLS/PAM login, a RAM-only ticket, patched Moonlight's native CSR/mTLS
lease, an explicit `--qsm-system-auth` child command, and a successful native
GameStream media session without PIN pairing.

Sunshine was rebuilt from v2026.830.223700 base commit
`4f39fc116294abf8241bcd30e1b1e23d371e6e7b`. A fresh detached replay applied
patches `0001` through `0008`, including ordered listener transport retirement,
native media lease enforcement, and request-log secret redaction; reverse
replay left no diff. The build succeeded. The separately recorded
`build-sunshine-qemu-no-x11` SHA-256 below is a 2026-09-01 legacy deployment
artifact and must be rebuilt from the `0008` series before it is used as a
native-system-auth release artifact:

    45fae7ee7c56ab0770d7dabbcbf4f9ce3aab66b7f1c097648ca836b260f8708c

The earlier artifact's helper reported:

    SUNSHINE_QEMU_BUILD_OK ... desktop_runtime=none host_audio=none vaapi=off

The direct ELF and complete ldd closure deny gates found no libX11, libXtst,
libXi, libXext, libXrender, libXrandr, libwayland, libpulse, or libasound.
Core libva/libva-drm intentionally resolve from the isolated project prefix
because the pinned static FFmpeg needs them even with VAAPI disabled.

## Environment

| Component | Qualified environment |
| --- | --- |
| Host runtime | Ubuntu 24.04 container, x86-64, no deployment X11/Wayland/PulseAudio/ALSA runtime |
| QEMU | 8.2.2 with Display1 D-Bus and D-Bus audio backend |
| Acceleration | KVM through /dev/kvm |
| Render path | QEMU virtio-vga-gl with rendernode /dev/dri/renderD128 |
| GPU evidence | Alpine reports virgl (NVIDIA GeForce RTX 3080/PCIe/SSE2) |
| Guest desktop | Alpine 3.20.10 cloud image, Weston 12 DRM, seatd/eudev, wl-clipboard |
| Sunshine video | QEMU DMA-BUF -> headless GBM/EGL CPU BGRX readback -> libx264 |
| Sunshine audio | QEMU AudioOutListener -> bounded FIFO -> Opus |
| Moonlight harness | current standalone Qt shell -> patched Moonlight Qt with native system-auth; legacy Embedded/stock Moonlight lanes are historical diagnostics; any Xvfb is disposable client-side only |

## Regression matrix

| Layer | Command/result | Outcome |
| --- | --- | --- |
| DMA-BUF-enabled project build | cmake --build .build-dmabuf; ctest --test-dir .build-dmabuf --output-on-failure | 10/10 passed |
| DMA-BUF-disabled fallback build | cmake --build .build-no-dmabuf; ctest --test-dir .build-no-dmabuf --output-on-failure | 10/10 passed |
| QSF protocol | local token control, production C guest PTY, TLS gateway | included in both CTest matrices; all passed |
| Static checks | bash -n scripts/tests, Python bytecode compile, strict C11 guest agent/watcher compile, diff check | passed |
| Sunshine replay | clean upstream git am 0001..0008, reverse replay, strict headless dependency build, and request-log redaction probe | passed |
| Sunshine listener retirement | real QEMU listener lifecycle after bounded Sunshine termination | passed; zero `UnknownMethod` callbacks to removed Display1 listeners |
| Headless dynamic closure | full ldd deny gate on final artifact | passed |
| Native video/input | fullscreen and windowed Moonlight -> Sunshine -> QEMU -> VirGL | passed |
| Native audio | QEMU guest tone -> Sunshine Opus -> decoded Moonlight PCM | passed |
| Native desktop companion | Moonlight plus QSF/Weston clipboard/files/resize in one VM | passed |
| Qt client CTest | `.build-qt-client`, Qt/QML/controller/system-auth/QSF focused tests | passed |
| Qt/Moonlight composite | patched Moonlight Qt -> TLS/PAM/authd lease -> Sunshine -> KVM/QEMU -> VirGL/Weston + ticket QSF | passed: `run.qAMtPU` |

The CTest matrix includes core state-machine tests, in-process and
cross-process D-Bus/FD tests, CPU H.264 self-test, inline/map message-bus E2E,
real-QEMU relative/absolute input tests, the QSF local protocol, strict
guest-agent UTF-8 PTY tests, and mTLS gateway tests.

## Historical legacy-pairing video and input

The two direct Embedded-Moonlight traces in this section predate the native
system-auth media route. They remain useful for codec/input regression work,
but their private pairing is not current authentication evidence. The current
accepted media route is the `run.qAMtPU` Qt composite below.

The direct fullscreen test is retained at:

    artifacts/validation/moonlight-sunshine-virgl-e2e/run.vCQmck/trace.txt

The direct windowed test is retained at:

    artifacts/validation/moonlight-sunshine-virgl-e2e/run.gNCula/trace.txt

Both use the historical no-X Sunshine artifact and require all of:

- private Moonlight pairing, HTTPS application launch, RTSP and RTP;
- Moonlight FFmpeg H.264 decode and a non-black client screenshot;
- fullscreen 1280x720 at root coordinate (0,0), or a 1280x720 window inside a
  1600x900 root;
- guest virtio_gpu, VirGL renderer containing NVIDIA, and kmscube;
- real guest evdev KEY_A, pointer movement and BTN_LEFT evidence;
- Sunshine SetUIInfo acceptance and a new Display1 scanout;
- nonzero DMA-BUF traffic with zero failures in Sunshine and an independent
  Display1 observer;
- H.264 evidence of the 1280x800 to 1280x720 guest mode transition.

The client Xvfb is not used by Sunshine, QEMU, the Display1 observer, or the
guest. The video implementation is native VirGL capture but is not a
zero-copy/hardware-encode claim: the final encoder is libx264 after CPU
readback.

## Native audio

The final audio test is retained at:

    artifacts/validation/moonlight-sunshine-qemu-audio-e2e/run.gki7t0/

It booted a KVM BIOS PC-speaker tone fixture. The guest emitted AUDIO_TONE_ON;
Sunshine registered AudioOutListener, accepted 48000 Hz, 2-channel, signed
16-bit PCM and started Opus. Moonlight received its first audio packet after
400 ms and SDL wrote decoded evidence:

    format: s16le / 48000 Hz / 2 channels
    bytes: 2670592
    duration: 13.909 seconds
    peak: 0.0 dB
    mean: -3.3 dB
    non_silent: yes
    PCM SHA-256: f6c65b44678259d83d2c465c294ee3569986c96c7bce06fbbe0d5e9a0211e915

This is decoded client-side audio evidence, not merely a guest write count or
Sunshine callback counter. No host sound server or hardware audio device is an
input to this test.

## Historical legacy-pairing Moonlight, QSF and Weston desktop

This retained Embedded-Moonlight composite is a historical compatibility
trace. It used private pairing and independent QSF controls, so it is not
evidence for the current TLS/PAM-to-native-lease route.

The retained composite outer trace is:

    vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.KEqkVc/trace.txt

The nested real-Moonlight hook trace is:

    vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.KEqkVc/moonlight-sunshine-hook.480Wca/trace.txt

The outer trace proves KVM, virtio_gpu, NVIDIA-backed VirGL, actual installed
Alpine package versions (including weston-12.0.4-r0 and
wl-clipboard-2.2.0-r1), a real Weston DRM selection in both directions,
file transfer in both directions, and QEMU SetUIInfo applied.

Independent SHA-256 assertions:

| Direction | SHA-256 |
| --- | --- |
| client QSF text -> guest wl-paste | 7a86b46c203c32ccbc5234b82bd7d05c404e9c4910a36093a3b9c788aecd6548 |
| guest wl-copy -> client QSF get | af2fd4676d16c4ffbcbc946d461ec21f16820dd948fdab1cc84cb715013dcfc6 |
| client upload -> guest | c42a9b7de80bc704a517486501b41e44a589700f13ea09b4775cc7c90de6a94e |
| guest download -> client | 7ad50987bb70f63abecaf196fd72e62e49a886f8136289549f80f95b5ef980cc |

The composite requires live-h264-geometry-order.txt to be exactly:

    1280x800->1280x720

Its final observer counters are DMA-BUF scanouts/updates/failures
13165/13167/0 with zero session errors. The nested Moonlight hook requires
fullscreen non-black decode and raw guest KEY_A + absolute pointer + button
markers. It names the final no-X Sunshine binary.

The failed run.tmazza is a deliberately excluded diagnostic trace from an
earlier package-version parser; it did not pass the runner and is not used as
evidence. The final run uses the corrected parser against Alpine's installed
package database.

## Current Qt desktop shell native system-auth composite

The standalone Qt client qualification uses the production `MoonlightController`
and `QsfClient` classes, the patched Moonlight Qt child, current Sunshine
native-lease enforcement, KVM QEMU, and the Alpine VirGL/Weston guest. The
current full run passed at:

    vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.qAMtPU/trace.txt

It has a nested `qsunshine-qt-moonlight-hook/trace.txt` and an atomic
`qsunshine-qt-e2e-summary.txt`. The summary requires all of:

- TLS 1.3/PAM fixture login, a VM-audience RAM-only `qsa1` ticket, and a
  patched Moonlight child launched with `--qsm-system-auth`; the ticket is
  supplied only through its managed FD rather than argv, environment, or disk;
- the child-owned CSR/mTLS lease and pinned Sunshine certificate, followed by
  HTTPS launch, RTSP/RTP, and H.264 media. The summary's
  `QSUNSHINE_QT_SYSTEM_AUTH_GAMESTREAM_LEASE_OK=1` proves that route;
- disabled legacy pairing: the retained `GET /pair?uniqueid=native-e2e`
  request has no request body, leaves Sunshine pairing state unchanged, and
  receives the exact GameStream protocol response
  `<root status_code="404"/>`. GameStream encodes this semantic 404 in an
  HTTP 200 envelope; transport status alone is intentionally not used as the
  assertion. The summary's `QSUNSHINE_QT_SYSTEM_AUTH_NO_PIN_FALLBACK_OK=1`
  records the complete no-PIN check;
- a non-black `1280x800` windowed client frame, then a controlled reconnect to
  a non-black physical `1600x900@(0,0)` fullscreen frame;
- raw guest evdev `KEY_A`, absolute pointer, and button in the windowed phase,
  followed by fresh `KEY_B`, pointer, and button evidence after fullscreen
  reconnect;
- TLS 1.3 ticket-authenticated Qt QSF clipboard in both directions,
  byte-for-byte upload and download, `1280x720` QSF resize, guest Weston DRM
  restart, QEMU `SetUIInfo`, and a second byte-for-byte download after QSF
  reactivation;
- ordered Display1 H.264 geometry `1280x800->1280x720`, nonzero DMA-BUF
  traffic with zero failures, and zero observer session errors.

The current client screenshots have the asserted dimensions `1280x800` and
`1600x900`. The disposable visual driver forces Moonlight's `software` decoder
only because `xwd` cannot reliably capture an NVIDIA VDPAU presentation
surface; the normal Qt client default is `auto` and omits that Moonlight CLI
option. Xvfb belongs solely to this client-side visual lane: the Sunshine
deployment artifact, QEMU, Display1 observer, and guest do not use a host GUI.

`run.FMrGhL`, `run.jxqPBO`, `run.Jcmdij`, and the Embedded-Moonlight traces
above are retained historical stock/pairing or legacy-mTLS diagnostics. They
must not be cited as proof that the current native system-auth media route
works.

## Scope and evidence handling

All run directories are ignored by Git because tickets, lease material,
historical pairing keys, and QSF tokens are ephemeral capability material. Do
not publish them unchanged.

The test suite does not claim:

- stock GameStream/Moonlight clipboard or file-transfer packets; QSF is a
  separate authenticated companion;
- unsolicited remote clipboard push; guest-originated text is delivered to the
  broker and retrieved by explicit clipboard-get pull/poll;
- automatic Weston hotplug: the fixture records an explicit Weston DRM restart
  after the accepted resize;
- GPU-native encoding, zero-copy capture, multi-plane DMA-BUF2, latency
  targets, reconnect/soak behavior, or production service supervision;
- remote deployment authorization/session binding beyond the current
  per-VM TLS/PAM ticket and native lease contract.
