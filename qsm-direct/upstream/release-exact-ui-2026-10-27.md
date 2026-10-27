# QSM Direct experimental exact UI — stock4.14~exp6, source revision 1

This source revision includes the worker integration omitted from the first
published Git snapshot. The tested deb payloads are unchanged and remain
version **0.4.0+stock4.14~exp6**. Use this release's source archive or tag;
the earlier experimental source tag is superseded.

An optional lossless pixel layer for text/UI above the existing desktop video.
The browser can display a small UI change without waiting for the next video
picture. Actual guest-rendered pixels are used; there is no OCR or substitute
font rendering. Frequently changing tiles stay in the video path and refine
when they settle.

This is a separate experimental snapshot. The signed APT preview remains
**stock4.13**. The package default is **off**. The native shared D-Bus proposal
and frozen Debian contrib review source are unchanged.

## What was checked

* A real Wayland LXC, Firefox and H.264/NVENC through TURN/TCP 8006, with a
  separate text-entry window overlapping a visibly playing clip. Typed content,
  scroll, move, resize and close were checked. All 65,536 pixels in a sampled
  512x128 text crop matched the guest source.
* In one small shaped-link run (15 typed keys/case, LAN RTT), median visible
  guest-marker response for the overlapping window changed from 157 to 59 ms
  at 20 Mbit/s, and 176 to 75 ms at 2 Mbit/s. These are not RDP comparisons,
  full-glyph timing or Internet latency guarantees.
* Old/new worker and bridge combinations preserve video and input. Installed
  service checks also cover H.264/libx264 and the automatic node default.
* 27 CTest gates and 83 bridge tests pass. Payload/decompression bounds,
  latest-state isolation, stale-cache recovery, async decode invalidation,
  replay bandwidth and persisted preference behavior have regression checks.

Large-window refinement still took about 4.5 seconds at the narrow target.
The whole desktop is still encoded as video; there is no CopyRect, compositor
window metadata or cropped video substream. Tile/video presentation is not
frame-synchronous. Equivalent live VM, HEVC, QSV and VA-API mixed-content runs,
rapid occlusion/transparency, 4K and high-RTT/lossy-link coverage remain work
before a default-on release. Common-path code does not replace those tests.

## Install and enable

Target: **PVE 9 / Debian 13, amd64**. Verify the detached signature and package
hashes before installation. The signing key is the existing QSM archive key:

`BA99528182B7EADD0199B6986A09741B1DBEAEF2`

GitHub asset filenames use `.exp6`; the package version inside each deb is
`0.4.0+stock4.14~exp6`. Download the matching debs. For an existing installation, include the core
and every already installed host profile in **one apt transaction**. For a
stock-PVE node with standalone, gateway and LXC profiles already installed:

```sh
apt install ./qsm-pve-direct_0.4.0+stock4.14.exp6_amd64.deb \
  ./qsm-pve-direct-standalone_0.4.0+stock4.14.exp6_all.deb \
  ./qsm-pve-direct-gateway_0.4.0+stock4.14.exp6_all.deb \
  ./qsm-pve-direct-lxc_0.4.0+stock4.14.exp6_all.deb
```

Omit profiles not installed. On an existing native/shared-platform node,
update only its installed QSM core/LXC profiles; do not install standalone
or gateway just to try this feature. No patched PVE packages are supplied
or replaced. QSM service restarts interrupt console connections while guests
keep running. This feature does not need a guest package update or reboot.
Never install `qsm-console-guest` on the PVE host.

Reload PVE with cache bypass, reopen the console, then enable
**Settings → Experimental exact UI**. It becomes available after the worker
advertises support and requires the browser's DecompressionStream API.
Disable it in the same place to return to the video-only view.

For an explicit per-node default, create
`/etc/systemd/system/qsm-pve-direct-terminal.service.d/exact-ui-default.conf`:

```ini
[Service]
Environment=QSM_DIRECT_EXACT_UI_DEFAULT=1
```

Run `systemctl daemon-reload` and restart only
`qsm-pve-direct-terminal.service`. Reopen consoles. An explicit browser
checkbox choice overrides the node default and persists.

The new channel is DTLS-protected. The existing optional gateway carries it
through TCP 8006 with no additional public port. The gateway's documented
single-node proxy/listener effects and outer plain-TCP TURN control remain.

## Rollback and review

Disabling the checkbox stops this viewer's overlay. To remove the automatic
node default, remove the drop-in above, reload systemd and restart the QSM
terminal service; explicitly saved browser opt-ins remain until unchecked.

For a package rollback, reinstall stock4.13 for the core and all installed
profiles together using `apt install --allow-downgrades` and explicit versions
or the matching previously saved debs. Preserve the existing native/standalone
profile and gateway configuration.

Design, wire bounds, installation and coverage:
[qsm-direct/docs/EXACT_UI.md](https://github.com/rdmitry0911/qsm-rd/blob/v0.4.0-qsm-direct-20261027-exact-ui-exp6-source1/qsm-direct/docs/EXACT_UI.md).
Measured run and raw traces:
[qsm-direct/upstream/review-exact-ui-2026-10-27](https://github.com/rdmitry0911/qsm-rd/tree/v0.4.0-qsm-direct-20261027-exact-ui-exp6-source1/qsm-direct/upstream/review-exact-ui-2026-10-27).
The release includes the full source snapshot, matching debs, review evidence
and signed SHA256 checksums.

Project sponsor: [Takt](https://taktcycles.com/).
