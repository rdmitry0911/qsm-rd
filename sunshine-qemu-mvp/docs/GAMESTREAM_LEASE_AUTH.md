# Native GameStream system-auth lease

This document defines the certificate-lease boundary used by the native
PIN-free GameStream route. It is not a PIN bridge and never calls
`/api/pin`: a system login mints a short-lived, VM-bound client certificate
only after proof that the client owns the corresponding ephemeral private key.
The patched Moonlight and Sunshine sides consume that identity directly.

## Security model

`q-sunshine-auth@VMID` already authenticates `login` with TLS 1.3 and the
host PAM policy.  Its `qsa1` ticket is short-lived (60--900 seconds), VM
audience-bound, HMAC authenticated and held only in the Qt client process.
The lease request presents that ticket and a public PKCS#10 CSR.  The ticket
is a bounded bearer credential: someone who steals it before expiry can mint
another leaf for a key they control.  It is therefore never written to
QSettings, logs, command lines, environment or disk.

For every VM, provision a separate RSA-3072-or-stronger CA.  Its private key
is readable only by the root-owned auth service; it is not the Sunshine server
key and it never reaches the client.  A root-owned C/OpenSSL helper opens it
without following symlinks, checks the ownership/mode, validates it against
the configured CA certificate, and accepts only a valid self-signed RSA CSR
(2048--4096 bits, public exponent 65537).  The CSR is the proof that the
client owns the private key.  Requested subjects and extensions are ignored.

The helper creates a new fixed profile:

- X.509 v3 leaf, random positive 160-bit serial and a 60--900 second
  not-after bound;
- `critical,CA:FALSE`, critical `digitalSignature,keyEncipherment`, and
  critical `clientAuth` EKU;
- `critical` URI SAN `urn:q-sunshine:aud:<VM audience>`;
- subject CN `q-sunshine user <PAM subject>`; and
- issuer and signature only from that VM's CA.

Equivalent OpenSSL extension syntax (the helper creates these programmatically
and does not consume client-controlled extension text) is:

```ini
[q_sunshine_gamestream_lease]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=critical,clientAuth
subjectAltName=critical,URI:urn:q-sunshine:aud:vm-100
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always
```

The client private key is never an authd input beyond its public CSR and is
never a response field.  In the future Qt/Moonlight integration it must remain
an in-memory client key; no PEM key file or launch argument is allowed.

## Wire contract

The normal login response remains unchanged.  A client that has received a
current ticket opens a new TLS connection to the same authd and sends one
newline-framed JSON object with exactly these fields:

```json
{
  "op": "gamestream_lease",
  "version": 1,
  "session_token": "qsa1.<payload>.<hmac>",
  "csr_pem": "-----BEGIN CERTIFICATE REQUEST-----\\n...\\n-----END CERTIFICATE REQUEST-----\\n"
}
```

`csr_pem` is ASCII, one PEM CSR only, ends in LF, and is at most 16 KiB.  No
unknown, optional, or duplicated fields are accepted.  Authd uses the same
HMAC key and *exact* audience as the login ticket, limits the leaf TTL to the
lesser of the configured lease TTL and remaining ticket lifetime, and refuses
to issue a leaf below the 60 second minimum.  It returns the existing generic
`{"ok":false,"error":"authentication failed"}` for every failed validation.

On success, the response is exactly:

```json
{
  "ok": true,
  "result": {
    "version": 1,
    "audience": "vm-100",
    "subject": "alice",
    "expires_at_unix_ms": 0,
    "client_certificate_pem": "-----BEGIN CERTIFICATE-----\\n...\\n-----END CERTIFICATE-----\\n",
    "sunshine_server_certificate_pem": "-----BEGIN CERTIFICATE-----\\n...\\n-----END CERTIFICATE-----\\n"
  }
}
```

`expires_at_unix_ms` is conservative by up to one second; the leaf's X.509
not-after is authoritative.  `sunshine_server_certificate_pem` is the
operator-configured public Sunshine leaf used by the client as the bootstrap
pin/trust anchor for this exact VM.  It is not a client CA and must not be
silently replaced by TOFU discovery.  This response contains no private key,
ticket, password, CA key or reusable server secret.

## Host provisioning and package contract

For instance `100`, use distinct material; do not reuse the authd TLS server
key, a QSF CA, or another VM's CA:

```bash
sudo install -d -o root -g root -m 0700 /etc/q-sunshine/auth/100
sudo openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 3650 \
  -subj '/CN=q-sunshine GameStream lease CA vm-100' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -keyout /etc/q-sunshine/auth/100/gamestream-ca.key \
  -out /etc/q-sunshine/auth/100/gamestream-ca.crt
sudo chown root:root /etc/q-sunshine/auth/100/gamestream-ca.key \
  /etc/q-sunshine/auth/100/gamestream-ca.crt
sudo chmod 0600 /etc/q-sunshine/auth/100/gamestream-ca.key
sudo chmod 0644 /etc/q-sunshine/auth/100/gamestream-ca.crt
```

The Debian package installs `q-sunshine-lease-issuer` at
`/usr/lib/q-sunshine/bin/q-sunshine-lease-issuer`, root-owned and mode 0755.
The authd wrapper must pass these per-instance configuration values only when
all are present; partial configuration disables lease issuing:

```ini
QSUNSHINE_GAMESTREAM_LEASE_ISSUER=/usr/lib/q-sunshine/bin/q-sunshine-lease-issuer
QSUNSHINE_GAMESTREAM_LEASE_CA_CERT=/etc/q-sunshine/auth/100/gamestream-ca.crt
QSUNSHINE_GAMESTREAM_LEASE_CA_KEY=/etc/q-sunshine/auth/100/gamestream-ca.key
QSUNSHINE_GAMESTREAM_LEASE_SUNSHINE_SERVER_CERT=/var/lib/q-sunshine/100/sunshine/credentials/cacert.pem
QSUNSHINE_GAMESTREAM_LEASE_TTL_SECONDS=300
```

Paths must be absolute.  In deployment the executable, CA files and Sunshine
public certificate are root-owned regular non-symlinks; the CA key is mode
0600 and no configured public input is group/world writable. The public CA
certificate is readable by Sunshine's verifier when native system-auth mode is
enabled; its private key stays in authd. The packaged `q-sunshine@100` unit
sets `XDG_CONFIG_HOME=/var/lib/q-sunshine/100`, so its unmodified Sunshine
default `cert=credentials/cacert.pem` resolves to the path shown above. Start
and verify `q-sunshine@100` once before starting `q-sunshine-auth@100`, so the
default leaf exists for the issuer to pin. If an operator configures an
explicit Sunshine `cert` path instead, this value must name that exact
root-owned public leaf; no unit copies it into `/etc/q-sunshine`.

## Implemented native media path

The coordinated client/server implementation is intentionally small and
fail-closed:

1. The Qt shell completes TLS 1.3/PAM sign-in and holds the `qsa1` ticket only
   in RAM. At each stream launch it starts patched Moonlight with the explicit
   `--qsm-system-auth` marker and sends the ticket only through its managed
   stdin pipe (FD 0). It is never in QSettings, argv, environment, logs, or a
   PEM key file.
2. Patched Moonlight creates an ephemeral RSA key and CSR, calls authd, pins
   the returned `sunshine_server_certificate_pem` exactly, and uses the
   returned leaf/private-key pair only in memory for GameStream HTTPS. Native
   mode bypasses pairing persistence, host discovery persistence, and every
   PIN route. A stock Moonlight executable rejects the explicit marker rather
   than falling back silently.
3. The transport-only Sunshine worker requires `qsm_system_auth_mode=enabled`
   and accepts only a current leaf issued by that VM CA. It starts no plaintext
   HTTP bootstrap listener: the six GameStream transport endpoints are served
   only over HTTPS. The public CA is opened as an absolute, root-owned,
   non-symlink, non-group/world-writable regular file; it is loaded once from
   that checked descriptor, not reopened by pathname. For every sensitive
   HTTPS request Sunshine reconstructs the identity from that request's actual
   TLS socket and strictly verifies chain, validity, `clientAuth`, `CA:FALSE`,
   exact critical URI SAN audience, and the fixed certificate profile.
4. A native `/launch` or `/resume` additionally requires `corever >= 1` and
   exactly 16 bytes of hex `rikey`. This forces `rtspenc://`: the RTSP control
   connection must prove possession of the secret sent only over the mTLS
   launch response before commands are dispatched. Sunshine carries the
   request-local lease expiry into the pending RTSP session and rejects a
   session that has expired before transport admission.
5. Native Sunshine does not construct `/pair`, `/api/pin`, Web UI, a
   plaintext GameStream responder, or the legacy paired-certificate queue. A
   changed or missing native lease cannot fall back to paired GameStream.

The canonical source contracts are
[`integration/moonlight/patches/0001-system-auth-gamestream-lease.patch`](../integration/moonlight/patches/0001-system-auth-gamestream-lease.patch),
[`integration/sunshine/patches/0008-nvhttp-require-system-auth-media-leases.patch`](../integration/sunshine/patches/0008-nvhttp-require-system-auth-media-leases.patch),
and the nested
[`Simple-Web-Server` patch](../integration/sunshine/simple-web-server/0001-server-http-expose-request-tls-native-handle.patch).
They are replayed by the package builders; a dirty upstream worktree alone is
not a release input.

Leaf expiry is an admission boundary, not a forced media disconnect. It blocks
new sensitive HTTPS requests and pending RTSP setup but does not terminate an
already admitted encrypted stream at the 60--900 second lease limit. A hard
mid-stream cutoff would make ordinary desktop sessions fail predictably; it
would require a separate reviewed in-band reauthentication protocol. Local
logout still stops the Qt-managed Moonlight child immediately, and emergency
revocation rotates the VM CA/restarts Sunshine.

## Verification

Run:

```bash
python3 tests/gamestream_auth/test_gamestream_lease.py
```

It compiles the helper with OpenSSL, creates an actual per-test CA and
client-owned CSR/key, and checks CA validation, `clientAuth`, `CA:FALSE`, the
exact VM SAN and no returned private key.  It also rejects a wrong-audience or
tampered ticket, malformed CSR, and an EC CSR outside the current deliberate
RSA profile.

The focused native path checks are also registered as
`qsunshine_gamestream_lease_issuer` and `qsunshine_gamestream_authd_lease`.
The full Moonlight → Sunshine → QEMU → VirGL gate is documented with the Qt
client in [`QT_DESKTOP_CLIENT.md`](QT_DESKTOP_CLIENT.md); it asserts the
native ticket-to-lease path in addition to video, input, clipboard, files, and
guest scanout handoff.
