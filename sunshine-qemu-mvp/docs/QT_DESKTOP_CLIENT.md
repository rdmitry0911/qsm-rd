# Qt desktop client

`qsunshine-client` is a portable client-side desktop shell.  It deliberately
does **not** embed Moonlight's SDL video surface in Qt and it does not modify
Moonlight's event loop.  That is what keeps the design viable on Linux/X11,
Linux/Wayland, Windows, and macOS.

```text
Qt shell ── TLS 1.3/PAM ──> per-VM system-auth gateway ──> short-lived in-memory ticket
   │
   ├── QProcess + one-shot ticket FD ──> patched Moonlight Qt CLI ──> authd mTLS lease
   │                                             │                         │
   │                                             └── pinned HTTPS + rtspenc ─> Sunshine ──> QEMU/VirGL
   │                                                                              video, audio, input
   └── TLS 1.3 QSF + ticket ───────────────────────────────────────> per-VM QSF gateway
                                                                        └── virtio-serial guest agent
```

Moonlight owns only GameStream media and input. The Qt shell owns the saved
desktop profile, TLS/PAM sign-in admission, launch/reconnect policy, QSF TLS
settings, platform clipboard bridge, and constrained file controls.
Consequently the headless Sunshine host remains free of X11, Wayland,
PulseAudio, and ALSA dependencies.

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

The production app never resolves `moonlight` from `PATH` and has no
executable-path field. On macOS it launches only the exact signed nested
`Contents/Resources/Moonlight.app/Contents/MacOS/Moonlight`; a Linux desktop
package must install the pinned patched child at the compile-time libexec path
`${CMAKE_INSTALL_FULL_LIBDIR}/q-sunshine-client/Moonlight/moonlight`. If that
child is absent or not executable, launch fails closed. The only arbitrary
child override is compiled into disposable tests and the explicitly invoked
real-E2E driver; it is absent from `qsunshine-client`.

Its normal invoked stream interface adds a fail-closed marker to the upstream
CLI contract:

```text
moonlight stream --qsm-system-auth --display-mode windowed|fullscreen \
  --resolution WIDTHxHEIGHT --no-quit-after --absolute-mouse \
  [--video-decoder software|hardware] HOST APP
```

The canonical patch is
[`integration/moonlight/patches/0001-system-auth-gamestream-lease.patch`](../integration/moonlight/patches/0001-system-auth-gamestream-lease.patch).
It consumes a one-time ticket from FD 0, creates the CSR/private key only in
RAM, receives the lease from authd, and pins the returned Sunshine certificate.
An ordinary Moonlight binary does not recognize the marker, so it cannot
silently fall back to pairing. The `--no-quit-after` flag is mandatory for a
VM desktop: disconnecting this client must never ask Sunshine to quit
`Desktop`.

### Native GameStream enrollment

The normal q-sunshine UI has no PIN field, client certificate/key selector, or
pairing action. It signs in with a system username and password through the
TLS/PAM service described in [`SYSTEM_AUTH.md`](SYSTEM_AUTH.md). The resulting
ticket is consumed by the native lease sequence described in
[`GAMESTREAM_LEASE_AUTH.md`](GAMESTREAM_LEASE_AUTH.md); the client-owned
ephemeral certificate is bound to the VM audience and expires shortly after
the ticket. Native Sunshine does not register `/pair` or `/api/pin`.

## Desktop workflow

1. Create or select a saved profile, then save its Sunshine host, application,
   initial resolution, presentation mode, and pinned TLS routes for the
   system-auth and QSF gateways. Set the non-secret **Expected VM audience**
   to the operator-provisioned value such as `vm-100`; it must exactly match
   the auth gateway's ticket audience and is not inferred from the editable
   desktop-profile label. QSF/system-auth settings use this profile identifier,
   not a mutable hostname alias.
2. Sign in with the allowed system username and password. The client sends one
   TLS 1.3/PAM login request, receives a VM-audience-scoped short-lived ticket,
   and keeps it only in memory. Passwords, tickets, subject, and expiry are not
   saved in the desktop profile. The **Connect** action remains disabled until
   this admission succeeds.
