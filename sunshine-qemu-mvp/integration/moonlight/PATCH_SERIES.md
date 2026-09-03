# Moonlight patch series

`0001-system-auth-gamestream-lease.patch` is the required native client layer.
It is applied only to the pinned Moonlight commit described in
[`PINNED_UPSTREAM.md`](PINNED_UPSTREAM.md).

It adds an all-or-nothing `QSM_GAMESTREAM_*` configuration path:

- consumes the q-sunshine ticket only from an inherited one-shot file
  descriptor, closes it, and keeps the newly generated private key in process
  memory;
- generates the CSR locally and obtains a short-lived VM-scoped `clientAuth`
  certificate from the system-auth endpoint over TLS 1.3;
- pins the Sunshine HTTPS leaf supplied by that authenticated endpoint and
  rejects HTTP discovery, changed hosts, and expired leases;
- prevents lease-mode certificates, host discovery, and client identity from
  entering Moonlight's persistent `QSettings` pairing database;
- requires the explicit `stream --qsm-system-auth` marker, so an unpatched
  Moonlight cannot silently run a legacy PIN flow.

The patch does not alter ordinary upstream Moonlight behavior when every
`QSM_GAMESTREAM_*` variable is absent. q-sunshine's production launcher always
supplies the full set and the explicit marker; it therefore has no legacy
fallback.

`0002-browser-encoded-video-tap.patch` is applied after it. It adds an
explicit internal output contract for the browser bridge:

- `QSM_BROWSER_VIDEO_RECORD_PATH` and `QSM_BROWSER_AUDIO_RECORD_PATH` accept
  either an owner-private recording pathname (qualification fallback) or a
  `unix:/absolute/path` local `SOCK_SEQPACKET` sink;
- the local path carries bounded framed **encoded** H.264/HEVC/AV1 access
  units and encoded Opus packets. It is read after GameStream decryption but
  before presentation, so the WebRTC bridge packetizes the original encoder
  output instead of decoding and re-encoding it;
- an Opus configuration packet declares the negotiated samples-per-frame;
  this preserves 5 ms versus 10 ms timing without guessing from payload size;
- recorder storage remains owned by the Moonlight `Session` for the complete
  connection lifetime;
- FFmpeg's pull queue is observed at its consumer boundary rather than being
  consumed a second time, so native presentation keeps its original threading
  contract.

The tap is intentionally local-only and carries no PVE credential, pairing
identity, GameStream ticket, or QSF authority. The production WebRTC bridge
uses the bounded local socket; the file form remains a forensic test sink.
