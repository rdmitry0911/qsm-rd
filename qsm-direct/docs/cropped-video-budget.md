# Cropped video and the shared path budget

The full-desktop fallback retains its configured encoder quality. Bandwidth
changes still affect raw-frame admission before encoding, preserving predictive
references and avoiding periodic changes in static text sharpness.

A genuinely cropped plane, negotiated with the exact UI layer, can instead
spend fewer coded bits per moving picture. Previously its encoder stayed at
the original maximum even when the path budget fell. The byte limiter then
admitted fewer pictures; cropping could make cadence worse than full-frame
video because the smaller busy image consumed more of its encoder target.

`CroppedVideoRate` selects 70% of the current complete path budget, rounded
down to 50 kbit/s, with the existing 500 kbit/s codec floor. The video budget's
12% transport allowance then leaves approximately 20% for exact UI updates.
Actual UI debt, peer reservations and encoded access-unit sizes remain charged
by the existing shared byte limiter. This target does not bypass pacing or
promise full cadence when actual encoded bursts or UI demand exceed capacity.

Geometry changes already require opening an encoder, so they apply the target
immediately. With unchanged geometry, a decrease of at least 20% must persist
for 1.5 seconds; an increase of at least 25% must persist for five seconds.
There is a five-second minimum quiet period between changes. Smaller changes
and brief bad estimates do not reconfigure the codec. Returning to a full
desktop restores its original codec target.

A sustained decrease of at least half the current codec target uses a 600 ms
confirmation period and a one-second minimum quiet period. Raw admission and
pacing already react immediately; this faster severe-decrease boundary keeps
the old codec from producing another large scene picture while they drain.
The tested 400/500 ms transient estimates still do not retune the codec.
Ordinary decreases and all increases retain the periods above.

All codec access remains on the video thread. The existing live NVENC/x264
rate update and QSV/VA-API context-reopen paths are used, with a recovery IDR.
The exact cache is not reset for a bitrate change. Source timestamps and
capture/layout checks remain in force across that boundary.

## Scene cuts and wire pacing

A one-frame VBV can crush the first picture after a cut even when average
video traffic fits. A cropped plane now permits up to 450 ms of codec burst
capacity, capped at 2 MiB. Each emitted access unit is still paid by raw
admission and packet pacing before another capture is admitted. This adds
no queued future frames or decoder playout delay. The full-desktop fallback
keeps its original codec buffer, including the existing HEVC QSV minimum.
Cropped NVENC uses P4 without lookahead or B-frames; full-desktop NVENC keeps
P1. Other encoders retain their existing presets and need separate tests.

The RTP/RTX pacer charges protocol overhead in every packet's cost. Its refill
rate now follows the shared path target directly. The former 1.25 multiplier
could create a routed TCP queue during a large picture even when average
raw admission fitted. This pacing correction also applies to full-desktop
streams. Exact UI and audio remain independently delivered; video repays
their transmission credit.

Credit waits can persist while an older queue drains after a rate decrease.
With sufficient recent byte volume, delivery at least 5% faster than offered
load and a falling frame age of at least 5 ms establish that progress. Those
waits alone cannot trigger another rate decrease. New growing queues and
confirmed RTCP loss remain effective. Deterministic regressions fail before
the draining and wire-pacing corrections, and pass afterward.

## Qualification

The newer private exp83 bundle adds early flight admission and leased raw
backpressure; see `rdp-flight-admission-20261028.json`. Matched receipt reports
now have a 16 ms minimum interval and one outstanding worker report. Rapid
receipt clients bound flight using the current path target and baseline plus
35 ms, with complete-frame admission and a 50 ms maximum wait. Legacy receipt
cadence retains its previous admission path.

Private input opcode 14 pauses raw admission, with a locally expiring lease
of at most 100 ms. Receipt waits and a two-picture encoded FIFO have separate
owners. Pacing refreshes the FIFO lease, while cancellation and track close
release their owners. The latest raw scanout remains replaceable, UI/audio/input
continue, and encoded byte debt and prediction references are preserved.

Both retained exp83 hot-drop trials have zero phase overflows and pass the
unchanged latency, exact-pixel and input gates: maximum composed source age
568.86/562.30 ms and visible-glyph peak 110/138 ms. Neither passes every gate:
the first recovers at 49.9894 fps against the unchanged 50 fps minimum; the
repeat has 44.21 fps on the narrow phase and ends recovery at 7352 rather
than 8000 kbit/s. A first receipt after a gap can still reuse preceding credit
waits to lower the target without a fresh delivery pair. Controller confirmation,
cadence/resource headroom and all broader qualification remain open.

