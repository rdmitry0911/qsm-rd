# Advisory desktop metadata

This is the guest-agent part of the RDP responsiveness work. It does not
replace capture, transmit application text, or independently authorize an
exact UI patch to paint over a video frame. Capture/layout synchronization
and pixel fallback remain necessary.

## Transport and compatibility

The existing QSF1 stream carries both clipboard and optional metadata. A
new host first sends `DESKTOP_SUBSCRIBE 2`, falling back to
`DESKTOP_SUBSCRIBE 1` if the agent rejects version 2. A new agent replies
`OK DESKTOP_SUBSCRIBE`, then emits `EVENT_DESKTOP <base64-json>` as state
changes. `DESKTOP_SUBSCRIBE 0` stops those events. `-` revokes unavailable
state. Version 2 adds bounded input-component clipping; version 1 retains its
original strict schema. Nothing is sent without subscription. Older agents can return
`ERR UNKNOWN_COMMAND`; the host tries once per agent incarnation and keeps
clipboard working. `READY QSF1` revokes old hints and rearms subscription:
QEMU can retain the same host socket across a guest-agent restart. A greeting
which arrives during the first subscription cannot suppress the next probe.
Clipboard user actions are not replayed.

VMs use the existing `org.qsm.direct.agent` virtio-serial port. LXC uses the
existing session `agent.sock`. No additional listener or forwarded port is
introduced. Browser input stays on its existing independent channel.

## Producer

`guest/qsm_desktop_metadata.py` owns `org.qsm.DesktopMetadata` on the desktop
session bus. Only that bus's current `org.kde.KWin` owner may publish window
state. A second bridge cannot replace the first bridge or its state. The
bridge loads only its own KWin script and unloads only that script on exit.

KWin 5 exposes each window's stack index; its `clientList()` is creation
order. KWin 6 also exposes the complete stack. The metadata list is ordered
from bottom to top. It includes opaque window IDs, process IDs, frame/client
rectangles, visibility, opacity, focus, desktop bounds and scale. Layout
changes increment a generation. Window damage is advisory and coalesced
over 50 ms; it is not a replacement for capture damage.

An asynchronous compositor pulse keeps the script lease alive. Script loss
revokes hints within about three seconds and reloads only the QSM script.
KWin 5 can reuse a D-Bus script ID after unloading a non-last script. The
bridge therefore uses the public scripting `start()` dispatcher, which
does not restart already running scripts, rather than addressing that
possibly colliding ID. It does not change enabled-plugin settings.

Optional AT-SPI events provide a small priority area near the focused text
caret. On Wayland, window-relative coordinates are translated using the
focused window's client origin; pretending they are desktop coordinates is
incorrect on Qt 5. An empty end-of-text box can fall back to the adjacent
character's bounds. KWin 5 uses a contained surface buffer to establish the
client origin; an ambiguous origin, such as an external client-side shadow,
leaves relative caret geometry unavailable. This box is a scheduling hint,
not a local text renderer.
An empty focused text control can supply its bounded component rectangle.
Unsupported accessibility APIs leave `caret` null. Window titles and text
contents are never read or included in the protocol.

