# QSF companion → QEMU → guest E2E gate

`scripts/run-qsf-initramfs-guest-e2e.sh` qualifies the data-plane that is
outside the interoperable GameStream protocol.  It is deliberately separate
from the Moonlight video/input gate:

```text
authenticated companion CLI
  -> 0600 local QSF control socket + per-run token
  -> qsf-control
  -> QEMU UNIX chardev + virtio-serial
  -> qsf-guest-agent in a real KVM Linux guest
```

The default companion is `extensions/qsf_control/qsf_client.py`; it is not a
host-local fixture or a mocked guest endpoint.  The runner boots the official
Alpine virt kernel from the pinned project ISO and a fresh static initramfs
containing the actual `guest/qsf_guest_agent.c`.  It starts QEMU with KVM,
`-display dbus,gl=off`, and a named `org.qsunshine.agent` virtio port.  There
is no guest network, SSH, package installation, X11, or host desktop session
in this lane.

The production guest agent validates non-NUL UTF-8 clipboard scalars itself,
including guest-originated state before it emits `EVENT_CLIP`; malformed text
is rejected/suppressed even if a guest-local writer bypasses the Python
broker. Files remain deliberately binary. The focused PTY test covers NUL,
overlong, truncated, surrogate and out-of-range UTF-8 rejection without
changing the previous good clipboard state.

## Run

The invoker needs access to `/dev/kvm`.  In this container a fresh shell may
need the `kvm` group applied explicitly:

```bash
cd /home/dima/Projects/q-sunshine
sg kvm -c 'cd /home/dima/Projects/q-sunshine && \
  QSF_BOOT_TIMEOUT_SECONDS=45 \
  sunshine-qemu-mvp/scripts/run-qsf-initramfs-guest-e2e.sh'
```

The runner requires the project-local pinned Alpine ISO at
`vm/alpine-virt-3.24.1/alpine-virt-3.24.1-x86_64.iso`; provision it first with
`./scripts/provision-alpine-reference-vm.sh` if needed.  The ISO is downloaded
and checksum-verified by that helper and is intentionally ignored by Git.
The runner also requires QEMU with the D-Bus display backend, `gcc` capable of
static linking, BusyBox, `isoinfo`, `cpio`, and a local user D-Bus session (the
script creates a private one).  Set
`QSF_ACCEL=tcg` only for a slower diagnostic fallback; the qualification above
uses `QSF_ACCEL=kvm`.

Each invocation creates a private `run.XXXXXX` beneath
`artifacts/validation/qsf-guest-e2e/`, mode `0700`.  The raw serial log is
retained alongside a CRLF-normalized copy used for strict guest markers.  Do
not publish a run directory while it still contains its ephemeral QSF token.

## Passed KVM evidence

The real KVM run at
`artifacts/validation/qsf-guest-e2e/run.V2rmDB/trace.txt` completed with QEMU
8.2.2 and recorded all of the following:

| Operation | Independent guest-side evidence |
| --- | --- |
| client → guest clipboard-state | guest SHA-256 `7a86b46c203c32ccbc5234b82bd7d05c404e9c4910a36093a3b9c788aecd6548` |
| guest → client clipboard-state | returned bytes and guest SHA-256 `af2fd4676d16c4ffbcbc946d461ec21f16820dd948fdab1cc84cb715013dcfc6` |
| client → guest file | guest SHA-256 `c42a9b7de80bc704a517486501b41e44a589700f13ea09b4775cc7c90de6a94e` |
| guest → client file | downloaded bytes and guest SHA-256 `7ad50987bb70f63abecaf196fd72e62e49a886f8136289549f80f95b5ef980cc` |
| resize request | guest state `1280x720` and QEMU `Console.SetUIInfo` reply `applied` |

