# QSM Direct

QSM Direct is a browser-based graphical console for Proxmox VE 9 virtual machines. It reuses the existing PVE session and the `VM.Console` permission: there is no separate account, PIN, public listener, native client, host X11 session, or legacy streaming stack.

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

The media transport is WebRTC with Opus audio and negotiated H.264 or HEVC video. Input is sent through QEMU Display1. Pointer motion uses a non-blocking lane, so stale mouse positions do not accumulate during a short overload. QSM Direct supports both VirGL/GL and ordinary `std` or non-GL `virtio` displays. The host chooses an encoder: NVENC, QSV, VA-API, or software `libx264`.

## Installation

Build, or download, the PVE 9 `qsm-pve-direct_*.deb` and install it on a PVE node:

```bash
apt install ./qsm-pve-direct_*.deb
systemctl enable --now qsm-pve-direct-terminal.service
```

In a VM, open **Hardware → Display → Advanced**, enable **QSM Display1**, and select a profile. The VirGL profile adds a private `virtio-vga-gl` and GL D-Bus Display1. The `std`/non-GL `virtio` profile keeps the selected video adapter and adds a non-GL D-Bus Display1. Once saved, use the existing **Console** entry in the left navigation, between **Summary** and **Hardware**. For an eligible VM QSM Direct is embedded in that area; an ineligible VM keeps the ordinary noVNC console.

The guest Display1 size changes after a window resize settles. Full screen is the same console at the full viewport size. For bidirectional guest clipboard, install `qsm-desktop-agent` in a Linux VM and run `qsm-desktop-agent-setup USER`. File transfer is deliberately not part of the browser transport: a browser cannot read the local filesystem without an explicit user-selected file.

See [installation](docs/INSTALLATION.md) and [testing](docs/TESTING.md) for complete instructions.

## Measured comparison with stock noVNC

The figures below are measurements, not a latency or bandwidth promise. They were taken on 2026-09-06 in the QSM laboratory: nested PVE 9/QEMU 11, a 4-vCPU/4-GiB VM using Standard VGA plus non-GL Display1, the same 1280×800 input-to-pixel fixture, and a separate Chrome browser VM on the same isolated lab network. QSM Direct used `libx264` at 60 fps. Both routes used the visible PVE Console split menu. Values vary with encoder, browser, guest workload, network, and host load.

| Measurement | QSM Direct | Stock noVNC | What was measured |
| --- | ---: | ---: | --- |
| Console click to first visible guest pixels | 4.680 s | 2.308 s | One cold controlled run. QSM requires SDP, DTLS, an H.264 configuration frame, and a decoded video frame; noVNC requires a non-empty RFB Canvas. This is not a WAN result. |
| Idle desktop receive rate | 0.553 Mbit/s | 0.029 Mbit/s | 10-second receive-side sample after the desktop was stable. QSM continued its 60-fps H.264 cadence; RFB sent only changed rectangles. noVNC is therefore cheaper for an idle screen. |
| Video/frame delivery | 600 decoded frames / 10 s (60 fps), 0 drops | 30 RFB WebSocket frames / 10 s | These counters are transport-specific and must not be treated as equal display-frame counts. |
| Browser video playout buffering | 7.65 ms mean | n/a | Chrome `inbound-rtp.jitterBufferDelay` divided by emitted video frames. noVNC has no WebRTC jitter buffer; its RFB data is ordered by TCP. |
| Pointer hover to changed guest pixels, 3 runs | 86.0–353.3 ms (median 173.5 ms) | 24.2–78.4 ms (median 64.2 ms) | The same blue-icon → magenta-popup guest fixture; measured at the browser canvas/video output. |
| Browser MouseEvent to painted guest-window drag, 1 run | 386.7 ms first paint; 216.0 ms median; 851.9 ms p95; 80.5 px p95 visual lag | n/a | Mouse is injected through the browser's DOM event path, then the orange guest card is located in decoded video frames. Its maximum presentation gap was 548.8 ms and endpoint error 40 px. This CPU/libx264 nested result is a diagnostic baseline, not a hardware-encoder claim. |
| Packet/frame loss during the sample | 0 Chrome video frame drops | n/a | noVNC does not expose an equivalent browser RFB drop counter. |

This small desktop fixture deliberately does not claim a result for 1080p/4K video, a WAN, packet loss, or a hardware encoder. It does show why noVNC remains useful for firmware, recovery, installers, text, and an idle screen; and why a codec-backed route needs a separate moving-image benchmark rather than an assumed advantage. Reproduce the measurements with `lab/proxmox9/measure-direct-browser-e2e.py --drag-runs 1` and `lab/proxmox9/measure-novnc-browser-e2e.cjs`; the latter opens PVE's actual stock **noVNC** menu item, not a synthetic RFB client.

## HEVC capability negotiation

HEVC can provide better compression efficiency, especially for high-resolution or high-motion content. It does not, by itself, reduce input latency: encoder look-ahead, B-frames, keyframe cadence, browser decode, and jitter buffering still determine the interaction delay.

The VM media policy defaults to **Automatic (HEVC hardware preferred)**. Before it starts a worker, the terminal reads the current browser SDP offer. It chooses HEVC only if that offer contains `video/H265` *and* a bounded probe has initialized `hevc_nvenc`, `hevc_qsv`, or `hevc_vaapi` on the PVE node. It then packetizes Annex-B HEVC according to [RFC 7798](https://www.rfc-editor.org/rfc/rfc7798.html) and performs normal WebRTC H.265/RTX negotiation. Otherwise it uses the separately verified H.264 lane; automatic H.264 may still use `libx264` when no accelerator works.

Safari can receive HEVC WebRTC video on compatible hardware, as documented in [WebKit's Safari 18.4 feature notes](https://webkit.org/blog/16574/webkit-features-in-safari-18-4/). Other browsers and devices must advertise it themselves; H.264 remains the interoperable fallback. A VM has one shared Display1 encoder stream, so its first active Console chooses the shared codec. A later browser which cannot decode that already-active codec is rejected with an explicit compatibility error rather than receiving mislabeled video; close the existing Console or choose H.264 for mixed browser fleets.
