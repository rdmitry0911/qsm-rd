# qsm-rd

QSM Direct: a WebRTC graphical console for Proxmox VE (virtual machines and
LXC containers), hardware-encoded where the node has an encoder.

Project sponsor: [Takt](https://taktcycles.com/).

[Latest standalone preview: stock4.2](https://github.com/rdmitry0911/qsm-rd/releases/tag/v0.4.0-qsm-direct-20261024-stock4.2)
runs on official PVE 9 packages. Start with the
[installation instructions](qsm-direct/packaging/debian/README.standalone).
The signed [APT repository](https://rdmitry0911.github.io/qsm-rd/)
provides standalone installation and updates with `apt`; follow the
[key and repository setup](qsm-direct/packaging/apt/README.md) first.
The optional [TCP 8006 gateway](qsm-direct/packaging/debian/README.gateway)
also supports a node behind NAT with only one forwarded TCP port. Video,
audio and input remain encrypted by WebRTC; the gateway's TURN control
traffic uses plain TCP, without TLS. The patched native integration remains
a separate upstream preview.

The project lives in [`qsm-direct/`](qsm-direct/) -- see its
[README](qsm-direct/README.md).  Upstream submission material for Proxmox
(bugzilla feature request, pve-devel RFC, patch series) is kept in
[`qsm-direct/upstream/`](qsm-direct/upstream/).

Debian contrib packaging has started with
[ITP #1151422](https://bugs.debian.org/1151422) and
[RFS #1151425](https://bugs.debian.org/1151425). A signed
[source package for review](https://github.com/rdmitry0911/qsm-rd/releases/tag/v0.4.0-debian-contrib-20261020-review2)
is available; Debian Developer review and upload sponsorship are pending.

## Related project

[escrow-box](https://github.com/abx-takt/escrow-box) is our prototype for TPM-sealed
conditional release of secrets, including source-code escrow with a reproducible-build check.
See the [project overview and limitations](https://taktcycles.com/projects/escrow-box.html).
