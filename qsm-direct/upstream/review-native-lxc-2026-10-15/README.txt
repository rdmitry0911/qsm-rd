Native VM and LXC WebRTC API qualification, 2026-10-15

QSM0.4.0+shared9 and matching LXC package, qemu-server9.2.11+shared2,
pve-container6.1.13+webrtc1, libpve-guest-common-perl6.0.5+webrtc1,
previous pve-manager9.2.21 shared-platform build; installed on192.168.64.20.

VM and container APIs share PVE::WebRTC, a root-owned local service socket.
LXC settings/offer/status use PVE HTTPS8006, normal session cookies/CSRF,
VM.Console for access/reads and VM.Config.Options for policy writes.
The backend validates the explicit kind and selects the existing root-owned
VM or container policy. No ticket or filesystem path is supplied over IPC.

During final browser tests qsm-pve-direct-signal was stopped and ss showed
no TCP8007 listener. Both real Firefox runners also blocked8007 and
asserted zero requests to it. This is ordinary authenticated PVE HTTP, not
a CLI relay. Disposable browser TLS verification was bypassed only for the
existing self-signed node certificate on8006; no8007 endpoint was reached.

CT105: QSM Direct enables; POST offer200, GET/PUT settings200, GET status200.
Settings were saved through actual UI without changing codec/encoder values.
Video1280x726, ready4, ICE/peer connected and growing decoded frame counts.
Unauthenticated GET and missing-CSRF PUT return401. No guest credentials
were entered. Pointer movement revealed the settings control; no guest
login, desktop audio/microphone or clipboard qualification is claimed.
VM103: embedded video1596x830, peer close, ordinary Reconnect button click,
then fresh video with a connected peer. Neither guest was rebooted.

56 Proxmox Python tests pass, as do JS behavior checks (including native
same-origin routing),19 LXC ACL/schema assertions,10 common socket assertions
and the existing common guest tests. Package build logs retain their normal
warnings. pve-container's setup/bindmount userns tests are skipped by its
standard sbuild marker: nested user namespaces are unavailable. Snapshot,
idmap and the new WebRTC tests execute. No userns test pass is claimed.

The first installed shared8 candidate exposed read-only containers.d in the
terminal's systemd sandbox and a UI installer dpkg trigger-state race.
shared9 allows writes only to instances.d and containers.d, and recognizes
configured pending/awaited trigger states while retaining version/hash
allowlisting. The final UI is active and settings PUT succeeds.

VM104 retains its legacy display configuration;8007 was restored after the
successful tests for compatibility. It is not required by native VM103 or
CT105. VM104's migration/restart was not performed in this follow-up.
Rollback packages/config/API snapshot: node/root/qsm-shared-platform-20261014/
rollback-shared8. All patches and discussion texts are drafts; nothing sent.

Source: pve-guest-common5b61a9d, pve-containera8bbb28, qemu-server3b7deebe,
QSM1d8f2fb. Apply shared-guest-common before the common client refactor and
LXC API. Alexandre's existing shared-display authorship is unchanged.
