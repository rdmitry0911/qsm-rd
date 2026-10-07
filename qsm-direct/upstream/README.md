# Upstream submission to Proxmox VE

Proxmox takes no pull requests: patches go to `pve-devel@lists.proxmox.com`
with `git send-email`, and every contributor signs a CLA first
([developer documentation](https://pve.proxmox.com/wiki/Developer_Documentation)).

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

4. **Patch series** -- prepared in [`patches/`](patches/), one directory per
   Proxmox repository, generated with `git format-patch -s` against current
   upstream master; `0000-cover-letter.patch` is each series' cover letter.
   Send a series as is, e.g.

       git send-email --to=pve-devel@lists.proxmox.com patches/qemu-server/*.patch

   Patch 1 (render node, bug #4771) stands on its own and can go first.

## Rules the patches follow

- `Signed-off-by: Dmitry R <rdmitry0911@gmail.com>` on every commit (`git commit -s`)
- imperative subject, `fix #4771: ...` for the bug fix, lines <= 72 columns
- Perl formatted with `proxmox-perltidy` (`make tidy`), JavaScript with eslint
- AGPL-3.0 like the target repositories