The same run obtained `{"agent": "ready"}` through the real companion CLI
and recorded `0600` modes for both the control socket and token file.  A
second successful run (`run.NbnmBm`) exercised the runner's external-companion
adapter contract with `tests/fixtures/qsf-local-companion-hook.sh`; that
adapter delegates to the real CLI and demonstrates that a later authenticated
remote client can be substituted without changing guest operations.

## Remote-companion hook

For a session launcher or mTLS gateway, provide an executable with
`QSF_COMPANION_HOOK`:

```bash
QSF_COMPANION_HOOK=/absolute/path/to/authenticated-qsf-client-adapter \
  sunshine-qemu-mvp/scripts/run-qsf-initramfs-guest-e2e.sh
```

The adapter receives the ordinary `qsf_client.py` operation as its positional
arguments (`status`, `clipboard-set`, `clipboard-get`, `upload`, `download`,
or `resize`) and payloads through the same stdin/stdout contract.  The runner
sets `QSF_CONTROL_SOCKET`, `QSF_TOKEN_FILE`, `QSF_AGENT_SOCKET`,
`QSF_OUTPUT_DIR`, `QSF_CLIENT_BINARY`, `QSF_PYTHON_BINARY`, and the private
`DBUS_SESSION_BUS_ADDRESS`.  It does not evaluate a shell command string.
This hook interface is qualified locally; a remote mTLS route must be run as
its own end-to-end evidence after its certificate and session-binding policy
are configured.

The repository's optional mTLS client can be used as such an adapter without
making certificates part of the default runner.  For example, an executable
adapter owned by the session launcher can be:

```bash
#!/usr/bin/env bash
set -euo pipefail
server_name="${QSF_TLS_SERVER_NAME:-${QSF_TLS_HOST:?set QSF_TLS_HOST}}"
exec "${QSF_PYTHON_BINARY:-python3}" \
  /absolute/path/to/extensions/qsf_control/qsf_tls_client.py \
  --host "${QSF_TLS_HOST}" --port "${QSF_TLS_PORT:-48122}" \
  --ca-file "${QSF_TLS_CA_FILE:?}" --cert-file "${QSF_TLS_CERT_FILE:?}" \
  --key-file "${QSF_TLS_KEY_FILE:?}" --server-name "$server_name" "$@"
```

Set that file as `QSF_COMPANION_HOOK` and supply its TLS paths through the
environment.  The host session launcher is responsible for starting
`qsf_tls_gateway.py` against the particular `QSF_CONTROL_SOCKET` and
`QSF_TOKEN_FILE` it receives after qsf-control is live; those local capability
paths must not be sent to the remote client.  The gateway and client enforce
mutual TLS, while the default guest runner neither generates credentials nor
opens a TCP listener.

## Precise scope

This proves an authenticated QSF companion transport reaches an actual Linux
guest agent through QEMU virtio-serial, in both directions for the constrained
text/file payloads, and that its resize request reaches both QEMU and guest
agent state. Guest-originated changes are delivered into the local broker as
`EVENT_CLIP`; the supplied client uses explicit `clipboard-get` polling rather
than a remote push subscription.

It does not prove any of the following:

- Moonlight/GameStream itself carries clipboard or file-transfer packets;
- the agent-state clipboard is a desktop GUI clipboard (an explicit guest GUI
  bridge must be qualified separately);
- `SetUIInfo` caused a guest DRM scanout or desktop mode change;
- a remote network/mTLS transport, a combined Moonlight/QSF session binding,
  or a VirGL/DMA-BUF stream path.

Those last desktop/native boundaries are now separately proven by the strict
one-VM composite in
`vm/alpine-virgl-3.20.10/wayland-qsf-e2e/run.KEqkVc/`; see
`VIRGL_QSF_WAYLAND_CLIPBOARD_E2E.md`. The mTLS gateway itself is exercised by
its protocol test, but a production launcher must still bind each certificate
and gateway lifetime to exactly one authorized session/VM.

The deployed q-sunshine host stays headless: Xvfb, SDL, and `xdotool` belong
only to the separate Moonlight client-test harness.
