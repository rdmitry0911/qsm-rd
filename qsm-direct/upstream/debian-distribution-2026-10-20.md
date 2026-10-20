# Distribution assessment: QSM, APT and the Debian archive

Updated October 20, 2026. The chosen route is contrib:
[ITP #1151422](https://bugs.debian.org/1151422) is registered and a signed source
package is published for review. [Procedure status](debian-contrib-status-2026-10-20.md)
records the sponsorship request and remaining work. Project sponsor:
[Takt](https://taktcycles.com/); a Debian Developer upload sponsor is still needed.

## What is independent today

Standalone stock3 does not require rebuilt Proxmox packages. Its core binary
package still declares `Depends: pve-manager, qemu-server`, uses PVE access
tickets/permissions and `/etc/pve`, and installs a guarded PVE UI overlay.
The LXC host helper also requires `pve-container`. The standalone profile is
therefore an add-on for PVE 9 on Debian 13, not a generic Debian desktop server.
The independent signing/distribution infrastructure does not change that.

The new [APT archive](../packaging/apt/README.md) solves installation/update
distribution now. It is an unofficial third-party archive on GitHub Pages,
not an upload to Debian. It distributes the five original stock3 QSM packages
plus an archive-keyring package, with a scoped public key, signed Release
metadata, content hashes, package restrictions and explicit optional profiles.
The `main` component name in OUR archive does not mean Debian-main acceptance.

## Possible Debian routes

1. **A general QSM core in Debian main.** Separate capture/encoding/transport and
   an independently usable endpoint from PVE authentication, VM lifecycle,
   configuration and UI. A candidate core must actually build and function
   using Debian-main dependencies. Keep PVE integration in the external APT
   repository initially. Removing a Depends field alone is not sufficient.
2. **A PVE add-on in Debian contrib (chosen).** Debian policy permits free accessories
   requiring software outside Debian in contrib. This is a possible subject
   for a Debian maintainer/sponsor discussion, not an automatic rejection or
   guaranteed acceptance. It still needs DFSG-compatible sources, proper source
   packaging, a useful maintenance plan and a sponsor willing to review it.

Policy reference:
https://www.debian.org/doc/debian-policy/ch-archive.html#archive-areas
Main may not require/recommend packages outside main; contrib explicitly covers
free packages requiring software not in the Debian archive.

## Remaining work before a Debian archive upload

- The contrib scope and ITP are in place. Confirm that scope with Debian
  reviewers; the package remains a PVE add-on, not a generic desktop service.
- The conventional [debian source packaging](../debian/) now provides control,
  debhelper rules, changelog, file-level copyright/license text, source format,
  services, manual pages and autopkgtest. Signed source assets are in the
  [review release](https://github.com/rdmitry0911/qsm-rd/releases/tag/v0.4.0-debian-contrib-20261020-review2).
  Review the packaging and choose long-term upstream release/watch conventions.
- Audit licenses/copyright of the C++/Python/JS code, vendored protocol files,
  generated/bundled browser resources and integration patches. GPL-3.0-or-later
  is declared for QSM; Proxmox-derived/integration material needs accurate
  file-level attribution rather than one blanket license assertion.
- Clean builds on Debian 13 and unstable with networking disabled, Lintian and
  a real source-units autopkgtest pass. Review root services, authentication,
  configuration ownership and service restarts; check the new debhelper
  install/upgrade/remove/purge lifecycle on a PVE testbed and with suitable
  piuparts coverage. Prior stock3 runtime checks do not cover the new packaging.
- Keep the optional NVENC driver path optional; qualify software encoding and
  available Intel/AMD paths using the selected archive's dependencies. Do not
  make Debian-main core depend on an external proprietary driver.
- The source is published on GitHub for review (Mentors hosting is optional).
  Find a Debian Developer willing to review and sponsor an initial experimental
  upload; admission through NEW remains a separate decision. This does not
  immediately add the package to the already released Debian 13.

ITP and sponsorship guidance:
https://www.debian.org/devel/wnpp/
https://mentors.debian.net/intro-maintainers/

The practical immediate path is the maintained signed APT archive. Official
Debian inclusion is a separate packaging/review project; no acceptance or
runtime-independence claim should be inferred from a successful `.deb` build.
