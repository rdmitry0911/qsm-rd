# Validation

Validation date: 2026-09-01 UTC.

## Result

The final acceptance artifact is
.upstream/build-sunshine-qemu-no-x11/sunshine with SHA-256:

    80d22a83a552f65cbbfa594b2a4ce2a02180ba13571a43558797e8ec83e9a920

It was built from Sunshine v2026.830.223700 base commit
4f39fc116294abf8241bcd30e1b1e23d371e6e7b. A clean detached replay applied
patches 0001 through 0006 and produced replay commit 5c179ad. The build helper
reported:

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
| Moonlight harness | legacy Moonlight Embedded SDL/FFmpeg plus standalone Qt shell -> clean stock Moonlight Qt; any Xvfb is disposable client-side only |

## Regression matrix

| Layer | Command/result | Outcome |
| --- | --- | --- |
| DMA-BUF-enabled project build | cmake --build .build-dmabuf; ctest --test-dir .build-dmabuf --output-on-failure | 10/10 passed |
| DMA-BUF-disabled fallback build | cmake --build .build-no-dmabuf; ctest --test-dir .build-no-dmabuf --output-on-failure | 10/10 passed |
| QSF protocol | local token control, production C guest PTY, TLS gateway | included in both CTest matrices; all passed |
| Static checks | bash -n scripts/tests, Python bytecode compile, strict C11 guest agent/watcher compile, diff check | passed |
| Sunshine replay | clean upstream git am 0001..0006 plus strict headless dependency build | passed |
| Headless dynamic closure | full ldd deny gate on final artifact | passed |
| Native video/input | fullscreen and windowed Moonlight -> Sunshine -> QEMU -> VirGL | passed |
| Native audio | QEMU guest tone -> Sunshine Opus -> decoded Moonlight PCM | passed |
| Native desktop companion | Moonlight plus QSF/Weston clipboard/files/resize in one VM | passed |
| Qt client CTest | `.build-qt-client`, Qt/QML/controller/QSF mTLS tests | 11/11 passed |
| Qt/Moonlight composite | clean stock Moonlight Qt -> Sunshine -> KVM/QEMU -> VirGL/Weston + Qt QSF, repeated in two fresh VMs | passed twice |

The CTest matrix includes core state-machine tests, in-process and
cross-process D-Bus/FD tests, CPU H.264 self-test, inline/map message-bus E2E,
real-QEMU relative/absolute input tests, the QSF local protocol, strict
guest-agent UTF-8 PTY tests, and mTLS gateway tests.

## Native video and input

The direct fullscreen test is retained at:

    artifacts/validation/moonlight-sunshine-virgl-e2e/run.vCQmck/trace.txt

The direct windowed test is retained at:

    artifacts/validation/moonlight-sunshine-virgl-e2e/run.gNCula/trace.txt

Both use the final no-X Sunshine artifact and require all of:

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

## Combined Moonlight, QSF and Weston desktop

The final composite outer trace is:

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

## Qt desktop shell composite, repeated twice

The standalone Qt client qualification uses the production `MoonlightController`
and `QsfClient` classes, a clean unmodified Moonlight Qt child, the final
no-X Sunshine binary, KVM QEMU, and the same Alpine VirGL/Weston guest. It
passed twice in independent fresh outer runs:

    vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.FMrGhL/trace.txt
    vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.jxqPBO/trace.txt

Each has a nested `qsunshine-qt-moonlight-hook/trace.txt` and an atomic
`qsunshine-qt-e2e-summary.txt`. The summary requires all of:

- clean stock-Moonlight Qt pair flow followed by independent `moonlight list`
  confirmation of `Desktop`;
- a non-black `1280x800` windowed client frame, then a controlled reconnect to
  a non-black physical `1600x900@(0,0)` fullscreen frame;
- raw guest evdev `KEY_A`, absolute pointer, and button in the windowed phase,
  followed by fresh `KEY_B`, pointer, and button evidence after fullscreen
  reconnect;
- TLS 1.3 mTLS Qt QSF clipboard in both directions, byte-for-byte upload and
  download, `1280x720` QSF resize, guest Weston DRM restart, QEMU `SetUIInfo`,
  and a second byte-for-byte download after QSF reactivation;
- ordered Display1 H.264 geometry `1280x800->1280x720`, nonzero DMA-BUF
  traffic with zero failures, and zero observer session errors.

The two client screenshots have the asserted dimensions `1280x800` and
`1600x900`. The disposable visual driver forces Moonlight's `software` decoder
only because `xwd` cannot reliably capture an NVIDIA VDPAU presentation
surface; the normal Qt client default is `auto` and omits that Moonlight CLI
option. Xvfb belongs solely to this client-side visual lane: the Sunshine
deployment artifact, QEMU, Display1 observer, and guest do not use a host GUI.

## Scope and evidence handling

All run directories are ignored by Git because pairing keys and QSF tokens are
ephemeral capability material. Do not publish them unchanged.

The test suite does not claim:

- stock GameStream/Moonlight clipboard or file-transfer packets; QSF is a
  separate authenticated companion;
- unsolicited remote clipboard push; guest-originated text is delivered to the
  broker and retrieved by explicit clipboard-get pull/poll;
- automatic Weston hotplug: the fixture records an explicit Weston DRM restart
  after the accepted resize;
- GPU-native encoding, zero-copy capture, multi-plane DMA-BUF2, latency
  targets, reconnect/soak behavior, or production service supervision;
- remote deployment authorization/session binding beyond the protocol-tested
  TLS 1.3 mTLS gateway.
