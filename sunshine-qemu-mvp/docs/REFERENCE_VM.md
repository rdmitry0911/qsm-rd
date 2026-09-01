# Alpine reference VM

The repository uses a pinned Alpine virt ISO as the small real Linux guest for
manual and E2E qualification.  It is deliberately not checked into Git: the
provisioner downloads it over HTTPS, verifies the publisher's SHA-256 file,
and creates an otherwise empty 2-GiB qcow2 overlay.

```bash
./scripts/provision-alpine-reference-vm.sh
./scripts/launch-alpine-reference-vm.sh
```

The current default is `alpine-virt-3.24.1-x86_64.iso`.  Override both
`ALPINE_VERSION` and `ALPINE_SERIES` together only when intentionally moving
the guest baseline.  The ISO, checksum and overlay live below `vm/`, which is
mode `0700` and ignored by Git.

`launch-alpine-reference-vm.sh` creates a fresh private D-Bus daemon and
prints a mode-`0600` address file.  In another terminal attach a built probe:

```bash
./build-runtime/qemu-display-probe \
  --dbus-address "$(cat vm/alpine-virt-3.24.1/runtime/dbus.address)" \
  --destination org.qemu --no-audio --input-smoke
```

The guest runs with `virtio-vga`, a USB tablet and `-nic none`; its display bus
is Unix-local and it has no guest network.  TCG is the default so it works
without `/dev/kvm`.  Set `ACCEL=kvm` only on a host where KVM is available.

For a non-interactive retained trace use:

```bash
./scripts/run-alpine-reference-e2e.sh
```

It waits for Alpine to load its USB HID path, then requires an absolute input
capability, inline CPU capture, H.264 output and zero session errors.  Evidence
is kept under `artifacts/validation/alpine-reference-e2e/`; unlike the VM image,
the trace is safe to retain or attach to an issue, but is generated rather than
versioned.
