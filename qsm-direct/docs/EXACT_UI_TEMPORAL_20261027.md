# Exact UI temporal-artifact regression, 2026-10-27

Candidate: `0.4.0+stock4.14~exp32`. Exp29 still produced visible squares,
jumping text and intermittent blur for the user on VM103/Firefox/macOS. Static
pixel equality and average FPS had missed the temporal composition failure.

## Confirmed causes and changes

1. A received newer patch was mistaken for an already painted replacement.
   If that patch was still inflating, presentation of the matching video
   removed its pending clear without clearing the older visible bitmap. The
   compositor now checks the painted bitmap's epoch and serial. Epoch resets
   use the same rule. The new browser regression fails against the committed
   exp29 viewer at the old-square assertion and passes with this fix.
2. A later patch could coalesce away an unsent clear in the sender's per-tile
   map. Invalidations now have a separate bounded map and are sent before
   replacement preparation, including while the decode window is full.
3. Bitmap decompression/cache/scroll analysis happened before checking whether
   the receiver's decode or SCTP window could accept the work. Logs from the
   affected session showed 39 decode jobs and 119 pending tiles, with video
   queue overflows while exact UI was enabled. Guards now precede expensive
   preparation. The regression asserts zero bitmap preparations while blocked,
   while clears continue to make progress.
4. Changing regions were initially published as exact pixels and were refined
   again during brief video pauses. Background now waits for 120 ms RGB quiet;
   established motion retains video classification for one second. Significant
   slow text motion is distinguished from caret-sized changes. Sparse recent
   keyboard edits remain immediate; a settled field replacing video can reclaim
   the interactive path after 120 ms without a significant change. This is a
   bounded heuristic, not semantic window/caret transport.
5. Unused alpha/X bytes cannot invalidate opaque RGB output. Small pending text
   edits briefly retain the unchanged exact glyphs instead of immediately
   erasing their entire tile. Opaque browser fills use native-word filling to
   avoid thousands of per-pixel JavaScript calls; RGBA is endian-independent.

No wire-version change is required. Existing source/bitmap/cache/geometry bounds,
shared bandwidth accounting, input leases and native-video fallback remain in
force. Cropping stays disabled on the affected node.

## Live visual reproduction

An owned native Wayland LXC runs the controlled Qt text/video fixture at 1280x720,
with a real overlapping text window and a continuously decoded synthetic clip.
A known 128x128 movie area alternates black/white at 30Hz for 600ms and pauses 400ms
on each cycle. Exp29 refines that pause, then leaves exact squares over resumed
video. The test records exact data-channel messages for that area and samples
actual overlay alpha plus native-video RGB at four points every 100ms. A stale
sample means an opaque exact pixel differs from the concurrently displayed native
video by more than 90 levels. This tests temporal correctness, not only an idle
snapshot or receipt of packets.

Real H264/NVENC, Firefox 157 Linux and authenticated TURN/TCP pass through a proxy
adding 50ms each way. Downlink is 20 or 2Mbit/s with fixed producer target 8000 or 1500
kbit/s; automatic adaptation is disabled. The PVE launch API is mocked. The wide
candidate deliberately adds 80ms to pixel inflation to exercise pending replacements;
no such artificial delay is added to the baseline. Separate reader/writer queues
avoid accidentally charging the delay for every TCP read.

| Build | Link Mbit/s | Mode | Extra inflate ms | Movie pixel records | Covered samples | Stale samples | Video FPS |
|---|---:|---|---:|---:|---:|---:|---:|
| exp29 | 20 | exact_native | 0 | 156 | 39/130 | 12/130 | 57.9 |
| exp32 | 20 | exact_native | 80 | 0 | 0/130 | 0/130 | 59.9 |
| exp32 | 2 | off | 0 | 0 | 0/128 | 0/128 | 18.1 |
| exp32 | 2 | exact_native | 0 | 0 | 0/128 | 0/128 | 17.6 |

All final enabled cases preserved 65,536 exact source pixels after scrolling and
all 15 typed characters. The moving area received zero exact pixel records and
had zero opaque/stale overlay samples during the probe. Client Off/Active state
was checked against the real server subscription. The worker hash is recorded.
These are short synthetic cases, not a general guarantee or physical macOS
acceptance. No new VM, HEVC, other-encoder, 4K or multi-viewer performance acceptance
is claimed; the repaired core and compositor are shared by those capture paths.

## Checks and rollout

27 CTest targets, 98 bridge Python tests and 27 signalling tests pass. Added checks
cover a pending replacement hiding a clear, epoch resets with pending inflations,
mandatory clear preservation, no bitmap work under backpressure, intermittent
motion, slow scrolling, RGB-vs-X/alpha stability, and foreground recovery.

To reproduce the browser regression against the old viewer:

```sh
git show 528e4fb:qsm-direct/integration/proxmox/pve9/direct_ui/qsm-direct-console.js > /tmp/qsm-exp29-viewer.js
QSM_EXACT_UI_SOURCE=/tmp/qsm-exp29-viewer.js node qsm-direct/tests/proxmox_pve9/test_exact_ui.mjs # expected failure
node qsm-direct/tests/proxmox_pve9/test_exact_ui.mjs # fixed viewer passes
```

The [owned Qt fixture](../upstream/review-exact-temporal-2026-10-27/fixture.py)
contains no personal content. It needs PyQt5, ffmpeg and a looping 640x360 clip at
`/tmp/qsm-exact-ui-clip.mp4`; the diagnostic pause region is generated by Qt itself.

Only the affected node is upgraded, retaining standalone/LXC/TCP8006 gateway and
`QSM_DIRECT_VIDEO_REGIONS=0`. Root-private rollback exp29 packages/configuration
are in `/root/qsm-exact-temporal-20261027/deploy`. QSM transport replacement requires
reopening consoles; host/guest reboot and guest desktop restart are unnecessary.
Reload the Proxmox page to load the corrected JavaScript before checking exact UI.
Mac client confirmation remains pending. No new public release or discussion
message is part of this regression rollout.

[Samples](exact-ui-temporal-20261027.json), [protocol/options](EXACT_UI.md),
[preceding bandwidth regression](EXACT_UI_SMOOTHNESS_20261027.md).