The application must expose its accessibility interface. On Qt 5 desktops
with AT-SPI disabled, registering the QSM listener alone may leave it
unavailable: the live default-settings test retains window geometry but
reports `caret: null` and uses ordinary pixel scheduling. A supported Qt
backend can request accessibility with `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`.
That override is not a guarantee of successful AT-SPI registration: the
native Qt 5 cases below still fail with it. Applying the variable only to
the metadata helper does not activate it in other applications. Qt documents
the environment override and AT-SPI status properties in its
[accessibility API reference](https://doc.qt.io/qt-6/qaccessible.html).

The session bridge writes `desktop-metadata.json` atomically, privately, and
with a one-second heartbeat. The VM agent watches the directory with
inotify; the LXC agent watches it through Qt. Both refuse stale, public,
non-regular or symlink payloads. The host validates the full schema, bounds
the JSON to 48 KiB / 128 windows / 64 damage IDs, drops sequence replays and
layout regressions, and revokes cached hints on expiry or disconnect.

Screen locking suppresses all live windows, focus, damage and caret data.
Missing metadata never disables the ordinary video/pixel path.

## Verification status

The private CT108 lab uses KWin 5.27.11 and a separate owned D-Bus/Wayland
session. It exercises geometry, real stacking order, visibility, focused
text accessibility, removal, rejected publishers, duplicate bridge startup,
script loss/recovery, and the actual LXC-agent-to-host channel. The C VM
agent's subscription, change events, safety bounds and legacy-host behavior
are exercised over a Linux PTY with the actual compiled binary.

A dedicated Debian 13 / KWin 6 VM exercises the real virtio serial port and
standard graphical-session units. Its Qt 6 character bounds contain the
independent native insertion point before and after moving the window. Stack,
focus, hide/close, real screen locking, new graphical login, agent restart and
guest reboot are covered in `rdp-vm-metadata-20261028.json`. Installation also
checks an already existing port's permissions, failed-service recovery and
preservation of an active agent's PID during repeat installation.

Native Ubuntu 24.04 / KWin 5 also exercises the real SDDM/PAM login path.
Login starts metadata automatically; ordinary Plasma logout revokes it and
returns the greeter; logging in again creates a new metadata session on the
same media worker and WebRTC connection. Outer Sway and SDDM retain their
PIDs. Evidence and limitations are in `rdp-native-lxc-20261028.json`.

The Ubuntu and Debian Qt 5 native Wayland fixtures did not appear in the
AT-SPI registry even with the accessibility override. Qt 5 reports a
disconnected registry connection, while a separate Qt D-Bus probe can reach
that same registry. This does not establish an upstream cause. The optional
Qt input-method provider below supplies an independent source of geometry;
ordinary pixel scheduling remains available.

Private exp42 trials of the priority UI path in `hybrid-priority-ui.md` cover
visible typing over video, low-rate byte pressure and independent pixel
checks after moving and closing the input window. A separate run with Qt
accessibility disabled verifies ordinary fallback. Native Qt input geometry
and the broader mixed-content/media matrix are tracked in `rdp-next-plan.json`;
these metadata tests alone do not prove completion of the plan.

## Optional Qt input geometry

`qsm-qt-input-geometry` contains separate Qt 5 and Qt 6 generic plugins. They
query input enablement and the public
[window-relative cursor rectangle](https://doc.qt.io/archives/qt-5.15/qinputmethod.html)
without requesting surrounding text, window titles or input-event contents.
The local D-Bus service exposes only an opaque per-window token, sequence,
enablement, client size, cursor bounds and the input item's clipping rectangle.
The host restricts text priority to that component, so video in the same
application cannot acquire text retention merely by being near the caret.
Tile retention also requires the actual changed pixels to lie inside that
area; a tile centre alone cannot prove that a change belongs to text.

The helper atomically writes both `desktop-metadata-v2.json` and the original
version-1 file. Agents select the file requested by their host. Legacy views
omit component-bounded caret hints which an older host could otherwise expand
over unrelated video; window geometry and ordinary pixel scheduling remain
available. AT-SPI caret hints without a component retain their original schema.

The metadata helper resolves the service to its unique D-Bus owner, verifies
that owner's process and user credentials against the focused KWin client,
and requires an exact client-size match. A well-known service name alone is
insufficient authentication. Layout changes cancel outstanding queries;
responses from an older generation cannot restore a caret. A 2.5-second lease
rejects stopped publishers. Locking, window closure and disabled input revoke
the hint. Live Qt geometry takes precedence over asynchronous AT-SPI events.

Plasma sources `/etc/xdg/plasma-workspace/env/qsm-qt-input-geometry.sh` during
the next graphical login. It preserves existing generic plugins and adds
`qsm-input-geometry` once. To exercise an owned application in an existing
session, set `QT_QPA_GENERIC_PLUGINS=qsm-input-geometry` when starting that
application. Existing processes do not load the new plugin retroactively;
package installation does not restart applications or the desktop.

Build the plugin in a guest SDK with both `qtbase5-dev`, `qt6-base-dev`, CMake,
a C++ compiler and `dpkg-dev`. The tested portable build uses Debian 12's
Qt 5.15 and Qt 6.4, then runs on Ubuntu 24.04 and Debian 13. Use one explicit
version for the plugin, VM agent and LXC packages:

```sh
export QSM_DIRECT_DEB_VERSION=YOUR_VERSION
export QSM_DESKTOP_DEB_VERSION="$QSM_DIRECT_DEB_VERSION"
bash packaging/guest/build-qsm-qt-input-geometry-deb.sh
bash packaging/guest/build-qsm-desktop-agent-deb.sh
bash packaging/debian/build-qsm-pve-direct-lxc-deb.sh
```

The LXC host builder requires the matching Qt deb in its output directory
(or `QSM_QT_GEOMETRY_DEB`). It bundles that deb with the container agent and
the installer explicitly installs both inside the container. Since it now
contains an architecture-specific payload, the LXC host package uses that
architecture rather than `all`. The PVE host does not load the Qt plugins.

The clean Trixie driver also requires that prebuilt portable Qt deb via
`QSM_QT_GEOMETRY_DEB` (or its output directory). It builds the VM agent and
LXC payloads with the same explicit version and retains the Qt deb for guest
installation. Building the Qt plugins against Trixie's newer Qt minor version
is not the Ubuntu 24.04 portability check described here.

Installed-package VM checks cover Qt 5 and Qt 6 with accessibility disabled:
native cursor bounds, movement, resize, a second noneditable window in the
same process, publisher stop/resume and metadata-helper restart over the same
virtio connection. The LXC bandwidth-transition run uses the Qt provider and
observes actual glyph pixels. Remaining queue peaks and the full application,
codec and browser matrix are separate gates; these checks do not qualify a
public release.

## Older GLib bindings and helper recovery

The original Gio closure registration API transfers an invocation reference
which older PyGObject versions fail to release. Frequent window damage exposed
this as native memory growth in Ubuntu 24.04's metadata helper, eventually
reaching the LXC memory limit. The compatibility registration releases that
reference only when the actual callable is the old unwrapped
`g_dbus_connection_register_object_with_closures` entry point. Existing Python
overrides and the newer `closures2` API keep their own ownership handling.
This follows the [upstream PyGObject fix](https://raw.githubusercontent.com/GNOME/pygobject/3.50.0/gi/overrides/Gio.py).

The regression test uses a real private D-Bus connection and native RSS
accounting; Python allocation accounting alone misses this leak. It also
checks delayed replies with GLib critical errors made fatal. The older Ubuntu
path and Debian 13's already corrected registration both pass. The LXC session
agent now retries a failed metadata child after five seconds and cancels that
retry on normal shutdown. Clipboard and media remain independent.

Thirty minutes of the installed exp50 helper under continuous owned video
damage use approximately 18–20 MiB RSS on the same process/session, without
new OOM events or metadata availability loss. This removes the previous
large native leak. It is a metadata resource check, not a thirty-minute
end-to-end encoded mixed-content soak; broader session checks remain open.
