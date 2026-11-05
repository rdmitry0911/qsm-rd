# Video presentation feedback

RTCP RTT alone does not describe the video presentation queue. In the owned
TURN/TCP8006 bandwidth-drop pilot, RTT stayed around 20 ms while the receiver
fell seconds behind. Exp51 eventually lost packets, reduced to 500 kbit/s and
recovered only to 898 kbit/s during the following 75 seconds.

The viewer optionally reports the RTP timestamp from
`requestVideoFrameCallback`, at most ten times per second, over the existing
unordered `qsm-guest-cursor` channel. This adds no listener or forwarded port.
The uplink message cannot inject keyboard, pointer or private worker commands.
The server accepts only timestamps from its last 512 completed primary video
frames, younger than eight seconds. Duplicate and reordered acknowledgements
are ignored using sender-local serial numbers, including across RTP wrap.
Only the timestamp comes from the viewer; send times and cumulative wire-byte
counts come from the server's monotonic clock and paced transmission history.

The controller compares arrival and send intervals for the same acknowledged
byte range. A persistent age increase accompanied by slower delivery confirms
a queue. The age baseline is the lower decile of the last minute's bounded
samples, rather than a single exceptionally young startup frame. The latter
caused a reproduced false-congestion/IDR loop on an otherwise healthy path.
Fresh media demand remains necessary for upward probes. Timely presentation
permits bounded probes despite an application-limited REMB; old viewers retain
the previous RTCP/REMB policy. Every reporting viewer must have drained before
a shared-producer increase. Old high-demand reports cannot drive idle probes.

When presentation age is elevated, whole-frame admission also limits video
sent ahead of acknowledged frames. Healthy or idle estimates do not impose
this limit. The wait occurs before the RTP/RTX pacing lock, so retransmission,
audio and exact UI can continue. A 25 ms timeout permits loss repair and
avoids waiting forever for a picture the decoder cannot acknowledge. Actual
credit waits feed the adaptive controller: otherwise pacing hides the
congestion and can permanently suppress the frame rate.

This admission policy retains the existing four-frame ingress FIFO and IDR
recovery on overflow. Overflow-induced recovery pauses are an explicit
qualification item, not a successful latency result. Static exact pixels,
actual source-frame cadence, visible typed glyphs and presentation age must
all be checked; connected state or a falling bitrate alone is insufficient.

Presentation age here runs from the last RTP packet's send time to the browser
frame callback and its return acknowledgement. It excludes capture, encoding
and admission before transmission. It is not a capture-to-screen latency
measurement. The source-frame oracle and glyph pixel checks remain necessary;
a clocked source-generation oracle is also required before broad latency
claims. Native Qt5 priority, the VM/codec/browser matrix, real packet loss,
long mixed sessions and RDP comparisons remain separate acceptance gates.

The prototype is private. Production rollout and release are governed by
`rdp-next-plan.json`; experiment failures are retained in the feedback report.

Exp57 preserves about 55 unique source frames/s at the constrained rate and
restores the complete target. Its maximum source-frame gap is 433 ms, but
presentation age still peaks at 573 ms and a layout-only fallback glyph at
712 ms. Two stricter transition gates fail. Full results and rejected
intermediate experiments are in `rdp-video-feedback-20261028.json`.

The packet estimator now confirms a moderate queue above 100 ms with another
matched arrival interval spanning at least 25 ms. An earlier pressure interval
above 20 ms cannot itself confirm that high queue. This preserves the existing
early raw-admission bound while avoiding a bitrate/codec change for one repaired
TCP interval that drains in the next report. A severe delivery decrease below
half the offered load retains its existing 50 ms queue response. Invalid,
draining or discontinuous delivery clears the confirmation evidence.

The frozen exp181 regression records a late cut from 8000 to 3649 kbit/s after
one 117 ms queue observation; the next pre-cut packet flight is already draining
at 35 ms. The corrected estimator leaves that recorded prefix at 8000. The same
run's real 20 → 2 Mbit/s cut still selects 1699 on its original first report.
These prefix tests establish the decision, not counterfactual live performance;
all original source-age, cadence, fidelity and recovery gates remain required.

A collapse below half the offered load no longer waits for the overuse trend.
At a tenfold step each 50 ms report costs roughly 150 ms of queue, and the report
already shows it. The 25 ms slow confirmation still excludes a single interval.
The newest 50 ms interval states the new capacity, because the 100 ms window can
still mix in the preceding fast link. It takes no further margin: delivery is
already low while the queue forms and the codec stays at 70% of the target.

A sender limited by its flight window offers only what the path delivers, so a
target above capacity never looks slower than offered. A queue above 80 ms held
for one second now reduces the target as well. Only the queue falling to 50 ms
ends that evidence, since this sawtooth alternates with draining intervals. A
longer route raises age without any capacity limit, so the rule also requires
frames actually waiting for the flight and delivery below 85% of the target. The
reduction uses the mean rate over the whole period and then cannot repeat.

Measured limits are in `rdp-candidates-183-188-20261104.json`. These rules do not
remove the first queue after a tenfold step on a long path, where detection
itself needs at least one round trip.

With exact UI enabled, the receive transform also decodes each assembled H.264
frame with WebCodecs, ahead of the browser's playout buffer, and hands the picture
to the bounded presenter. The original frame always continues unchanged into the
browser path, which stays live as the fallback. No extension or special API
permission is involved. TCP delivers pictures in bursts, so this source keeps
three pictures rather than two: two loses the third picture of a burst.

A hardware decoder can take a few hundred milliseconds to start and hold pictures
beyond its reported queue. A picture older than 200 ms since decode began is never
shown. After 150 ms without a fresh picture the display returns to the browser
path until fresh pictures resume. Five seconds of silence, a decoder error or a
key frame without an H.264 SPS ends direct decode for that session.