3. Connect to `Desktop`. The shell deliberately presents exactly two client
   presentation choices: `windowed` and `fullscreen`.  A legacy saved
   `borderless` choice is migrated to `fullscreen` when read, but cannot be
   selected or saved again. Changing app, initial size, or presentation while
   connected performs a controlled Moonlight graphics reconnect. It does not
   pretend that an active media session can change those modes in-place. The
   patched Moonlight process exchanges the current in-memory system-auth
   ticket for a client-owned mTLS lease and pins the VM's Sunshine certificate
   before its native GameStream launch; no PIN or persistent paired identity
   is involved.
4. After the Moonlight window visibly displays the intended desktop, explicitly
   activate QSF.  Process startup is not taken as evidence that GameStream
   authentication completed, and the shell does not use brittle log parsing as
   a security authority.  Stopping or reconnecting Moonlight immediately
   cancels every in-flight QSF operation.
5. To make the VirGL desktop match a size selected in the client, use **Choose
   optimal stream profile** while the current video is visible and normal QSF
   is verified. The coordinator ends normal QSF (cancelling clipboard, files,
   and standalone resize), stops Moonlight, and waits for the old Sunshine
   capture to retire. It then opens a temporary **profile-only** authenticated
   QSF lease.
   That lease accepts only `connection_optimize`: the client supplies the
   selected `WIDTHxHEIGHT`, decoder capability, and refresh ceiling; Sunshine
   supplies an encoder envelope; the QEMU/VirGL guest supplies its display
   envelope and later its actual scanout acknowledgement. After QEMU
   `Console.SetUIInfo` and the exact generation-bound guest acknowledgement,
   the temporary lease closes and a new patched Moonlight process starts with the
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

QSF endpoint, SNI, CA, and expected-audience fields are locked while the
companion is active. Changing one after sign-in clears the ticket and requires
a fresh login, so an admission for one VM cannot silently survive a
route/trust/audience change.

The Connection and companion pages use a leading-edge, viewport-width layout.
At compact desktop widths their label/control grids stack instead of centering a
fixed-width form, so resizing a native window from either side cannot place the
left part of a page outside the visible viewport.

The **Video decoder** selector is deliberately a small client-global enum:
`auto` (the production default), `software`, or `hardware`. It is not saved in
an individual VM profile. `auto` preserves Moonlight's own selection and
does not add a `--video-decoder` option to its child argv; the two explicit
values add only their corresponding validated option. The selector is locked
while streaming, so changing it requires disconnecting before the next
controlled stream launch.

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

The system-auth QSF gateway enforces TLS 1.3 server authentication and requires
a signed, expiring ticket on every QSF JSON request; it verifies the exact VM
audience before reading the host-only QSF token from its `0600` file. That token
never travels to the Qt client. The alternative legacy QSF gateway remains
strict client-certificate mTLS; deployments select one gateway mode per VM,
never an optional mixed mode. Use distinct TLS material and trust anchors per
VM profile.

The Qt desktop UI intentionally exposes only the ticket route: gateway host,
port, SNI, CA, required VM audience, and system username/password. It has no
client certificate/key selector and never treats legacy mTLS as a selectable
profile option. The retained mTLS gateway is deployment/CLI/test compatibility
only; use the ticket gateway for this UI.

QSF and GameStream are intentionally separate protocols: clipboard/files and
resize do not travel in GameStream packets. The same system-auth ticket
authorizes both, but the GameStream side performs its own client-owned mTLS
lease exchange and pins the Sunshine certificate; QSF does not receive the
GameStream private key. Explicit logout or a route change stops the native
Moonlight child; ordinary ticket expiry wipes admission for future launches but
does not interrupt media that Sunshine already admitted. The shell locks the
route while active and cancels QSF on Moonlight teardown. Do not configure a
profile to use an untrusted auth or QSF gateway.

## Tests

