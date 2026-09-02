# systemd packaging

The Proxmox VE 9 package now ships the production templates in
`packaging/debian/`:

```text
q-sunshine@<VMID>.service
q-sunshine-qsf-control@<VMID>.service
q-sunshine-qsf-gateway@<VMID>.service
q-sunshine-auth@<VMID>.service
q-sunshine-qsf-system-auth-gateway@<VMID>.service
/etc/q-sunshine/instances.d/<VMID>.conf
```

Each Sunshine instance has its own state directory and consumes an
operator-provisioned, private QEMU Display1 D-Bus address. The optional QSF
broker and either its retained mTLS gateway or its TLS/PAM ticket gateway are
separately enabled and use VM-scoped runtime sockets/tokens. The two remote
gateway modes conflict for the same VM and cannot run together. Package
installation does not create a QEMU socket, alter a VM configuration, generate
TLS material, or enable any unit; see
`packaging/debian/README.Debian` for the activation sequence and security
requirements.

The three exposed TLS services use bounded gateway workers together with
systemd task, memory, file-descriptor, and stop-time limits. They are capacity
controls; keep the listener on a protected network and retain the explicit PAM
allowlist for every VM.

The development display probe remains intentionally separate: it writes
diagnostic PPM/MKV files and is not installed as a Moonlight service.

For the ordered Proxmox VE 9 activation sequence, guest/QEMU prerequisites,
native system-auth configuration, client launch, verification, and removal,
see [`docs/RELEASE_NOTES.md`](../../docs/RELEASE_NOTES.md).
