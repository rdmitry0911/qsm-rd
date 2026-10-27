# Experimental exact UI layer

This prototype sends small, losslessly compressed pixel tiles over a separate
WebRTC data channel, above the existing full-desktop video. Text, menus and
other compressible, infrequently changing areas can arrive without waiting
for the next encoded/decoded video picture. Motion stays in the existing
H.264/HEVC path. No OCR, replacement fonts or application text extraction is
used: the overlay contains the guest's actual rendered pixels.

The option is **off by default**. In a console built from this source, open
**Console settings → Experimental exact UI**. The checkbox becomes available
after both worker and bridge advertise support, and requires the browser's
`DecompressionStream` API. The choice persists in this browser. Turning it off
clears the canvas and, after the last subscriber leaves, stops tile processing
in the shared worker. Stable stock4.13 does not contain this mode.

An administrator can opt a test node into the default with a systemd drop-in
for `qsm-pve-direct-terminal.service`:

```ini
[Service]
Environment=QSM_DIRECT_EXACT_UI_DEFAULT=1
```

Reload systemd and restart that QSM service to apply it; existing QSM viewers
must reconnect, while guests keep running. The browser enables the option only
after worker capability is confirmed. An explicit checkbox choice overrides
the node default and persists; false values saved implicitly by older versions
do not block the administrator's new default. Removing the drop-in restores
the off default for browsers with no explicit choice.

## Scope and limits

The worker uses the common captured BGRA surface, so the implementation covers
both VM and LXC capture paths and does not depend on NVENC, QSV, VA-API or
libx264. Live qualification so far covers a native Wayland LXC, H.264/NVENC and
Firefox over TURN/TCP 8006. VM capture, HEVC and the other encoders have not yet
received equivalent live mixed-content qualification.

This is the first stage of a mixed-content transport. The video still encodes
the full desktop: there is no separate cropped video stream, compositor window
metadata, CopyRect/scroll command, or semantic text protocol. The existing
guest package is sufficient. Later compositor hints may improve classification
and avoid encoding the same UI twice.

At a narrow bandwidth target, refining a large newly opened text window can
take several seconds. New input takes priority over that background work, but
high-frequency changes fall back to video and refine after settling. The
prototype does not guarantee immediate lossless updates for every application,
nor RDP-equivalent latency. There has been no measured comparison with RDP.

Tile and video presentation use independent clocks. Epochs and serials prevent
old tile decodes from repainting after a clear/reset/resize. This is not a
frame-synchronous video compositor: substantial video backlog, loss recovery,
animated/translucent windows and rapid repeated occlusion still need wider
qualification before enabling the feature by default.

## Data path and bounds

* Tile edge: 64 pixels. Maximum surface: 8,294,400 pixels, with each dimension
  at most 16,384. Larger surfaces continue through video with no exact overlay.
* Payload: zlib-compressed opaque RGBA, or one exact sample per pixel for a
  neutral grayscale tile. No quantization or chroma subsampling is applied.
  Incompressible tiles stay in video. A payload is at most 6,144 bytes.
* Three changes less than 75 ms apart classify a tile as motion. It remains in
  video for 250 ms after the most recent busy change. This is a pixel-change
  heuristic, not an application/window classifier.
* The worker reserves at most 15% of the current video target for refinement,
  capped at 128 KiB/s with a 16 KiB burst. UI bytes also debit the shared video
  budget. Each viewer applies the same refinement rate limit to cached replay.
  Small invalidations can borrow credit so an old tile does not conceal video.
* One shared compressed cache is limited to 16 MiB. Every viewer keeps only the
  latest pending update for each tile and a 16 KiB SCTP queue threshold.
  Slow viewers do not accumulate a history of the desktop or block other peers.
* The reliable ordered `qsm-exact-ui` channel carries only these bounded binary
  records. Input retains its existing control/pointer channels. DTLS protects
  the new channel; gateway deployments continue to use TCP 8006 without a new
  public port.
* Geometry, compressed input and inflated output are bounded on the bridge and
  browser. A decode error disables the optional overlay. Worker record gaps or
  missing heartbeats clear the shared cache and request a fresh snapshot.

## Private protocol and upgrades

The existing authenticated local `SOCK_SEQPACKET` video tap carries records
with flag `0x80`. A 28-byte network-order header has layout
`!4sBBHIIHHHHHH`: magic `QUI1`, operation, encoding, zero reserved field,
epoch, serial, surface width/height, tile x/y/width/height. Operations are
HELLO=0, RESET=1, PATCH=2, CLEAR=3, HEARTBEAT=4. Encoding is zero for RGBA or
one for grayscale PATCH only. A browser receives RESET/PATCH/CLEAR with its
own epoch and serial; it never receives a worker HELLO.

Private input opcode 10 uses one byte: off=0, on=1, resync=2, probe=3.
A new worker emits no new media record until a bridge probes support. Old
workers safely ignore that unknown opcode. This preserves video/input when
old and new processes overlap during an upgrade; the new checkbox stays
unavailable until the complete new path is present. The browser control
message is `{"op":"qsm_ui_mode","enabled":true}` and is not forwarded as
guest input.

## Build and checks

The added build dependency is `zlib1g-dev`; runtime uses `zlib1g`. Build the
PVE packages on Debian 13 using the existing packaging scripts. For example:

```sh
export QSM_DIRECT_DEB_VERSION='0.4.0+stock4.14~exp6'
for profile in qsm-pve-direct qsm-pve-direct-standalone qsm-pve-direct-lxc qsm-pve-direct-gateway; do
    bash "packaging/debian/build-$profile-deb.sh"
done
```

The source suite adds worker pixel fidelity, motion/settling, priority, gap
recovery and resize checks; bounded decompression and multi-viewer bridge
checks; and browser tests for delayed inflation versus clear/resize/disable.

```sh
cmake -S . -B build-exact-ui -G Ninja -DBUILD_TESTING=ON
cmake --build build-exact-ui
ctest --test-dir build-exact-ui --output-on-failure
PYTHONPATH=. python3 -m unittest discover -s tests/browser_bridge -v
```

Mixed-content acceptance must include a **separate overlapping input window**,
with the underlying clip visibly advancing. Compare feature off/on at the same
network and video target. Verify typed content, source pixel fidelity, scroll,
move, resize and close; sample the old window location to detect resurrected
tiles. Repeat through a shaped link and both directions of an old/new worker
and bridge combination. Preserve the original desktop output and guest state.

The [2026-10-27 qualification](../upstream/review-exact-ui-2026-10-27/qualification.md)
records the experimental package version, measurements and coverage limits.
The [exp6 deployment check](../upstream/review-exact-ui-2026-10-27/deployment.md)
adds the per-node default and records its installation and real PVE/Firefox
check on the requested node.
