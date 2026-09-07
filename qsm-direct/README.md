# QSM Direct

**A real desktop inside the Proxmox web console.** QSM Direct replaces the
rectangle-by-rectangle noVNC picture with a steady 60 fps, hardware-encoded
WebRTC stream — video *and* audio — in the same browser tab, with the same PVE
login and the same `VM.Console` permission. Nothing to install on the client,
nothing new to open on the network.

```text
PVE Web UI / VM.Console
        │ protected same-origin SDP request
        ▼
qsm-pve-direct-terminal ── private Unix sockets ── qsm-direct-media-worker
        │                                                │
        └──────────────── QEMU Display1 D-Bus ───────────┘
                                                         │
                                             QEMU VM / guest display
```

## Why it feels like a local desktop

Open the **Console** of a VM and:

- **Video plays as video.** A 720p clip arrives at its full 30 frames per
  second. Stock noVNC on the same guest shows about 7 pictures per second and
  needs six times the bandwidth to do it.
- **Scrolling, dragging and animation stay smooth.** Every guest frame is
  encoded at a constant cadence. On a GPU node a scrolling document reaches
  the browser as 52 distinct pictures per second with a 34 ms p95 gap between
  pictures; noVNC manages 12 pictures per second with a 134 ms gap.
- **The pointer never goes stale.** Mouse motion travels on its own
  low-latency lane, so a busy frame cannot turn into a laggy cursor.
- **Sound.** Opus audio comes with the picture.
- **It uses the hardware you have.** NVENC, QSV, VA-API or CPU encoders;
  H.264 everywhere, HEVC negotiated automatically when both the browser and
  the node support it; VirGL/GL guests or plain VGA guests.
- **Same security model.** Same origin, same PVE session, same ACLs. No
  pairing PIN, no public listener, no native client, no host X11 session.
- **It copes with a sleeping guest.** When the guest turns its screen off, the
  console says so and any mouse or key press wakes it — no reconnecting.

## Pick a configuration

Three ways to look at a PVE 9 virtual machine, measured on 2026-09-07 with one
guest image, one browser host and the same workloads. "Pictures/s" is the
number of *visibly different* pictures that reached the browser each second;
the gap is the 95th-percentile pause between two of them. Full tables,
method and reproduction commands: [docs/COMPARISON.md](docs/COMPARISON.md).

| What you do in the VM | VirGL + QSM Direct | Standard VGA + QSM Direct | Standard VGA + stock noVNC |
| --- | --- | --- | --- |
| Watch a 720p clip | **37.5 pictures/s**, 39 ms gap, 18.8 Mbit/s | 26.6 pictures/s, 67 ms gap, 19.6 Mbit/s | 7.4 pictures/s, 175 ms gap, **120 Mbit/s** |
| Scroll a long document | **36.2 pictures/s**, 39 ms gap, 5.5 Mbit/s | 14.4 pictures/s, 118 ms gap, 12.7 Mbit/s | 12.0 pictures/s, 134 ms gap, 15.4 Mbit/s |
| Animated UI, dashboards, games | **38.5 pictures/s**, 34 ms gap, 12.8 Mbit/s | 16.2 pictures/s, 115 ms gap, 9.0 Mbit/s | 11.0 pictures/s, 152 ms gap, 31.6 Mbit/s |
| Hover the mouse over a button | 86 ms until the tooltip shows | 85 ms | **65 ms** |
| Leave the desktop idle | 0.55 Mbit/s | 0.55 Mbit/s | **≈ 0** |
| Open the Console | first picture after 3.4 s | 3.4 s | **1.0 s** |
| Guest OpenGL / 3D applications | **yes, VirGL** | no | no |
| Audio | **yes** | **yes** | no |
| Needed on the node | render node + any encoder | any encoder (CPU works) | nothing |

The three columns above come from the CPU-only nested laboratory (4-vCPU
guests with software rendering, `libx264` on the node), so the guest itself
caps the frame rate in every column. The same VirGL + QSM Direct workloads on
a real node with an **RTX 3080** (NVENC H.264, KDE Plasma guest) reached
28 pictures/s for the clip (the full 30 fps source), **52 pictures/s** for
the scrolling document and **45 pictures/s** for the animated scene, all with
a 34–67 ms p95 gap and 0 dropped frames, while the GPU sat at 13 % and its
encoder at 4 %. Hover latency there was 102 ms.

