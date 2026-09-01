# Qt desktop client

`qsunshine-client` is a portable client-side desktop shell.  It deliberately
does **not** embed Moonlight's SDL video surface in Qt and it does not modify
Moonlight's event loop.  That is what keeps the design viable on Linux/X11,
Linux/Wayland, Windows, and macOS.

```text
Qt shell ── QProcess ──> Moonlight Qt CLI ── GameStream ──> Sunshine ──> QEMU/VirGL
   │                              video, audio, input
   └── TLS 1.3 mTLS QSF ───────────────────────────────────> per-VM QSF gateway
                                                               └── virtio-serial guest agent
```

Moonlight owns only GameStream media and input.  The Qt shell owns the saved
desktop profile, launch/reconnect policy, QSF TLS settings, platform clipboard
bridge, and constrained file controls.  Consequently the headless Sunshine
host remains free of X11, Wayland, PulseAudio, and ALSA dependencies.

## Build

The shell needs Qt only on the client/build machine.  On Debian/Ubuntu, install
the Moonlight Qt and shell development dependencies:

```bash
apt update && apt install -y \
  build-essential cmake ninja-build pkg-config \
  libegl1-mesa-dev libgl1-mesa-dev libopus-dev libsdl2-dev libsdl2-ttf-dev \
  libssl-dev libavcodec-dev libavformat-dev libswscale-dev libva-dev \
  libvdpau-dev libxkbcommon-dev wayland-protocols libdrm-dev \
  qt6-base-dev qt6-declarative-dev qt6-svg-dev qt6-wayland \
  qml6-module-qtquick qml6-module-qtquick-controls \
  qml6-module-qtquick-templates qml6-module-qtquick-layouts \
  qml6-module-qtqml-workerscript qml6-module-qtquick-window
```

Build the shell without changing the normal headless server build:

```bash
cmake -S . -B .build-qt-client -G Ninja -DQMDP_BUILD_QT_CLIENT=ON
cmake --build .build-qt-client --target qsunshine-client qsunshine-qsf-e2e
```

The app expects a compatible `moonlight` (Moonlight Qt) executable in `PATH`,
or an explicit path entered in its Connection tab.  Its invoked interface is
the upstream CLI contract:

```text
moonlight pair --pin PIN HOST
moonlight stream --display-mode windowed|fullscreen|borderless \
  --resolution WIDTHxHEIGHT --no-quit-after --absolute-mouse \
  [--video-decoder software|hardware] HOST APP
```

Build or install Moonlight Qt separately; it is intentionally not vendored or
patched by this project.  The `--no-quit-after` flag is mandatory for a VM
desktop: disconnecting this client must never ask Sunshine to quit `Desktop`.

Upstream Moonlight's current CLI accepts the pairing PIN only as `--pin PIN`.
That means the short-lived PIN can be visible to other same-user local
processes through normal process inspection while pairing is running. The Qt
shell clears its input field immediately, but cannot remove a value from a
child process argv. Prefer a single-user client machine and request an
upstream stdin/FD PIN interface before treating this as a stronger local
secrecy boundary.

## Desktop workflow

1. Create or select a saved profile, then save its Sunshine host, application,
   initial resolution, and presentation mode.  QSF settings use this profile
   identifier, not a mutable hostname alias.
2. Pair once.  The four-digit value entered in the shell is supplied to
   Moonlight and must be entered in Sunshine's pairing UI.  An exit code from
   Moonlight alone is not treated as proof of pairing; verify it in Sunshine
   before streaming.
3. Connect to `Desktop`.  `windowed`, `fullscreen`, and `borderless` map to
   upstream Moonlight modes.  Changing app, initial size, or presentation while
   connected performs a controlled Moonlight graphics reconnect.  It does not
   pretend that a stock Moonlight client can change those modes in-place.
4. After the Moonlight window visibly displays the intended desktop, explicitly
   activate QSF.  Process startup is not taken as evidence that GameStream
   authentication completed, and the shell does not use brittle log parsing as
   a security authority.  Stopping or reconnecting Moonlight immediately
   cancels every in-flight QSF operation.
5. To make the VirGL desktop match a size selected in the client, use **Choose
   optimal stream profile** while the current video is visible and normal QSF
   is verified. The coordinator ends normal QSF (cancelling clipboard, files,
   and standalone resize), stops Moonlight, and waits for the old Sunshine
   capture to retire. It then opens a temporary **profile-only** mTLS lease.
   That lease accepts only `connection_optimize`: the client supplies the
   selected `WIDTHxHEIGHT`, decoder capability, and refresh ceiling; Sunshine
   supplies an encoder envelope; the QEMU/VirGL guest supplies its display
   envelope and later its actual scanout acknowledgement. After QEMU
   `Console.SetUIInfo` and the exact generation-bound guest acknowledgement,
   the temporary lease closes and a new stock Moonlight process starts with the
   resolved profile. Activate normal QSF again only after that new video is
   visibly verified. `fullscreen` remains a client presentation mode; it does
   not replace the selected guest scanout size.

   Once the `connection_optimize` request bytes have entered the encrypted
   socket, **Cancel profile handoff** does not abort that socket or unlock a
   reconnect: the gateway may already be executing QEMU/guest work. It marks
   cancellation and waits for the authoritative terminal reply, then closes
   the profile-only lease without launching replacement Moonlight. If the
   response path fails after dispatch, the shell closes its local lease but
   keeps the UI locked for a conservative 90-second remote-settlement window
   before allowing a manual reconnect.

   Before the profile bytes enter `QSslSocket`, the coordinator synchronously
   writes a durable recovery marker scoped to the current Moonlight desktop
   profile. If the GUI exits or crashes in the small interval where a gateway
   worker may continue independently, a fresh shell restores that marker
   before QML is loaded and blocks both normal Moonlight start and normal QSF
   activation for its bounded 180-second crash-recovery window. A terminal
   profile reply, a proven in-process settlement, or an expired marker releases
   the corresponding profile; unrelated saved desktop profiles retain their
   own state.

