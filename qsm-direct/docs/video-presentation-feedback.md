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
