# Upstream submission to Proxmox VE

Proxmox takes no pull requests: patches go to `pve-devel@lists.proxmox.com`
with `git send-email`, and every contributor signs a CLA first
([developer documentation](https://pve.proxmox.com/wiki/Developer_Documentation)).

## Shared-base status (2026-10-14)

QSM now consumes Alexandre Derumier's shared per-VM D-Bus foundation.
qemu-server owns the bus; WebRTC, encoding and guest companion support
remain in the separate QSM package. Native browser signalling and codec
settings use PVE's API on port 8006.

The review-ready series are:

1. [Common qemu-server foundation](patches/shared-qemu-server/0000-cover-letter.patch)
   — six patches, including Alexandre's original patch with his authorship.
2. [WebRTC qemu-server API](patches/webrtc-qemu-server/0000-cover-letter.patch)
   — one patch above the common foundation.
3. [pve-manager UI](patches/shared-pve-manager/0000-cover-letter.patch)
   — three patches for native configuration and Console integration.
4. [Optional Venus compatibility](patches/optional-venus/0000-cover-letter.patch)
   — Markus Frank's adapted patch and a configuration test; not required.

[Installation, protocol and validation](shared-platform-review-2026-10-14.txt)
records source revisions, dependencies, migration commands and verified
limitations. [Review evidence](review-shared-platform-2026-10-14/) contains
build/test logs and the known laboratory display fixture.
[Follow-up draft](pve-devel-shared-platform-followup.eml) is ready for review.
Nothing in this update was sent to the mailing list or forum.

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
