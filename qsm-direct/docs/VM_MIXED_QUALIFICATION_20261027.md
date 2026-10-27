# VM mixed-content qualification, 2026-10-27

Candidate: `0.4.0+stock4.14~exp26`. The recorded mixed-content runs used
exp23/exp24/exp25 as individually recorded in the JSON. Exp26 contains the same
runtime source as exp25, plus this report and the reproducible fixture. Exp25
commits cursor deduplication state only after successful private publication and
charges exact/audio/control traffic to the video transmission budget. This report is a private-lab
qualification, not an RDP benchmark or a stable-release announcement.

## Coverage and method

Two dedicated Debian 13 QEMU VMs displayed an owned Qt editor, a continuously
FFmpeg-decoded synthetic clip and a separate text window overlapping the clip.
NVIDIA capture used Display1/VirtIO GL and NVENC on an RTX 3080. A separate colocated NVIDIA/HEVC run used an RTX 4080
Laptop with CPU-readable VM capture; Intel used the
Display1 CPU-readable path and actual QSV/VA-API on Raptor Lake Iris Xe. No CPU
encoder fallback was configured. PVE bootstrap/launch API was mocked; encoding,
WebRTC, authenticated TURN/TCP, composition and guest input were real.

The desktop was 1280×720. A TCP proxy shaped aggregate receiver downlink to
20 or 2 Mbit/s, with fixed producer targets of 8,000 or 1,500 kbit/s. Automatic
adaptation was disabled for these comparisons. Firefox 157 ran on the controller.
Chromium 147 ran on the Intel lab host with an owned Xvfb/profile and Intel ANGLE
Vulkan; its shaper and burst driver were local to that receiver. Earlier CDP
keyboard measurements crossing SSH per key did not represent a 10 ms burst and
are excluded. Different browser/encoder routes must not be ranked as a benchmark.

Each ordinary case typed 15 controlled characters. Real Qt glyph masks were
observed in the composed browser video/exact layers, with guest string checks.
The driver requested 15 characters at 10 ms intervals, and recorded actual
browser keydown timestamps; scheduling can lengthen the burst. The field was
focused by the owned fixture without an additional racing USB pointer click. Its reported interval ends
when all glyphs are visible and begins at the final browser keydown; subsequent
guest verification RPC time is excluded. HEVC negotiated `video/H265`; increasing
Intel DRM video-engine counters were measured only in the owned Chromium cgroup,
confirming hardware decoder activity rather than an H.264 fallback.

| Actual encoder | Source / route | Receiver | Ordinary cases | Burst: final key to all glyphs |
|---|---|---|---:|---:|
| h264_nvenc | RTX 3080 / inter-host | Firefox 157 | 6 | 237 ms |
| h264_qsv | Iris Xe / lab route | Firefox 157 | 6 | 342 ms |
| h264_vaapi | Iris Xe / lab route | Firefox 157 | 6 | 450 ms |
| hevc_nvenc | RTX 3080 / inter-host | Chromium 147 | 1 | 634 ms |
| hevc_nvenc | RTX 4080 / colocated | Chromium 147 | 1 | 428 ms |
| hevc_qsv | Iris Xe / lab route | Chromium 147 | 1 | 397 ms |
| hevc_vaapi | Iris Xe / lab route | Chromium 147 | 1 | 394 ms |

Full H.264 runs contain six scenarios, exact mode off/on beside a clip and off/on
overlapping it at both link rates: 105 verified characters including the burst.
HEVC qualification uses the narrow overlapping exact-mode scenario plus the burst:
30 verified characters. Both NVENC and QSV/H.264 passed six scenarios with exp25. The full VA-API/H.264
case predates the final transmission accounting; VA-API/HEVC was repeated with
exp25. Each tested source revision is preserved in the machine-readable record.

NVIDIA/H.264 glyph median / nearest-rank p95 (milliseconds, 15 samples per case):

| Scenario | Exact off | Exact on |
|---|---:|---:|
| Beside clip, 20 Mbit/s | 113 / 491 | 48 / 114 |
| Overlapping clip, 20 Mbit/s | 180 / 461 | 51 / 137 |
| Overlapping clip, 2 Mbit/s | 186 / 278 | 70 / 194 |

The NVIDIA/HEVC inter-host run passed pixel, input and window-transition checks,
but took 634 ms after the last key of the burst, exceeding the 500 ms lab
performance target. A colocated RTX 4080 run took 428 ms; earlier colocated trials
also measured 550–578 ms, and an earlier inter-host run measured 633 ms. This is
variable performance, not a consistently met 500 ms bound. A QSV/HEVC exp25
trial also reached 505 ms (its exp24 run was 352 ms). The trace showed
exact updates arriving before final composed glyphs, so local classification,
decode and presentation also need profiling before attributing every stall to
network capacity. H.264 remains the better-qualified path for this workload.

