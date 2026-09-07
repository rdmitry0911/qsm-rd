# Installing QSM Direct on Proxmox VE 9

Install `qsm-pve-direct` only on the PVE node. An operator needs only a modern browser with WebRTC H.264/Opus support; no client application is installed.

```bash
apt install ./qsm-pve-direct_*.deb
systemctl enable --now qsm-pve-direct-terminal.service
systemctl status qsm-pve-direct-terminal.service
```

The package safely adds a small PVE UI script only for the `pve-manager` versions it knows. On an unsupported version it restores the stock template instead of applying an unsafe patch.

In **Hardware → Display → Advanced**, enable **QSM Display1**.

- **VirGL GPU (GL):** QSM creates a private `virtio-vga-gl` and GL D-Bus display. VNC is not used with that GL backend.
- **CPU — Standard VGA or VirtIO (no GL):** the selected PVE `std` or non-GL `virtio` adapter remains in place and QSM adds a non-GL D-Bus display. No GPU or render node is required; stock VNC may run in parallel.

Encoder settings are stored per VM in `/etc/qsm-pve-direct/instances.d/<VMID>.conf`, mode `0600`. Automatic mode tries NVENC, QSV, VA-API, then `libx264`; hardware-only and software modes can also be selected explicitly. The browser WebRTC route currently supports only H.264/Opus. HEVC is rejected rather than presented as a setting that cannot form a valid peer connection.

For bidirectional clipboard in a Linux guest, install the matching `qsm-desktop-agent_*.deb`, then configure the desktop user once:

```bash
apt install ./qsm-desktop-agent_*.deb
qsm-desktop-agent-setup USER
```

QEMU Guest Agent remains useful for standard PVE operations, but it does not replace the desktop-session companion: it cannot access a graphical session's clipboard.
