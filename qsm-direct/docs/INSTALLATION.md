# Installing QSM Direct on Proxmox VE 9

Install `qsm-pve-direct` only on the PVE node. An operator needs only a modern browser with WebRTC H.264/Opus support; HEVC is selected opportunistically where both browser and node support it. No client application is installed.

```bash
apt install ./qsm-pve-direct_*.deb
systemctl enable --now qsm-pve-direct-terminal.service
systemctl status qsm-pve-direct-terminal.service
```

The package safely adds a small PVE UI script only for the `pve-manager` versions it knows. On an unsupported version it restores the stock template instead of applying an unsafe patch.

In **Hardware → Display → Advanced**, enable **QSM Display1**.

- **VirGL GPU (GL):** QSM creates a private `virtio-vga-gl` and GL D-Bus display. VNC is not used with that GL backend.
- **CPU — Standard VGA or VirtIO (no GL):** the selected PVE `std` or non-GL `virtio` adapter remains in place and QSM adds a non-GL D-Bus display. No GPU or render node is required; stock VNC may run in parallel.

Encoder settings are stored per VM in `/etc/qsm-pve-direct/instances.d/<VMID>.conf`, mode `0600`. The default codec policy is `auto`: when the browser SDP offers H.265, QSM verifies `hevc_nvenc`, `hevc_qsv`, then `hevc_vaapi` and selects the first working hardware encoder. If either side lacks HEVC, it verifies the H.264 path (NVENC, QSV, VA-API, then `libx264`). A forced HEVC policy requires hardware mode; it never silently uses a CPU encoder or emits an H.264 stream labelled as HEVC.

For bidirectional clipboard in a Linux guest, install the matching `qsm-desktop-agent_*.deb`, then configure the desktop user once:

```bash
apt install ./qsm-desktop-agent_*.deb
qsm-desktop-agent-setup USER
```

QEMU Guest Agent remains useful for standard PVE operations, but it does not replace the desktop-session companion: it cannot access a graphical session's clipboard.
