# Browser console media bridge

The browser console is the intended primary client route.  Its PVE adapter
uses the PVE session and the same `VM.Console` ACL check as noVNC/SPICE; no
client package, pairing database, PIN, extra PVE credential, or browser
extension is required.  This directory is the media primitive used by that
adapter, not an unauthenticated HTTP console on its own.

The Unix ingress is deliberately producer-neutral: it is the contract for the
new direct QEMU Display1 encoder worker. Only the direct worker feeds these
sockets; no compatibility capture route is part of the browser protocol or
authorization model.

```text
PVE Web UI -> protected node-local terminal broker -> QEMU Display1 media worker
          <- SDP answer / WebRTC DTLS-SRTP                     |
browser <----------------------------------------------- private Unix encoded tap
                                                              |
                                            H.264/Opus -> QEMU Display1 D-Bus
```

The two local `AF_UNIX/SOCK_SEQPACKET` endpoints accept only the service UID,
are mode `0600`, have fixed bounded packet sizes, and live below a non-writable
runtime directory.  H.264 and Opus stay encoded: aiortc packetizes the
`av.Packet` payload for WebRTC, rather than accepting a decoded `VideoFrame`
or `AudioFrame` and invoking an encoder.  The producer drops local packets on
backpressure; it must never hold the QEMU capture or encoder queue.

`qsm_browser_bridge.py` intentionally has no HTTP listener and no PVE
authentication parser. The terminal service owns the one PVE-authorized,
VM-scoped media worker, and the PVE API remains the only browser-facing
authorization boundary. A PVE adapter passes only a bounded SDP offer after
`VM.Console` succeeds.

The current no-transcode path requires that the browser offer both WebRTC
H.264 and Opus.  The bridge checks those capabilities before it adds an RTP
sender or consumes the one-use offer.  This matters for free/minimal Chromium
builds which omit H.264: they receive a capability error, not a half-created
session or a silent CPU transcode.  Normal Chrome/Edge/Safari builds advertise
H.264; the PVE UI will surface an unsupported browser explicitly.  AV1 is a
future direct path once the server-side RTP stack has a production AV1
packetizer and the selected NVIDIA encoder supports it.

A browser extension is not required: the browser speaks WebRTC directly.
Native Messaging would reintroduce a client-side installer, while an extension
would add no authority or performance advantage to this path.
