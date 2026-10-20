# Upstream submission to Proxmox VE

Proxmox takes no pull requests: patches go to `pve-devel@lists.proxmox.com`
with `git send-email`, and every contributor signs a CLA first
([developer documentation](https://pve.proxmox.com/wiki/Developer_Documentation)).

## Current status (2026-10-20)

The public RFC thread has seven messages, most recently Dmitry's reply on
October 16. Alexandre's October 15 questions about pveproxy/8007 and the latency
method were answered there. No newer reply or formal maintainer acceptance was
found in the public archive. The linked shared-base series remain proposals.

The [discussion audit](discussion-status-2026-10-20.md) records forum #7,
Bugzilla #8106, related #4771 and GitHub issue/PR status, with update drafts.
The standalone stock3 release runs on official PVE packages, separately from
the native shared-base proposal. A [signed APT repository](../packaging/apt/README.md)
now distributes the standalone debs. This does not make the PVE integration
independent of Proxmox at runtime; see the
[official Debian packaging assessment](debian-distribution-2026-10-20.md).

The sections below preserve the earlier shared-base submission material and
its dated validation scope; their shared9 links are historical.

## Debian contrib procedure (2026-10-20)

[ITP #1151422](https://bugs.debian.org/1151422) and
[RFS #1151425](https://bugs.debian.org/1151425) are registered. Project sponsor:
[Takt](https://taktcycles.com/). The conventional Debian source package and
[source review release](https://github.com/rdmitry0911/qsm-rd/releases/tag/v0.4.0-debian-contrib-20261020-review2)
are available, with clean Debian 13/unstable builds, Lintian and autopkgtest.
See [procedure status](debian-contrib-status-2026-10-20.md) and
[validation](review-debian-contrib-2026-10-20/qualification.txt).
Takt's project sponsorship is distinct from Debian Developer upload sponsorship.

## Shared-base status (2026-10-15)

QSM now consumes Alexandre Derumier's shared per-VM D-Bus foundation.
qemu-server owns the bus; WebRTC, encoding and guest companion support
remain in the separate QSM package. Native browser signalling and codec
settings use PVE's API on port 8006.

The review-ready series are:

1. [Common qemu-server foundation](patches/shared-qemu-server/0000-cover-letter.patch)
   — six patches, including Alexandre's original patch with his authorship.
2. [WebRTC qemu-server API](patches/webrtc-qemu-server/0000-cover-letter.patch)
   — two patches above the common foundation; patch 0002 uses shared-guest-common.
3. [pve-manager UI](patches/shared-pve-manager/0000-cover-letter.patch)
   — three patches for native configuration and Console integration.
4. [Optional Venus compatibility](patches/optional-venus/0000-cover-letter.patch)
   — Markus Frank's adapted patch and a configuration test; not required.
5. [Common guest WebRTC transport](patches/shared-guest-common/0000-cover-letter.patch)
   — shared local service IPC, needed before WebRTC qemu-server patch 0002.
6. [LXC WebRTC API on 6.1.14](patches/webrtc-pve-container-6.1.14/0000-cover-letter.patch)
   — the API and local package version on the newer container baseline.

[Published preview and deb packages](https://github.com/rdmitry0911/qsm-rd/releases/tag/v0.4.0-qsm-direct-20261015-shared9)
include the installation order, hashes and source/review archives.

[Installation, protocol and validation](shared-platform-review-2026-10-14.txt)
records source revisions, dependencies, migration commands and verified
limitations. [Review evidence](review-shared-platform-2026-10-14/) contains
build/test logs and the known laboratory display fixture.
[Follow-up draft](pve-devel-shared-platform-followup.eml) is kept locally.
The shared9 announcement was sent to pve-devel on October 15; the local draft
has since been edited and is not an exact copy of that archived message.

The current consumer package is `0.4.0+shared9`. It includes the Firefox
mDNS/ICE fix and clickable Reconnect after the initial shared3 lab build.
[Firefox verification](review-firefox-2026-10-14/README.txt) records the
reproduction and native HTTP/browser checks. shared7 adds LXC menu retries
and a connection-check link after a network or TLS failure; see the
[LXC menu verification](review-lxc105-menu-2026-10-14/README.txt).
QSM consumer patches are in [qsm-shared-consumer/](patches/qsm-shared-consumer/).

The native LXC follow-up routes availability, offers, status and media
settings through the existing PVE HTTPS origin, just like VM consoles.
Apply [shared-guest-common](patches/shared-guest-common/0000-cover-letter.patch)
before WebRTC qemu-server patch 0002 and
[webrtc-pve-container](patches/webrtc-pve-container/0000-cover-letter.patch).
The [installed-package verification](review-native-lxc-2026-10-15/README.txt)
records Firefox video/settings for CT105 and VM103/Reconnect with the
8007 service stopped. VM104 retains its legacy configuration and the
compatibility service; no guest was restarted for this follow-up.

The [earlier readiness audit](shared-base-audit-2026-10-14.txt) records the
state before this implementation. The original p2p patch directories below
are historical and should not be submitted as the shared-base revision.

## Order

1. **CLA** -- sign the
   [Proxmox Individual CLA](https://www.proxmox.com/images/download/pve/agreements/Proxmox-Individual-CLA.pdf)
   (Harmony-based) and mail it to `office@proxmox.com`.
2. **Feature request** -- file
   [`bugzilla-feature-request.txt`](bugzilla-feature-request.txt) at
   <https://bugzilla.proxmox.com> (product *pve*, component *Qemu*).  Filed as
   [bug #8106](https://bugzilla.proxmox.com/show_bug.cgi?id=8106); the RFC
   and both cover letters reference it.
3. **RFC** -- send [`pve-devel-rfc.eml`](pve-devel-rfc.eml) (from/subject
   headers included) and wait for the maintainers' answer on the design:

       git send-email --to=pve-devel@lists.proxmox.com upstream/pve-devel-rfc.eml

   (one-time setup: <https://git-send-email.io/>; sender
   `Dmitry R <rdmitry0911@gmail.com>`).
   Sent on 2026-10-01.  Follow-up: [`pve-devel-rfc-followup.eml`](pve-devel-rfc-followup.eml)
   answers the RFC thread with Alexandre Derumier in Cc and proposes one
   shared D-Bus display base with his RDP/Kyber series (an earlier RFC on
   the same foundation).  Reply to the RFC in Gmail and add the Cc, or:

       git send-email upstream/pve-devel-rfc-followup.eml

   [`kyber-nvidia/`](kyber-nvidia/) holds the patch the follow-up offers
   for his kyber-qemu-server: `--dmabuf-readback` (EGL import +
   glReadPixels, any encoder) so a virtio-gl guest streams on NVIDIA,
   where his VAAPI zero-copy path cannot; tested on an RTX 3080.

4. **Original patch series** -- preserved in [`patches/`](patches/), one
   directory per Proxmox repository, generated with `git format-patch -s`
   for the initial RFC; `0000-cover-letter.patch` is each cover letter.
   All five qemu-server patches and three pve-manager patches apply to
   upstream master as checked on 2026-10-14, but still implement the
   original p2p design. They are not the proposed shared-base series.
   Use the new shared-base series linked above for review.

   Patch 1 (render node, bug #4771) is independent of the shared-base work.

## Rules the patches follow

- `Signed-off-by: Dmitry R <rdmitry0911@gmail.com>` on every commit (`git commit -s`)
- imperative subject, `fix #4771: ...` for the bug fix, lines <= 72 columns
- Perl formatted with `proxmox-perltidy` (`make tidy`), JavaScript with eslint
- AGPL-3.0 like the target repositories
