# Experimental mixed-content desktop

The exact UI layer sends the guest's actual pixels over a reliable WebRTC data
channel. Text, menus and static backgrounds arrive independently of H.264/HEVC
video. It uses neither OCR nor substituted fonts. Capture is shared by the VM
and LXC backends; no additional guest package is required for these changes.

The option remains experimental and **off by default** in the package. Open
**Console settings → Experimental exact UI** in a supported browser. An explicit
browser choice persists, including an unchecked choice overriding an administrator
default. The checkbox requests the mode; the status reports Off, Enabling or Active
from the server acknowledgement. Reload the Proxmox page before opening a new
console to load updated viewer code. Administrators can choose the default in a terminal
service drop-in:

```ini
[Service]
Environment=QSM_DIRECT_EXACT_UI_DEFAULT=1
```

Apply with `systemctl daemon-reload` and a restart of
`qsm-pve-direct-terminal.service`; existing QSM consoles must reconnect. Guests
continue running. Remove the environment setting to restore the package default.
Stable stock4.13 does not implement this mode.

## Delivery and scheduling

Capture damage, mode changes and SCTP buffer notifications wake the sender
immediately. Timers cover bandwidth refill, motion settling and liveness;
there is no 16 ms worker or 20 ms UI bridge polling interval.

The common video/UI budget charges every source UI byte, including invalidations.
Refinement, full snapshots and scroll repaints normally receive 15% of the
current target. Only recent forwarded keyboard activity may borrow up to 50%;
source priority remains bounded near the last guest click for one second.
The ceiling is 512 KiB/s. Bursts are limited to about 20 ms of the complete path,
with a one-maximum-tile floor and a 16 KiB ceiling. A bulk pending queue alone
never authorizes the foreground share. Source UI byte debt is repaid progressively
rather than stopping video to pay an entire tile burst at once.
Video pays by admitting fewer raw pictures; the encoder's static-picture quality
and predictive reference chain are retained. Pending foreground tiles preserve
their priority across refills. Keyboard events temporarily prioritize a bounded
area near the last guest click. This is an input-location hint, not guest focus
or caret metadata; keyboard-only focus changes may fall back to video. Cached replay reserves producer capacity as well;
several viewers share the maximum requested reservation rather than multiplying
it. Old workers retain their original per-viewer 15%/128 KiB/s limit.

The per-peer RTP pacer also charges successfully queued exact records, small
control messages, cursor updates and audio/RTCP. Those updates stay immediate;
subsequent video repays their conservative wire cost, including for an already
encoded large picture. Changing the video target retains outstanding debt.
Large clipboard/result transfers are outside this small-control accounting.
Repeated identical VM cursor notifications are suppressed; the bridge refreshes
the final unreliable cursor position at most four times/s to repair loss.

Each viewer keeps one latest pending pixel update and one latest mandatory
invalidation per tile. A replacement cannot erase the unsent invalidation of
an older visible bitmap. Cheap clears are sent first in bounded batches.
Bitmap inflation/cache analysis waits until SCTP and decoder windows have room;
a full decoder window must not trigger repeated preparation of blocked work. Browser inflation is
limited to eight concurrent jobs and coalesces queued tile history. Processing
acknowledgements report received sequence, outstanding decode work and decoded
bitmaps waiting for their video presentation fence. Waiting bitmaps occupy the
same 32-update feedback window, so network congestion cannot accumulate an
unbounded queue of future snapshots. Bitmap
references require a separate acknowledgement that the bitmap was decoded.
These acknowledgements do not claim physical display or input completion.

## Video/UI composition

