# Installing QSM Direct on Proxmox VE 9

Install `qsm-pve-direct` only on the PVE node. An operator needs only a modern browser with WebRTC H.264/Opus support. No client application is installed, and nothing is patched inside pveproxy or pvedaemon.

```bash
apt install ./qsm-pve-direct_*.deb
systemctl enable --now qsm-pve-direct-terminal.service qsm-pve-direct-signal.service
systemctl status qsm-pve-direct-signal.service
```

Authorisation and signalling run in a standalone node-local HTTPS service (`qsm-pve-direct-signal`, TCP 8007, using the node certificate) that relays the browser's ticket to PVE's own `/access/ticket`; pveproxy and pvedaemon are never modified. The package does add one small PVE UI script by diverting `index.html.tpl`, only for the `pve-manager` versions (and template checksums) it knows; on removal or an unknown version it restores the stock template, and VMs then show the ordinary noVNC console until a package update adds that version (`qsm-pve-direct-ui status`). Open TCP 8007 to operator networks in the host firewall, and reach PVE by a host name the node certificate is valid for so the cross-origin signalling fetch is not blocked.

As `root@pam` (the setting edits the VM's `args:` line, which Proxmox reserves for root), open **Hardware → Display → Advanced** and enable **QSM Display1**. Then **stop and start the VM**: the new display devices exist only after QEMU is relaunched, and a reboot from inside the guest is not enough.

- **VirGL GPU (GL):** QSM creates a private `virtio-vga-gl` and GL D-Bus display. VNC is not used with that GL backend.
- **CPU — Standard VGA, VirtIO or VMware (no GL):** the selected PVE `std`, non-GL `virtio` or `vmware` adapter remains in place and QSM adds a non-GL D-Bus display. No GPU or render node is required; stock VNC may run in parallel.
- **Audio** (on by default) adds an Intel HDA sound card whose output plays in the console; the console starts muted and its speaker button turns the sound on.

Encoder settings are stored per VM in `/etc/qsm-pve-direct/instances.d/<VMID>.conf`, mode `0600`. The default codec policy is `auto`: when the browser's offer contains H.265 and the node has a working `hevc_nvenc`, `hevc_qsv` or `hevc_vaapi`, QSM negotiates HEVC (answering with the browser's own payload type and profile/tier/level); otherwise it uses the H.264 path (NVENC, QSV, VA-API, then `libx264`). `QSM_DIRECT_CODEC=hevc` forces HEVC and requires a hardware encoder; it never silently uses a CPU encoder or emits an H.264 stream labelled as HEVC. HEVC is qualified against Chrome on Apple silicon with `hevc_nvenc`.

For bidirectional clipboard in a Linux guest, install the matching `qsm-desktop-agent_*.deb`, then configure the desktop user once:

```bash
apt install ./qsm-desktop-agent_*.deb
qsm-desktop-agent-setup USER
```

QEMU Guest Agent remains useful for standard PVE operations, but it does not replace the desktop-session companion: it cannot access a graphical session's clipboard.

## LXC containers

Install `qsm-pve-direct-lxc` next to `qsm-pve-direct`, then prepare a Debian/Ubuntu container that has a KDE Plasma desktop:

```bash
apt install ./qsm-pve-direct-lxc_*.deb
qsm-pve-direct-lxc enable <vmid>
pct exec <vmid> -- passwd qsm
```

`enable` passes the host GPU through, installs NVIDIA userspace matching the host driver and the container package `qsm-console-guest` (login screen per console, clipboard agent). Video, input, resizing and clipboard work as for a VM; a container console has no audio yet. Details: `packaging/debian/README.qsm-pve-direct`.
