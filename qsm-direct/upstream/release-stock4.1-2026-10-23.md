QSM Direct **0.4.0+stock4.1** preserves static reference detail when network feedback changes the working target. It uses the video code tested as stock4, with version-matched packages and current installation guides.

Project sponsor: [Takt](https://taktcycles.com/).

### Video changes

The previous isolated-loss fix remains. Genuine target changes previously reset NVENC rate control or reopened Intel encoders, causing full refreshes and visible changes in static text. The worker now keeps its configured encoder settings and budgets raw pictures before encoding. Every encoded picture, including an IDR, is paid for before another is admitted. Under constraint, motion cadence falls while reference detail is retained; cheap static pictures can still keep the configured cadence.

Explicit monotonic media timestamps preserve presentation time across skipped raw ticks. Fresh REMB bounds upward recovery. RTP/RTX pacing no longer adds the former hardware-overshoot allowance at low targets. The configured 0.5–100-Mbit/s maximum, default 8, remains available per guest.

The common worker serves VM and LXC and both installation profiles. No new Proxmox backend patch is required for this media change. New viewers, real loss and resizing can still require recovery keyframes.

### Keyboard focus correction

The viewer now releases held keys when the guest video or on-screen keyboard field loses focus inside the page. A document capture listener handles key releases at other elements. Browser autorepeat cannot start a guest press whose original keydown belonged to another field, and holding Copy/Paste does not repeatedly launch clipboard requests. Genuine held keys still use guest autorepeat. These shared viewer changes apply to VM/LXC and both installation profiles.

Node regression tests and real Firefox/Chromium DOM-focus tests cover focus loss while a key is held, return during autorepeat, a press started outside the console and resumed normal typing. They check transmitted input edges, without a production login. Reload or reopen existing consoles to load the updated JavaScript and retire any old held-key state.

### Validation and limits

- All 54 bridge/controller tests and the core admission/timing checks pass. Production worker/fake QEMU Display1 E2E covers resize, cursor, input release and recovery.
- On an unchanging 2560×1440 fixture, NVENC H.264 target reconfiguration changed whole-picture mean luma by up to 11.52/255. With the new admission budget, the maximum is 0.017/255 for H.264 and 0.010/255 for HEVC, with one initial IDR across the target transitions.
- Firefox 157 H.264: controlled 8→2→8-Mbit/s target changes leave the checked 150×100 text region unchanged over 2111 frame comparisons. The final larger static region also remains unchanged; motion and PLI recovery pass.
- Chromium 145 H.264: a dedicated receiver link shaped 20→1→20 Mbit/s adapts and recovers to 60 fps on the same stream. There are zero decoder drops in the completed run. The sudden drop still causes three freezes totalling 5.573 seconds. This is not seamless playback through an arbitrary bandwidth collapse.
- New hardware checks cover NVIDIA NVENC. Intel/AMD hardware and interaction-latency benchmarks were not rerun. These private-bridge media tests complement the earlier PVE authentication/UI/gateway tests; they do not repeat all of them.

[Detailed evidence](https://github.com/rdmitry0911/qsm-rd/blob/v0.4.0-qsm-direct-20261023-stock4.1/qsm-direct/upstream/review-video-budget-2026-10-23/qualification.txt) and the attached review bundle record the setup and limits. The stock4 worker/bridge were installed on the regular node with VM 103 and LXC 105 running. Stock4.1 retains those tested worker/bridge payloads, adds the browser input fix below and refreshes distribution material.

### Installation and upgrade

For PVE 9 / Debian 13 amd64, use the [signed APT repository](https://rdmitry0911.github.io/qsm-rd/) or the attached matching debs. Keep official Debian/PVE repositories enabled for dependencies.

After repository setup, fresh standalone installation:

```sh
apt update
apt install qsm-archive-keyring qsm-pve-direct-standalone
```

Existing installations can update all installed QSM profiles/helpers together:

```sh
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone \
  qsm-pve-direct-lxc qsm-pve-direct-gateway
```

`--only-upgrade` skips uninstalled profiles. QSM service restarts interrupt consoles; reconnect afterwards. The media fix does not require a guest reboot or LXC desktop reprovisioning. The optional gateway remains explicitly enabled and is not activated by installing an inactive profile. **Never install qsm-console-guest on the PVE host.** It is the container payload embedded in the host helper.

Read **INSTALL-STOCK.txt** for VM/LXC setup and native-preview migration, **INSTALL-GATEWAY.txt** for NAT/TCP-8006 setup, and **INSTALL-APT.txt** for key/repository setup. Existing native/shared-display VMs retain their backend requirements until a separately planned conversion; these packages do not remove those dependencies automatically.

The gateway remains an experimental single-node option. HTTPS protects UI/login/signalling; SRTP protects media and DTLS protects input/clipboard. Outer TURN control uses plain TCP, without TURN/TLS. This release does not change that transport or the proxy/SPICE limitations.

The release contains all five QSM debs, complete tracked project sources, installation/update texts, validation material, source commit/provenance and signed SHA256 checksums. Debian ITP/RFS review remains a separate process; this release is an unofficial preview, not Debian or Proxmox archive acceptance.
