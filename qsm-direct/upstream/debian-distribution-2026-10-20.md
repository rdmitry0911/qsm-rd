# Distribution assessment: QSM, APT and the Debian archive

Reviewed October 20, 2026. No Debian ITP, sponsorship request or upstream message
was submitted by this work.

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
2. **A PVE add-on in Debian contrib.** Debian policy permits free accessories
   requiring software outside Debian in contrib. This is a possible subject
   for a Debian maintainer/sponsor discussion, not an automatic rejection or
   guaranteed acceptance. It still needs DFSG-compatible sources, proper source
   packaging, a useful maintenance plan and a sponsor willing to review it.

Policy reference:
https://www.debian.org/doc/debian-policy/ch-archive.html#archive-areas
Main may not require/recommend packages outside main; contrib explicitly covers
free packages requiring software not in the Debian archive.

## Work before a Debian submission

- Choose the independently useful core or contrib add-on scope; search WNPP
  for an existing request, then file one Intent To Package (ITP) per proposed
  source package. Do not file an ITP claiming that today's PVE daemon is a
  generic Debian service.
- Replace the current direct `dpkg-deb --build` release scripts with a proper
  source package: `debian/control`, `rules` (debhelper), `changelog`,
  `copyright`, source format, install/service files and upstream watch metadata.
  Produce an upstream source tarball, `.dsc` and signed source `.changes`.
- Audit licenses/copyright of the C++/Python/JS code, vendored protocol files,
  generated/bundled browser resources and integration patches. GPL-3.0-or-later
  is declared for QSM; Proxmox-derived/integration material needs accurate
  file-level attribution rather than one blanket license assertion.
- Build in a clean Debian environment using only declared Build-Depends,
  without network downloads during the build. Add meaningful autopkgtests,
  run Lintian and check install/upgrade/remove/purge with piuparts. Review
  root services, authentication, configuration ownership and service restarts.
- Keep the optional NVENC driver path optional; qualify software encoding and
  available Intel/AMD paths using the selected archive's dependencies. Do not
  make Debian-main core depend on an external proprietary driver.
- Upload the source package for review to mentors.debian.net and request a
  sponsor. New packages normally enter unstable through Debian's review/NEW
  process; this does not immediately add them to the already released Debian 13.

ITP and sponsorship guidance:
https://www.debian.org/devel/wnpp/
https://mentors.debian.net/intro-maintainers/

The practical immediate path is the maintained signed APT archive. Official
Debian inclusion is a separate packaging/review project; no acceptance or
runtime-independence claim should be inferred from a successful `.deb` build.
