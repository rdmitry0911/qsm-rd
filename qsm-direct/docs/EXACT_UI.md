# Experimental mixed-content desktop

The exact UI layer sends the guest's actual pixels over a reliable WebRTC data
channel. Text, menus and static backgrounds arrive independently of H.264/HEVC
video. It uses neither OCR nor substituted fonts. Capture is shared by the VM
and LXC backends. The pixel-based path works with existing guest tools; optional
expanded guest metadata improves input-field priority and window tracking.

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
Without usable guest metadata, source priority remains bounded near the last
guest click for one second. Supported guest producers can instead identify
the focused input component and caret.
The ceiling is 512 KiB/s. Bursts are limited to about 20 ms of the complete path,
with a one-maximum-tile floor and a 16 KiB ceiling. A bulk pending queue alone
never authorizes the foreground share. Source UI byte debt is repaid progressively
rather than stopping video to pay an entire tile burst at once.
Video pays by admitting fewer raw pictures; the encoder's static-picture quality
and predictive reference chain are retained. Pending foreground tiles preserve
their priority across refills. Keyboard events temporarily prioritize a bounded
area near the last guest click when guest focus/caret metadata is unavailable;
keyboard-only focus changes then use the ordinary-pixel fallback. Cached replay reserves producer capacity as well;
several viewers share the maximum requested reservation rather than multiplying
it. Old workers retain their original per-viewer 15%/128 KiB/s limit.

The per-peer RTP pacer also charges successfully queued exact records, small
control messages, cursor updates and audio/RTCP. Those updates stay immediate;
subsequent video repays their conservative wire cost, including for an already
encoded large picture. Changing the video target retains outstanding debt.
Large clipboard/result transfers are outside this small-control accounting.
Repeated identical VM cursor notifications are suppressed; the bridge refreshes
the final unreliable cursor position at most four times/s to repair loss.

For VirGL capture, a pending `ScanoutDMABUF` and its following `UpdateDMABUF`
share one GPU readback. A five-ms deadline starts readback if no damage call
follows. Replacement scanouts retain that deadline; disabling the display or
switching to an inline/mapped scanout cancels the pending buffer.

Before encoding, VM and LXC retain at most two fresh captures, bounded to
64 MiB and two negotiated frame intervals. This preserves short capture
bursts that a single latest-image slot could discard between encode ticks.
Backpressure, keyframe recovery, layout/size changes and mode switches discard
that history; the latest image remains available independently. Exact UI still
uses the latest capture, while video carries the selected image's own capture
stamp. This small queue does not accumulate a congestion backlog.

A confirmed cropped-codec rate change may admit one fresh predictive picture
before resetting the encoder if its preceding access unit exceeds 100 ms of
the current path budget. This separates an expensive scene picture from the
reset's keyframe. It postpones the boundary only once; normal byte admission,
RTP pacing, rate hysteresis and explicit keyframe repair remain authoritative.
The full-desktop fixed-quality policy is unchanged.

With pre-decoder receipt feedback, a moderate delivery slowdown needs matched
old receipts spanning 100 ms before a short credit wait may lower the path
target. A clock timeout cannot supply the missing receipt. This avoids
treating one repaired TCP segment as newly lost capacity.
A delivery interval below half the offered rate retains its immediate
response; sustained credit blocking, RTCP loss and legacy feedback retain
their existing checks. Raw admission remains bounded during confirmation.

For a rapid receipt-feedback viewer, admission measures the complete next
access unit, including its actual packetized RTP headers and conservative
wire overhead. It reserves the bytes pacing can emit during one 16-ms
feedback interval; small access units reserve their complete cost. Larger
pictures remain serialized by the packet pacer, so their whole size does not
add an RTT of credit waiting before that serialization. The window uses the
current path target and receipt baseline. A rate change wakes the pending
admission check; a partial acknowledgement cannot keep the preceding
high-rate window. Encoded payloads, timestamps and predictive references are kept.

When the peer negotiates transport-cc, authenticated RTCP packet receipts also
release video flight credit. Video, RTX and bundled audio share transport
sequence numbering; audio does not acknowledge video bytes. RTP extensions
are reserved before pacing, with the final sequence assigned at dispatch.
The packet history is limited to 16,384 entries and eight seconds. Missing
video packets remain holes until packet delivery or a host-matched complete
frame proves repair; history eviction cannot acknowledge a missing packet.
Packet credit expires after 1.5 seconds without new video receipt evidence.
Complete-frame acknowledgements still fence pixels. A peer without
negotiated/fresh packet feedback retains the complete-frame rate/credit path.
Packet feedback never acknowledges a layout or image. Bounded send timing
remains available after a complete-frame ACK, so a later transport report can
supply each packet's actual arrival interval once.

The private packet-capacity controller uses a recent receiver-clock interval
of at least 100 ms and 9,200 wire bytes. The installed aiortc interarrival
filter supplies an overuse trend; excess transit and slower packet delivery
must agree before a reduction. Moderate slowdowns retain a 100 ms transit
allowance; a delivery rate below half the offered load can act at 50 ms.
A gap longer than 50 ms rebuilds the delivery window. QSM retains active
recovery probes, shared/legacy guards and RTCP loss handling. An
application-limited estimate alone cannot impose a low recovery ceiling.
Codec dwell/quiet and full-desktop quality are unchanged.

