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
`ERR UNKNOWN_COMMAND`; the host tries once per connection and keeps
clipboard working.

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
Unsupported accessibility APIs leave `caret` null. Window titles and text
contents are never read or included in the protocol.

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

The work is not yet qualified on a real VM virtio port or KWin 6. It is not
yet connected to an early-paint UI path. Those checks and mixed text/video
benchmarks remain in `rdp-next-plan.json`; the metadata tests alone do not
prove a latency improvement or completion of the plan.
