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
