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
A packet of at most 256 bytes pays too but does not wait: a nearly empty picture
only keeps capture stamps moving, and holding it overflows the four-picture queue,
whose repair is a whole independent picture (80–100 KB over text, 370 ms at 2 Mbit/s).
Repeated identical VM cursor notifications are suppressed; the bridge refreshes
the final unreliable cursor position at most four times/s to repair loss.

A cursor image is cropped to its visible pixels and hotspot, since compositors hand
out a fixed 64×64 buffer (22 KB once encoded), and defined only once per viewer. The
worker numbers every change while a compositor alternates a few images, so the
bridge reuses the id the viewer already holds. It mirrors the viewer's eviction: 32
images for version 7 and 4 for older viewers.

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
admission check. Only the bounded old-flight exception described below may
retain earlier headroom. Normal admission preserves encoded payloads,
timestamps and predictive references; the negotiated refresh below replaces
an obsolete uncommitted chain only at an independent picture.

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

The private confirmed-refresh candidate negotiates source feature 16 before
using server-only opcode 15. The request correlates a path target and 64-bit
repair token. A complete local AU carries that token and its configured codec
target; the bridge accepts an independent picture at the requested cropped
rate, or an explicit fresh full-desktop fallback. The request survives reset
quiet time. Ordinary codec hysteresis and fixed full-desktop quality remain.
Credit waiting now precedes RTP sequence/history assignment, so obsolete
uncommitted work can retire without holes; committed fragmented AUs finish.
A separate 25ms/3450-byte packet hint may hold the next AU for 75ms once per
congestion episode. It grants no ACK or rate and cannot retune the codec.
The initial native119 run passes all 16 unchanged gates, with 360.7ms source-age
excess and 117ms maximum source gap. A hold was not observed before that run's
first cut, so the pass does not establish that hint's causal benefit. Repeat
and remaining qualification are recorded in the
[confirmed-refresh report](rdp-confirmed-refresh-20261029.json). This is still
a private candidate; full matrix/fidelity/browser/release work remains.

The private 120 host separates packet evidence from full-frame cadence. A
packet-confirmed repair and its bounded pre-RTP hold also work when complete
frames arrive slowly or only presentation feedback is available. Retained
old-flight arithmetic keeps its original rapid-receipt guard. A frame ACK
wake cannot end an active packet-evidenced hold before its deadline. The
198-test target-SDK suite and normal/presentation-only LXC/NVENC profiles pass;
source-age excess is 313.4/347.3ms and recovered cadence 50.94/54.00fps. Actual
forwarded pixels confirm those ages. Native 120, repeated/fidelity/matrix and
release work remain; see the [packet-cadence report](rdp-packet-cadence-20261029.json).

The private121 host also accepts a bounded growing received-frame hint before
the next packet report; it changes no bitrate or image acknowledgement. The
200-test suite passes and the LXC/NVENC transition passes all13 gates, but the
native QSV run still fails source freshness and final rate recovery. Hard-cut
SSIM/cadence and component cadence also fail; functional component pixels pass
only the completed fast phase. The passive trace exposes repeated packet hints
while the overuse filter is NORMAL. These are private diagnostics, not release
qualification; see the [frame-admission report](rdp-frame-admission-20261029.json).

Private122 requires the estimator overuse trend for a packet-only early hold.
The growing matched-frame hint remains independent. The201-test suite passes;
the fast hard-cut trace has no early holds, but ordinary flight-credit waits
still occupy5.53s of20s. Fast43.49/narrow43.39fps and narrow SSIM.879 fail the
unchanged gates. Native/full transition and remaining qualification stay open;
see the [packet-trend report](rdp-packet-trend-20261029.json).

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
fence. Exact text shares that presentation timeline. Fenced clears compare the epoch and
serial of the bitmap actually painted, rather than the latest received update.
An in-flight replacement therefore cannot keep an older square on top of new
video. Same-size epoch resets use the same painted-bitmap rule. Same-size resets retain backing pixels
until the matching presentation fence; late inflations cannot resurrect old tiles.

Browsers without usable RTP frame metadata negotiate the original tile protocol
and retain full-desktop video. A callback API alone is not treated as evidence
that its RTP metadata is available.

The experimental receiver can also present pictures from a cloned remote track
through `MediaStreamTrackProcessor`, when each decoded `VideoFrame.metadata()`
provides its original RTP timestamp. It uses the same region, capture and layout
fences; it does not instantiate another decoder. At most two waiting pictures
are retained, at most one is drawn per animation frame, and waiting pictures
older than 50 ms are discarded. The displayed picture remains alive for a late
matching region map. A missing map cannot acknowledge an undrawn picture.
Disabling exact UI, hiding or closing the console releases the reader, cloned
track and retained pictures. Missing APIs, missing RTP identity, a stopped or
stalled reader, or a drawing error fall back to HTML video. The original media
track remains intact, and an older HTML picture cannot move the presentation
fences backwards when that fallback takes over. [Audio/video offset](rdp-av-sync-20261105.json)
was measured on the container sound path. Performance and browser/codec qualification
remain open in the RDP plan; this implementation is not a qualification or rollout claim.

