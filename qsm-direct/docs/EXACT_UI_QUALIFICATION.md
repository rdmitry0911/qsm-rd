# Mixed-content desktop qualification, 2026-10-27

Candidate package: `0.4.0+stock4.14~exp19`. The complete mixed-content run used
exp18. Exp19 adds only a bounded frame-metadata send under SCTP backpressure;
that branch has a dedicated regression test. Worker/UI composition code is the
same. This is an experimental candidate, not a stable release or RDP benchmark.

## Method

An owned native Wayland LXC displayed a Qt text editor beside a continuously
FFmpeg-decoded synthetic clip and a separate text window overlapping the clip.
The desktop was 1280×720. Actual H.264/NVENC capture, encoding, WebRTC media and
guest input ran through authenticated TURN/TCP. PVE bootstrap/launch API were
mocked to isolate the media path. No personal desktop content was sampled.

Firefox 157 ran headless on Linux. A real TCP proxy shaped aggregate downlink
to 20 or 2 Mbit/s; producer targets were 8,000 and 1,500 kbit/s respectively.
Automatic target adaptation was disabled for the comparison. This tests fixed
rate operation, not every bandwidth transition or packet-loss distribution.

Each of six cases typed 15 controlled characters and verified the complete
string in the guest. Glyph masks came from the Qt widget's own rendered pixels;
the browser observed the composed video/exact layer once per animation frame.
Marker timing was also collected but is not substituted for glyph timing.
The nearest-rank p95 with 15 samples is the maximum observed sample.

## Glyph appearance

Values are median / p95 milliseconds from browser key event to visible glyph.

| Scenario | Exact UI off | Exact UI + cropped video on |
|---|---:|---:|
| Input beside video, 20 Mbit/s | 113 / 115 | 48 / 70 |
| Input over video, 20 Mbit/s | 165 / 181 | 53 / 71 |
| Input over video, 2 Mbit/s | 186 / 205 | 66 / 107 |

For context, the earlier exp6 exact-UI run measured a 216 ms glyph median in the
same 2 Mbit/s overlapping-window scenario. Individual small-marker timing was
much faster and concealed that text delay; these figures use actual glyphs.
The runs are small controlled samples, not a statistical performance guarantee.

A further burst typed 15 characters at 10 ms intervals into the overlapping
window. All glyphs were visible 98 ms after the final key. The separate
container verification RPC took about 739 ms and is excluded from this timing.
Earlier exploratory burst figures accidentally included that RPC and were
invalid as input-latency measurements.

## Correctness and transport

* All 105 controlled characters matched the guest, including the burst.
* A 65,536-pixel source crop matched exactly; all pixels were opaque.
* The uncovered clip changed in all six sampled video observations.
* Scrolling changed the actual scrollbar position. The separate overlapping
  window resized to 720×400, moved, and closed. Old marker samples never
  reappeared after being cleared in either transition.
* Desktop resize to 1024×768 and back retained matching exact/video geometry.
* All enabled cases confirmed an acknowledged full-frame backing and a real
  cropped encoded area, commonly 640×448 rather than 1280×720.
* The stabilized crop envelope retained coverage. Video overflow/drop counters
  did not grow from the narrow-link typing case through burst, move and close.
  Startup queue recovery still occurs; this is not a zero-drop claim.
* Acknowledged bitmap references, scroll commands and fills were exercised live:
  322 references, 213 scroll copies and 857 fills by the end of the run.
* Real Firefox media/input also worked over a private TURN/UDP relay. With UDP
  unavailable, ICE selected the existing TURN/TCP relay. Selected relay protocol
  and decoded-frame counters were checked; these are not router/NAT tests.
* Dedicated-UID nftables response rules were exercised in an isolated network
  namespace: established listener responses passed, unsolicited UDP was blocked,
  and only an explicitly leased ICE destination tuple passed the relay rule.
  The production host firewall was not changed.
* Real old/new exp6 worker/bridge combinations retained video, exact UI and
  verified guest input in both directions. Original records are preserved
  when the original probe is used.

Source checks cover 27 CTest tests, 11 browser test scripts (including 26,784
modifier routing cases), 91 bridge Python tests and 27 signalling tests. They
include bounded decompression, delayed decode versus clear/resize, queue
coalescing, shared ownership/budgets, cache acknowledgement and immutable scroll
sources, full-frame crop gating, region bounds and UDP startup failure handling.

[Machine-readable samples](exact-ui-qualification-20261027.json) accompany this
report. [Protocol, opt-ins and build instructions](EXACT_UI.md) describe the
implementation. [Gateway instructions](../packaging/debian/README.gateway)
explain optional UDP while retaining TCP/8006.

## Remaining qualification

Equivalent live mixed-content coverage is still needed for VM capture, HEVC,
QSV/VA-API, other browsers and long multi-viewer sessions. Shared capture code
is not a substitute for those tests. Viewer crop gating is unit tested, not a
multi-viewer soak result. Per-application KWin focus/caret/window metadata is
not implemented; the keyboard hint uses the last click location. No Microsoft
RDP comparison has been measured. Production packages remain at exp6; the new
candidate was tested privately without rebooting/stopping guests or replacing
production services. Cropped video and UDP require separate administrator opt-in.