The sender and receiver clocks have different origins. Packet timing uses
within-clock intervals and relative transit changes, never uncalibrated
absolute one-way latency. Packet arrival does not claim decoder completion
or physical display. The initial native108 trial passed 15 of 16 unchanged full-profile gates,
including full-rate recovery, but still fails the 400 ms source-age excess
limit because obsolete encoded frames wait before dispatch. It remains a
private, unqualified candidate. See the
[packet capacity report](rdp-packet-capacity-20261029.json) and the earlier
[transport credit report](rdp-transport-credit-20261029.json).

For a packet-confirmed halving with rapid receipt feedback, the private
candidate retains an admission ceiling bounded by its previous baseline
window and actually outstanding video bytes. Pacing adopts the new path
rate immediately. Actual prefix receipt, missing packet credit or 1.5 seconds
retires the old allowance. Complete-frame/image ACKs and codec quality remain
independent. Existing bounded loss-repair deadlines still apply; this is not
a hard no-overshoot guarantee. Native111 passes 15 of 16 original gates but
still has 461.7 ms source-age excess, above 400 ms. Forced old-rate keyframes,
one-off headroom spending, raw-only early pressure and a smaller configured
video window did not qualify and are not retained. See the
[flight-drain report](rdp-flight-drain-20261029.json). These private candidates
are not a qualified release.

Frame-aware waits permit one baseline round trip, bounded to 50–250 ms, with
one deadline across all acknowledgements. Short healthy waits keep raw capture
history; a wait lasting one short media tick applies the bounded raw lease.
Long waits renew that lease every 50 ms and release it on completion or
cancellation. A future encoded picture held behind a frame being paced or
awaiting credit for one short media tick pauses raw admission; normal short
pacing overlaps preserve the bounded capture history. An idle consumer can
receive its first picture without pausing capture. The independent two-frame
guard stays immediate, and close cancels a pending one-frame pause.
Legacy feedback and senders without a complete-frame reservation retain their
previous admission behavior. Audio, UI, cursor and RTX stay independent.

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

While exact UI is enabled, the browser combines the presented base picture and
exact pixels into one sRGB canvas at guest resolution, before applying CSS scaling
and the monitor colour transform. Independent hardware-video and canvas layers
can otherwise differ in colour conversion or fractional scaling. This client
composition requires an additional canvas copy; disabling the option restores
the ordinary video presentation path. Cropped video retains its full-desktop
base canvas, including pixels outside the current crop. UI-only redraws use that
presented base rather than sampling an unfenced future media-element frame.

The encoder describes the original desktop as BT.709/sRGB primaries and transfer,
with limited-range YUV and the existing BT.601 conversion matrix. Both libavcodec
and the FFmpeg process path explicitly configure the conversion and signal these
values; VA-API uploads retain the frame tags. This avoids asking decoders or the
display pipeline to guess the colour space of an untagged video. The exact layer
uses an explicitly sRGB canvas. These tags do not make 4:2:0 video pixel-exact.

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

## Optional guest metadata

The expanded `qsm-desktop-agent` for VMs and `qsm-console-guest` for LXC carry
bounded, versioned desktop metadata over the existing guest connection.
The KWin session helper reports window geometry, stacking, visibility and focus.
Supported accessibility producers and the optional Qt5/Qt6 input-geometry plugin
add caret and input-component bounds. GTK accessibility and unavailable
producers retain conservative fallbacks. Window titles and field text are not
required or transferred by this metadata interface.

In nested Plasma, outer Sway captures the desktop while the helper inside the
KWin session supplies application geometry. The host intersects the focused
component with visible window bounds; actual changed pixels must fit the
priority area. The browser still draws the guest's actual pixels. Metadata
helps scheduling and occlusion handling, rather than substituting fonts or
rendering application text locally.

Metadata has bounded sizes, sequence/session checks and an expiry lease.
Missing, expired or old-agent metadata restores the pixel-based path. Support
for metadata producers does not establish performance qualification for every
application, encoder or browser. See the [current plan](rdp-next-plan.json),
[VM metadata checks](rdp-vm-metadata-20261028.json),
[Qt geometry checks](rdp-qt-input-geometry-20261028.json) and
[GTK media checks](rdp-gtk-media-20261028.json) for their actual scopes.

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
  Pixel classification remains necessary even with advisory guest geometry.
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

Private input opcode 14 carries a one-byte raw-video backpressure lease of
0–100 ms; zero releases it. The bridge uses a 75 ms lease around a frame-credit
wait of at most 50 ms and releases it in `finally`, including cancellation.
Several consoles retain independent leases. The worker keeps the newest raw
scanout replaceable, continues UI/audio/input work and repays existing encoded
byte debt while paused. The lease expires locally if the bridge disappears;
old workers ignore the unknown opcode. Browser JSON cannot request a lease.
Two queued encoded pictures also pause raw admission under a separate owner.
Packet pacing refreshes that lease every 50 ms; draining below two pictures
or closing the track releases it before the local input socket is closed.

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

Release qualification must cover owned native Wayland LXC and QEMU VM capture,
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
