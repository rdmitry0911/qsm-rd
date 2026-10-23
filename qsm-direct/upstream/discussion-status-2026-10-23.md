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

Stock4.1 publication
-------------------
The signed tag/release points at source commit
`9b3320a44df9b804a1a2bdd7c34f85868519717b`. All five debs were built from
that tracked source archive in the isolated Debian 13 build root. The
worker/bridge fingerprints match the tested stock4 video code; the viewer
also includes the new keyboard focus correction. The release includes full
sources, signed checksums, package provenance and discussion drafts.

All four host packages were upgraded to stock4.1 on 192.168.64.20. Final
[versions/service/guest state](review-video-budget-2026-10-23/node-stock4.1-result.json)
and [build provenance](review-video-budget-2026-10-23/build-stock4.1-provenance.json)
are recorded separately from the earlier stock4 qualification. A globbed
manual dpkg install initially reached the LXC helper before the core and
its versioned Breaks rejected it; installing the helper after the core
completed the matching upgrade. The APT transaction handles ordering.
VM 103 and CT 105 remained running. Users must reload the Proxmox page with
cache bypass and reopen consoles to replace pre-upgrade JavaScript.
