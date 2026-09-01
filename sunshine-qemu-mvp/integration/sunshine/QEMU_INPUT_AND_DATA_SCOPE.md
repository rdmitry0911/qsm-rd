# QEMU input, clipboard, and file-transfer scope

This document records the contract of the `capture=qemu_dbus` adapter pinned
by this repository.  It deliberately distinguishes working GameStream input
from features that require a client extension; the stock Moonlight protocol
must not be represented as supporting capabilities it does not carry.

## Keyboard and mouse

When QEMU Display1 capture becomes active, Sunshine's global keyboard and
mouse wrappers route to `/org/qemu/Display1/Console_0` over the same private
D-Bus session used for capture.  They do not submit those events to
libvirtualhid or the Sunshine host.

The adapter requires QEMU Display1 `Keyboard` and `Mouse` interfaces during
startup.  It maps the Windows virtual-key values emitted by Moonlight to QEMU
`qnum` values using QEMU's `keycodemapdb` Win32-to-AT-set-1 mapping, including
the special encoding for extended keys.  Calls are queued asynchronously, so a
wedged VM cannot block the GameStream control reader.  `input::reset()` already
releases every tracked key and mouse button through the same wrappers on client
disconnect, stream replacement, and shutdown.

For a VM with `Mouse.IsAbsolute=true`, absolute Moonlight positions use
`SetAbsPosition`. Moonlight's SDL frontend normally emits *relative* deltas,
but QEMU rejects `RelMotion` for an absolute device. The adapter therefore
scales those deltas into the current scanout coordinate space, accumulates and
clamps a virtual cursor, then sends `SetAbsPosition`. It resets that cursor on
a new `Scanout` geometry and updates the negotiated client coordinate space on
`SetUIInfo`. `RelMotion` is used only when QEMU reports a relative pointer.
If a client instead sends absolute positions to a relative-only QEMU device,
the adapter maintains a virtual position and emits relative deltas instead of
falling back to the host cursor. Vertical wheel input is converted to QEMU
wheel press/release pairs. Display1 has no horizontal-wheel operation, so
horizontal scroll is intentionally consumed and reported once. Touch, pen, and
gamepad forwarding remain separate layers and are blocked from leaking to the
host while QEMU capture is active.

At display shutdown the adapter emits a stable diagnostic line:

```text
[qemu-dbus] input stats: relative_calls=N relative_nonzero=N absolute_calls=N button_calls=N queued_rel=N queued_abs=N dropped_no_geometry=N
```

For an absolute QEMU device, a real SDL-relative run must have both
`relative_nonzero > 0` and `queued_abs > 0`; an explicit SDL-ungrab absolute
run must have `absolute_calls > 0` and `queued_abs > 0`. These counters
distinguish a client that generated no motion from an adapter that sent an
invalid `RelMotion`; guest evdev motion remains the end-to-end assertion.

Moonlight's standard UTF-8 text packet is supported only for printable ASCII
using the guest's US-layout key positions.  Non-ASCII text is rejected rather
than being typed into the Sunshine host or silently corrupted by an assumed
guest keyboard layout.

## Input E2E gate

`tests/run_qemu_input_e2e.sh` runs a TCG-only QEMU fixture and sends
`Keyboard.Press(0x1e)` then `Keyboard.Release(0x1e)`.  The guest reads raw PS/2
set-1 make and break bytes and emits the exact debugcon marker
`INPUT_PRESS_RELEASE_OK`.  This tests guest delivery of both press and release,
not merely D-Bus method acceptance.

The Moonlight end-to-end gate must reuse that marker after generating physical
`A` input in its SDL client window.  For an absolute pointer use a tablet-only
QEMU configuration, for example:

```text
-machine pc,i8042=off,usb=on
-device qemu-xhci,id=xhci
-device usb-tablet,bus=xhci.0
```

The usual relative mode works with QEMU's default PS/2 devices.

`tests/fixtures/qmdp_input_reset_ack.S` is the separate disconnect contract.
Its driver must send **only** physical `A` down, wait until the guest has
consumed the make byte, then terminate the Moonlight client without a client
key-up.  Within 10 seconds of disconnect it must observe
`INPUT_RESET_RELEASE_OK` in the QEMU debugcon log.  The expected break comes
from Sunshine's queued `input::reset()` release-all; no particular Sunshine
log line is an adequate substitute for this guest-visible acknowledgement.

## Resolution requests

The adapter sends a bounded best-effort `Console.SetUIInfo` request using the
stream dimensions and a 96-DPI physical size.  A successful reply only means
QEMU accepted the UI preference: the guest video driver remains authoritative
for the actual scanout mode.  `qemu_dbus_request_ui_size(width, height)` is
available to a future authenticated resize companion.  A mode change is
qualified only after a new Display1 `Scanout` reaches the client at the requested
resolution; fullscreen is a client presentation change, not a QEMU resolution
guarantee.

For example, QEMU 8.2.2 accepts this request for `virtio-vga` and reports
`org.qemu.Display1.Error.Unsupported` for standard VGA.  Both outcomes are
normal; the adapter records them and continues capturing the guest's current
scanout.

## Clipboard and files: protocol boundary

QEMU Display1 offers a bidirectional `org.qemu.Display1.Clipboard` D-Bus
interface (`Register`, `Grab`, `Request`) between its UI client and the guest.
That is a viable guest-side clipboard transport, but it is **not** a Moonlight
clipboard protocol.

Stock `moonlight-common-c` has keyboard, mouse, controller, touch, pen, and
UTF-8 text input packets; it has no bidirectional clipboard or file-transfer
packet.  Moonlight Qt's familiar paste shortcut sends client clipboard text as
a UTF-8 input event, which is one-way typing rather than clipboard
synchronization.  Therefore this adapter does not claim guest-to-client copy,
rich clipboard MIME data, drag-and-drop, or file transfer.

The implemented QSF companion closes the constrained desktop use case without
claiming a GameStream extension:

1. A per-session local `0600` Unix socket carries a random 256-bit token; an
   optional TLS 1.3 mTLS gateway reads that token locally and does not transmit
   it to the remote companion.
2. The Python broker and static guest agent enforce non-NUL UTF-8 clipboard
   data up to 1 MiB. Guest-state changes become a broker `EVENT_CLIP`; the
   supplied client retrieves them with `clipboard-get` polling rather than an
   unsolicited remote-push API.
3. Files use QEMU virtio-serial, 2 MiB binary payload limits, plain safe
   basenames, atomic guest writes, and independent endpoint SHA-256 evidence.
4. A guest-only Weston DRM bridge maps constrained QSF text to real
   `wl-copy`/`wl-paste` selections and suppresses feedback loops by hash.

The native composite validates both clipboard directions, both file directions
and the guest desktop mode transition alongside a real Moonlight session. It
does not provide rich MIME data, drag-and-drop, user-consent UI, cancellation,
or an interoperable Moonlight clipboard packet.

The QEMU interface specification is the primary source for Keyboard, Mouse,
SetUIInfo, and Clipboard semantics:
https://www.qemu.org/docs/master/interop/dbus-display.html
