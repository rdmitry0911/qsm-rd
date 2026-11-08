# QSM Direct 0.4.0+stock4.14~exp233: the console shows qdev-host's out-of-process VirGL GPU

Unofficial preview for **PVE 9 / Debian 13, amd64**, now the current version on the
[signed APT repository](https://rdmitry0911.github.io/qsm-rd/). It needs no patched
Proxmox packages. Everything in the [exp231 notes](release-stock4.14-exp231-2026-11-08.md)
applies; this version adds one thing and the repository one package version.

## What changed since stock4.14~exp231

* **A VirGL GPU of qdev-host is a QSM VirGL display.** qdev-host 0.5.0's `virgl-gpu` plugin
  runs a VM's GPU in QEMU's own out-of-process `vhost-user-gpu` (shipped in pve-qemu-kvm) and
  puts `vhost-user-vga` into the VM's `args` in place of QSM's own `virtio-vga-gl`. QSM took
  that VirGL display without its own GPU for an old configuration: the QSM Direct console
  was not offered and the terminal service refused it. Now the VirGL display with exactly
  one such GPU is managed. The PVE UI and the terminal both accept it, the Display dialog
  adds no QSM GPU next to it on save, and enabling Display1 on a VM that has only that GPU
  adds the display alone. The CPU profile and switching Display1 off are refused while it
  exists, because the GPU's frames are dma-bufs that only the GL display shows.
* The repository serves **qdev-host and qdev-host-pve 0.5.0**: Edit in the Hardware panel
  and the `virgl-gpu` plugin. See its [release](https://github.com/rdmitry0911/qdev-host/releases/tag/v0.5.0).

exp232 was a local build with only the UI half and is not published.

## Checked

On 172.16.16.17 (PVE 9.2.21, pve-qemu-kvm 11.0.3, AMD Raphael render node), VM 990, a
Debian 13 guest with QSM Display1 VirGL, given a `virgl-gpu` from the Hardware panel:
the guest found `virtio-vga` with `+virgl +edid` and two virgl capability sets, its
framebuffer console on that GPU; the QSM Direct popup showed it twice (first video 3.8 and
2.3 s, 1024x768, captured as dma-buf, encoded with VA-API H.264). Adding and removing the
GPU gave back the VM's config byte for byte, with QSM's own GPU in its old place.
The UI and terminal changes have unit tests: the console overlay suite and 19 terminal
autoprovision tests pass, and the isolated build ran browser bridge 283 and signal/gateway
48 tests. A 3D application in the guest was not run (the test guest has no Mesa).

## Update

Through the repository, with every installed QSM profile (pve-ui-overlays must be in the pin
file, see the exp231 notes):

```sh
apt update
apt install --only-upgrade qsm-archive-keyring qsm-pve-direct qsm-pve-direct-standalone
# add qsm-pve-direct-lxc and qsm-pve-direct-gateway if installed, qdev-host qdev-host-pve if wanted
```

Or the debs attached here with `apt install ./file.deb ...`. Reload PVE with cache bypass
and reopen the consoles. Guest packages need no update.

## Go back

To exp231: `apt install --allow-downgrades` with `=0.4.0+stock4.14~exp231` for every installed
QSM host package in one transaction. A VM with a qdev-host GPU then loses its QSM console
until the GPU is removed.

## Review material

Attached: the source of the release commit, the seven QSM debs, qdev-host 0.5.0, signed
checksums, build provenance, install guides and `review-stock4.14-exp233-20261108.tar.gz`.
Summary: `qsm-direct/docs/rdp-deployment-exp233-20261108.json`. Signing key:
`BA99528182B7EADD0199B6986A09741B1DBEAEF2`.

Project sponsor: [Takt](https://taktcycles.com/).
