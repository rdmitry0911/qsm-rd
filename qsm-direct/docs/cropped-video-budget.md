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

Exp50 is a private candidate. The LXC108/KWin5/NVENC/H264 run over TURN/TCP8006
passes overlap typing, exact pixels, move/close, scroll, burst input and
viewport resizing. Its short 2 Mbit/s cadence sample improves from exp48's
approximately 14 to approximately 39 visible frames/s, while overlap glyph
latency is 69 ms median / 104 ms p95. These are short samples of one fixture.

A further persistent-crop run covers sustained low/high/low rate changes and
recovery without toggling exact UI. A 400 ms bad target does not retune the
codec. All 2162 observed presentations retain the original static pixels and
complete exact coverage, including an independently restarted metadata child.

Moving-video fidelity, consistent fast-link cadence, real adaptive feedback
and packet loss, longer mixed-content runs, other codecs/hardware and the
complete VM/browser matrix must still qualify before enabling cropping by
default. Fast-link samples vary between 46 and 59 fps across these short
unsynchronized clip phases. See `rdp-video-budget-20261028.json` and
`rdp-next-plan.json`; faster cadence alone is not acceptance of video quality.
