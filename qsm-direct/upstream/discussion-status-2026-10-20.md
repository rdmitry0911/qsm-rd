# Public discussion audit — October 20, 2026

Checked the live forum page, pve-devel public-inbox HTML and full thread mbox,
Bugzilla REST bug/comment endpoints and GitHub issues/pull requests. Search
results alone can be stale: the RFC search snippet still showed four messages,
while the live mbox contains seven. Private email inboxes were not inspected.

| Location | Observed state | Useful next action |
| --- | --- | --- |
| [Forum topic 187042](https://forum.proxmox.com/threads/187042/) | Seven posts. Latest #7 is Dmitry's October 20 stock3/NAT/packet-loss update. No later reply. chrcoluk's earlier #3 says they plan to try it; no test report followed. | Do not repeat #7. A short APT installation follow-up is ready below. |
| [pve-devel RFC thread](https://lore.proxmox.com/pve-devel/30bceb85ccdffaa64be2221199f7193cb9699a01.camel@groupe-cyllene.com/T/) | Seven messages, latest Dmitry on October 16, 06:58 UTC. Alexandre's October 15 questions concerned pveproxy forwarding, the PBS/8007 conflict and latency methodology; the October 16 reply answered them. No newer reply found. | An optional concise progress note can link current standalone testing/distribution and keep the native review questions separate. |
| [Bugzilla #8106](https://bugzilla.proxmox.com/show_bug.cgi?id=8106) | NEW, assigned to bugs@proxmox.com, last changed October 1, 14:59:24 UTC. Only description/comment #0. No review, requests or attachments. | Update the original p2p-based description with the shared per-VM bus proposal and the now-installable standalone implementation. |
| [Related Bugzilla #4771](https://bugzilla.proxmox.com/show_bug.cgi?id=4771) | NEW, unchanged since June 14, 2023; one initial description, rendernode configurability. | Still relevant to the separate native rendernode patch. No new question to answer and no reason to mark it fixed by an external add-on. |
| [GitHub qsm-rd issues](https://github.com/rdmitry0911/qsm-rd/issues) / [PRs](https://github.com/rdmitry0911/qsm-rd/pulls) | No open or closed issue/PR found. Latest release stock3. | Publish the signed APT feed and link installation instructions. |

Bugzilla's creator search returns one issue for rdmitry0911@gmail.com: #8106.
Public-inbox author, WebRTC, dbus-display and Kyber searches found no newer QSM
reply or standalone native patch submission. Alexandre's earlier v2 series
remains a proposal; no upstream acceptance should be inferred from silence.

## Prepared updates (not sent)

- [Bugzilla comment](bugzilla-update-2026-10-20.txt): highest-value correction,
  because the ticket still describes the initial p2p architecture.
- [pve-devel follow-up](pve-devel-standalone-apt-followup-2026-10-20.eml): threaded
  to the October 16 reply, Cc Alexandre; concise status and review request.
- [Forum APT follow-up](forum-apt-update-2026-10-20.txt): matches the existing
  informal first-person style and only adds the new installation route.

The full shared-base patches are linked in the repository, but the public
archive contains RFC discussion rather than a mailed, numbered QSM patch
series. Before a formal series submission, refresh its base/apply/build checks
against current Proxmox source and attach the corresponding validation. This
audit did not repeat those old patch-application tests or assert maintainer
review. The public conversation has no unanswered new question at present.

## Distribution

The [signed GitHub Pages APT archive](../packaging/apt/README.md) supplies the
standalone release using official PVE packages. It does not remove runtime PVE
dependencies and does not constitute admission to Debian. The separate
[Debian distribution assessment](debian-distribution-2026-10-20.md) covers
main/contrib options, source packaging, ITP and sponsorship.

[APT validation evidence](review-apt-2026-10-20/qualification.txt) records the
successful public deployment, actual signed downloads, tamper rejection,
Debian 13 keyring installation and dependency/upgrade simulations.