Where noVNC is still the better tool: the first picture after opening the
Console (it needs no codec negotiation), a single isolated pointer event (about
20 ms less), zero bandwidth on an idle screen, and anything before an OS is
running — firmware, installers, recovery, text consoles. QSM Direct is for the
hours you spend *inside* a running desktop.

## Install

Build, or download, the PVE 9 `qsm-pve-direct_*.deb` and install it on the
PVE node:

```bash
apt install ./qsm-pve-direct_*.deb
systemctl enable --now qsm-pve-direct-terminal.service
```

In a VM open **Hardware → Display → Advanced**, enable **QSM Display1** and
pick a profile:

- **VirGL GPU (GL)** adds a private `virtio-vga-gl` and a GL D-Bus Display1 —
  the smooth column above, with guest 3D.
- **CPU — Standard VGA or VirtIO (no GL)** keeps the selected adapter and adds
  a non-GL D-Bus Display1 — no GPU or render node required; stock VNC keeps
  working next to it.

Save, then use the existing **Console** entry in the left navigation, between
**Summary** and **Hardware**. An eligible VM gets QSM Direct embedded right
there; the top **Console** split menu opens it in a separate window. The guest
display follows the window size, full screen is the same console at the full
viewport, and bidirectional clipboard comes with `qsm-desktop-agent` in a
Linux guest (`qsm-desktop-agent-setup USER`). File transfer is deliberately
not part of the browser transport.

Details: [installation](docs/INSTALLATION.md), [testing](docs/TESTING.md),
[measured comparison](docs/COMPARISON.md), [PVE 9 laboratory](lab/proxmox9/README.md).

## How it works

The browser sends a WebRTC offer to a protected PVE API route; a node-local
terminal service answers it and starts one media worker per VM. The worker
reads QEMU's Display1 (GL scanouts through DMA-BUF, or plain framebuffers),
encodes H.264 or HEVC plus Opus, and hands the elementary streams to the
WebRTC bridge, which only packetizes — nothing is decoded or re-encoded on the
way. Input goes back through Display1: keys and clicks on an ordered channel,
pointer motion on an unordered latest-state lane so a lost packet is replaced
by the next position instead of delaying a click.

All viewers of one VM share one encoder stream. The first Console picks the
codec; a later browser that cannot decode it is told so explicitly rather than
being handed mislabelled video.

### Codecs

The VM media policy defaults to **Automatic (HEVC hardware preferred)**.
Before starting a worker the terminal reads the browser's SDP offer and
chooses HEVC only if it contains `video/H265` *and* a bounded probe has
initialized `hevc_nvenc`, `hevc_qsv` or `hevc_vaapi` on the node
([RFC 7798](https://www.rfc-editor.org/rfc/rfc7798.html) packetization).
Otherwise it uses the separately verified H.264 lane, which may fall back to
`libx264` when no accelerator works. Safari on compatible hardware and Chrome
with hardware HEVC decoding negotiate HEVC; everything else gets H.264. HEVC
lowers bitrate, not input latency: encoder look-ahead, keyframe cadence,
browser decode and jitter buffering still decide the interaction delay.

### When the guest goes to sleep

A desktop guest turns its virtual output off after an idle timeout, or locks
its session. QEMU then keeps emitting the last picture (or a black one) and
the guest will not change its display mode until it wakes, which used to leave
the Console with a stale picture and a dead-end "did not acknowledge this
window size" message. The Console now recognises an all-black decoded picture
and says *Guest display looks asleep — move the mouse or press a key here to
wake it*; when a guest keeps its own size it says so and names the same wake
action instead of retrying forever; every mouse or key event re-issues the
pending window size once, and the status returns to *Connected* the moment
the guest adopts it. Measured on a KDE Plasma guest: the desktop is back about
250 ms after the first mouse or key event, and never without one.