A monotonic capture identifier accompanies exact updates and the actual encoded
picture. The bridge maps that identifier to the picture's RTP timestamp. Browsers
that expose `requestVideoFrameCallback` RTP metadata paint new overlay pixels
and release old pixels only after an appropriate video picture is submitted to
the browser compositor. Reliable UI delivery can precede video presentation by
seconds on a congested TURN/TCP connection. Painting those future snapshots
immediately would mix different scenes as visible 64-pixel squares, even though
both streams individually decoded correctly. The browser retains only the latest
decoded bitmap per tile until its corresponding base picture is presented;
newer invalidations, epoch resets and disable discard waiting replacements.
When video is composed into a region canvas, the matching picture must also
have been successfully painted with the expected decoded dimensions before
releasing old exact pixels. Invalid or late region metadata cannot bypass that
fence. Exact text shares that presentation timeline; this mode does not remove
existing video playout latency. Fenced clears compare the epoch and
serial of the bitmap actually painted, rather than the latest received update.
An in-flight replacement therefore cannot keep an older square on top of new
video. Same-size epoch resets use the same painted-bitmap rule. Same-size resets retain backing pixels
until the matching presentation fence; late inflations cannot resurrect old tiles.

Browsers without usable RTP frame metadata negotiate the original tile protocol
and retain full-desktop video. A callback API alone is not treated as evidence
that its RTP metadata is available.

Protocol version 3 adds bounded opaque fills, acknowledged bitmap references,
and vertical scroll copies. Scroll commands read an immutable cached bitmap and
send only newly exposed rows. Copies that overlap therefore cannot read earlier
writes. The per-viewer cache holds at most 256 bitmaps (at most 4 MiB of RGBA);
it is discarded on an epoch change. An unconfirmed bitmap is never referenced.

## Optional cropped video

An additional, separately opt-in experiment is available in the same terminal
service drop-in:

```ini
[Service]
Environment=QSM_DIRECT_EXACT_UI_DEFAULT=1
Environment=QSM_DIRECT_VIDEO_REGIONS=1
```

With version 4 viewers, the worker can encode one bounding region containing
busy/incompressible tiles instead of the entire desktop. One encoder/session
is retained. Static pixels outside the region use the exact layer and the last
full desktop backing. Pointer coordinates, resize and zoom retain desktop
coordinates. Clears outside a presented crop wait for coverage or a replacement
exact patch.

Cropping starts only when **every viewer** supports/enables the mode and has
acknowledged a presented full desktop. A legacy viewer, disabled exact layer or
new unready viewer restores full video. Invalid, large, empty or odd-sized crop
candidates use full video. The region grows to retain coverage and does not shrink with per-frame tile
classification; mode or desktop changes reset it. Changing crop dimensions
requires a fresh encoder picture and can briefly reduce video cadence. Leave this experiment disabled
for installations that have not qualified those transitions. A separate
`QSM_DIRECT_VIDEO_REGIONS=0` override disables it for new sessions. Even full
fallback in a primed region session uses a browser canvas; native video plus
exact UI is the current conservative configuration for the Firefox/macOS
regression. Reopen the console after changing the service environment.

This implementation uses pixels, not window or text semantics. In nested Plasma,
the outer Sway sees the nested KWin surface; per-application focus, caret and
window metadata would require KWin-side integration. That integration is not
implemented here. The current generic path works without it.

## Bounds and transport

* Tiles are 64×64 pixels. Maximum surface is 8,294,400 pixels; each dimension
  is at most 16,384. Larger desktops continue through video without an overlay.
* Worker payloads are bounded zlib-compressed opaque RGBA or exact grayscale,
  at most 6,144 bytes. There is no chroma subsampling or quantization in tiles.
* Background tiles must remain RGB-stable for 120 ms before refinement.
  Two significant changes within one second, or three rapid changes within
  75 ms, select video for one second after the last motion. This prevents
  short playback pauses from repeatedly acquiring exact squares. Small caret
  edits can retain exact backing briefly; recent keyboard focus permits sparse
  text updates immediately and can reclaim a field after 120 ms without a
  significant change. Larger background text motion is not treated as a caret.
  Classification is still a pixel heuristic, not guest window/caret semantics.
* Unused source alpha/X changes do not invalidate opaque RGB output.
* The shared compressed cache is limited to 16 MiB. Per-viewer SCTP buffering is
  bounded to 16 KiB. Slow viewers do not accumulate desktop history.
