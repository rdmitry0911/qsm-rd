# Exact UI rollout and discussion updates — October 27, 2026

The experimental package set **0.4.0+stock4.14~exp6** is now enabled by an
explicit node default on the three requested existing QSM nodes. The two
additional deployments preserve native/shared API and standalone/TCP-8006
profiles respectively. Real Firefox checks on owned LXC seats verified
automatic enable, PATCH arrival, explicit persisted opt-out and re-enable.
Host boot IDs, VM states/PIDs and CT states matched their baselines. Test
seat modes were restored. [Deployment evidence](review-exact-ui-2026-10-27/deployment.md).

[Corrected source1 release, debs and signed checksums](https://github.com/rdmitry0911/qsm-rd/releases/tag/v0.4.0-qsm-direct-20261027-exact-ui-exp6-source1)
are public. Ten assets were downloaded, the signature and eight payload hashes
verified, and ten runtime/build source files compared byte for byte with the
exp6 build snapshot. All five tested deb payload hashes are unchanged. Normal
signed APT remains stock4.13; this snapshot is distributed separately and is
off by default without the explicit node/browser opt-in.

The first published source commit/archive omitted the worker integration,
although the tested debs contained it. The complete integration is committed
in `f7294b8`. The first tag is retained and its release page points to source1.
A short source-publication correction was sent to pve-devel and added to
Bugzilla after verifying the corrected public download.

Before sending, the forum had ten posts (latest Dmitry's stock4.13 post), the
RFC ten messages and Bugzilla #8106 three comments. No new external feedback
or review response was found. Debian ITP/RFS still had two/one public messages;
GitHub had no open issues or PRs.

| Channel | Result |
| --- | --- |
| pve-devel RFC, Alexandre copied | [Main follow-up](https://lore.proxmox.com/pve-devel/179308326004.3823774.17621657443116900285.qsm-exact-ui-exp6-update@taktcycles.com/) and [source correction](https://lore.proxmox.com/pve-devel/179308402281.3826615.2893539585058852580.qsm-exact-ui-source-correction@taktcycles.com/) published. Both exact bodies and Message-IDs verified in the public thread; 12 messages total. |
| Bugzilla #8106 | [Comment #3](https://bugzilla.proxmox.com/show_bug.cgi?id=8106#c3) and [source correction #4](https://bugzilla.proxmox.com/show_bug.cgi?id=8106#c4) published and exact public text/author verified. Status remains NEW; no native-integration/rendernode resolution is claimed. |
| Debian ITP #1151422 and RFS #1151425 | One short [status mail](debian-exact-ui-status-2026-10-27.eml) delivered to both addresses: Debian's recipient MX returned SMTP 250 for each, after a temporary RFS greylist retry. The body was not yet visible in the public BTS mailbox on the final checks. The frozen review source and pending upload sponsorship remain separate; no Debian upload is claimed. |
| Forum topic 187042 | [Ready-to-post text](forum-exact-ui-update-2026-10-27.txt) prepared in the existing style, with the source1 link. Not submitted: no authenticated rdmitry0911 session is available. The user was asked for session access or manual publication. |

The update describes the opt-in lossless pixel-tile layer and the overlapping
text/video test. It labels the measured marker timings as a small shaped-link
LAN-RTT run, reports the 4.5-second large-window refinement limit and missing
live VM/HEVC/QSV/VA-API mixed-content coverage, and makes no RDP performance
claim. The additional node checks are connectivity/default-setting evidence,
not new latency or mixed-content benchmarks.

[Delivery and verification records](review-discussion-2026-10-27/).
