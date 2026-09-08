# Measured comparison: QSM Direct and stock noVNC

Unless stated otherwise, every number on this page was produced by
`lab/proxmox9/measure-console-scenarios.cjs` on 2026-09-07, through the
visible PVE 9 web UI, with the shipped console surfaces (the QSM Direct
`<video>` and noVNC's RFB `<canvas>`). Each workload is one 10-second sample
and hover latency is the median of five runs; they are not a benchmark suite,
and they describe the laboratory below, not a promise for other hardware or
networks. The 2026-09-06 section at the end predates the scenario fixture and
came from the older scripts named there; the GPU utilisation figures were read
from `nvidia-smi` on the node.

## Environments

| Label | Node | Guest display | Encoder | Guest | Measuring browser |
| --- | --- | --- | --- | --- | --- |
| VirGL + QSM (GPU node) | PVE 9, AMD Ryzen Threadripper PRO 5975WX, NVIDIA RTX 3080 | `virtio-vga-gl`, GL Display1 | `h264_nvenc` (HEVC available, not offered by the measuring Chromium) | Ubuntu 26.04, KDE Plasma 6 Wayland, 8 vCPU, Firefox kiosk fixture | Chromium 152.0.7977.75 on the node, headless, 1280×800 |
| VirGL + QSM (nested lab) | nested PVE 9 VM, 8 vCPU, 7.9 GiB | `virtio-vga-gl`, GL Display1 | `libx264` (CPU) | Debian 13, Weston + Chrome kiosk fixture, 4 vCPU, 4 GiB | Google Chrome 152.0.7977.75 in a 4-vCPU browser VM, headless, 1280×800 |
| VGA + QSM (nested lab) | same nested node | `std` VGA, non-GL Display1 | `libx264` (CPU) | same fixture image, 4 vCPU, 4 GiB | same browser VM |
| VGA + noVNC (nested lab) | same nested node | `std` VGA, QEMU VNC | none (RFB rectangles) | the same VM as the row above | same browser VM |
| VirGL + noVNC (nested lab) | same nested node | `virtio` VirtIO-GPU, QEMU VNC, **QSM Display1 off** | none (RFB rectangles) | the VirGL VM with the QSM profile disabled | same browser VM |

The nested guests render with software GL; their own frame rate is the
ceiling for both transports. The two VGA rows are the *same* VM measured
through the two consoles minutes apart. No sample carried audio: the clip is
played muted and the cached file has no audio track, so every bandwidth figure
is video plus signalling only.

## Workloads

The guest shows one of four pages, switched from inside the console with
Alt+1..4 (see `lab/proxmox9/scenarios/`):

1. **Idle fixture** — the static input-to-pixel test page (blinking caret only).
2. **Video clip** — a looping 1280×720 H.264 clip at 30 fps (Big Buck Bunny, 10 s), letterboxed in a 1280×800 window.
3. **Document scroll** — a 40-section text document scrolling itself at 240 px/s.
4. **Animated scene** — 160 moving discs, a rotating bar and scrolling text on a full-window 2D canvas at display refresh.

## Metrics

* **Distinct pictures per second** — how many visibly different pictures reached the browser surface. A picture counts when, in a 160×90 downscale, at least 0.5 % of cells changed their luma by more than 16 *or* the mean absolute luma change is ≥ 0.25. An H.264 repeat frame and an untouched RFB canvas both count as "nothing new". The detector can exceed a source frame rate when the guest presents partial or torn updates between frames, which is what the 37.5 for the nested VirGL guest below reflects.
* **Picture gap p95 / max** — the 95th percentile and the longest interval between two distinct pictures: the objective counterpart of "smooth" versus "jerky".
* **Received Mbit/s** — bytes the browser received for the console during the sample: WebRTC inbound-rtp for QSM Direct, RFB WebSocket payload for noVNC.
* **Decoded / dropped frames** — WebRTC `inbound-rtp` `framesDecoded` per second and `framesDropped`; the encoder runs at a constant 60 fps, so this is the transport cadence, not the number of different pictures. Only recorded for the clip.
* **RFB updates per second** — WebSocket frames on noVNC's RFB connection; a transport counter with no QSM Direct equivalent.
* **Hover latency** — pointer moved from a neutral spot onto the fixture's blue icon; time until the magenta hover popup is visible on the browser surface. Median of five runs. Includes the guest's own repaint in both cases.
* **Time to first picture** — Console menu click to a decodable/painted picture. noVNC paints a canvas from the first rectangle; QSM Direct needs SDP, DTLS, a configuration frame and a decoded frame.

## Results

### Watching a 720p clip (30 fps source)

| | VirGL + QSM (GPU node) | VirGL + QSM (nested) | VGA + QSM (nested) | VGA + noVNC (nested) | VirGL + noVNC (nested) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Distinct pictures / s | 28.2 | 37.5 (see Metrics) | 26.6 | 7.4 | 6.9 |
| Picture gap p95 / max | 67 / 101 ms | 39 / 53 ms | 67 / 148 ms | 175 / 355 ms | 197 / 418 ms |
| Received | 20.1 Mbit/s | 18.8 Mbit/s | 19.6 Mbit/s | 120.4 Mbit/s | 112.8 Mbit/s |
| Decoded fps / dropped (WebRTC) | 60 / 0 | 60 / 0 | 59.8 / 0 | n/a | n/a |
| RFB updates / s | n/a | n/a | n/a | 180 | 175 |

### Scrolling a long document (240 px/s)

| | VirGL + QSM (GPU node) | VirGL + QSM (nested) | VGA + QSM (nested) | VGA + noVNC (nested) | VirGL + noVNC (nested) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Distinct pictures / s | 51.7 | 36.2 | 14.4 | 12.0 | 21.6 |
| Picture gap p95 / max | 34 / 84 ms | 39 / 52 ms | 118 / 158 ms | 134 / 200 ms | 74 / 98 ms |
| Received | 9.8 Mbit/s | 5.5 Mbit/s | 12.7 Mbit/s | 15.4 Mbit/s | 28.4 Mbit/s |

### Animated scene (full-window motion)

| | VirGL + QSM (GPU node) | VirGL + QSM (nested) | VGA + QSM (nested) | VGA + noVNC (nested) | VirGL + noVNC (nested) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Distinct pictures / s | 44.8 | 38.5 | 16.2 | 11.0 | 11.6 |
| Picture gap p95 / max | 34 / 51 ms | 34 / 50 ms | 115 / 184 ms | 152 / 274 ms | 116 / 188 ms |
| Received | 17.0 Mbit/s | 12.8 Mbit/s | 9.0 Mbit/s | 31.6 Mbit/s | 29.1 Mbit/s |

### Everything else

| | VirGL + QSM (GPU node) | VirGL + QSM (nested) | VGA + QSM (nested) | VGA + noVNC (nested) |
| --- | ---: | ---: | ---: | ---: |
| Idle desktop, received | 0.71 Mbit/s | 0.55 Mbit/s | 0.55 Mbit/s | 0.001 Mbit/s |
| Jitter buffer, idle mean | 15.8 ms | 7.5 ms | 7.8 ms | n/a |
| Time to first picture | 3.5 s | 3.4 s | 3.4 s | 1.0 s |
| Node cost during the animated scene | 13 % GPU, 4 % NVENC (`nvidia-smi`, read once during the scene) | n/a | ≈ 43 % of one core (`ps` lifetime average of the worker process) | n/a |

### Interaction latency (2026-09-08)

Measured with `measure-interaction.cjs`, which times from the browser's own
mouse-event to the painted response — `requestVideoFrameCallback` on the QSM
`<video>`, `requestAnimationFrame` on the noVNC `<canvas>` — with the same
clock for every column. Each detection poll redraws the surface before reading
its pixels, so a stale frame cannot be read between runs; the reset also
confirms the popup has cleared before the next run. Every column here was
measured in one session with this one tool.

| | VirGL + QSM | VGA + QSM | VGA + noVNC | VirGL + noVNC |
| --- | ---: | ---: | ---: | ---: |
| Pointer onto icon → hover popup, median of 5 | 88 ms | 88 ms | 52 ms | 34 ms |
| Window drag → first painted motion (one run, indicative) | 154 ms | 215 ms | n/a | 77 ms |
| Window drag → p95 visual lag (one run, indicative) | 65 px | 52 px | n/a | 27 px |

The four columns share one VM per family — VM 102 for VirGL, VM 106 for VGA —
measured through both consoles; the VirGL + noVNC column is that VM with QSM
Display1 turned off so QEMU's VNC can serve the VirtIO-GPU card. **VGA + noVNC
drag is n/a:** the fixture's draggable card does not grab through noVNC on the
software `std` VGA guest (the pointer-button press does not start the drag);
its hover is unaffected.

For a single isolated event noVNC is the lower-latency transport: it ships one
small changed rectangle, while QSM Direct pays a fixed encode-decode-jitter
cost — here a full software `libx264` encode on the CPU-only lab. That cost is
per event, not per second, so it does not accumulate under motion, where QSM
Direct delivers far more frames at a fraction of the bandwidth (the tables
above). A node with a hardware encoder shrinks the per-event cost; hover has
not been re-measured on the GPU node.

### Earlier input-to-pixel results (same lab, 2026-09-06)

Taken with `measure-direct-browser-e2e.py` and `measure-novnc-browser-e2e.cjs`
on the VGA guest, before the scenario fixture existed:

| Measurement | VGA + QSM (nested, libx264) | VGA + noVNC (nested) |
| --- | ---: | ---: |
| Console click to first visible guest pixels, cold | 4.680 s | 2.308 s |
| Pointer hover to changed guest pixels, 3 runs | 86.0–353.3 ms (median 173.5 ms) | 24.2–78.4 ms (median 64.2 ms) |
| Browser MouseEvent to painted guest-window drag, 1 run | 386.7 ms first paint; 216.0 ms median; 851.9 ms p95; 80.5 px p95 visual lag | n/a |
| Browser video playout buffering | 7.65 ms mean | n/a |

The drag row is the only drag measurement so far and it is not good: on the
CPU-only nested lab a held window trailed the pointer by 80 px at p95. It has
not been re-measured on a GPU node yet.

## Reading the numbers

* On every workload with motion, QSM Direct delivers more of the guest's frames with a shorter and steadier gap between them, at a fraction of noVNC's bandwidth for video (19–20 Mbit/s against 120 Mbit/s) and animation (9–17 Mbit/s against 32 Mbit/s).
* On the scrolling and animated workloads the VirGL guests render more smoothly themselves (36–52 pictures/s against 14–16 for the software-VGA guest through QSM Direct and 11–12 through noVNC), so VirGL + QSM Direct is the configuration that turns a VM into a desktop that feels local. For the clip the transport, not the guest, makes the difference: 26.6 against 7.4 on the very same VM.
* noVNC remains faster for the first picture after opening the Console (about a second, since it needs no codec negotiation), for a single isolated pointer event (the interaction table above: 34–52 ms against QSM Direct's 88 ms, since it ships one rectangle where QSM Direct runs an encode-decode round trip), and costs nothing while the screen is idle. That single-event edge is per event and does not accumulate under motion, where QSM Direct dominates. Firmware, installers and text consoles stay the natural home of noVNC on a VM that still has a VNC server; while the QSM Display1 profile is active noVNC is black for that VM, since the profile removes the PVE graphics card.
* VirGL + noVNC (QSM off) renders a smoother guest than Standard VGA, so its noVNC scroll (21.6 pictures/s) beats Standard VGA noVNC (12.0) and even Standard VGA QSM (14.4); but it still ships whole changed rectangles, costing 113 Mbit/s for the clip against QSM Direct's encoded 18.8 Mbit/s on the same guest.
* The GPU node numbers use H.264 through NVENC because the measuring Chromium does not offer HEVC in WebRTC. A browser whose offer contains `video/H265` negotiates `hevc_nvenc` automatically on that node; no such browser session has been qualified yet.

## Reproduce

Provision a scenario guest from the reviewed hover fixture (adds the pages, the
clip and an SSH key, keeps the fixture's Weston + Chrome kiosk). The clip used
here is the public 10-second, 5 MB 1280×720 H.264 Big Buck Bunny sample from
test-videos.co.uk saved as `lab/proxmox9/cache/bbb720.mp4` (`cache/` is not
in Git; any 720p H.264 MP4 works):

```bash
python3 lab/proxmox9/build-scenario-seed.py \
  --fixture-user-data lab/proxmox9/hover-gate-102/user-data \
  --scenarios lab/proxmox9/scenarios --clip lab/proxmox9/cache/bbb720.mp4 \
  --ssh-public-key ~/.ssh/id_ed25519.pub --mac bc:24:11:1c:f1:fc \
  --address 192.168.76.6/24 --instance-id qsm-scenarios-106-v1 --output seed-106
genisoimage -output /var/lib/vz/template/iso/qsm-scenarios-106.iso -volid cidata -joliet -rock seed-106/*
qm set 106 --ide2 local:iso/qsm-scenarios-106.iso,media=cdrom && qm stop 106 && qm start 106
```

Measure both consoles from the browser host (a PVE account with `VM.Console`
on the VM is enough):

```bash
node lab/proxmox9/measure-console-scenarios.cjs --transport qsm \
  --pve-url https://PVE_NODE:8006 --user USER@REALM --password-file /secure/pve.password \
  --vmid 106 --chrome /usr/bin/google-chrome --scenarios 1,2,3,4 --hover-runs 5 --report qsm.json
node lab/proxmox9/measure-console-scenarios.cjs --transport novnc ... --report novnc.json
```

`--scenarios 5` holds the document still and scrolls it with the console's
own mouse wheel; it verifies the wheel direction rather than the transport.
The script prints one `QSM_SCENARIO_MEASUREMENT {...}` line to stdout and,
with `--report`, writes the same JSON object to that file; it never writes
credentials, SDP or pixels.