The exp83 hard-cut run passes all declared fidelity, pixel and cadence gates:
51.27/50.81 source fps, 100/132 ms largest gaps, first-cut minimum RGB PSNR
36.43/31.99 dB and 8x8-box luma SSIM 0.9851/0.9258. All seven matching packages
are built; their main payload and install/control scripts equal the tested
pilot. The installed portable VM agent and Qt5/Qt6 geometry interop pass on
the owned KWin6 VM; its hardware media paths remain unqualified. Production
192.168.64.20 retains exp35. There is no public release or rollout.

### Earlier exp80 evidence

The private exp80 bundle contains all seven matching packages. Its main payload
and install/control files match the tested pilot; LXC guest and Qt payloads
match exp68. The portable VM core/watch binaries are rebuilt in Debian 12 and
still need installed-runtime qualification. All 125 bridge tests and the
Qt 5/6 GUI/widget shutdown checks pass.

Controlled LXC108/KWin5/NVENC H264 and Firefox/Linux over TURN/TCP8006 passes
all declared hard-cut gates: 53.91/52.12 source frames/s on 20/2 Mbit/s links,
first-cut minimum RGB PSNR 36.43/31.49 dB and explicit 8x8-box luma SSIM
0.986/0.916. Largest gaps are 84/134 ms. A preceding run during portable
compilation narrowly fails the fast cadence gate (49.885 versus 50); its
cause is not isolated and it remains in the resource-qualification evidence.

Two continuous 0.3% TCP-loss profiles with 15/3 ms delay/jitter pass all their
queue/input/recovery gates. Their starting targets differ: one starts at
3550; the other starts at 8000 but reduces during the impaired fast phase.
They do not establish the stricter simultaneous full-8000-target capacity
drop plus new loss. That retained test still fails: full source age reaches
1206 ms, one visible glyph takes 637 ms, and the final target is 3018 rather
than 8000. Exact static pixels and all 15 actual input characters remain
correct. No threshold is waived and this bundle is not release-qualified.

Remaining work includes that admission/flight backlog, final component and
full-desktop fallback repeats, broader applications/resolutions, the supported
VM/LXC encoder/codec matrix, long sessions/resource checks, Mac and matched
RDP comparison. Evidence and rejected experiments are in
`rdp-scene-cuts-20261028.json`; the full plan stays in `rdp-next-plan.json`.

The historical exp51 LXC108/KWin5/NVENC/H264 run over TURN/TCP8006
passes overlap typing, exact pixels, move/close, scroll, burst input and
viewport resizing. Overlap glyph latency is 70 ms median / 101 ms p95 at a
2 Mbit/s downlink. The live no-metadata fallback also passes pixel/input checks.

Earlier raster-hash counts are not unique source-frame cadence: the original
clip was 30 fps and compression changes of a repeated picture can inflate
that counter. A new oracle paints a phase, source-frame number and an 8-bit
XOR checksum in the same Qt paint as the source image. It counts source IDs
over a fixed ten-second source interval, then separately compares displayed
unoccluded pixels to those exact RGB frames. Diagnostic PNG sampling is not
included in cadence timing.

The corrected synthetic comparison at a 2 Mbit/s downlink gives about
17.8 versus 29.6 unique frames/s for the 30 fps source, and 18.4 versus 45.5
for the 60 fps source (exp35 full video versus exp50 cropped video). Exp51
also removes a real source-rate-transition defect: 14 invalid composite
counter observations caused by an old exact overlay become zero.

A 60 fps animation excerpt gives about 15.6 versus 54.1 unique frames/s for
exp35 versus exp51 at the constrained rate; median RGB PSNR improves from
31.0 to 32.3 dB. The candidate also samples a lower-quality first frame at a
hard clip-loop cut absent from the baseline's sampled frames. Its minimum is 28.48 dB;
cut-frame quality remains a qualification item. These are selected short
clips, not a claim of universal quality improvement.

A further persistent-crop run covers sustained low/high/low rate changes and
recovery without toggling exact UI. A 400 ms bad target does not retune the
codec. All 2162 observed presentations retain the original static pixels and
complete exact coverage, including an independently restarted metadata child.

Cut handling, real adaptive feedback and packet loss, longer mixed-content
runs, other codecs/hardware and the complete VM/browser matrix must still
qualify before enabling cropping by default. Results, source attribution,
test limits and the cadence correction are in
`rdp-source-fidelity-20261028.json`; the full scope remains in
`rdp-next-plan.json`. Faster cadence alone is not acceptance of video quality.

Automatic-feedback trials are recorded in `rdp-video-feedback-20261028.json`.
The private exp57 candidate reaches about 55 unique source frames/s at a
2 Mbit/s downlink and recovers its 8000 kbit/s target after capacity returns.
Exact static pixels remain unchanged. Residual transition presentation and
visible-input peaks still fail qualification; full capture-to-screen age,
real loss and the wider matrix remain required.
