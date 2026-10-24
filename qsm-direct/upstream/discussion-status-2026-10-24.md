# Discussion publication — October 24, 2026

The user explicitly requested publication of the accumulated update, replacing
the earlier draft-only preference for this task. Live public content was
checked before sending: forum topic 187042 still had eight posts (latest
873560, October 20), the complete pve-devel RFC mbox had eight messages
(latest October 20), and Bugzilla 8106 still had only the original comment.

The new update combines the stock4.1 reference-detail/video-budget fix,
stock4.2 Exact typing and stock4.3 mouse/trackpad/touch actions. It links the
stock4.3 source/debs/qualification and signed APT feed. It distinguishes
injected delivery-gap tests from a physical VPN or hardware trackpad test,
preserves the limitations of holds/double-click timing, and keeps the native
shared-bus/API/UI proposal separate from the standalone package.

| Destination | Material | Publication state |
| --- | --- | --- |
| pve-devel RFC, with Alexandre copied | [Exact sent message](pve-devel-input-video-update-2026-10-24.eml) / [body](pve-devel-input-video-update-2026-10-24.txt) | [Published and verified](https://lore.proxmox.com/pve-devel/179283663410.2345286.10377571210474311317.qsm-stock4.3-update@taktcycles.com/) in the original RFC, threaded to the October 20 message. Alexandre's MX accepted the copy. The Proxmox MX initially deferred delivery, then accepted the same queued message with SMTP 250; the public RFC mbox contains nine messages and the exact new Message-ID/body. See [delivery record](review-discussion-2026-10-24/pve-submission.json). |
| [Forum topic 187042](https://forum.proxmox.com/threads/187042/) | [Combined update](forum-input-video-update-2026-10-24.txt) | Not posted: no authenticated forum session or credentials available. A local access-file path was requested from the user. |
| [Bugzilla 8106](https://bugzilla.proxmox.com/show_bug.cgi?id=8106) | [Updated architecture/runtime comment](bugzilla-update-2026-10-24.txt) | [Published as comment #1](https://bugzilla.proxmox.com/show_bug.cgi?id=8106#c1), October 24 at 10:24 UTC, after the user supplied the API-key configuration. The account and exact public comment body were verified; see [publication record](review-discussion-2026-10-24/bugzilla-submission.json). |

The forum publication record retains authentication as outstanding. The
Bugzilla comment was checked for duplicates before submission and verified
through the public API afterward. No duplicate message was sent when SMTP
temporarily deferred the mailing-list recipient.

The Debian ITP 1151422 and RFS 1151425 were checked separately. Their
conventional source review remains the frozen 0.4.0+20261020-0exp1/review2
submission. The standalone stock4.3 release does not replace that source,
claim Debian acceptance, or warrant another duplicate review request.
