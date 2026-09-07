# PVE 9 laboratory checks

These tools exercise real PVE 9, QEMU Display1, and a browser WebRTC peer. They do not install runtime packages automatically and require no client application.

- `qualify-pve-direct-browser-e2e.cjs` checks the protected same-origin browser transport.
- `qualify-pve-display1-ui.cjs` checks the Display1 fields in the real **Hardware → Display** dialog.
- `qualify-qsm-direct-worker-e2e.py` and `qualify-qsm-direct-worker-media-e2e.py` exercise the worker against a real QEMU Display1.
- `run-qsm-direct-stress-suite.py` is the release gate for video, input, resize, full screen, clipboard, reconnect, and concurrent viewers.
- `measure-direct-browser-e2e.py` records Chrome WebRTC receive-side metrics.
- `measure-novnc-browser-e2e.cjs` records aggregate traffic from PVE's stock noVNC split-menu item for a like-for-like lab comparison.
- `measure-console-scenarios.cjs` drives the shipped QSM Direct popup or the stock noVNC popup through the PVE UI, switches the guest between the `scenarios/` pages with Alt+1..5 inside the console, and reports visible pictures per second, picture gaps, received bandwidth, WebRTC decoder statistics and hover latency. Its results are collected in [docs/COMPARISON.md](../../docs/COMPARISON.md).
- `build-scenario-seed.py` turns the reviewed `hover-gate-102` cloud-init fixture into a scenario guest: it adds the `scenarios/` pages, a local H.264 clip and a maintenance SSH key.
- `probe-display-sleep-recovery.cjs` opens the QSM Direct popup for a guest whose compositor may have turned its output off, records the console status and decoded-picture timeline, optionally blanks the guest mid-session (`--sleep-cmd`) and resizes the popup, then wakes the guest with real browser mouse or key input.

For a precise UI regression, create a temporary PVE account with `PVEVMUser` only on the test VM, sign in through the browser, and select the left **Console** item. Success requires exactly one QSM iframe in the Console card and zero calls to `window.open`.

For a numerical QSM/noVNC comparison, use one stable guest fixture and the
same browser host for both probes. Run the WebRTC probe on the PVE node and
the noVNC probe on the browser host:

```bash
# PVE node
python3 measure-direct-browser-e2e.py \
  --vmid VMID --width 1280 --height 800 --warmup-seconds 10 \
  --drag-runs 1 \
  --browser-host BROWSER_HOST --browser-user USER --browser-key KEY \
  --browser-script /absolute/path/browser-webrtc-receiver.cjs

# browser host
node measure-novnc-browser-e2e.cjs \
  --pve-url https://PVE_NODE:8006 --user USER@REALM \
  --password-file /secure/pve.password --vmid VMID --duration-ms 10000
```

`measure-novnc-browser-e2e.cjs` reads only aggregate WebSocket frame sizes; it
never writes tickets, cookies, credentials, or RFB payloads to its output.
