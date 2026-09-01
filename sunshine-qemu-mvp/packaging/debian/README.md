# Proxmox VE 9 package build

`q-sunshine-pve` is built in a disposable Debian 13 (Trixie) chroot, the
userspace base for Proxmox VE 9.  The outer host only needs an amd64 Linux
system with passwordless `sudo`, `debootstrap`, `tar`, `git`, and network
access to Debian and the pinned LizardByte build-deps release.

Build from the release commit (not from a tree that has unreviewed changes)
and ensure the checked-out tree contains the pinned upstream sources required
by the recipe:

```bash
test -f .upstream/Sunshine/CMakeLists.txt
test -f .upstream/Sunshine/third-party/moonlight-common-c/enet/CMakeLists.txt
test -f .upstream/libva-2.21.0/meson.build
sudo apt-get install debootstrap
QSUNSHINE_DEB_VERSION='0.4.0+g<release-revision>' \
  ./scripts/build-proxmox9-deb-in-trixie.sh
```

If the Sunshine submodule check fails, populate the pinned worktree before
building: `git -C .upstream/Sunshine submodule update --init --recursive`.
The required QEMU Display1 replay, its base revision, and its patch boundary
are documented in `docs/SUNSHINE_QEMU_INTEGRATION.md`; packaging a stock
Sunshine tag without that replay is unsupported.

The resulting `.deb` and its SHA-256 sidecar are written to `dist/` by
default.  Set `QSUNSHINE_DEB_OUTPUT_DIR` to write them elsewhere.  The driver
retains its `/var/tmp/q-sunshine-trixie.*` chroot and source archive on
purpose: they are the evidence for a failed build and may be inspected before
an operator removes that exact temporary directory.

The build gate does all of the following in the clean Trixie userspace:

- compiles the pinned Sunshine source with QEMU Display1 enabled and all host
  desktop/audio backends disabled;
- builds and stages the private libva ABI beneath `/usr/lib/q-sunshine`, checks
  its runtime prefix and relative loader scope, and rejects source/build paths
  in the shipped ELF files;
- derives Debian shared-library dependencies with `dpkg-shlibdeps`, runs
  `lintian --fail-on error`, then installs the produced package in the same
  clean chroot and runs `q-sunshine --version`.

On the Proxmox host, install the artifact with `apt install ./<artifact>.deb`,
then follow the installed
`/usr/share/doc/q-sunshine-pve/README.Debian`.  Installation does not enable a
service or alter a VM.  Run `q-sunshine-preflight --virgl` only after the
private QEMU Display1 socket and the render node have been provisioned.
