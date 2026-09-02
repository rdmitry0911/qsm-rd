# System authentication

`q-sunshine-auth@VMID` is the password-login boundary for q-sunshine-aware
connections. It is deliberately separate from Sunshine's GameStream pairing
server.

## What it authorizes today

The Qt client opens TLS 1.3 to the per-VM auth gateway and sends exactly one
login request containing a system username and password. The gateway invokes
a narrowly scoped PAM service through a stdin-only helper, applies an explicit
per-VM user allowlist, and returns a signed ticket valid for 60–900 seconds
(600 by default). The client keeps the ticket only in memory.

Each desktop profile also stores an operator-provisioned **expected VM
audience**, for example `vm-100`. It is routing/trust metadata, not a username,
password, or bearer credential. The client rejects a successful TLS/PAM reply
unless its returned audience exactly equals this field; it never guesses from a
user-editable desktop-profile label. This fail-closed check prevents a
misrouted or misconfigured auth gateway from opening local launch admission
for another VM.

The QSF remote gateway can be switched from its existing mTLS-client-certificate
mode to this ticket mode. In that mode QSF still uses TLS 1.3 with normal
server certificate verification, but every JSON operation carries the
short-lived ticket and the gateway verifies its HMAC signature, expiry, and
exact VM audience before it reads the host-local QSF control token.

The shipped desktop route does not use stock pairing. After a successful login
the Qt shell gives the in-memory ticket only to the patched Moonlight child
through a one-shot stdin pipe. Moonlight creates a client-owned ephemeral CSR,
asks the same authd for a VM-bound mTLS lease, and pins the returned Sunshine
server certificate before its GameStream requests. Patched Sunshine validates
that lease on each sensitive HTTPS operation and requires encrypted RTSP
possession proof before stream setup. Neither the PIN nor a paired identity is
a fallback in this mode.

QSF and GameStream remain separate protocols: the ticket authorizes each
independently, while clipboard/files/resize travel only over QSF. Explicit
logout or a profile route/trust/audience change tears down QSF and stops the
Qt-managed Moonlight child. Ordinary ticket expiry removes local admission and
QSF readiness for future work, but does not hard-cut an already admitted
encrypted media stream at the short leaf expiry.

## Native GameStream lease endpoint

When all five `QSUNSHINE_GAMESTREAM_LEASE_*` instance values are explicitly
provisioned, authd additionally accepts a valid qsa1 ticket plus a public CSR
and returns a short-lived VM-audience client certificate lease. It is an
all-or-disabled root-local CA boundary: no private client key, password, or
ticket is written to disk or returned in the response. The Qt production
composition requires the patched Moonlight consumer; an unpatched binary sees
the explicit `--qsm-system-auth` marker and fails rather than silently
enrolling/pairing. The exact provisioning, JSON schema, certificate profile,
server enforcement, and canonical patch contract are in
[`GAMESTREAM_LEASE_AUTH.md`](GAMESTREAM_LEASE_AUTH.md).

The packaged `/usr/bin/q-sunshine` launcher consumes the same complete five-
variable lease configuration. It injects
`qsm_system_auth_mode=enabled`,
`qsm_system_auth_ca=$QSUNSHINE_GAMESTREAM_LEASE_CA_CERT`, and
`qsm_system_auth_audience=$QSUNSHINE_AUTH_AUDIENCE` into Sunshine only when
all values are present; otherwise it refuses a partial configuration. In that
mode the patched Sunshine side must fail closed and has no legacy
pairing/PIN fallback. The CA and audience are therefore exactly the same
per-VM values used by authd, not independently editable Sunshine settings.

## Host setup

The package does not create an account policy, certificate, or signing key.
For VM `100`, provision distinct server TLS material and a raw 32-byte ticket
key readable only by root:

```bash
sudo install -d -o root -g root -m 0700 /etc/q-sunshine/auth/100
sudo openssl rand -out /etc/q-sunshine/auth/100/ticket.key 32
sudo chown root:root /etc/q-sunshine/auth/100/ticket.key
sudo chmod 0600 /etc/q-sunshine/auth/100/ticket.key

sudo install -o root -g root -m 0644 \
  /usr/share/doc/q-sunshine-pve/q-sunshine-remote.pam \
  /etc/pam.d/q-sunshine-remote
```

