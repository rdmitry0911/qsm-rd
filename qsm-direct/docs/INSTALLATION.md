# Installing QSM Direct on Proxmox VE 9

Install `qsm-pve-direct` only on the PVE node. An operator needs only a modern browser with WebRTC H.264/Opus support. No client application is installed.

```bash
apt install ./qsm-pve-direct_*.deb
systemctl enable --now qsm-pve-direct-terminal.service
systemctl status qsm-pve-direct-terminal.service
```

The package safely adds a small PVE UI script only for the `pve-manager` versions it knows. On an unsupported version it restores the stock template instead of applying an unsafe patch. The API module is likewise loaded only on the qualified `pve-manager` 9.2.11 / `qemu-server` 9.2.7 with matching checksums of the hooked PVE Perl files; on any other version the stock daemons run untouched and the console route is absent until a matching package is installed.

As `root@pam` (the setting edits the VM's `args:` line, which Proxmox reserves for root), open **Hardware → Display → Advanced** and enable **QSM Display1**. Then **stop and start the VM**: the new display devices exist only after QEMU is relaunched, and a reboot from inside the guest is not enough.

- **VirGL GPU (GL):** QSM creates a private `virtio-vga-gl` and GL D-Bus display. VNC is not used with that GL backend.
- **CPU — Standard VGA or VirtIO (no GL):** the selected PVE `std` or non-GL `virtio` adapter remains in place and QSM adds a non-GL D-Bus display. No GPU or render node is required; stock VNC may run in parallel.

Encoder settings are stored per VM in `/etc/qsm-pve-direct/instances.d/<VMID>.conf`, mode `0600`. The default codec policy is `auto`, which is H.264: QSM verifies the H.264 path (NVENC, QSV, VA-API, then `libx264`). `QSM_DIRECT_CODEC=hevc` is an explicit, experimental choice that requires H.265 in the browser's offer and a working `hevc_nvenc`, `hevc_qsv` or `hevc_vaapi`; it never silently uses a CPU encoder or emits an H.264 stream labelled as HEVC, and it has not yet been qualified with a real browser.

For bidirectional clipboard in a Linux guest, install the matching `qsm-desktop-agent_*.deb`, then configure the desktop user once:

```bash
apt install ./qsm-desktop-agent_*.deb
qsm-desktop-agent-setup USER
```

QEMU Guest Agent remains useful for standard PVE operations, but it does not replace the desktop-session companion: it cannot access a graphical session's clipboard.
