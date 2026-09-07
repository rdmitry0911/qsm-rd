# Testing QSM Direct

Run the fast source-level suite:

```bash
cmake -S . -B build-direct-tests -G Ninja -DBUILD_TESTING=ON
cmake --build build-direct-tests
ctest --test-dir build-direct-tests --output-on-failure
```

It covers core/D-Bus code, the guest channel, package layout, encoder choice, terminal-service auto configuration, and the static PVE Console overlay check.

The automatic encoder policy also requires a worker E2E test. It forces an already-selected hardware FFmpeg encoder to fail, then verifies that the first and all concurrent viewers continue on `libx264`. `Hardware only` is not part of this case and does not fall back:

```bash
python3 lab/proxmox9/qualify-qsm-direct-worker-media-e2e.py \
  --worker /usr/lib/qsm-pve-direct/bin/qsm-direct-media-worker \
  --fake-qemu /path/to/qmdp-fake-qemu \
  --encoder h264_nvenc --width 1280 --height 798 --frames 180 --fps 60 \
  --force-hardware-failure
```

Use the real stress suite on a PVE node. It does not substitute QEMU, the WebRTC peer, or browser input:

```bash
python3 lab/proxmox9/run-qsm-direct-stress-suite.py \
  --vmid 103 \
  --browser-host BROWSER_HOST --browser-user USER --browser-key KEY \
  --browser-script /absolute/path/browser-webrtc-receiver.cjs
```

The suite covers first video, concurrent viewers, mouse/keyboard input, window ↔ full-screen transitions, viewport changes, clipboard, VM reboot, and terminal-service restart. Add `--browser-headful --visual-evidence` for visual evidence.

The PVE UI integration must separately prove that selecting **Console** in the left navigation creates an iframe in the existing content area rather than a popup. A dedicated browser gate also covers the order-dependent embedded and separate-window cases (`frame → window` and `window → frame`) and rejects an iframe with the height of an empty ExtJS panel:

```bash
node lab/proxmox9/qualify-pve-direct-embedded-window-e2e.cjs \
  --pve-url https://PVE_NODE:8006 --user TEMP_USER@pve \
  --password-file /secure/pve.password --vmid 103 \
  --chrome /usr/bin/google-chrome
```

The temporary account needs `PVEVMUser` only on the test VM.

## Sleeping guest

A desktop guest that turned its output off (idle DPMS) or locked its session
keeps QEMU emitting its last or a black scanout and ignores a requested
display mode until it wakes. The Console must explain that state and recover
on ordinary input rather than dead-end on "did not acknowledge this window
size". The static overlay test asserts the wake hint, the sleep detector, the
bounded fast retry and the once-per-input re-issue of a stalled size request;
the behaviour itself is verified on a real guest with the probe below, which
records the console's own status line and the decoded picture over time:

```bash
node lab/proxmox9/probe-display-sleep-recovery.cjs \
  --pve-url https://PVE_NODE:8006 --user TEMP_USER@pve \
  --password-file /secure/pve.password --vmid 103 --chrome /usr/bin/chromium \
  --observe-ms 46000 --sleep-after-ms 6000 \
  --sleep-cmd "qm guest exec 103 -- sudo -u USER XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 kscreen-doctor --dpms off" \
  --resize-after-ms 13000 --resize-to 1100x700 --wake-after-ms 27000 --wake mouse
```

A passing run shows *Guest display looks asleep…* a few seconds after the
guest blanks, no dead-end size message after the resize, a bright picture
within about a second of the wake input, and the final size equal to the
resized popup. Run it once more with `--wake none` to prove that nothing but
input wakes the guest.

## Wheel direction and scenario comparison

`measure-console-scenarios.cjs --scenarios 5 --wheel-delta 100` holds the
fixture document still and scrolls it with the console's mouse wheel; a
browser scroll-down must produce visible motion (distinct pictures per second
well above zero). The worker E2E additionally asserts `wheel_down=1 wheel_up=0`
in the fake QEMU trace for one positive-delta scroll. Scenarios 1–4 produce
the comparison tables in [COMPARISON.md](COMPARISON.md).
