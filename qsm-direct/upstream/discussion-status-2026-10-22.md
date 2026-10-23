# Public discussion audit — October 22, 2026

Checked at approximately 13:00–13:05 UTC. Compared with the October 20 audit.
Evidence: live forum HTML, the complete pve-devel thread mbox, public-inbox
searches, Bugzilla REST bug/comment/attachment data, Debian BTS HTML/mbox,
Debian mailing-list thread indexes and GitHub issue/PR APIs. Private email
inboxes were not inspected.

## Findings

| Location | Current state | Action |
| --- | --- | --- |
| [Forum topic 187042](https://forum.proxmox.com/threads/187042/) | Eight posts. Latest is [Dmitry's APT installation update, #8](https://forum.proxmox.com/posts/873560/), October 20 at 15:20 UTC. No later reply. The earlier offer to test from chrcoluk (#3) has no subsequent test report. | The October 20 APT draft has now been published. Do not resend it. No unanswered question or fresh runtime change warrants another post now. |
| [pve-devel RFC thread](https://lore.proxmox.com/pve-devel/30bceb85ccdffaa64be2221199f7193cb9699a01.camel@groupe-cyllene.com/T/) | Eight messages. Latest is Dmitry's standalone/APT/gateway update, October 20 at 15:25 UTC. No later response from Alexandre or a Proxmox maintainer. His October 15 pveproxy/8007/latency questions were answered on October 16. | The previous standalone/APT follow-up draft has now been published. No new reply is required. The native configuration, bus lifetime and API/service boundary still await upstream review. |
| [Bugzilla #8106](https://bugzilla.proxmox.com/show_bug.cgi?id=8106) | NEW; assigned to bugs@proxmox.com; unchanged since October 1, 14:59:24 UTC. Only the original description/comment #0; no attachments or review requests. It still describes p2p=yes/getfd/add_client and the original prototype. | Highest-priority update: describe the proposed shared per-VM bus, distinguish the standalone implementation from native integration, and link current installation/source/review material. [Comment draft](bugzilla-update-2026-10-22.txt). |
| [Related Bugzilla #4771](https://bugzilla.proxmox.com/show_bug.cgi?id=4771) | NEW; unchanged since June 14, 2023; one original comment about rendernode configurability. | No new question to answer. The external QSM add-on does not resolve this native Proxmox enhancement. |
| [Debian ITP #1151422](https://bugs.debian.org/1151422) | Open; only the original submission. Also published in [debian-devel](https://lists.debian.org/debian-devel/2026/10/msg00151.html), with no reply in the live monthly thread index. | The submission still says conventional source packaging is being prepared. An optional [ITP progress draft](debian-itp-progress-2026-10-22.eml) links the completed source review release and RFS. |
| [Debian RFS #1151425](https://bugs.debian.org/1151425) | Open; only the original submission. Also published in [debian-mentors](https://lists.debian.org/debian-mentors/2026/10/msg00101.html), with no reply in the live monthly thread index. No reviewer or upload sponsor has agreed publicly. | No question to answer. The source URL and review request are already present; no reminder after only two days. Independent review, PVE packaging lifecycle checks and Debian Developer upload sponsorship remain outstanding. |
| [GitHub issues](https://github.com/rdmitry0911/qsm-rd/issues) / [pull requests](https://github.com/rdmitry0911/qsm-rd/pulls) | Both APIs return empty lists with state=all. | No open or previously closed issue/PR requiring follow-up. |

The forum #8 post and October 20 pve-devel message were posted after the
previous audit. Its labels “prepared, not sent” describe that earlier check;
they must not be used to resend either update. Bugzilla remains unmodified.

## Additional checks and limits

- Live public-inbox searches for `qsm`, `webrtc`, `dbus-display` and
  `f:rdmitry0911@gmail.com` found no later QSM message or separate numbered
  QSM patch-series submission. The full RFC mbox, rather than stale search
  snippets, establishes the eight-message count.
- Bugzilla's creator search still returns only #8106 for
  rdmitry0911@gmail.com. The related #4771 was checked separately.
- Debian BTS mboxes each contain just one submitted message. Debian list
  search CGI missed the known submissions; direct October thread indexes
  and message pages confirmed both deliveries. Search absence alone is not
  evidence that a message was not delivered.
- The October 20 pve-devel plain-text mail wraps the stock3 release URL in
  the middle of its tag. The APT URL remains intact. In the next substantive
  reply, keep URLs unbroken or use the shorter repository releases URL;
  another status email solely to repeat that link is not necessary.
- No new performance tests, package lifecycle qualification, native patch
  rebase or upstream acceptance are implied by this discussion audit.

## Prepared material and next steps

1. [Bugzilla update](bugzilla-update-2026-10-22.txt): ready to paste into
   #8106, updating the obsolete architecture and linking current work.
2. [Optional ITP progress message](debian-itp-progress-2026-10-22.eml):
   cross-reference the completed source packaging and existing RFS; this is
   an informational update, not a new ITP or another sponsorship request.
3. Before a formal native patch-series submission, refresh the base and
   apply/build checks against current Proxmox source and publish that
   validation. The earlier shared-base patch files remain review proposals.

Neither prepared message was sent or posted during this audit.

## Publication follow-up

The user subsequently requested publication of both prepared updates.
The ITP progress message was sent from rdmitry@taktcycles.com with replies
directed to rdmitry0911@gmail.com. Debian's MX accepted the same queued
message at 14:20:51 UTC after greylisting (SMTP 250,
`1xJtf1-00AeyF-0d`). At 14:23 UTC, the public BTS mbox contained the exact
message and its RFS/source links; publication is verified.

The Bugzilla comment is ready, but has not been posted: the available
environment has no authenticated Bugzilla session or configured API key.
A local API-key file path has been requested from the user.
