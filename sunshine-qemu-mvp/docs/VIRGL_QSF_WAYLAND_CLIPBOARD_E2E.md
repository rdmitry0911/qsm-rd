# Native VirGL + Weston DRM + QSF Wayland clipboard E2E

`scripts/run-virgl-qsf-wayland-clipboard-e2e.sh` boots a fresh pinned Alpine
3.20.10 cloud guest and qualifies an actual guest desktop clipboard, not merely
the QSF agent's private state file.

```text
local authenticated QSF client
  -> 0600 token-protected qsf-control socket
  -> QEMU virtio-serial
  -> qsf-guest-agent private state
  -> qsf-wayland-clipboard-bridge
  -> Weston DRM wl-copy / wl-paste

Alpine virtio-gpu / VirGL / Weston DRM
  -> QEMU Display1 DMA-BUF
  -> headless GBM or headless-EGL CPU readback -> H.264 probe
```

The deployment host path has no X11, Wayland, GTK, Xvfb, or desktop-session
dependency. The only graphical stack in this standalone guest gate is inside
the guest: `weston`, `weston-backend-drm`, `weston-clients`, `wl-clipboard`,
`wayland-utils`, `seatd`, `eudev`, and `gnu-libiconv`. The optional Qt/Moonlight composite
below deliberately starts a short-lived **client-only** Xvfb for visual
attestation; Sunshine, QEMU, the Display1 observer, and the guest remain
headless on the host.

## Run

The native render node, KVM access, the pinned cloud image, and the
DMA-BUF-capable probe are required.

```bash
./scripts/provision-alpine-virgl-guest.sh
cmake --build .build-dmabuf --target qemu-display-probe
QEMU_DISPLAY_PROBE="$PWD/.build-dmabuf/qemu-display-probe" \
VIRGL_QEMU_RUN_AS=dima VIRGL_QEMU_USE_SUDO=1 \
./scripts/run-virgl-qsf-wayland-clipboard-e2e.sh
```

Run the outer script as the same unprivileged account that owns its private
`dbus-run-session` and QEMU process (for example, start a shell as `dima`
first). `VIRGL_QEMU_RUN_AS` is for the QEMU child and does not make a root
caller's private D-Bus socket usable by an unrelated VM account.

The runner creates a fresh qcow2 overlay, one NoCloud `cidata` CD, and a
separate read-only virtio block payload volume. It provisions three verified
guest artifacts per run: the static constrained QSF agent, a mode-`0700` shell
QSF/Wayland bridge, and the static evdev evidence watcher. SHA-256 hashes and
guest modes are recorded in the private output directory. Do not publish the
directory: it contains an ephemeral local QSF token.

## What the gate proves

The guest starts `eudevd`, triggers its input rules, and starts guest-local
`seatd`; that is necessary because the generic cloud image has no logind or
running udev service. Weston then owns `/dev/dri/card0`, uses
`/dev/dri/renderD128`, exposes a real `wl_seat`, and reports the VirGL renderer.

The adapter and static guest agent accept only non-NUL UTF-8
`text/plain;charset=utf-8` up to 1 MiB. The bridge uses `gnu-iconv` for
byte-preserving UTF-8 validation and atomically replaces the QSF state file
only for a native Wayland selection change. Hash comparison prevents feedback
loops in both directions.

The Alpine cloud image is pinned, while its guest-only Wayland packages are
resolved from the configured `v3.20` repositories at boot. Rather than claim a
fixed package version, every trace records the post-install APK database
version as `guest_wayland_package_<name>=<name>-<version>` for Mesa, Weston,
`wl-clipboard`, `wayland-utils`, `seatd`, `eudev`, and `gnu-libiconv`. An offline or fully
package-reproducible deployment must provide a pinned APK mirror/cache.

The runner independently asserts:

- QSF client text arrives in guest state and then matches a separate guest
  `wl-paste` read.
