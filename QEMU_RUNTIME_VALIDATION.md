# Real QEMU validation

## Target

The real runtime gate intentionally needs no GPU, KVM, installed guest or
external disk image. It boots a project-owned 512-byte BIOS boot sector under
TCG, registers the project as a QEMU D-Bus display listener and performs CPU
capture/input/reconnect checks.

## Install on Debian 13 / Ubuntu

```bash
sudo ./scripts/install-qemu-debian.sh
```

The minimum relevant packages are:

```text
qemu-system-x86
qemu-system-modules-opengl   # Debian 13 D-Bus/OpenGL display modules
# or: qemu-system-gui        # package layout used by some Ubuntu releases
qemu-utils
seabios
dbus-daemon
binutils
ffmpeg
```

The script chooses the available display-module package and verifies both
`qemu-system-x86_64` and the `dbus` entry in `qemu-system-x86_64 -display help`.

## Run

```bash
./scripts/run-real-qemu-selftest.sh
```

The runner:

1. builds `tests/fixtures/qmdp_vga_smoke.S` into an exact 512-byte boot sector;
2. validates the `55 aa` boot signature and embeds it in a 1.44-MiB floppy image;
3. starts a private D-Bus session;
4. launches QEMU with `-accel tcg`, `-device VGA` and `-display dbus,gl=off`;
5. keeps one resilient probe alive;
6. replaces the entire QEMU process twice;
7. checks that frames resume after both replacements;
8. checks that viewport state is re-applied and IDR transitions are requested;
9. fails on probe errors, missing generations or missing captured frames.

CMake automatically adds `qmdp_real_qemu_e2e` when the QEMU binary is present.

## Result in the supplied execution container

The test harness and boot image passed their local checks:

```text
boot sector size: 512 bytes
floppy image size: 1474560 bytes
boot signature: 55 aa
shell syntax: PASS
CMake conditional test registration: PASS
```

The real QEMU process was not run because the binary could not be installed.
The recorded installation attempt shows:

```text
Temporary failure resolving 'deb.debian.org'
apt exit status: 100
qemu-system-x86_64: absent
```

This is an execution-container network restriction, not a QEMU/project test
failure. Evidence is retained under:

```text
artifacts/validation/real-qemu-install/
artifacts/validation/real-qemu/
artifacts/validation/host_inventory.json
```