These small samples include startup/individual stalls; p95 is the maximum of
15 observations. They are not a latency guarantee or a Microsoft RDP comparison.

## Correctness

Every listed run verified 65,536 opaque source pixels without differences and
observed real visible clip motion in all six samples of the composed image.
Scrolling changed the guest scrollbar. The input window resized to 720×400,
moved, and closed without any old marker reappearing after the controlled guest
transition. A guest-rendered token prevents counting compression noise before
the actual move as a resurrection. Desktop changes to 1024×768 and back kept
video/exact geometry aligned. Sway's output mode was explicitly changed through
its IPC for this test; this does not establish automatic Display1/EDID resizing.

All enabled cases confirmed acknowledged full backing and an actually smaller
encoded video region. Source cache/scroll/fill commands were exercised live.
Encoded startup/reconfiguration may still cause bounded queue recovery; no
zero-drop or zero-interruption claim is made.

A real two-viewer run verified single-viewer cropping, full fallback for a new
or disabled viewer, and cropping after both enabled viewers acknowledged full
backing. Throttling the second viewer to 1 Mbit/s left the first viewer moving
in all 12 samples. Closing the second left the first connected and moving in
all 12 samples. A grown envelope may legitimately retain full-video fallback;
returning to a crop after every disconnect is not required for correctness.

## Issues found and corrected

* A clean QEMU USB keyboard overflowed its 16-entry scancode queue four times
  during the unpaced burst. A lost release caused native repeat and affected
  later typing. QEMU keyboard edges, including modifiers, are now spaced by
  10 ms locally; the first edge after idle remains immediate. This bounds a
  queued plain tap stream to about 50 characters/s. Network latency cannot
  stretch an atomic tap into a native hold. This pacing is not applied to LXC
  virtual input. Previously corrupted test USB devices were replugged only in
  the owned lab; reconnecting a viewer does not promise to repair an already
  corrupted USB device. A paused or overloaded guest can still stop consuming
  HID events; this is not a guest-acknowledged input protocol. See upstream [HID queue](https://github.com/qemu/qemu/blob/master/include/hw/input/hid.h)
  and [USB polling descriptors](https://github.com/qemu/qemu/blob/master/hw/usb/dev-hid.c).
* VM Display1 repeated identical cursor state hundreds of times per second,
  consuming narrow-link headroom. Identical successful private publications
  are suppressed. The bridge sends real changes immediately and refreshes the
  final unreliable position at most four times/s, including on an idle screen.
  Failed private publication remains retryable; cursor-channel backlog is bounded.
* More than 32 invalidations used to reset the entire exact cache, discarding
  neighboring static text. Invalidations now continue in bounded immediate
  batches. The regression animates 96 tiles while preserving 32 static tiles
  and the original cache epoch.
* A crop-size transition could report an RTP presentation callback before its
  base canvas accepted the matching decoded dimensions. The exact layer now
  waits for actual successful painting of that RTP picture. A coupled browser
  regression covers rejected dimensions and late metadata. The failing full
  NVIDIA run had eight resurrection samples; the accepted exp24 run had zero.

* Source UI admission already reserved video capacity, but an encoded IDR could
  still fill TURN/TCP ahead of text. Successful exact records, small control
  messages, cursor and audio/RTCP now debit the per-peer video transmission
  pacer. They remain immediate; later video pays the conservative wire cost.
  Rate changes retain existing debt. Large clipboard/result transfers are not
  included in this small-control budget. Regressions cover idle credit, UI
  preemption of encoded video, successful-send accounting and debt across rate
  changes. This fix does not establish a HEVC latency bound.

Source checks pass 27 CTest targets, including 11 browser scripts and 26,784
modifier-routing cases, 94 bridge Python tests and 27 signalling tests. The
new tests cover cursor duplicate/loss repair/backpressure, bounded invalidation
without unrelated cache resets, and video/exact painting fences.

## Limits and deployment

No 4K throughput measurement, Microsoft RDP comparison, macOS/Safari acceptance,
long viewer soak or arbitrary WAN-loss distribution was measured here. No AMD
mixed-content qualification is included. KWin application/focus/caret semantics
remain future work; this implementation transports actual pixels and bounded
raster commands. UDP remains a separate opt-in; these acceptance runs used TCP.

Keep the existing installation profile when upgrading matching packages. The
package default for exact UI remains off; existing administrator/browser choices
persist. Cropped video requires a separate administrator opt-in and should stay
disabled on unqualified paths. QSM service replacement requires console reconnect;
production guest/host restart is not part of this upgrade. Guest payloads are
unchanged by these media fixes. Package smoke/deployment records are separate
from the private mixed-content samples.

[Machine-readable samples](vm-mixed-qualification-20261027.json),
[protocol and opt-ins](EXACT_UI.md),
[historical LXC acceptance](EXACT_UI_QUALIFICATION.md).