- A guest `wl-copy` of a separate fixture changes native Wayland selection;
  the bridge writes constrained QSF state, the agent emits its change, and a
  real QSF client `clipboard-get` receives exactly those bytes.
- QSF upload/download fixtures have independent guest-side SHA-256 evidence.
- A client-selected `1280x720` is resolved through `connection_optimize` as a
  bounded client-decoder / Sunshine-encoder / VirGL-display profile.  The
  broker requests QEMU `Console.SetUIInfo` before it commits that profile to
  the guest; a successful D-Bus reply alone is never treated as a resize.
- The guest compositor adapter writes `connection-profile-applied` only after
  a newly started Weston DRM instance reports the exact **current** `wl_output`
  mode through `wayland-info`, confirms fresh DRM + VirGL logs, restarts its
  Wayland bridge, and keeps a new `weston-simple-egl` workload alive.  It
  copies the exact six-line canonical record (`version=2`, `generation`,
  `resolution`, `fps`, `bitrate_kbps`, `video_codec`); the agent rejects a
  stale or merely similar acknowledgement.
- The real desktop reconfigures: a short pre-hook Display1 H.264 capture is
  `1280x800`; the runner requires ordered live H.264 `1280x800` then
  `1280x720` segments.  Thus the guest-side current-mode acknowledgement and
  the independent QEMU Display1 video trace jointly prove the scanout rather
  than a state-file write.
- Native Display1 DMA-BUF CPU readback has zero failures and zero session
  errors.

Weston 12 does not automatically select a new preferred virtio-gpu mode while
its DRM backend is already live. Therefore this fixture deliberately performs
and records `weston-drm-restart` after the committed profile becomes visible.
The restart re-reads QEMU's requested connector mode, verifies the compositor's
current mode, restarts the bridge (which reseeds the persistent QSF selection),
and starts a new `weston-simple-egl` workload before publishing the generation-
bound acknowledgement. This is an explicit desktop reconfiguration fallback,
not a claim of automatic hotplug handling.

## Recorded standalone result

The v2 negotiated-profile transaction passed on 2026-09-01 at
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.bi1FPD/trace.txt`. Its client
requested `1280x720`; the resolved H.264 profile was 60 FPS / 8000 Kbit/s with
generation `7680650639916349473`. The trace records both the exact guest
`CONNECTION_PROFILE_APPLIED` and independent ACK-observed markers, then
retains ordered Display1 H.264 `1280x800` → `1280x720` segments, DMA-BUF
scanouts/updates/failures `10638/10640/0`, and zero session errors.

`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.rEvqnH/trace.txt` passed on
2026-09-01 with:

- guest driver `virtio_gpu` and renderer `virgl (NVIDIA GeForce RTX
  3080/PCIe/SSE2)`;
- QSF client → real `wl-paste` SHA-256
  `7a86b46c203c32ccbc5234b82bd7d05c404e9c4910a36093a3b9c788aecd6548`;
- guest `wl-copy` → QSF client SHA-256
  `af2fd4676d16c4ffbcbc946d461ec21f16820dd948fdab1cc84cb715013dcfc6`;
- H.264 `1280x800` pre-hook and live `1280x800` → `1280x720` transition;
- live DMA-BUF scanouts/updates/failures `4453/4455/0`, with zero session
  errors.

A separate direct-Display1 diagnostic,
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.0pYeJY/trace.txt`, also passed
the static guest watcher for `KEY_A`, absolute pointer movement, and a pointer
button. Its live DMA-BUF counts were `9048/9050/0`, with zero session errors.
That validates QEMU's Display1 input ABI and the guest evidence collector; it
does **not** represent a Moonlight/Sunshine connection.

## Moonlight/Sunshine composite hook

The runner can retain the exact powered-on guest for a Moonlight → Sunshine
test rather than starting a second VM. Set an executable path explicitly:

```bash
VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK=/absolute/path/to/hook \
VIRGL_QSF_WAYLAND_PROBE_DURATION_MS=60000 \
./scripts/run-virgl-qsf-wayland-clipboard-e2e.sh
```

Before invoking the hook, the runner closes an independently encoded
`1280x800` pre-hook capture and starts a longer Display1 listener. That live
listener stays attached through the hook and all QSF actions. Increase its
duration when a hook has a deliberately longer client setup phase.

The hook receives these environment variables for the already-running QEMU
session:

| Variable | Meaning |
| --- | --- |
| `QSF_WAYLAND_OUTPUT_DIR` | Private per-run evidence directory |
| `QSF_WAYLAND_QEMU_PID`, `QSF_WAYLAND_QEMU_PIDFILE` | Live QEMU identity |
| `QSF_WAYLAND_DBUS_ADDRESS`, `QSF_WAYLAND_DBUS_DESTINATION` | Private Display1 bus and `org.qemu` |
| `QSF_WAYLAND_AGENT_SOCKET` | QEMU virtio-serial server socket |
| `QSF_WAYLAND_CONTROL_SOCKET`, `QSF_WAYLAND_TOKEN_FILE` | Existing local authenticated QSF endpoint |

The hook is trusted session-launcher code: it receives local capability paths
and must neither log nor forward the token. An ordinary Moonlight client never
receives those paths.

Before the Qt hook asks its `MoonlightController` to make a controlled
reconnect, it requires both guest markers below. The first is emitted by the
Weston/VirGL adapter after it atomically publishes the exact profile; the
second is an independent fixture-coordinator readback of that same file.

```text
QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_APPLIED=version=2,generation=...,resolution=1280x720,...
QSF_VIRGL_WAYLAND_GUEST_CONNECTION_PROFILE_ACK_OBSERVED=generation=...,resolution=1280x720
```

The outer trace also requires ordered H.264 Display1 geometry evidence. A
profile transaction is therefore not complete merely because `SetUIInfo`
returned `applied` or because `state/resolution` changed.

The guest starts a static evdev watcher before `VIRGL_READY`. Its telemetry
file is `$QSF_WAYLAND_OUTPUT_DIR/guest-telemetry.log`; it first emits
`QSF_VIRGL_WAYLAND_GUEST_INPUT_WATCH_READY`. A strict hook must inject real
`KEY_A`, absolute pointer motion, and a pointer button. Once the hook exits
successfully, the runner requires all of:

```text
QSF_VIRGL_WAYLAND_GUEST_INPUT_KEY_A=observed
QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_ABS=observed
QSF_VIRGL_WAYLAND_GUEST_INPUT_MOUSE_BTN=observed
QSF_VIRGL_WAYLAND_GUEST_INPUT_E2E_OK
```

Those are raw Linux evdev observations in the guest, not a compositor focus
heuristic. Without an explicit hook, the standalone runner does not claim any
Moonlight/Sunshine input result.

For a narrow diagnostic of QEMU Display1 input only, the repository also
contains `tests/fixtures/qsf-wayland-display1-input-hook.sh`. It sends the
documented Display1 `Keyboard.Press/Release` and absolute `Mouse` calls over
the already-private D-Bus session:

```bash
VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK=\
  "$PWD/tests/fixtures/qsf-wayland-display1-input-hook.sh" \
./scripts/run-virgl-qsf-wayland-clipboard-e2e.sh
```

That diagnostic can validate the guest evdev watcher, but it is not a
Moonlight/Sunshine result and must not be used as one.

### Qt shell composite mode

`scripts/run-qsunshine-qt-moonlight-virgl-qsf-wayland-hook.sh` is different
from the older Embedded-Moonlight hook: it drives the production Qt
`MoonlightController` and `QsfClient`, including pair/list verification,
windowed→fullscreen reconnect, clipboard, files, and resize. Set
`VIRGL_QSF_WAYLAND_QT_QSF_OWNER=qt` with that hook.  The outer runner then
skips its legacy mutating Python QSF block and verifies the Qt hook's atomic
summary instead; using both QSF owners would race the same guest state.