* Gaps and missing worker heartbeats invalidate source caches. Decode errors
  disable the optional layer. Geometry, compression, cache and copy bounds are
  checked before drawing.
* The `qsm-exact-ui` data channel uses SCTP/DTLS. Video/audio remain SRTP. Input
  keeps its existing independent control and pointer channels.
* TCP/8006 gateway operation needs no new port. An optional TURN/UDP listener
  can be configured separately; see [README.gateway](../packaging/debian/README.gateway).

## Protocol and upgrades

Private `SOCK_SEQPACKET` UI records use flag `0x80` and header
`!4sBBHIIHHHHHH`: `QUI1`, operation, encoding, extension length, epoch, serial,
surface dimensions and tile rectangle. Operations 0–4 are HELLO, RESET, PATCH,
CLEAR and HEARTBEAT. Encoding 0 is RGBA; 1 is grayscale PATCH.

Private input opcode 10 uses off=0, on=1, resync=2, original probe=3 and
capability probe=4. A new bridge first tries 4, then falls back to 3 if needed.
An original probe receives the original 28-byte HELLO. Capability HELLO adds a
four-byte bitmap: shared budget=1, capture stamps=2, cropped video=4. Workers
emit no new UI records until probed. Opcode 11 reserves 0–50% UI capacity;
opcode 12 disables cropping (0), primes full-frame region metadata (1) or
permits cropping (2). Both are private server controls, never guest input.

Stamped UI records have extension length 8 and a network-order capture u64
before their payload. Timed video retains its timestamp prefix, adding capture
u64 under flag `0x100`, and six u16 surface/region values under `0x200` after a
new bridge explicitly primes that protocol. Encoder PTS associates metadata
with the submitted picture; metadata is not sampled later from the latest frame.

Browser negotiation retains `qsm_ui_mode`; new bridges advertise `max_version`.
Old viewers receive their original unstamped RESET/PATCH/CLEAR records. Version
2 adds capture stamps and processing feedback; 3 adds FILL=6, STORE=7,
REFERENCE=8 and SCROLL=9; 4 adds region composition and full-frame acknowledgement.
All cache resources are local to a peer and epoch. Video/input compatibility
with old workers/bridges remains part of upgrade testing.

## Build and qualification

Build on Debian 13 with `zlib1g-dev` and the existing package dependencies:

```sh
export QSM_DIRECT_DEB_VERSION='0.4.0+stock4.14~exp32'
for profile in qsm-pve-direct qsm-pve-direct-standalone qsm-pve-direct-lxc qsm-pve-direct-gateway; do
    bash "packaging/debian/build-$profile-deb.sh"
done
cmake -S . -B build-exact-ui -G Ninja -DBUILD_TESTING=ON
cmake --build build-exact-ui
ctest --test-dir build-exact-ui --output-on-failure
PYTHONPATH=. python3 -m unittest discover -s tests/browser_bridge -v
PYTHONPATH=. python3 -m unittest discover -s tests/signal -v
node tests/proxmox_pve9/test_exact_ui.mjs
```

Live qualification includes owned native Wayland LXC and QEMU VM capture,
H.264/NVENC/QSV/VA-API, and HEVC with an actual Chromium hardware receiver.
Check real glyph appearance, exact source pixels, composed visible motion,
scrolling, move/resize/close and old pixel resurrection. Compare identical shaped
paths and video targets. There has been no measured comparison with Microsoft RDP.

The [VM qualification report](VM_MIXED_QUALIFICATION_20261027.md) records current
coverage, input/cursor/cache/painting fixes and limits. The
[historical LXC qualification](EXACT_UI_QUALIFICATION.md) retains its original
samples and transport checks.

The [subsequent smoothness regression report](EXACT_UI_SMOOTHNESS_20261027.md)
records the real 100 ms RTT / narrow-channel comparison and scheduling changes.

The [temporal-artifact regression](EXACT_UI_TEMPORAL_20261027.md) reproduces old
squares over a moving picture and an unpainted replacement defeating a clear.
It supersedes any implication that static-pixel/FPS checks proved visual correctness.