With `QMDP_BUILD_QT_CLIENT=ON`, `qsunshine_moonlight_controller` starts a
disposable fake Moonlight child to assert the shell's real process contract:
windowed/fullscreen argv, controlled reconnect, prompt QSF-teardown signal,
saved profiles, manual disconnect, native `--qsm-system-auth` launch,
metadata isolation, one-shot ticket-pipe delivery without argv/environment
leakage, disabled PIN fallback, and `--no-quit-after`.

That fake child is currently a POSIX shell fixture, so this particular CTest
is registered on Linux/macOS only. The production Qt client itself has no
POSIX-only process or window embedding dependency.

`qsunshine_qt_qsf_mtls_e2e` drives the production `QsfClient` under the Qt
offscreen platform through the real Python TLS gateway and local QSF broker.
It asserts TLS 1.3 mTLS status, guest-to-client clipboard, client-to-guest
clipboard, upload, download, and negotiated client-to-VirGL geometry:

```bash
ctest --test-dir .build-qt-client \
  -R '^(qsunshine_moonlight_controller|qsunshine_qt_qsf_mtls_e2e|qsunshine_qt_system_auth_client)$' \
  --output-on-failure
```

`qsunshine_qt_system_auth_client` is a separate admission E2E. It drives the
production `SystemAuthClient` through TLS/PAM login, injects the resulting
in-memory ticket into the production `QsfClient`, and reaches a distinct
ticket-only QSF gateway, local broker, and fake guest agent. It then logs out
and proves the ticket, QSF ready lease, and QSettings secret boundary are all
cleared. It then renews the lease and simulates a server-issued expiry becoming
past due, proving the periodic client clock guard also tears QSF down. It
also delivers the same ticket to the in-process native-Moonlight lease sink;
the media-side fixture verifies the later CSR/lease exchange independently.

`qsunshine_qt_profile_cancel_regression` uses the same real mTLS gateway,
broker, guest fixture, `QsfClient`, and process controller. It delays the
authoritative guest profile ACK after `CONNECTION_OPTIMIZE`, requests Cancel,
and proves that the handoff remains busy and launches no replacement Moonlight
process until that terminal reply arrives.

`qsunshine_qt_profile_settlement_recovery` seeds the durable per-profile
recovery record before the coordinator exists and proves that startup blocks
both normal Moonlight/QSF admission paths, then verifies synchronous cleanup
of an expired record without waiting for the production crash window.

## Native Qt/Moonlight/VirGL E2E gate

The current graphics qualification is a composite run, not a mock of the
media process. It uses the exact production `MoonlightController` and
`QsfClient` classes, the required patched Moonlight Qt child, native
system-auth Sunshine, KVM QEMU, and the Alpine Weston/VirGL guest.

First create the pinned patched Moonlight checkout exactly as documented in
[`integration/moonlight/PINNED_UPSTREAM.md`](../integration/moonlight/PINNED_UPSTREAM.md).
The required patch is part of the build input; a clean unmodified upstream
Moonlight checkout is intentionally not a valid native E2E child. Build that
checkout, for example:

```bash
(
  cd .upstream/moonlight-qt-clean
  qmake6 -r moonlight-qt.pro
  make -j"$(nproc)"
)
test -x .upstream/moonlight-qt-clean/app/moonlight
```

Build the Qt targets and a Sunshine tree containing patches `0001` through
`0008`. No server GUI dependency is added: the default native hook uses
`.upstream/build-sunshine-qemu/sunshine`, while any rebuilt package artifact
with the same patch series may be supplied explicitly.

The QEMU runner must be the user that owns the private D-Bus session and has
read/write access to `/dev/kvm` and the render node. Do not use root merely to
work around KVM permissions; root cannot attach to that user's private bus.

