# Exact UI smoothness regression, 2026-10-27

Candidate: `0.4.0+stock4.14~exp29`.

The user reported that Experimental exact UI reduced video smoothness and text
clarity in Firefox/macOS on the NVIDIA node, VM 103. The previous glyph/pixel
qualification did not adequately measure video presentation cadence at realistic
RTT. It must not be treated as evidence that the option is faster for that client.

## Reproduction and measured scope

An owned native Wayland LXC displayed the controlled Qt text/video fixture at
1280x720, including a text window above a continuously decoded 640x360 synthetic
clip. Real NVENC/H.264, WebRTC and authenticated TURN/TCP ran through a proxy
adding 50 ms each way. Separate bounded reader/writer queues avoided accidentally
adding 50 ms for every TCP read. Aggregate downlink was 20 or 2 Mbit/s, producer
target 8000 or 1500 kbit/s, with automatic adaptation disabled.

Firefox 157 ran on Linux. Each case used a fresh peer, measured roughly 12 seconds
of actual requestVideoFrameCallback timing, and scrolled the owned text window.
Geometry and received guest scrolling were checked; initial setup runs with a
covered clip or a scrollbar already at its limit were discarded. These short
non-paired samples have differing clip phases/history, and are not a benchmark
or a macOS acceptance result. The PVE launch API was mocked; media and input were
real. New VM performance, HEVC, other GPU encoders, 4K and a long WAN soak were
not qualified by this regression run.

| Build | Link Mbit/s | Mode | Presented video FPS | Maximum gap ms | Gaps >100 ms |
|---|---:|---|---:|---:|---:|
| exp26 | 2 | off | 17.4 | 88 | 0 |
| exp26 | 2 | exact_native | 15.0 | 266 | 10 |
| exp26 | 2 | exact_crop | 12.3 | 281 | 23 |
| exp29 | 2 | off | 18.3 | 88 | 0 |
| exp29 | 2 | exact_native | 16.1 | 108 | 7 |
| exp29 | 20 | exact_native | 58.9 | 33 | 0 |

The final native-video cases also verified 65,536 exact RGBA source pixels after
scrolling and all 15 typed characters, with no duplicated/lost text. Off/Active
status was checked against the actual server subscription. Machine-readable
samples preserve the tested worker hash and numeric server counters.

The narrow-channel result still has a video cadence cost for UI bytes. The
change reduces large stalls; it does not establish zero cost, a 100 ms bound,
or RDP parity. Client-specific text presentation on macOS remains to be checked
by the user. Cropped mode stays disabled on the affected node; its prior Linux
qualification did not establish acceptable Firefox/macOS composition cost.

## Changes

* A large snapshot or any already-seen changed tile used to authorize half the
  shared bandwidth. Bulk snapshots, scrolling and ordinary refinement now use
  the background 15% share. Only actually forwarded keyboard activity grants a
  one-second borrowing lease; source priority is bounded near the last click.
  Heartbeats, pointer changes and releases cannot renew that keyboard lease.
* Producer and peer burst credit now represents about 20 ms of the complete
  path, with a maximum-tile floor and 16 KiB ceiling. Idle time cannot accumulate
  a large narrow-link UI burst. Cache replay reservation respects the current
  refinement share rather than reserving 50% for arbitrary pending work.
* Fresh source UI previously debited the entire byte cost from video admission
  immediately. It now repays debt progressively with wire/liveness headroom,
  shared with cached replay rather than double-charged. Unpaid debt survives a
  rate decrease without being silently clamped away. Video access-unit debt
  and the existing per-peer transmission accounting remain in force.
* Settings distinguish the requested checkbox from Off / Enabling / Active
  based on server confirmation. Active's update counter shows decoded exact
  records; it is not a physical-display acknowledgement. An explicitly saved
  unchecked preference continues to override the administrator default.
* Ten-second numeric health logs now include actual UI state, UI kbit/s,
  pending/refinement/decode queues, buffered bytes, gaps and region eligibility.
  They contain no pixels, typed characters, credentials or peer addresses.

Checks: 27 CTest targets, 96 bridge Python tests, and 27 signalling tests. New
regressions cover bulk-vs-typing reservation, low-rate burst bounds, keyboard-only
priority renewal, smooth source debt repayment and retaining unpaid bytes across
a rate decrease. Existing exact pixels, invalidation, input and composition tests
remain passing.

## Use and deployment

The checkbox controls the exact raster layer; cropping is a separate service
opt-in. Keep `QSM_DIRECT_VIDEO_REGIONS=0` on the affected node for this rollout.
Reload the Proxmox page, then open a new QSM console to load the new viewer.
With the checkbox selected, the status should say Active and its update count
should rise. Off means ordinary video; Enabling means confirmation is pending.

Only the affected NVIDIA node is upgraded in this regression rollout, preserving
its standalone/LXC/gateway profile and existing TCP/8006 configuration. QSM
service replacement closes console transports; no host/guest reboot or guest
desktop restart is needed. Root-private configuration backup and rollback exp26
packages are at `/root/qsm-exact-regression-20261027/deploy`. Other nodes remain
on exp26 pending client evaluation. No public release/APT or new external message
is part of this fix.

[Samples](exact-ui-smoothness-20261027.json), [protocol/options](EXACT_UI.md),
[preceding VM qualification](VM_MIXED_QUALIFICATION_20261027.md).
