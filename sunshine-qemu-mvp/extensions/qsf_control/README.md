# QSF control side-channel

`qsf_control.py` is the companion channel for functionality that the
GameStream protocol deliberately does not standardize: UTF-8 clipboard,
file transfer, and client-driven desktop geometry.  It is **not** a public
replacement for Sunshine's network listener.

```text
Moonlight-compatible stream: client ↔ Sunshine ↔ QEMU Display1
QSF extension:              client ↔ local 0600 socket ↔ qsf-control
                                            ↕
                                    QEMU virtio-serial
                                            ↕
                                      qsf-guest-agent
```

The local control socket has a random 256-bit token in a `0600` file. Do not
expose that socket or token directly on TCP. A remote companion uses the
separate TLS 1.3 mTLS gateway described below; the gateway reads the local
token on the host and never transmits it.

The guest agent accepts and emits only non-NUL UTF-8 clipboard data up to 1
MiB, including when a guest-local state writer bypasses the host broker; bad
guest state is not emitted as an event. Files remain arbitrary binary data up
to 2 MiB. File names are plain relative basenames. A production UI can bind
`clipboard-get`/`clipboard-set` to its platform clipboard implementation.
Neither qsf-control nor qsf-guest-agent links to, starts, or requires an X11
or Wayland service on the host.  A desktop integration belongs at a client or
guest endpoint and is deliberately outside this headless transport.

The virtio-serial protocol keeps an explicit distinction between a requested
clipboard response (`CLIP <base64>`) and a guest-originated change
(`EVENT_CLIP <base64>`).  That prevents an unsolicited clipboard notification
from being mistaken for the response to a simultaneous `clipboard-get`.
`EVENT_CLIP` updates the local broker state; the supplied client contract is
pull/poll through `clipboard-get`, not an unsolicited remote push subscription.

Run the host bridge after QEMU has created its virtio-serial server socket:

```bash
python3 extensions/qsf_control/qsf_control.py \
  --agent-socket /run/user/$UID/qsf-agent.sock \
  --control-socket /run/user/$UID/qsf-control.sock \
  --token-file /run/user/$UID/qsf-control.token
```

Use the companion CLI:

```bash
printf 'client to guest' | python3 extensions/qsf_control/qsf_client.py \
  --socket /run/user/$UID/qsf-control.sock \
  --token-file /run/user/$UID/qsf-control.token clipboard-set
```

`resize WIDTH HEIGHT` forwards a validated request to both the guest agent and
QEMU `Console.SetUIInfo`.  The guest display stack remains authoritative: a
guest that cannot switch modes retains its current scanout and the Moonlight
renderer scales it, rather than producing a black stream.

## Remote Moonlight companion

GameStream/Moonlight has no interoperable clipboard or file-transfer packet.
For a remote Moonlight-based client, run the optional QSF companion beside the
Moonlight client rather than pretending these operations are GameStream
messages.  `qsf_tls_gateway.py` is the server-side bridge from an mTLS
connection to the local `0600` control socket; `qsf_tls_client.py` is the
matching client CLI.  The local token remains on the host and is never sent
over TLS.

The gateway binds to loopback by default.  To expose it to a remote client,
provide a server certificate/key and a CA that issues the companion client's
certificate, then deliberately select a reachable address:

```bash
python3 extensions/qsf_control/qsf_tls_gateway.py \
  --control-socket /run/user/$UID/qsf-control.sock \
  --token-file /run/user/$UID/qsf-control.token \
  --server-cert /etc/qsf/server.crt --server-key /etc/qsf/server.key \
  --client-ca /etc/qsf/client-ca.crt --listen-host 0.0.0.0
```

```bash
printf 'client to guest' | python3 extensions/qsf_control/qsf_tls_client.py \
  --host q-sunshine.example --ca-file qsf-server-ca.crt \
  --cert-file client.crt --key-file client.key clipboard-set
```

TLS 1.3 with a required client certificate is enforced.  Start one gateway
per VM/session and stop it with that session; a client certificate trusted by
that gateway can operate only the attached VM's QSF control socket.  This
transport is headless and has no X11 or Wayland dependency.
