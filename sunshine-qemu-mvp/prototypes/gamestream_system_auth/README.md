# GameStream system-auth lease proof

This directory is an interoperability proof for the only safe migration away
from interactive Moonlight/Sunshine PIN pairing: the client owns a fresh key,
PAM authentication authorizes a short-lived X.509 client certificate for one
VM, and the GameStream HTTPS listener verifies the issuing CA, validity,
client-auth EKU, and VM audience before `/launch`.

It is intentionally not wired into the package or current runtime.  Stock
Moonlight and Sunshine cannot consume this lease yet: Moonlight persists one
long-lived global identity and Sunshine trusts an exact paired leaf while
accepting expired certificates.  Treating the existing QSF ticket as if it
authenticated GameStream would leave that paired identity authoritative.

Run the proof on a host with OpenSSL and Python's standard TLS module:

```bash
python3 prototypes/gamestream_system_auth/test_mtls_lease.py
```

It asserts an actual TLS 1.3 client-certificate handshake, private-key proof,
issuer/EKU validation, audience distinction, rejection of an untrusted leaf,
and expiry at a future verification time.  The generated CA, private keys,
CSRs, and certificates remain under a temporary directory and are removed.

The production patch must additionally use a 60--900 second leaf lifetime,
unique per-VM CA, strict admission-time expiry, and a request-local identity
handoff from Sunshine's TLS object into the launch session. A live GameStream
session should not be hard-stopped merely because the admission leaf later
expires: safe continuous reauthentication needs a separately designed,
protocol-level media-session renewal path.