QSF endpoint and credential fields are locked while the companion is active.
Deactivate it before selecting another endpoint or credential set.

The **Video decoder** selector is deliberately a small client-global enum:
`auto` (the production default), `software`, or `hardware`. It is not saved in
an individual VM profile. `auto` preserves stock Moonlight's own selection and
does not add a `--video-decoder` option to its child argv; the two explicit
values add only their corresponding validated option. The selector is locked
while pairing or streaming, so changing it requires disconnecting before the
next controlled stream launch.

## Clipboard, file transfer, and resize

QSF is a separate, authenticated companion protocol; these features are not
misrepresented as GameStream packets.

- Clipboard is bidirectional, plain non-NUL UTF-8, at most 1 MiB.  The client
  uses hashes and a request-local revision to suppress feedback loops and avoid
  overwriting a newer local copy with a stale guest poll reply.  Disabling sync
  aborts queued and active clipboard operations immediately. On each
  activation the user explicitly chooses whether the current client or guest
  clipboard wins; arbitrary existing clipboard histories cannot be safely
  merged. Later changes synchronize in both directions. A failed local update
  remains a dirty snapshot and retries with bounded backoff rather than being
  overwritten by a stale guest poll.
- Files use QSF only, have safe ASCII basenames, and are limited to 2 MiB.
  Upload reads guest `inbox`; download reads guest `outbox`; local downloads are
  written atomically.
- The advanced standalone resize control remains a diagnostic request: its
  reply confirms guest state plus QEMU `Console.SetUIInfo`, not a compositor
  mode change.  The **Choose optimal stream profile** path is the production
  route for client-selected guest geometry. Its profile-only QSF lease rejects
  clipboard, file, and standalone-resize operations, so no companion traffic
  can race the scanout transition. It returns only after the guest copied a
  generation-bound `connection-profile-applied` record after checking its
  actual scanout; a fresh Moonlight launch follows only after the lease closes.
  The full wire contract is in
  [`QSF_STREAM_NEGOTIATION.md`](QSF_STREAM_NEGOTIATION.md).

## Security boundary

The gateway enforces TLS 1.3 with a client certificate and reads the host-only
QSF token from its `0600` file; that token never travels to the Qt client.
Use a unique gateway/certificate set per VM profile and protect the client
private key with normal filesystem permissions.

QSF and GameStream are intentionally separate protocols.  The current gateway
does not cryptographically bind a GameStream session to a QSF request.  The
shell therefore requires explicit post-video activation, locks the endpoint
while active, and cancels it on Moonlight teardown.  Do not configure a
profile to use an untrusted QSF gateway.

## Tests

With `QMDP_BUILD_QT_CLIENT=ON`, `qsunshine_moonlight_controller` starts a
disposable fake Moonlight child to assert the shell's real process contract:
windowed/fullscreen argv, controlled reconnect, prompt QSF-teardown signal,
saved profiles, manual disconnect, pairing result semantics, and
`--no-quit-after`.

That fake child is currently a POSIX shell fixture, so this particular CTest
is registered on Linux/macOS only. The production Qt client itself has no
POSIX-only process or window embedding dependency.

`qsunshine_qt_qsf_mtls_e2e` drives the production `QsfClient` under the Qt
offscreen platform through the real Python TLS gateway and local QSF broker.
It asserts TLS 1.3 mTLS status, guest-to-client clipboard, client-to-guest
clipboard, upload, download, and negotiated client-to-VirGL geometry:

```bash
ctest --test-dir .build-qt-client \
  -R '^(qsunshine_moonlight_controller|qsunshine_qt_qsf_mtls_e2e)$' \
  --output-on-failure
```

`qsunshine_qt_profile_cancel_regression` uses the same real mTLS gateway,
broker, guest fixture, `QsfClient`, and process controller. It delays the
authoritative guest profile ACK after `CONNECTION_OPTIMIZE`, requests Cancel,
and proves that the handoff remains busy and launches no replacement Moonlight
process until that terminal reply arrives.

`qsunshine_qt_profile_settlement_recovery` seeds the durable per-profile
recovery record before the coordinator exists and proves that startup blocks
both normal Moonlight/QSF admission paths, then verifies synchronous cleanup
of an expired record without waiting for the production crash window.

