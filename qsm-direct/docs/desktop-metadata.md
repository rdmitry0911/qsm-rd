# Advisory desktop metadata

This is the guest-agent part of the RDP responsiveness work. It does not
replace capture, transmit application text, or independently authorize an
exact UI patch to paint over a video frame. Capture/layout synchronization
and pixel fallback remain necessary.

## Transport and compatibility

The existing QSF1 stream carries both clipboard and optional metadata. A
new host sends `DESKTOP_SUBSCRIBE 1`; a new agent replies
`OK DESKTOP_SUBSCRIBE`, then emits `EVENT_DESKTOP <base64-json>` as state
changes. `DESKTOP_SUBSCRIBE 0` stops those events. `-` revokes unavailable
state. Nothing is sent without subscription. Older agents can return
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
that same registry. The failure is still under investigation; ordinary
pixel scheduling remains available. This differs from the supported Qt 5
path in the isolated Ubuntu/KWin 5 lab.

Private exp42 trials of the priority UI path in `hybrid-priority-ui.md` cover
visible typing over video, low-rate byte pressure and independent pixel
checks after moving and closing the input window. A separate run with Qt
accessibility disabled verifies ordinary fallback. Native Qt 5 accessibility
and the broader mixed-content/media matrix remain in `rdp-next-plan.json`;
these metadata tests alone do not prove completion of the plan.

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
