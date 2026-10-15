Second-node installation: pve-asrock,2026-10-15

Installed QSM and matching LXC0.4.0+shared9, qemu-server9.2.11+shared2,
pve-manager9.2.21, pve-container6.1.14+webrtc1 and common guest6.0.5+webrtc1.
Official dependency updates: libpve-common-perl9.2.1->9.2.3 and storage
9.1.10->9.1.13. Exactly8 packages upgraded, none installed/removed otherwise.
The node initially had pve-manager9.2.18, qemu-server9.2.7 and container6.1.14.
A separate6.1.14 WebRTC variant avoids downgrading its container package.

Its API/ACL patch is the existing implementation cherry-picked onto
1c0488315a1df22a9bba635a81e7543b5ce664b7. Package build and WebRTC schema,
snapshot and idmap tests pass. The ordinary sbuild marker skips nested
userns setup/bindmount tests; this environment cannot run them.

Only stopped VM145 had a legacy QSM display. Preview and explicit stopped-
VM migration saved a root-private backup, preserved unrelated properties
and replaced only the planned display/audio/owned argument fields. VM145
remains stopped. Running VM and CT process IDs are unchanged after install
and migration. No guest was started, stopped or rebooted for this task.

Real authenticated Firefox HTTP:145 status/settings200 via PVE8006,
WebRTC menu available, zero requests to8007. The old signalling service
and certificate timer are disabled; ss confirms no TCP8007 listener.
There is no video qualification on this node because the QSM VM remains
stopped for the user to start. The existing Vilnius VM103/CT105 media
qualification is recorded separately in review-native-lxc-2026-10-15.

Private rollback packages, configuration archive and installation logs:
/root/qsm-shared-platform-20261015 on the target node. A pre-existing
TrueNAS storage-plugin schema warning was present before the upgrade;
no storage configuration or plugin was edited.

Sources: container6.1.14 variant43a524c (API parent commit follows the
existing a8bbb28 changes), QSM1d8f2fb; common/QEMU/manager patches unchanged.
