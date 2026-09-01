# Pinned Sunshine source

The CPU Display1 patch is generated against the following exact upstream base:

```text
repository: https://github.com/LizardByte/Sunshine.git
tag:        v2026.830.223700
commit:     4f39fc116294abf8241bcd30e1b1e23d371e6e7b
patch:      patches/0001-platform-linux-add-QEMU-Display1-CPU-capture.patch
patch SHA-256: 717258e53c73f40a51433067326b50375e91b511e53d9dec767ae5e84e2f3bdd
```

The patch is an opt-in Linux build feature: configure upstream with
`-DSUNSHINE_ENABLE_QEMU_DBUS=ON`, then select it at runtime with
`capture=qemu_dbus` and `encoder=software`.  It consumes a per-process
`SUNSHINE_QEMU_DBUS_ADDRESS`; it does not place a VM D-Bus address in Sunshine's
public network configuration.

Check that the patch still applies before updating the pin:

```bash
git -C .upstream/Sunshine checkout v2026.830.223700
git -C .upstream/Sunshine apply --check \
  ../../integration/sunshine/patches/0001-platform-linux-add-QEMU-Display1-CPU-capture.patch
```

This pin is a source-integration checkpoint, not an upstream Sunshine release
or an endorsement by the Sunshine project.