```bash
cmake -S . -B .build-qt-client -G Ninja \
  -DQMDP_BUILD_QT_CLIENT=ON -DQMDP_ENABLE_REAL_QEMU_E2E=OFF
cmake --build .build-qt-client --parallel 4
ctest --test-dir .build-qt-client --output-on-failure

SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu/sunshine" \
QSUNSHINE_MOONLIGHT_QT_BINARY="$PWD/.upstream/moonlight-qt-clean/app/moonlight" \
VIRGL_QEMU_RUN_AS="$(id -un)" \
VIRGL_QSF_WAYLAND_QT_QSF_OWNER=qt \
VIRGL_QSF_WAYLAND_PROBE_DURATION_MS=390000 \
VIRGL_QSF_WAYLAND_POST_AGENT_READY_HOOK="$PWD/scripts/run-qsunshine-qt-moonlight-virgl-qsf-wayland-hook.sh" \
./scripts/run-virgl-qsf-wayland-clipboard-e2e.sh
```

The outer runner hands QSF ownership to the Qt driver in this mode. Its raw
Python QSF mutation block is skipped, so exactly one client changes guest
clipboard, inbox/outbox, and display state. It retains an atomic
`qsunshine-qt-e2e-summary.txt` only after every assertion passes, plus client
screenshots and trace under `qsunshine-qt-moonlight-hook/` in the outer run
directory.

The current accepted result is
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.qAMtPU/`. It proves all of the
following together:

- TLS 1.3/PAM login, a VM-audience RAM-only `qsa1` ticket, and a patched
  Moonlight child whose command contains `--qsm-system-auth`. The ticket is
  sent only through its managed FD, not argv, environment, log, or disk.
- A child-owned ephemeral CSR/mTLS lease and pinned Sunshine certificate before
  native HTTPS launch, RTSP/RTP, and H.264 media. The summary records
  `QSUNSHINE_QT_SYSTEM_AUTH_GAMESTREAM_LEASE_OK=1`.
- No PIN fallback: `GET /pair?uniqueid=native-e2e` has an empty body, no
  pairing-state mutation, and the exact generic GameStream not-found payload
  `<root status_code="404"/>`. This protocol encodes the semantic 404 in an
  HTTP 200 envelope, so the test deliberately asserts the XML payload rather
  than only the transport status. The summary records
  `QSUNSHINE_QT_SYSTEM_AUTH_NO_PIN_FALLBACK_OK=1`.
- A non-black `1280x800` `windowed` video presentation, then a controlled
  reconnect to a non-black physical `1600x900@(0,0)` `fullscreen`
  presentation on the disposable `1600x900` client Xvfb root.
- Guest-observed `KEY_A`, absolute-pointer, and button input in the windowed
  stream, then fresh `KEY_B`, pointer, and button evidence after fullscreen
  reconnect.
- TLS 1.3 ticket-authenticated QSF clipboard in both directions, byte-for-byte
  upload and download, two client-selected profiles (`1280x720` and
  `1600x900`), QEMU `SetUIInfo`, and exact Weston/VirGL scanout
  acknowledgements. The second guest download occurs only after fullscreen
  video and normal QSF reactivate.
- Ordered Display1 H.264 geometry evidence `1280x800 -> 1280x720`, nonzero
  DMA-BUF traffic with zero failures, KVM, and an NVIDIA-backed VirGL guest.

Fullscreen presentation and guest scanout are distinct assertions. The client
root/final fullscreen stream is `1600x900`; the guest independently
acknowledges first `1280x720` and then a selected `1600x900` profile. Neither
is inferred from the other or from host GPU hardware. The real-E2E driver
selects `--video-decoder software` only for the disposable `xwd` capture lane,
because an NVIDIA VDPAU surface is not reliably readable by `xwd`; normal
`qsunshine-client` launches retain the `auto` default described above.

`Xvfb` in this command is a disposable client-side drawable solely for patched
Moonlight Qt visual checks. Sunshine is launched with `DISPLAY`,
`WAYLAND_DISPLAY`, and Qt platform variables unset and captures QEMU Display1
through the private D-Bus bus.

The historical `run.FMrGhL`, `run.jxqPBO`, and `run.Jcmdij` Qt traces used
stock Moonlight/legacy pairing or legacy mTLS QSF. They remain ignored local
diagnostics only and are not evidence for this native system-auth route.
