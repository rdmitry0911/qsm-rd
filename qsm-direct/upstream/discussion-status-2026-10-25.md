# Discussion update — October 25, 2026

The user requested an update in the existing discussions and a prepared forum
message. The pve-devel and Bugzilla follow-ups were published; the forum text
remains a draft for the user to post.

Public content was checked before publication. The RFC had nine messages,
latest Dmitry's October 24 stock4.3 update; the forum had nine posts, latest
[873965](https://forum.proxmox.com/posts/873965/), also the stock4.3 update.
Bugzilla #8106 remained NEW with comments #0 and #1. No new maintainer reply or
external test report was found. Debian ITP #1151422 had the original request
and October 22 progress message; RFS #1151425 had only the original request.
GitHub had no open issues or pull requests.

| Destination | Material | Result |
| --- | --- | --- |
| pve-devel RFC, Alexandre copied | [Sent message](pve-devel-input-routing-update-2026-10-25.eml), [body](pve-devel-input-routing-update-2026-10-25.txt) | [Published follow-up](https://lore.proxmox.com/pve-devel/179293981837.3090704.6601261792618612901.qsm-stock4.13-update@taktcycles.com/). Threaded to the October 24 update. The public thread now has ten messages; Message-ID and complete body verified. [Delivery record](review-discussion-2026-10-25/pve-submission.json). |
| Bugzilla #8106 | [Comment](bugzilla-input-routing-update-2026-10-25.txt) | [Published as comment #2](https://bugzilla.proxmox.com/show_bug.cgi?id=8106#c2), October 25 at 14:51:30 UTC. Author and exact public body verified; duplicate checks performed. [Publication record](review-discussion-2026-10-25/bugzilla-submission.json). |
| Forum topic 187042 | [Ready-to-post text](forum-input-routing-update-2026-10-25.txt) | Prepared only, as requested; no forum submission attempted. |
| Debian ITP/RFS | Existing frozen source review | No new review response and no new source upload; no duplicate update sent. |

The follow-up focuses on changes since the published stock4.3 post: explicit
fullscreen keyboard capture, focus/settings/control fixes, complete shortcut
rules, logical modifier remapping and the stock4.13 modifier-order fix. It
also reports another successful standalone TCP 8006 node deployment. The
native shared-bus/API/UI proposal remains pending upstream review, and the
Debian source review is not replaced by the standalone runtime release.

The [additional gateway check](review-discussion-2026-10-25/additional-stock-node-gateway.json)
used Firefox 157, a Debian 13 / Plasma Wayland LXC desktop and relay-only
TURN/TCP on port 8006. Video, control and pointer channels were verified;
private service listeners stayed on loopback. VM PIDs, guest states and the
host boot were preserved. This is connectivity evidence, not a new physical
Internet-router test or latency benchmark. Node addresses, guest identifiers,
credentials, TURN secrets and personal desktop contents are excluded from
the public record.

An API-key-header write attempt to Bugzilla returned HTTP 401 and created no
comment. After confirming the public comment list was unchanged, the same
comment was submitted through the previously working authenticated JSON
`api_key` field and verified. No credential was logged or committed.
