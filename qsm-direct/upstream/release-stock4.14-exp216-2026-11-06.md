# QSM Direct 0.4.0+stock4.14~exp216: exact UI on by default

Unofficial preview for **PVE 9 / Debian 13, amd64**. This is now the current
version on the [signed APT repository](https://rdmitry0911.github.io/qsm-rd/). It
needs no patched Proxmox packages. It keeps the name of the tested candidate
because these are the same binaries that were qualified.

## What changed since stock4.13

* **Exact UI is on by default.** Text, menus and other UI areas arrive as the
  guest's own pixels, losslessly, over a WebRTC data channel, independently of the
  video encoder. There is no OCR or font substitution.
* **Video is cropped to what is moving, and its target follows the path.** Sharp text
  no longer depends on the video encoder, so a narrow link lowers the encoder's target
  and not only the frame rate. A full-screen clip at 2 Mbit/s went from 12 to 43 fps.
* **Moved pixels are not sent again.** Scrolling and window drags reuse pixels the
  viewer already holds, and new tiles refer to earlier pixels. A 660×290 test region
  of repeated text lines went from 85 KB to about 20 KB.
* **Browsers that support it decode directly** with WebCodecs. A decoder error restarts decoding at most three times a minute. A live test had
  six such errors in 20 minutes and recovered from each.
* **A failure stays local.** An internal exact UI error turns the layer off for that
  console only and shows *Off after an error*. It is not saved as a choice.
* **`qsm-desktop-effects reduce|restore|status`** is a new guest helper that lowers
  window shadows. It only runs when you run it.

## Measured responsiveness against RDP

Median ms, **QSM / RDP**. QSM: PVE container, KWin, H.264 NVENC, Chromium. RDP:
Windows Server 2025 RDS with the FreeRDP3 client on X11. Same widget geometry on a
flat backdrop with no media, same shaped path. The label is path rate and one-way
delay. The RDP reference was measured once for this series and not rerun.

| | 20 Mbit/s, 15 ms | 2 Mbit/s, 15 ms | 20 Mbit/s, 100 ms | 2 Mbit/s, 100 ms |
| --- | --- | --- | --- | --- |
| Key to glyph | 45 / 39 | 43 / 36 | 129 / 123 | 133 / 123 |
| Click to response | 45 / 67 | 45 / 69 | 137 / 151 | 136 / 153 |
| Wheel, first change | 44 / 41 | 51 / 40 | 138 / 125 | 138 / 126 |
| Wheel, settled | 68 / 41 | 68 / 40 | 140 / 126 | 153 / 126 |
| Drag, first movement | 54 / 92 | 53 / 87 | 150 / 171 | 150 / 185 |
| Drag, lag behind pointer | 44 / 48 | 45 / 50 | 134 / 134 | 134 / 135 |
| Bytes on the wire per run, KB | 3070 / 107 | 3058 / 54 | 3075 / 107 | 3073 / 48 |

Read this table with its limits:

* The client is FreeRDP, not mstsc. OS, toolkit, compositor and encoder all differ,
  so this is not a single-variable comparison.
* The QSM observer is quantized to animation frames, about +8 ms on average, against
  1-2 ms polling of X11 pixels on the RDP side.
* A flat backdrop is the best case. With text behind the moved window, QSM drag lag
  is 50 ms at 15 ms delay, and 292 ms (p95 441 ms) at 2 Mbit/s and 100 ms.
* QSM uses about 30 to 60 times the bytes of RDP in these runs, including constant
  Opus audio.
* **Virtual machines are not at this level.** In the QEMU test guest with KWin the
  same type, click and wheel actions take 155-175 ms at 15 ms delay. Drag lag is
  about 170 ms on a flat backdrop, and 404 ms at 2 Mbit/s with text behind the
  window, with up to 234 px of error left at the end. Removing window shadows took
  that lag from 300-426 ms to 177-184 ms, which is why the helper above exists. It
  is manual because it changes how the desktop looks.

## Measured video

| Stand | Open path | Narrow phase | Recovered |
| --- | --- | --- | --- |
| Clip in a window, container, NVENC, three runs | 57-59 fps | 53 fps | 55-58 fps |
| Clip in a window, Intel QSV | 51 fps | 47 fps | 51 fps |
| Clip in a window, Intel VA-API | 53 fps | 47 fps | 50 fps |

A real 720p60 clip, full screen, at 2 Mbit/s:

| Stand | fps | Median SSIM | Worst SSIM |
| --- | --- | --- | --- |
| Container, NVENC | 43.9 | 0.958 | 0.942 |
| VM, Intel QSV, two runs | 48.8, 51.1 | 0.953, 0.947 | 0.843, 0.907 |
| VM, Intel VA-API | 49.3 | 0.949 | 0.871 |

These fps values count distinct source frames, not received or decoded frames.
On the real container sound path, sound follows the picture by 29 ms on an open path
and 40 ms at 2 Mbit/s (range -7 to +75 ms). The long runs on PVE, 31 minutes of UI and
30 minutes of clip, were on the earlier exp213 candidate and were not repeated.

The installed packages were then checked on one stock node through a normal PVE
login, the QSM menu and TURN over TCP 8006, on a dedicated container with libx264.
Chrome 157 with a clean profile and Firefox 157 with an older saved setting both came
up Active without a click, and both kept an explicit opt-out. The maintainer also
used the installed version by hand on that node. Automated: 37 of 38 CTest gates pass
in the isolated build environment. `qsm_desktop_metadata_wire` needs a pty, which that
environment does not have, and passes outside it.

## Known limits

* **With exact UI off** a full-screen clip at 2 Mbit/s stays near 15 fps, with sound
  about 200 ms ahead of the picture. Its open-path rate also varied between runs
  (26 and 43 fps) with no explanation yet.
* **Hard-cut quality has little margin.** Median narrow-path SSIM is 0.903-0.904
  against a gate of 0.90, and the previous candidate, exp215, had one run of seven at
  0.898.
* **Not covered by an automated run of this version:** a Mac, a phone or a tablet.
  The latest Mac figures are from an earlier candidate.
* **No A/V measurement on a VM guest**, because the test VM has no audio device.
* **HEVC** decodes on a real Intel receiver but delivers about 40 fps against 46 and
  three times the bytes. H.264 stays the recommended codec, and HEVC is not qualified
  with this mode.
* **Do not enable hardware video decoding inside a VirGL guest.** With
  `AcceleratedVideoDecodeLinuxZeroCopyGL` the guest hit `Illegal command buffer` at
  the first frames, and one run reset the host's AMD GPU. Without zero-copy the
  picture is correct but decoding is in software, which is the recommendation.
* Update and rollback were simulated with APT on a copy of a node's package state.
  The update itself ran on that node, but with one `dpkg -i` and not through APT, and
  the rollback has not been run on a live node.

## Update

Through the signed repository, with every installed QSM profile in one transaction:

```sh
apt update
apt install --only-upgrade qsm-archive-keyring qsm-pve-direct qsm-pve-direct-standalone
# add qsm-pve-direct-lxc and qsm-pve-direct-gateway if they are installed
```

Or use the debs attached here, with `apt install ./file.deb ...`. Do not use one
`dpkg -i` with several files: the LXC helper's `Breaks` on an older core makes dpkg
order-sensitive. GitHub stores `~` in asset names as `.`, so
`qsm-pve-direct_0.4.0+stock4.14.exp216_amd64.deb` contains version
`0.4.0+stock4.14~exp216`.

A plain `apt upgrade` also picks this version up. Then reload PVE with cache bypass and
reopen the consoles. Restarting QSM services interrupts open consoles while guests
keep running. No guest update or reboot is
needed. The guest packages only add optional metadata and the helper above. Never
install `qsm-console-guest` on the PVE host.

A node carrying a `QSM_DIRECT_VIDEO_REGIONS=0` drop-in stays out of the qualified
mode. Remove that drop-in, reload systemd and restart
`qsm-pve-direct-terminal.service`.

## Turn it off or go back

* **One browser:** Console settings → Exact UI. The choice persists.
* **One node:** add `Environment=QSM_DIRECT_EXACT_UI_DEFAULT=0` in a
  `qsm-pve-direct-terminal.service` drop-in, run `systemctl daemon-reload`, restart
  the service and reopen consoles.
* **Package rollback:** `apt install --allow-downgrades` with `=0.4.0+stock4.13` for
  **every** installed QSM host package in one transaction. In the simulation, APT
  planned to remove the gateway and LXC profiles when the command left them out, so
  read its plan before confirming.

## Review material

Attached: the source of the release commit, the seven debs, signed checksums, build
provenance, install guides and `review-stock4.14-exp216-20261106.tar.gz` with the raw
runs behind this page. Summaries are in `qsm-direct/docs/`, starting with
`rdp-candidate-216-20261105.json` and `rdp-deployment-exp216-20261106.json`. Signing
key: `BA99528182B7EADD0199B6986A09741B1DBEAEF2`.

Earlier development runs named in those documents are not in the public tree or in
this archive because of their size (about 380 MB).

Project sponsor: [Takt](https://taktcycles.com/).
