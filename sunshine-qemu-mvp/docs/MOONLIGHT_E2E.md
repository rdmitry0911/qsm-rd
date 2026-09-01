# Moonlight → Sunshine → QEMU end-to-end gate

## What this gate proves

`scripts/run-moonlight-sunshine-qemu-e2e.sh` is a disposable, local and
headless qualification of the actual GameStream path:

```text
Moonlight Embedded SDL client on Xvfb
  -> pair + HTTPS application launch + RTSP/RTP
  -> patched Sunshine (`capture=qemu_dbus`, software H.264)
  -> QEMU Display1 CPU listener on a private D-Bus session
  -> TCG BIOS guest
  -> QEMU Display1 CPU scanout
  -> Sunshine/libx264 H.264
  -> Moonlight FFmpeg H.264 decode and SDL drawable
```

The runner generates fresh Sunshine state, Moonlight keys, QEMU guest media,
and a session D-Bus per invocation.  It does not use, alter, or trust normal
user pairings.  All evidence is retained in a new `run.XXXXXX` child of
`OUTPUT_DIR` (default:
`artifacts/validation/moonlight-sunshine-qemu-e2e/`).

The BIOS fixture starts with a red VGA framebuffer, waits for raw PS/2
set-1 `A` make (`0x1e`) and break (`0x9e`) bytes, writes
`INPUT_PRESS_RELEASE_OK` to QEMU debugcon, then changes the framebuffer to
green.  The runner injects `a` into the real Moonlight SDL window with
`xdotool`; it therefore asserts this whole route rather than accepting a
Sunshine or D-Bus method return:

```text
X11 key down/up -> Moonlight common-c input packet -> GameStream input stream
  -> Sunshine QEMU keyboard route -> Display1 Keyboard.Press/Release
  -> QEMU PS/2 controller -> guest fixture acknowledgement
```

It retains and checks:

- successful private pairing and the `Desktop` application list;
- RTSP setup, video-stream start and the first received RTP video packet;
- Moonlight's `Using FFmpeg decoder: h264` log entry;
- QEMU Display1 capture, software `libx264`, and a Sunshine streaming session;
- the exact guest debugcon acknowledgement after the real Moonlight key event;
- a PNG sampled from the SDL drawable.  Its center pixel must be green after
  the guest acknowledgement (`moonlight-client-center-rgb.txt`), proving that
  the decoded image is the post-input guest frame;
- a bounded client shutdown (`timeout` status `124`) rather than an early
  disconnect.

This is meaningful codec and input E2E evidence.  It is not merely a local
Display1 probe or a fake Moonlight callback.

## Prerequisites and build

The following is the Debian/Ubuntu package shape used for the headless client
lane.  QEMU's D-Bus display module is installed by the project helper.

```bash
sudo apt install build-essential cmake ninja-build git pkg-config \
  libasound2-dev libavahi-client-dev libavcodec-dev libavutil-dev \
  libcurl4-openssl-dev libegl1-mesa-dev libgles2-mesa-dev libopus-dev \
  libsdl2-dev libudev-dev libvdpau-dev xvfb xdotool x11-utils ffmpeg
./scripts/install-qemu-debian.sh
```

Build Moonlight Embedded with its real SDL/FFmpeg platform (the `fake`
platform is deliberately not used by this gate):

```bash
git clone https://github.com/moonlight-stream/moonlight-embedded.git \
  .upstream/moonlight-embedded
cmake -S .upstream/moonlight-embedded -B .upstream/build-moonlight-embedded \
  -G Ninja -DCMAKE_BUILD_TYPE=Release -DENABLE_CEC=OFF
cmake --build .upstream/build-moonlight-embedded -j2
```

Build the final strict deployment artifact with the reproducible helper. It
checks out the pinned upstream revision, applies patches `0001` through
`0006`, uses the isolated libva ABI needed by Sunshine's bundled FFmpeg, and
rejects X11, Wayland, PulseAudio, and ALSA from the complete runtime `ldd`
closure. See `integration/sunshine/PINNED_UPSTREAM.md` for the exact upstream
commit and patch hashes.

```bash
./scripts/build-isolated-libva-2.21.sh
SUNSHINE_BUILD_DIR="$PWD/.upstream/build-sunshine-qemu-no-x11" \
  ./scripts/build-upstream-sunshine-qemu.sh
```

The test requires `assets/apps.json` beside the Sunshine executable, as a
normal installed/build-tree Sunshine layout provides.

## Run it

Windowed and fullscreen client presentation are independent invocations:

```bash
SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine" \
MOONLIGHT_BINARY="$PWD/.upstream/build-moonlight-embedded/moonlight" \
STREAM_SECONDS=12 MOONLIGHT_WINDOW_MODE=windowed \
./scripts/run-moonlight-sunshine-qemu-e2e.sh

SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine" \
MOONLIGHT_BINARY="$PWD/.upstream/build-moonlight-embedded/moonlight" \
STREAM_SECONDS=12 MOONLIGHT_WINDOW_MODE=fullscreen \
./scripts/run-moonlight-sunshine-qemu-e2e.sh
```

Use `OUTPUT_DIR=/path/to/evidence-parent` to select an evidence parent.  The
successful terminal line prints the exact fresh child directory, for example:

```text
MOONLIGHT_SUNSHINE_QEMU_E2E_OK output=.../run.XXXXXX
```

The test forces Mesa software EGL for Xvfb
(`__EGL_VENDOR_LIBRARY_FILENAMES=.../50_mesa.json` and
`LIBGL_ALWAYS_SOFTWARE=1`), and uses `SDL_AUDIODRIVER=dummy`.  This avoids an
unrelated NVIDIA EGL/Xvfb crash and a physical audio-device requirement; it
does **not** qualify audio.  It binds Sunshine only to `127.0.0.1` with a
private base port and uses PIN `4242` only inside its disposable state.

Xvfb, SDL, and `xdotool` are dependencies of this disposable **client-side
test harness only**.  The deployed q-sunshine host runtime for the CPU
Display1 path is headless and has no X11 runtime dependency.

## Resolution and fullscreen semantics

The stream request is `1280x720@30`.  The fullscreen lane starts a disposable
`1280x720` Xvfb root and requires the Moonlight SDL window to be `1280x720` at
absolute `(0,0)`; it also captures the root drawable at the same geometry.
The windowed lane uses a `1600x900` Xvfb root and retains the normal centered
`1280x720` drawable.  This is a strict, headless proof of client fullscreen
presentation, not just a command-line flag.

It is deliberately **not** a claim that the guest changed its native desktop
mode.  The BIOS fixture's real scanout is mode 13h (`320x200`) and Sunshine
scales it into the negotiated stream.  `Console.SetUIInfo` is advisory: the
guest video stack must actually produce a new Display1 scanout before guest
resolution change can be claimed.  See
`integration/sunshine/QEMU_INPUT_AND_DATA_SCOPE.md` for that contract.

## Clipboard, files, and VirGL boundaries

Stock Moonlight/GameStream input has keyboard, mouse, controller, touch, pen,
and UTF-8 text input.  It does not standardize bidirectional clipboard or file
transfer.  In particular, Moonlight Qt's paste shortcut is client-to-host
text typing, not guest-to-client clipboard synchronization.  Consequently,
the runner makes no clipboard or file-transfer claim.

`extensions/qsf_control/` supplies a separate authenticated local companion
channel for clipboard text, constrained file upload/download, and resize
requests over QEMU virtio-serial.  It is intentionally not exposed as a raw
TCP listener and is not a Moonlight protocol extension; a remote product must
bind it to the authenticated session launcher and demonstrate each guest
endpoint separately.  Its protocol boundary and commands are documented in
`extensions/qsf_control/README.md`.  The real KVM guest companion gate has
now passed independent endpoint hashes; its exact command, artifacts, and
strict agent-state/GUI boundary are in `QSF_GUEST_E2E.md`.

This Moonlight gate's QEMU Display1 capture lane is CPU-only:

```text
-display dbus,gl=off
```

Its post-input proof is a CPU VGA scanout. It does **not by itself** establish
a combined Moonlight → Sunshine → QEMU Display1 → VirGL guest E2E result;
Display1 GL/DMA-BUF ingestion is qualified separately in
`MOONLIGHT_SUNSHINE_VIRGL_E2E.md` and the final combined QSF/Wayland gate.

## Current exclusions

- audio transport/playback in this particular CPU/video/input runner; the
  separate KVM decoded-audio gate is documented in
  [MOONLIGHT_AUDIO_E2E.md](MOONLIGHT_AUDIO_E2E.md);
- a guest desktop's native mode change or dynamic-resolution verification;
- GameStream-native clipboard or file transfer (there is no such interoperable
  packet in stock Moonlight);
- combined CPU-fixture capture and VirGL in this particular runner; the
  separate native combined gate has passed;
- KVM, hardware encoding, DMA-BUF, reconnect/soak, controller, touch, pen,
  or clipboard ownership races.
