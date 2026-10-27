# Exact UI prototype qualification

Package set: **0.4.0+stock4.14~exp4**, five Debian packages built together in
the Debian 13 build environment. This is an experimental source/package set;
stable APT and the production installation were not replaced.
See [the design and controls](../../docs/EXACT_UI.md).

## Automated checks

* **27/27 CTest tests pass**, including the new C++ tile encoder, Python
  protocol and JavaScript renderer tests. [Output](ctest.log).
* **82/82 browser-bridge Python tests pass** under Debian 13, including bounded
  decompression, worker gaps/heartbeats, enable/disable cache invalidation,
  two-viewer ownership/latest-state isolation, replay bandwidth limits, and
  fresh-input/clear priority. [Output](bridge-tests.log).
* Matching core, standalone, gateway, LXC and guest debs were built; the core
  contains the worker, bridge module, new tile module and viewer JavaScript.
  [Package hashes](packages.sha256), [tested source hashes](source-fingerprints.json).

The isolated Python test namespace had a private non-loopback veth, because
ICE cannot gather a usable candidate from an environment with loopback only.
It had no Internet route and changed no host firewall rules.

## Real guest/browser test

An owned test LXC ran a Qt application decoding an H.264 clip with FFmpeg.
A separate floating Qt text window covered part of the playing clip. The
underlying application remained 1280×720 while that window was visible.
The [fixture source](fixture.py) contains only synthetic text and counters.

The normal complete QSM viewer JavaScript ran in headless Firefox 157 with
OpenH264 2.6.0, a real private media worker using H.264/NVENC, the real bridge,
and authenticated TURN/TCP through the existing node's TCP 8006 gateway.
Production services and installed packages were not modified for the test.
The test page stubbed only the PVE launch/settings API; capture, encoding,
WebRTC, browser rendering and keyboard/pointer delivery were real.

A local TCP proxy limited the actual downlink to 20 or 2 Mbit/s. The video
budget was held at 8000 or 1500 kbit/s respectively, with adaptation disabled
for the off/on comparison. This is a bandwidth test with local-network RTT,
not a simulated high-RTT Internet/VPN or packet-loss benchmark.

Every case typed 15 controlled, distinct characters through the actual viewer
input path. The guest verified the complete resulting string. A guest-drawn
colored marker updated on each text edit; browser pixels measured key event
to first visible marker response. Thus these numbers measure visible guest
response to input, **not** end-to-end OCR timing of every glyph. Each sample
used the composited exact canvas when opaque, otherwise decoded video.

| Case | Exact UI | Median response | p95 of 15 samples |
| --- | --- | ---: | ---: |
| Text beside clip, 20 Mbit/s | off | 108 ms | 109 ms |
| Text beside clip, 20 Mbit/s | on | 59 ms | 94 ms |
| Text window overlapping clip, 20 Mbit/s | off | 157 ms | 175 ms |
| Text window overlapping clip, 20 Mbit/s | on | 59 ms | 108 ms |
| Text window overlapping clip, 2 Mbit/s | off | 176 ms | 223 ms |
| Text window overlapping clip, 2 Mbit/s | on | 75 ms | 124 ms |

The first row should be read together with the raw sample array in
[the recorded run](firefox-mixed-content.json); it is one small laboratory
run and not a general latency guarantee.

Additional acceptance results from the same run:

* All **90 characters** match the controlled input, with no extra/repeated
  characters and no browser page errors.
* A 512×128 text-area crop in the overlapping window has **65,536/65,536
  pixels exactly equal** to a Qt source grab, including the alpha channel.
  Full opacity of that sample took an additional **4.47 seconds** after
  the input cases at the narrow target. This remains a prototype limitation.
* The uncovered video area produced **six distinct pixel samples out of six**,
  taken 200 ms apart, while the text overlay was active. This proves visible
  motion, not preserved 60 fps at a 1.5 Mbit/s video target.
* Wheel input advanced the text window scrollbar from 3 to 6. Resizing that
  window to 720×400 was acknowledged by the guest.
* Moving and closing the window removed its old marker. The marker was
  initially visible and finally absent; 2.3-second animation-frame traces
  detected **zero reappearing samples** after first disappearance. Each check
  sampled the marker center and two uniquely painted corners, over the moving
  clip. Raw traces are included in the run JSON. This checks those sampled
  pixels and does not prove artifact-free occlusion for every application.
* Viewport resizing to 1024×768 and back to 1280×720 kept video and overlay
  geometry in agreement. The connection stayed connected; worker UI gap count
  remained zero. Video queue drops/overflows were initial handshake counters
  (202/4) and did not grow during the input/move/close cases.

## Upgrade compatibility

Both live combinations used the new viewer and existing stable stock4.13
components without replacing installed files:

* **New bridge + old worker**: decoded video and all 15 typed characters work;
  new support is unavailable and the exact-UI checkbox stays disabled.
  [Result](compat-old-worker.json).
* **Old bridge + new worker**: decoded video and all 15 typed characters work;
  the worker sends no new media flag because the old bridge never probes.
  The exact-UI checkbox stays disabled. [Result](compat-old-bridge.json).

## Remaining coverage

Equivalent live VM/QEMU, HEVC, QSV, VA-API and libx264 mixed-content runs are
still required before a default-on release. Also unqualified are real tablet
and macOS interaction, multiple concurrent real browsers, high-RTT/lossy
Internet links, 4K desktops, rapid occlusion, translucent windows and video
backlog during loss recovery. Common-path/unit coverage does not replace
those tests. There is no measured RDP comparison, CopyRect or cropped-video
implementation in this prototype.

## Lab cleanup

The owned fixture was stopped and the original 1280×720@60 output restored.
No private worker/bridge process remained. Host boot ID, VM states/PIDs and
container states matched the saved baseline. All four production QSM services
remained active, with installed stock4.13 packages. [Result](lab-cleanup.json).
FFmpeg and its ten new dependencies were added only to the owned test LXC
for clip decoding; no guest packages were upgraded or removed.
