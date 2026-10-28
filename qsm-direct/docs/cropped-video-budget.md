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

All codec access remains on the video thread. The existing live NVENC/x264
rate update and QSV/VA-API context-reopen paths are used, with a recovery IDR.
The exact cache is not reset for a bitrate change. Source timestamps and
capture/layout checks remain in force across that boundary.

## Qualification

Exp51 is a private candidate. The LXC108/KWin5/NVENC/H264 run over TURN/TCP8006
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
