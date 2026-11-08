# QSM Direct 0.4.0+stock4.14~exp231: shared PVE page entry, routes on the TCP 8006 gateway

Unofficial preview for **PVE 9 / Debian 13, amd64**, now the current version on the
[signed APT repository](https://rdmitry0911.github.io/qsm-rd/). It needs no patched
Proxmox packages. The debs are the ones installed on the three test nodes, unchanged.
Builds exp224 to exp230 were local test builds and are not published.

## What changed since stock4.14~exp223

* **The PVE page entry goes through [pve-ui-overlays](https://github.com/rdmitry0911/pve-ui-overlays).**
  Proxmox VE has no plug-in API for its web UI, so qsm-pve-direct diverted
  `index.html.tpl` itself. A file has one dpkg diversion, so a second add-on doing the
  same found a "foreign diversion", and which one worked depended on the install order.
  The new dependency `pve-ui-overlays` owns that one diversion and renders every
  registered add-on; qsm-pve-direct only ships a description of its script. The
  upgrade takes the diversion over from older QSM versions, whose prerm does not run
  on an upgrade, and removes the retired page injector's leftovers. The safety rules
  stay: the page is changed only for reviewed `pve-manager` versions and template
  checksums, and the stock page comes back otherwise.
* **The TCP 8006 gateway carries other node services.** A node behind a router that
  forwards only TCP 8006 could reach no other HTTPS port of the node. A package now
  ships a route in `/usr/share/qsm-pve-direct-gateway/routes.d/`, and
  `https://NODE:8006/PREFIX/` reaches that service on 127.0.0.1, verified against the
  cluster CA like the signal service. A dpkg trigger applies a new or removed route
  with an HAProxy reload: no pveproxy restart, open consoles stay. Only the route lines
  of the gateway's `haproxy.cfg` change; lines added by hand after enable, such as
  more `bind` addresses, stay. `qsm-pve-direct-gateway routes` lists what is
  published, `refresh` applies a route edited under `/etc`. qdev-host-pve 0.4.0 is
  the first user.
* **Signalling works on clusters first installed with an older PVE.** Python 3.13
  rejects a CA without the key usage extension, which such clusters have, so every
  ticket check failed and every console got 502 "authority unavailable".
* **QSM Display1 on a VM that already carries QSM's guest channel and input
  arguments** (a VM moved off the native `dbus-display`) no longer gets a second set;
  QEMU refused the duplicate chardev id and the VM did not start.
* **The terminal service** uses the VM's private bus for a policy file that sets only
  codecs, as with no file, and skips a VM with an invalid policy at start instead of
  stopping for every guest.

Guest packages carry the version, but their source is that of exp223: nothing to do
in guests.

## Also in the repository

The signed repository now serves, with their own versions:

* `pve-ui-overlays` 0.1.0, the new dependency;
* `qdev-host` and `qdev-host-pve` 0.4.0, the PVE device host from the same author,
  which hot-plugs emulated PCIe devices (vfio-user) into running q35 VMs and adds
  them to the VM's Hardware panel. On a node with the gateway its panel reaches its
  service through TCP 8006. See [qdev-host](https://github.com/rdmitry0911/qdev-host).

## Checked

* **Nested PVE lab**, official PVE 9.2.21: exp223 with the gateway enabled, upgraded
  to exp230 (exp231 differs only in how a route is written into `haproxy.cfg`, see
  below), pve-ui-overlays and qdev-host in one transaction.
  pve-ui-overlays took the template over from exp223; the trigger added the qdev-host
  route; pveproxy and the HAProxy master kept their PIDs. Through the gateway: VM
  console popup twice (first video 2.4 and 0.9 s) and embedded with a window; the
  qdev-host panel with requests only to its same-origin path; an add and remove
  through it. qdev-host's checks refused a VM whose config this qemu-server cannot
  read and a node without snippets storage, before any change. A gateway disable and
  enable put the route in at once. The lab was then returned to its original state.
* **192.168.64.20**, exp223 with the gateway: VM 103 popup through the gateway,
  first video 1.7 and 1.8 s; the qdev-host panel through TCP 8006.
* **172.16.16.17**, exp229 without the gateway; the gateway was enabled with this
  update. CT 147 popup through the gateway, first video 3.9, 2.2 and 3.9 s. qdev-host
  on VM 990 through TCP 8006: added, attached as 01:00.0, removed; the VM's config
  was the same as before.
* **10.30.0.240**, exp229 with the gateway: the qdev-host panel through TCP 8006 on
  VM 108. HAProxy listens on all ten addresses again, and PVE answered on the node
  address and both public ones checked; the exp231 upgrade, `prepare` and `refresh`
  left `haproxy.cfg` byte for byte unchanged.
* Unit tests in the isolated build environment: browser bridge 283, signal and
  gateway 48 (one skipped needs root), all pass; APT manifest tests pass.

**One incident during this work.** exp230, the first build with routes, rendered the
gateway's `haproxy.cfg` from its template. On 10.30.0.240 the administrator had added
`bind` lines for eight more addresses after enable; the route refresh dropped them,
and for 44 s PVE took no new connections on those addresses until the file was
restored by hand. exp231 edits only the route lines, which was checked against that
node's real file in both directions. exp230 was never published.

## Known limits

* **An existing APT setup must allow pve-ui-overlays** first: add it (and `qdev-host
  qdev-host-pve` if you want them) to the `Package:` line of
  `/etc/apt/preferences.d/qsm-direct.pref`. Otherwise the repository's `Pin-Priority:
  -1` for other packages keeps the QSM packages back.
* Gateway routes are for services of the same single node that listen on 127.0.0.1
  with the node certificate. Authentication stays the service's own.
* Consoles were checked by automated popup and embedded runs, not by long sessions;
  no console of 10.30.0.240 was opened, because its only QSM container was in use.
* LXC: on a Debian 13 container `qsm-pve-direct-lxc enable` stops at the package
  step, asking for `plasma-workspace-wayland`, which Debian 13 does not have; the
  guest's screen-locker defaults are written with `kwriteconfig5`, absent in Plasma 6;
  after a host GPU reset (seen once with amdgpu) the nested KWin of a running desktop
  stays black until the session logs out. Not fixed in this version.
* Everything in the [exp223 notes](release-stock4.14-exp223-2026-11-07.md) still
  applies.

## Update

**Through the signed repository**, after adding `pve-ui-overlays` to the pin file as
above, with every installed QSM profile in one transaction:

```sh
apt update
apt install --only-upgrade qsm-archive-keyring qsm-pve-direct qsm-pve-direct-standalone
# add qsm-pve-direct-lxc and qsm-pve-direct-gateway if they are installed
```

The core pulls in `pve-ui-overlays`. **From files**, include its deb:

```sh
apt install ./pve-ui-overlays_0.1.0_all.deb \
  ./qsm-pve-direct_0.4.0+stock4.14.exp231_amd64.deb \
  ./qsm-pve-direct-standalone_0.4.0+stock4.14.exp231_all.deb
# plus the gateway and lxc debs if they are installed
```

GitHub stores `~` in asset names as `.`. Then reload PVE with cache bypass and reopen
the consoles; `pve-ui-overlays status` should say `active`. Restarting QSM services
interrupts open consoles while guests keep running. An enabled gateway stays enabled.

**qdev-host**, optional: `apt install qdev-host qdev-host-pve`.

## Go back

`apt install --allow-downgrades` with `=0.4.0+stock4.14~exp223` for **every** installed
QSM host package in one transaction; APT removes an installed profile that the command
leaves out, so read its plan. exp223 diverts the template itself and cannot while
pve-ui-overlays holds it, so hand it back afterwards:

```sh
systemctl stop pve-ui-overlays-reconcile.path
pve-ui-overlays remove
qsm-pve-direct-ui install
systemctl start pve-ui-overlays-reconcile.path
```

## Review material

Attached: the source of the release commit, the seven QSM debs, the three companion
debs, signed checksums, build provenance, install guides and
`review-stock4.14-exp231-20261108.tar.gz` with the build scripts, node states and
rollback notes behind this page. The summary is
`qsm-direct/docs/rdp-deployment-exp231-20261108.json`. Signing key:
`BA99528182B7EADD0199B6986A09741B1DBEAEF2`.

Project sponsor: [Takt](https://taktcycles.com/).