The supplied PAM example delegates to Debian's `common-auth` and
`common-account` instead of bypassing the host policy with direct `pam_unix`.
It therefore retains the site's SSSD/LDAP, faillock, access, and
account-expiry configuration. The helper sets PAM's `PAM_RHOST` to the numeric
kernel-observed TLS peer IP, so origin-aware PAM policy sees the real remote
address. The wire protocol supplies exactly one username and one password: a
second interactive PAM prompt (OTP, challenge/response, or password-change
dialogue) is deliberately rejected, not answered with the password. Do not
advertise interactive MFA for this endpoint; use a PAM policy compatible with
one password conversation, or a separately designed multi-step protocol. On a
non-Debian PAM layout, replace those includes with the site's equivalent
auth/account policy. PAM success alone is never enough: set a non-empty
`QSUNSHINE_AUTH_ALLOWED_USERS` allowlist in the VM's root-only instance file.
Do not use a broad administrative account merely for convenience.

Add these values to `/etc/q-sunshine/instances.d/100.conf` after providing a
server certificate/key whose SAN matches the Qt client's configured SNI:

```ini
QSUNSHINE_AUTH_SERVER_CERT=/etc/q-sunshine/auth/100/server.crt
QSUNSHINE_AUTH_SERVER_KEY=/etc/q-sunshine/auth/100/server.key
QSUNSHINE_AUTH_TICKET_KEY=/etc/q-sunshine/auth/100/ticket.key
QSUNSHINE_AUTH_AUDIENCE=vm-100
QSUNSHINE_AUTH_ALLOWED_USERS=alice,bob
QSUNSHINE_AUTH_PAM_SERVICE=q-sunshine-remote
QSUNSHINE_AUTH_GATEWAY_HOST=192.168.64.25
QSUNSHINE_AUTH_GATEWAY_PORT=48123
QSUNSHINE_AUTH_MAX_CONCURRENT_REQUESTS=16
```

The auth unit runs as root so it requires the ticket key to be root-owned,
mode `0600`, a regular non-symlink file. Its PAM helper clears its input buffer
on exit and never places the password in command arguments, environment,
temporary files, stdout, stderr, or QSettings. As with any in-process PAM
conversation, memory clearing is best effort; the helper receives the numeric
source IP in its non-secret `--rhost` argument solely to set `PAM_RHOST`.
Use TLS and a hardened host.

The auth TLS certificate and key are also opened fail-closed as checked regular
non-symlink files. When the service runs as root, both must be root-owned; the
certificate must not be group/world writable and the private key must be mode
`0600` (or stricter). The QSF TLS gateways enforce the same rule for their
server certificate/key. Restart the affected gateway after rotating either
TLS file because an already running `SSLContext` retains the old identity.

The packaged unit retains `PrivateDevices=true`; token/biometric PAM modules
which require a physical device need an explicitly reviewed local unit
override.

Enable it only after reviewing the policy:

```bash
sudo systemctl enable --now q-sunshine-auth@100
sudo systemctl status q-sunshine-auth@100
```

## QSF ticket mode

The same `QSUNSHINE_AUTH_TICKET_KEY` and exact audience bind the dedicated QSF
system-auth gateway to the login service. Configure the QSF server
certificate/key as usual, leave `QSUNSHINE_QSF_CLIENT_CA` unset, and enable
the ticket-only gateway only after the auth service is healthy:

```ini
QSUNSHINE_QSF_SERVER_CERT=/etc/q-sunshine/auth/100/qsf-server.crt
QSUNSHINE_QSF_SERVER_KEY=/etc/q-sunshine/auth/100/qsf-server.key
QSUNSHINE_QSF_GATEWAY_HOST=192.168.64.25
QSUNSHINE_QSF_GATEWAY_PORT=48122
QSUNSHINE_QSF_MAX_CONCURRENT_REQUESTS=16
```

```bash
sudo systemctl enable --now q-sunshine-qsf-system-auth-gateway@100
```

`q-sunshine-qsf-system-auth-gateway@100` requires and binds to
`q-sunshine-auth@100`, so it is stopped with its issuer. It accepts only a
valid system ticket. The existing `q-sunshine-qsf-gateway@100` remains the
strict legacy mTLS service:

- `q-sunshine-qsf-system-auth-gateway@100` requires a valid system ticket and
  does not accept a client certificate;
- `q-sunshine-qsf-gateway@100` requires `QSUNSHINE_QSF_CLIENT_CA` mTLS.

There is no unauthenticated fallback or mixed optional mode. The QSF ticket
authorizes its separate clipboard/file/resize companion, while the same
system-auth admission independently feeds patched Moonlight's native
GameStream lease. QSF remains cryptographically distinct from an already
admitted GameStream media session; it is not a GameStream packet channel.

The two QSF gateway units also `Conflicts=` with each other because they use
the same per-VM listener by default. For an upgrade, stop and disable the
legacy mTLS unit before enabling the system-auth unit; reverse that order to
return to mTLS. Do not enable both for one VM.

## Client behavior

The Qt `SystemAuthClient` stores host, port, server name, CA path, and required
VM audience per desktop profile, but never stores the password, ticket,
authenticated subject, or expiry in `QSettings`. The expected audience must
match the server reply exactly before the client reports an authenticated
session. Switching profile or changing host/SNI/CA/audience clears the ticket
and ends QSF. An idempotent endpoint Save preserves a live ticket. Explicit
logout or a route/trust/audience change also stops the local patched Moonlight
child. Ticket expiry zeroes the in-memory ticket, deactivates QSF, and cancels
queued QSF operations, but does not signal an already admitted media child to
stop; the next launch requires a fresh login. In addition to its normal expiry
timer, the client
checks the server-issued expiry once per second while admitted, so a forward
wall-clock jump promptly removes local launch/QSF admission instead of waiting
for the original monotonic timer deadline.

The password field should be cleared by the Qt UI immediately after calling
`login()`. A server CA trust anchor and expected VM audience are configuration,
not additional user credentials; password-only login does not mean TLS
certificate verification or audience matching is optional. QML, Qt JSON, and
TLS internals can make unavoidable short-lived process-memory copies while the
request is serialized and sent; the client wipes its owned transient byte
buffer promptly, but does not claim perfect memory erasure. It does guarantee
that those values are not put into QSettings or another durable profile store.

Both exposed TLS gateways use a bounded worker semaphore (16 by default,
configurable from 1 to 16), short ingress timeouts, a small listen backlog,
and systemd `TasksMax=48`/`MemoryMax`/file-descriptor/stop-time bounds. At the
16-worker cap the auth service can have at most 16 Python workers plus 16 PAM
helpers and its main process; the QSF gateways have at most 16 workers plus
their main process. Excess accepted peers are closed before TLS handshake or
PAM/QSF work begins. This is capacity protection, not a replacement for a
firewall or rate limiting.

The packaged exposed gateways also use `NoNewPrivileges`, private temporary
and device namespaces, `ProtectSystem`, kernel/control-group/clock protections,
restricted `/proc`, namespace/realtime/set-id restrictions, and a narrow socket
family list. They intentionally do not impose a global capability or syscall
filter: the administrator-selected PAM stack may legitimately need NSS/SSSD or
other site-specific host interactions. Review any local PAM override before
relaxing the supplied service sandbox.

## Ticket lifecycle and residual replay

The QSF ticket is a short-lived signed bearer credential, not a server-side
session record. Local logout and the client expiry guard wipe the local copy
and close the local QSF lease, but cannot revoke a ticket that was already
copied from client process memory or a compromised TLS endpoint. Such a copy
can be replayed to the ticket gateway until its signed expiry (at most 900
seconds; 600 seconds by default). Use a short TTL, per-VM TLS trust and
audience, and rotate the per-VM signing key (or stop the ticket gateway) to
invalidate outstanding tickets during an incident. The key is loaded only at
service start: after rotation explicitly restart both live consumers, for
example stop `q-sunshine-qsf-system-auth-gateway@100`, restart
`q-sunshine-auth@100`, then start
`q-sunshine-qsf-system-auth-gateway@100`; otherwise an old auth or QSF process
can continue accepting/signing with its in-memory key. This residual bounded
bearer-replay window is separate from QSF's lack of cryptographic binding to
an already-admitted media stream. Native GameStream media is independently
admitted through the patched Moonlight CSR/lease path; legacy paired
GameStream is disabled for the native instance.