For every client-selected guest size, the production path is deliberately
serialized:

1. normal QSF is deactivated and its clipboard/file/diagnostic-resize work is
   cancelled;
2. the visible stock Moonlight stream is stopped, then the driver waits for its
   host capture to retire;
3. a temporary TLS 1.3 mTLS **profile-only** QSF lease proves ready and permits
   only `connection_optimize`;
4. the broker combines client decoder, Sunshine host encoder, and VirGL guest
   display capabilities, performs QEMU `Console.SetUIInfo`, and waits for the
   exact generation-bound guest scanout acknowledgement;
5. the temporary lease closes, a fresh Moonlight process starts with the
   resolved profile, and normal QSF is reactivated only after that video is
   verified.

This prevents an old Sunshine capture or a clipboard/file request from racing
the Weston restart. `fullscreen` still controls the Moonlight client window;
it does not itself prove or select a guest scanout.

The current safe-handoff gate passed on 2026-09-01 at
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.Jcmdij/trace.txt`. Its outer hook
contains `QSUNSHINE_QT_MOONLIGHT_VIRGL_QSF_HOOK_OK`; the nested trace records:

- a non-black `1280x800` windowed surface;
- a quiesced windowed stream, profile-only QSF, and an exact guest
  `1280x720` acknowledgement before the negotiated Moonlight process starts;
- normal QSF reactivation after that video is verified, followed by a quiesced
  negotiated stream, another profile-only lease, and an exact guest
  `1600x900` acknowledgement before the fullscreen process starts;
- a non-black physical `1600x900@(0,0)` fullscreen surface, both guest input
  barriers, bidirectional mTLS clipboard evidence, and byte-for-byte QSF
  upload/download with a second download after normal QSF reactivation.

The driver selects Moonlight's `software` decoder only in this disposable
Xvfb/`xwd` capture lane; production Qt clients default to Moonlight's `auto`
decoder selection. The older Embedded-Moonlight result in the next section is
not a Qt shell qualification.

The exact build and invocation are in
[`QT_DESKTOP_CLIENT.md`](QT_DESKTOP_CLIENT.md#retained-qtmoonlightvirgl-e2e-gate).

## Legacy Embedded-Moonlight composite result

The fresh strict composite passed on 2026-09-01 at
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.KEqkVc/trace.txt`; its nested
Moonlight proof is
`run.KEqkVc/moonlight-sunshine-hook.480Wca/trace.txt`. Both use the exact
`.upstream/build-sunshine-qemu-no-x11/sunshine` artifact.

The outer trace records actual package versions from Alpine's installed-package
database, including `weston-12.0.4-r0` and `wl-clipboard-2.2.0-r1`, the
NVIDIA-backed VirGL renderer, both clipboard hashes, both file hashes,
`SetUIInfo` applied, and a checked live H.264 geometry order
`1280x800->1280x720`. Its final DMA-BUF counters are `13165/13167/0` with zero
session errors. The nested trace records fullscreen Moonlight presentation,
non-black decode, and guest `KEY_A` + absolute pointer + button evidence.

## Scope boundary

This verifies a guest GUI clipboard bridge and the local QSF companion
channel. GameStream itself still has no interoperable clipboard or file
transfer packet, so a remote Moonlight deployment must carry QSF through its
authenticated companion path. A green standalone trace is not by itself a
claim that Moonlight/Sunshine was connected; that claim requires a green
post-agent hook and its input markers.

The guest-to-client direction is currently a safe pull operation:
the guest agent emits `EVENT_CLIP` to update the local broker and a companion
uses `clipboard-get` to retrieve it. This E2E does not claim unsolicited
push delivery to a remote client. Text is non-NUL UTF-8 at both agent and
Wayland boundaries (1 MiB); files are separate binary payloads (2 MiB,
plain basenames).
