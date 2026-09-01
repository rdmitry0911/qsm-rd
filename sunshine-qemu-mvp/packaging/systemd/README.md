# systemd packaging

The Proxmox VE 9 package now ships the production templates in
`packaging/debian/`:

```text
q-sunshine@<VMID>.service
q-sunshine-qsf-control@<VMID>.service
q-sunshine-qsf-gateway@<VMID>.service
/etc/q-sunshine/instances.d/<VMID>.conf
```

Each Sunshine instance has its own state directory and consumes an
operator-provisioned, private QEMU Display1 D-Bus address.  The optional QSF
broker and mTLS gateway are separately enabled and use VM-scoped runtime
sockets/tokens.  Package installation does not create a QEMU socket, alter a
VM configuration, generate TLS material, or enable any unit; see
`packaging/debian/README.Debian` for the activation sequence and security
requirements.

The development display probe remains intentionally separate: it writes
diagnostic PPM/MKV files and is not installed as a Moonlight service.
