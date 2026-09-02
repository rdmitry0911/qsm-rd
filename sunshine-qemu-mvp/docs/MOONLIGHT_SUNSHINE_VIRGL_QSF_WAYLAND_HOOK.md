# Moonlight/Sunshine post-agent hook for the VirGL QSF Wayland gate

> **Historical compatibility hook.** This Embedded-Moonlight hook uses private
> pairing and the legacy outer QSF owner. Its `run.KEqkVc` evidence is retained
> for regression diagnosis only. The current accepted composite is the
> patched-Moonlight, TLS/PAM-to-native-lease Qt route in
> [`QT_DESKTOP_CLIENT.md`](QT_DESKTOP_CLIENT.md#native-qtmoonlightvirgl-e2e-gate),
> with final evidence `run.qAMtPU`.

`scripts/run-moonlight-sunshine-virgl-qsf-wayland-hook.sh` is an executable
post-agent hook for the already-running native
`run-virgl-qsf-wayland-clipboard-e2e.sh` guest.  It adds a real Moonlight
video/input session to that live QEMU instance; it does not start, stop, or
reconfigure QEMU, Weston, the QSF agent, or the outer runner's private D-Bus.

```text
Moonlight Embedded SDL (disposable client-only Xvfb)
  -> private pair / HTTPS / RTSP / RTP
  -> headless patched Sunshine, libx264 software encoding
  -> live QEMU Display1 on QSF_WAYLAND_DBUS_ADDRESS
  -> existing KVM VirGL + Weston DRM guest
```

The sole X11 server is a short-lived client drawable for Moonlight.  Sunshine
is launched with no `DISPLAY` or `WAYLAND_DISPLAY`; QEMU and the guest remain
headless on the host. The verified Sunshine deployment artifact is
`$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine`; its `ldd` closure has no
X11, Wayland, PulseAudio, or ALSA dependency. Only Moonlight's disposable
client Xvfb uses X11 libraries.

## Use with the outer runner

Build the no-X Sunshine binary by replaying the exact ordered patch series:

1. `0001-platform-linux-add-QEMU-Display1-CPU-capture.patch`
2. `0002-platform-linux-route-QEMU-Display1-input.patch`
3. `0003-platform-linux-add-QEMU-Display1-guest-audio-source.patch`
4. `0004-platform-linux-qemu-dmabuf-egl-readback.patch`
5. `0005-cmake-avoid-X11-FFmpeg-glue-for-software-builds.patch`
6. `0006-platform-linux-add-QEMU-guest-audio-only-build.patch`

Then pass that exact artifact to the outer gate:

```bash
SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine" \
MOONLIGHT_BINARY="$PWD/.upstream/build-moonlight-embedded/moonlight" \
VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK=\
"$PWD/scripts/run-moonlight-sunshine-virgl-qsf-wayland-hook.sh" \
VIRGL_QSF_WAYLAND_PROBE_DURATION_MS=60000 \
./scripts/run-virgl-qsf-wayland-clipboard-e2e.sh
```

The outer runner invokes the hook after
`QSF_VIRGL_WAYLAND_GUEST_VIRGL_READY` and exports:

- `QSF_WAYLAND_OUTPUT_DIR` — parent evidence directory;
- `QSF_WAYLAND_QEMU_PID` — live QEMU PID, checked but never stopped;
- `QSF_WAYLAND_DBUS_ADDRESS` and `QSF_WAYLAND_DBUS_DESTINATION` — private
  Display1 endpoint used by Sunshine;
- QSF agent/control/token paths for callers that need them.  This hook does
  not use or mutate those control channels.

The hook writes its own fresh `moonlight-sunshine-hook.*` evidence directory
under the outer result directory.  `STREAM_SECONDS` defaults to 15 and must
remain shorter than the outer live Display1 probe window; use a larger outer
`VIRGL_QSF_WAYLAND_PROBE_DURATION_MS` if increasing it.

## Guest input contract

Before injection, the hook waits for the outer fixture's exact readiness
marker:

```text
QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCH_READY
```

It then injects `A`, two distinct absolute pointer positions, and a left
press/release into the live Moonlight SDL window.  Success requires these
guest-evdev markers in the outer telemetry, not merely a successful D-Bus or
xdotool call:

```text
QSF_VIRGL_WAYLAND_GUEST_INPUT_KEY_A=observed
QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_ABS=observed
QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_BTN=observed
QSF_VIRGL_WAYLAND_GUEST_INPUT_E2E_OK
```

For deterministic Xvfb input, the hook uses Moonlight Embedded's own
`Ctrl+Alt+Shift+Z` ungrab action and releases `Z` before releasing its three
modifiers.  That makes the following XTest moves use Moonlight's normal
absolute-position protocol; Sunshine's final input statistics must show
nonzero `absolute_calls`, `button_calls`, and `queued_abs`, with no geometry
drop.  The guest marker remains the authoritative end-to-end proof.

## Assertions and scope

The hook requires fresh private pairing, HTTPS application listing/launch,
RTSP/RTP startup, Moonlight's FFmpeg H.264 decoder, a non-black decoded PNG,
and a `1280x720` fullscreen Moonlight window at `(0,0)` on a `1280x720` Xvfb
root.  It independently requires Sunshine's native QEMU DMA-BUF import banner
and nonzero, zero-failure final counters after Sunshine shuts down.

The outer QSF/Wayland gate retains ownership of its clipboard, file transfer,
and guest-side resize assertions after this hook returns.  Stock GameStream
does not acquire a clipboard or file-transfer protocol from this hook; no such
claim is made by its video/input evidence.

## CPU-readback and QSF boundary

The hook's video boundary ends at Sunshine: QEMU `ScanoutDMABUF` is imported
with headless EGL, read back into CPU BGRX memory, and software-encoded by
`libx264` for the Moonlight GameStream session. It makes no zero-copy GPU
encoding claim and does not route video through QSF.

QSF remains the independent authenticated agent/control path used by the
outer runner for guest↔client clipboard, file transfer, and Weston resize.
The hook neither invokes nor mutates those QSF control channels; it only
consumes the live Display1 endpoint and returns after its own video/input
proof. Consequently, the outer trace establishes QSF state transfer while
the nested hook trace establishes Moonlight/Sunshine video and input.

## Final deployment evidence

The final no-X composite passed on 2026-09-01:

- outer QSF/VirGL/Wayland trace:
  `vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.KEqkVc/trace.txt`;
- nested Moonlight/Sunshine trace:
  `vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.KEqkVc/moonlight-sunshine-hook.480Wca/trace.txt`.

The outer trace ends `QSF_VIRGL_WAYLAND_GUEST_E2E_OK` and carries the QSF
clipboard/file/resize and VirGL evidence. The nested trace names
`build-sunshine-qemu-no-x11/sunshine`, ends
`QMDP_MOONLIGHT_SUNSHINE_VIRGL_QSF_WAYLAND_HOOK_OK`, and records fullscreen
geometry, non-black decode, final nonzero absolute-input statistics, and
zero-failure Sunshine DMA-BUF counters. For the separately refreshed direct
fullscreen/windowed evidence, see `run.vCQmck` and `run.gNCula` in the native
Moonlight E2E document.
