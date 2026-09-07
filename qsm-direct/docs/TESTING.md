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