On an initial offer, the bridge can negotiate a separate SCTP transport for
input and exact UI while keeping RTP audio/video bundled. It does so only when
the application section has a nonzero port, unique MID and no `bundle-only`
requirement, as permitted by [RFC 8843 section 7.3.2](https://www.rfc-editor.org/rfc/rfc8843.html#section-7.3.2).
Mandatory BUNDLE and ambiguous layouts retain the offered grouping. The gateway
already leases all negotiated ICE transports; both relay connections use the
existing TCP/8006 listener, so one forwarded TCP port remains sufficient. UI and
video still share the same byte budget. This avoids placing guest input behind
an unrelated RTP TCP flight without changing its authentication or protocol.

Protocol version 3 adds bounded opaque fills, acknowledged bitmap references,
and vertical scroll copies. Scroll commands read an immutable cached bitmap and
send only newly exposed rows. Copies that overlap therefore cannot read earlier
writes. The per-viewer cache holds at most 256 bitmaps (at most 4 MiB of RGBA);
it is discarded on an epoch change. An unconfirmed bitmap is never referenced.

Protocol version 6 adds region copy (private candidate, not qualified). When
scrolled or dragged pixels already displayed exactly reappear at a new position,
the worker sends one COPY=10 record instead of repainting the area. A hash search
and an axis search only propose a motion vector. The worker keeps a per-pixel
model of what the viewer holds after every record. A tile relies on the copy only
after its new pixels compare equal to that model moved by the vector, and every
pixel it needs is known to be opaque there. A wrong guess therefore costs a repaint
and cannot display wrong pixels, and a late strip at a window's edge cannot stop
the interior from moving by copy.

Ordering is strict because a copy reads the viewer's canvas. The bridge holds a
copy behind older tile work for that viewer and coalesces later tiles only behind
that copy; more than 128 waiting copies request a fresh epoch. The browser waits
for earlier inflations, paints bitmaps still waiting for a video fence if the
copy touches them, performs the copy, then resumes queued records. A copy is not
fenced by video. Strip tiles exposed by a copy carry flag 4 and are also unfenced,
skip the 120 ms settling delay and may use the 50% share for 300 ms. Other exact
tiles keep their capture fences.

After a copy the bridge's compressed tile cache no longer describes the desktop,
so a new or recovering viewer requests a fresh worker epoch instead of replaying
it. Copy requires version 6 on every exact viewer, region mode and source feature
32 (probe 6, private opcode 16). `QSM_DIRECT_UI_COPY=0` disables it for new
sessions. After the first copy of an epoch, video keeps the last encoded value
under tiles the viewer holds exactly, so moved pixels do not also consume video
bitrate; capture stamps continue and a tile is released as soon as it stops being
exact. One motion vector is used per capture. Fractional or animated motion, and
motion faster than the exposed strips can be sent, remain on the video path.

With history a strip waits for bandwidth for up to 2 s instead of reaching video, since
it is re-encoded fresh at low cost while video would be paid for on top of the repaint.
Without history an exposed strip may wait up to 400 ms when the copy was isolated.
While copies keep arriving it hands over to video after the ordinary 120 ms, for
150 ms renewed by each further change, so scrolled pixels return to exact soon
after motion stops rather than after a clip's one-second hold. A pointer press, release, wheel event or key press gives the first change of each
tile up to 300 ms the same immediate, unfenced path when at most 32 tiles changed.
A second change during that event, or a larger repaint, keeps the ordinary path and
its capture fence, so an animation or a clip started by that click still reaches
video and a large repaint cannot appear as mixed 64-pixel squares.

The video target says nothing about a path while video is idle, and SCTP over
TURN/TCP sees no loss. Version 6 therefore limits exact bytes in flight from the
viewer's own acknowledgements: when even the fastest acknowledgement of a round
exceeds the 30-second minimum by more than 60 ms the window shrinks, directly to what
the path delivers in one round trip, and being blocked without a queue grows it, within
16–256 KiB. The fastest one is used because a real
path has jitter and a busy viewer delays some replies, while only a standing queue
delays them all. One record always makes progress, keystroke responses are never held
back and silence expires after two seconds. After a seen queue, while this limit holds
tiles back, private opcode 17 gives the worker the measured delivery rate, so strips
it cannot afford reach video instead of queueing. A window that is merely small does
not count, because it limits the rate it measures.
The worker uses the slowest viewer's rate. aiortc's SCTP window is kept at least
as large, since it would otherwise stay near 3.6 KB per round trip for sparse bursts.
The viewer replies at most every 10 ms. Each reply draws an SCTP acknowledgement back,
and one pair per record cost a narrow path more than a small record does.
A cut of the video target adds what exact records recently took of the path to video's
own delivered rate. Idle video beside busy tiles otherwise states a small part of the
path as all of it (663 kbit/s on 2 Mbit/s). Video has its own TCP connection, so even
this can be low, and with history the worker's rate for strips and input responses
ignores the target while no tile is busy: only a delivery rate reported above binds it.

Protocol version 7 adds pixel history (private candidate, not qualified). A text
tile deflated alone costs about 1.3 KB at 64×64, although the same glyphs and whole
rows have usually been sent already: its rows repeat at a fixed distance, only
across messages. With flag 8 a PATCH references the exact pixels of earlier records
by distance, in a ring of 4 M pixels (about four 720p screens). Its payload is a u32
inflated size and a deflated `checksum, ops size, distances size, ops, distances,
R…, G−R…, B−R…`, and `src/core/pixel_history.hpp` documents the tokens. Measured on
a 660×290 text region: 85 KB became about 20 KB for the benchmark's repeated lines,
and 150 KB about 45 KB for prose that does not repeat. FILL, STORE, REFERENCE and
SCROLL are unnecessary then, because a repeated or uniform tile is one match.

Decoding depends on every earlier record, so record order is the same for every
viewer and nothing may be dropped, replaced or overtaken. Once an epoch has a history
record the bridge queues everything for that viewer strictly in order, without its
tile map or its bitmap cache. The worker already paced those bytes; the bridge adds
only the flow window, the decode window and SCTP's buffer, since a second pacer would
build a queue that cannot be coalesced. More than 256 KiB or 2048 queued records
request a new epoch. A viewer that holds everything since the worker's RESET may
start immediately; a later one must wait for a new epoch, since these bytes mean
nothing without their history. A viewer without version 7 lowers the whole session
to version 6 and its plain records through opcode 16 (mode 2 is copies and history).

The browser starts inflation immediately and applies tokens in record order, before
any paint decision, so a tile replaced or cleared before it paints still extends what
later records reference. A checksum over each tile's pixels, like any other
inconsistency, disables the layer rather than painting wrong pixels. Worker and viewer
restart history at every RESET. Source feature 64 and probe 7 negotiate it;
`QSM_DIRECT_UI_HISTORY=0` disables it for new sessions.

On a wlroots source the worker asks for twice the capture rate on the output, up to
240 Hz, and capture still starts at most at the requested rate. The nested compositor
and the capture each wait for that output's next frame, so its period counts more than
once in the latency of every input: 120 Hz took about 15 ms off every first response
on the PVE container lane. VM sources keep their own display rate.

Cropped hardware encoders have a QP floor of 20 and do not use CBR: NVENC and QSV
would otherwise keep refining a static crop at a few KB per picture and emit bursts
of large pictures for invisible detail, and AMD VA-API pads CBR up to its target.
VA-API also uses VBR and this floor for an uncropped picture while regions are on, since
a desktop under exact tiles may stay uncropped until something moves: AMD otherwise
spends all 8 Mbit/s on a static picture.
Crops are at least 192×128 because AMD VA-API refuses to encode below 128 pixels.
`QSM_DIRECT_CROPPED_QMIN` and `QSM_DIRECT_UI_FREEZE=0` are private diagnostics.

## Optional cropped video

The video plane grows with the area in motion and does not shrink, since reopening an
encoder costs an independent picture. Only motion that itself covers 80% of the desktop
for a second leaves the plane for the whole picture at original quality, for at least 5
seconds. A window dragged across the screen only widens the envelope, up to all of it,
and stays a plane that follows the path. See `src/core/video_envelope.hpp`.

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
node tests/proxmox_pve9/test_decoded_presentation.mjs
```

With exact UI the codec target follows the path even when motion covers the whole
picture. At a fixed target a full-screen clip at 2 Mbit/s fell to 12 frames per second
with each picture as large as on an open path; it now holds about 43. Without exact UI
the target stays fixed. See [the record](rdp-full-screen-narrow-20261105.json).

A moving window shadow over changing content is new blended pixels on every frame,
which copy detection and pixel history cannot reuse. On a VM desktop with text behind
the window and a 2 Mbit/s path, Breeze shadows raised median drag lag from 177-184 ms
to 300-426 ms and left the picture behind after release. `qsm-desktop-effects reduce`
in the guest turns them off and `restore` returns the user's setting; see
[the record](rdp-narrow-path-20261105.json). Like the display effects Microsoft RDP
offers to disable, this is the user's choice: the console never changes it alone.

Release qualification must cover owned native Wayland LXC and QEMU VM capture,
H.264/NVENC/QSV/VA-API, and HEVC with an actual Chromium hardware receiver.
Hardware decoding inside a VirGL guest is not usable: see [the record](rdp-virgl-hardware-decode-20261105.json).
HEVC decodes directly on a real Intel receiver but does not yet pass the clip gates:
see [the record](rdp-hevc-receiver-20261105.json).
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
