# Installing QSM Direct on Proxmox VE 9

Install `qsm-pve-direct` only on the PVE node. An operator needs only a modern browser with WebRTC H.264/Opus support. No client application is installed, and nothing is patched inside pveproxy or pvedaemon.

```bash
apt install ./qsm-pve-direct_*.deb
systemctl enable --now qsm-pve-direct-terminal.service qsm-pve-direct-signal.service
systemctl status qsm-pve-direct-signal.service
```

Authorisation and signalling run in a standalone node-local HTTPS service (`qsm-pve-direct-signal`, TCP 8007, using the node certificate) that relays the browser's ticket to PVE's own `/access/ticket`; pveproxy and pvedaemon are never modified, so any `pve-manager` 9.x works and a point-release upgrade does not disable the console. The package still adds a small PVE UI script by patching `index.html.tpl` only for the `pve-manager` versions it knows, and restores the stock template on removal or an unknown version. Open TCP 8007 to operator networks in the host firewall, and reach PVE by a host name the node certificate is valid for so the cross-origin signalling fetch is not blocked.

As `root@pam` (the setting edits the VM's `args:` line, which Proxmox reserves for root), open **Hardware → Display → Advanced** and enable **QSM Display1**. Then **stop and start the VM**: the new display devices exist only after QEMU is relaunched, and a reboot from inside the guest is not enough.

- **VirGL GPU (GL):** QSM creates a private `virtio-vga-gl` and GL D-Bus display. VNC is not used with that GL backend.
- **CPU — Standard VGA or VirtIO (no GL):** the selected PVE `std` or non-GL `virtio` adapter remains in place and QSM adds a non-GL D-Bus display. No GPU or render node is required; stock VNC may run in parallel.

Encoder settings are stored per VM in `/etc/qsm-pve-direct/instances.d/<VMID>.conf`, mode `0600`. The default codec policy is `auto`: when the browser's offer contains H.265 and the node has a working `hevc_nvenc`, `hevc_qsv` or `hevc_vaapi`, QSM negotiates HEVC (answering with the browser's own payload type and profile/tier/level); otherwise it uses the H.264 path (NVENC, QSV, VA-API, then `libx264`). `QSM_DIRECT_CODEC=hevc` forces HEVC and requires a hardware encoder; it never silently uses a CPU encoder or emits an H.264 stream labelled as HEVC. HEVC is qualified against Chrome on Apple silicon with `hevc_nvenc`.

For bidirectional clipboard in a Linux guest, install the matching `qsm-desktop-agent_*.deb`, then configure the desktop user once:

```bash
apt install ./qsm-desktop-agent_*.deb
qsm-desktop-agent-setup USER
```

QEMU Guest Agent remains useful for standard PVE operations, but it does not replace the desktop-session companion: it cannot access a graphical session's clipboard.
