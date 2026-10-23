# Public discussion and stock4.1 material — October 23, 2026

Live public pages/mboxes were checked before preparing the release updates.
The previously published forum/APT and pve-devel follow-ups are already online.

| Location | Observed state | Prepared action |
| --- | --- | --- |
| [Forum topic 187042](https://forum.proxmox.com/threads/187042/) | Eight posts; latest is Dmitry's October 20 APT update, #8. No later reply or new user test report. | [Stock4.1 follow-up](forum-video-budget-update-2026-10-23.txt) explains the broader genuine-target-change fix. An optional [dated note for the first post](forum-first-post-note-2026-10-23.txt) corrects the prototype's old port/package information. |
| [pve-devel RFC](https://lore.proxmox.com/pve-devel/30bceb85ccdffaa64be2221199f7193cb9699a01.camel@groupe-cyllene.com/T/) | Eight messages; latest is Dmitry on October 20. Alexandre's October 15 questions were answered on October 16. No newer reply. | [Threaded media update](pve-devel-video-budget-update-2026-10-23.eml), replying to the October 20 message. It distinguishes the shared worker change from the native bus/API review. |
| [Bugzilla #8106](https://bugzilla.proxmox.com/show_bug.cgi?id=8106) | Only the October 1 description/comment #0, still describing p2p attachment. | [Current comment](bugzilla-update-2026-10-23.txt) supersedes the unsent October 22 draft and adds stock4.1. Publication still requires Bugzilla authentication. |
| [Debian ITP #1151422](https://bugs.debian.org/1151422) | Two submitted messages: original ITP and the delivered October 22 source/RFS progress update. No reviewer reply. | The [October 22 delivery evidence](review-discussion-2026-10-22/itp-progress-submission.json) records verified publication. No duplicate ITP or reminder. |
| [Debian RFS #1151425](https://bugs.debian.org/1151425) | Original submission only; no reviewer or upload sponsor has agreed publicly. | The currently submitted conventional source remains 0.4.0+20261020-0exp1/review2. The standalone stock4.1 release does not replace that frozen source upload or assert Debian acceptance. |

The user requested ready-to-post texts. The forum, first-post note, mailing-list
message and Bugzilla comment are prepared; none is sent or posted as part of
this release. Repository commits, release assets and the signed APT archive
are published separately.

The [video qualification](review-video-budget-2026-10-23/qualification.txt)
records the actual regression and new worker checks. In particular, it
distinguishes Firefox target changes on LAN from Chromium physical shaping,
and documents remaining freezes after a sudden capacity collapse.