## Retained Qt/Moonlight/VirGL E2E gate

The final graphics qualification is intentionally a composite run, not a mock
of the media process.  It uses the exact `MoonlightController` and `QsfClient`
classes from the shell, a clean unmodified Moonlight Qt child, headless
Sunshine, KVM QEMU, and the Alpine Weston/VirGL guest.

First build the clean detached Moonlight source.  Do this in
`.upstream/moonlight-qt-clean`, never in the older experimental Moonlight
worktree:

```bash
git -C .upstream/moonlight-qt-clean diff --quiet
git -C .upstream/moonlight-qt-clean diff --cached --quiet
(
  cd .upstream/moonlight-qt-clean
  qmake6 -r moonlight-qt.pro
  make -j"$(nproc)"
)
test -x .upstream/moonlight-qt-clean/app/moonlight
```

Build the Qt targets, then run the retained composite gate.  Its default
Sunshine binary is the existing no-X11/no-Wayland build; no GUI dependency is
added to the server lane.

```bash
cmake -S . -B .build-qt-client -G Ninja \
  -DQMDP_BUILD_QT_CLIENT=ON -DQMDP_ENABLE_REAL_QEMU_E2E=OFF
cmake --build .build-qt-client --parallel 4
ctest --test-dir .build-qt-client --output-on-failure

VIRGL_QSF_WAYLAND_QT_QSF_OWNER=qt \
VIRGL_QSF_WAYLAND_PROBE_DURATION_MS=390000 \
VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK="$PWD/scripts/run-qsunshine-qt-moonlight-virgl-qsf-wayland-hook.sh" \
./scripts/run-virgl-qsf-wayland-clipboard-e2e.sh
```

The outer runner hands QSF ownership to the Qt driver in this mode.  Its old
raw Python QSF mutation block is skipped, so exactly one client changes the
guest clipboard, inbox/outbox, and display state.  It retains an atomic
`qsunshine-qt-e2e-summary.txt` only after every assertion passes, plus client
screenshots and trace under `qsunshine-qt-moonlight-hook/` in the outer run
directory.  When the default clean Moonlight binary is used, the hook also
rejects tracked source/index changes in that worktree and records its source
revision and binary SHA-256 in the trace.

The run proves all of the following together:

- stock Moonlight Qt pair flow, followed by an independent `moonlight list`
  check for `Desktop`;
- a non-black `windowed` video presentation at `1280x800`, then a controlled
  Moonlight reconnect and non-black physical `fullscreen` presentation at
  `1600x900@(0,0)` on the isolated `1600x900` client Xvfb root;
- guest-observed keyboard, absolute-pointer, and button input through the
  stock Moonlight/GameStream/Sunshine/QEMU path in both the windowed stream
  and, behind a distinct `KEY_B` phase barrier, after the fullscreen reconnect;
- TLS 1.3 mTLS Qt QSF clipboard in both directions, byte-for-byte upload and
  download, then two client-selected profiles (`1280x720` and `1600x900`). For
  each profile the driver first stops the visible stream, waits for capture
  retirement, proves a profile-only mTLS QSF ready phase, receives QEMU
  `SetUIInfo` plus an exact guest Weston/VirGL scanout acknowledgement, closes
  that lease, and starts a replacement Moonlight process. The byte-for-byte
  guest download is repeated only after fullscreen video and normal QSF have
  both been reactivated, rather than treating a ready signal as a usable
  post-reconnect data channel;
- retained Display1 H.264 evidence in the ordered geometry sequence
  `1280x800 -> 1280x720`.

Fullscreen presentation and guest scanout remain distinct assertions. In the
recorded safe handoff, the client root and final fullscreen stream are
`1600x900`, and the guest separately acknowledges first `1280x720` and then
a separately selected `1600x900` guest profile used for fullscreen; neither
assertion is inferred merely from the other or from host GPU hardware.
The real-E2E driver selects
`--video-decoder software` only for this disposable `xwd` capture lane, since
an NVIDIA VDPAU surface is not reliably readable by `xwd`; normal
`qsunshine-client` launches retain the `auto` default described above.

`Xvfb` in this command is a disposable client-side drawable solely for the
stock Moonlight Qt windows and visual checks.  Sunshine is launched with
`DISPLAY`, `WAYLAND_DISPLAY`, and Qt platform variables unset and captures
QEMU Display1 through the private D-Bus bus.

The native graphics/VirGL qualification remains documented in
[`MOONLIGHT_SUNSHINE_VIRGL_E2E.md`](MOONLIGHT_SUNSHINE_VIRGL_E2E.md) and
[`VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md`](VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md).
The safe lifecycle described above passed on 2026-09-01 in
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.Jcmdij/trace.txt`, whose outer
hook emits `QSUNSHINE_QT_MOONLIGHT_VIRGL_QSF_HOOK_OK`. Its nested
`qsunshine-qt-moonlight-hook/trace.txt` contains the stock-Moonlight
window/input/QSF evidence and ordered profile-only/normal-QSF phase markers.
These ignored run directories contain ephemeral pairing and QSF credentials
and are local verification artifacts only.
