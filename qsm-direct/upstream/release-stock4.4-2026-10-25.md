QSM Direct **0.4.0+stock4.4** fixes Firefox keyboard capture and adds a mouse-accessible **Capture keyboard (full screen)** action in the toolbar and Settings, including embedded consoles. F11 and macOS shortcut remapping are not required.

Earlier builds only called Chromium's `navigator.keyboard.lock()`. Current Firefox supports a different API: `requestFullscreen({keyboardLock: "browser"})`, also used by Safari where supported. QSM now requests that mode and detects option consumption without browser sniffing; Chromium retains its existing all-key capture and permission flow. Unsupported/denied capture is visible rather than reported as active. The earlier assertion that Firefox could not capture browser shortcuts was incomplete.

Click Capture keyboard, then the guest picture. A short Esc and available browser shortcuts go to the guest. Click Release keyboard, open Settings, or hold Esc to leave capture. Guest first also reserves Ctrl+Alt+Shift+Esc; Raw still reserves no QSM chord. Explicit capture selects Guest first if Client first was saved, preserving Exact typing/clicks. The embedded console's ordinary in-page maximize control stays separate.

Focus loss/close releases held input and capture. Async fullscreen completion after cancellation cannot reacquire capture. OS-reserved keys remain platform-dependent; Send to guest buttons remain available.

Project sponsor: [Takt](https://taktcycles.com/).

### Checks and scope

- Headful Firefox 157, native Linux XTEST input, real SCTP/worker/Sway/Qt LXC: short Esc, Ctrl+W/T/L/Tab, F5 and Ctrl+R/F reach the guest while fullscreen stays active, for top-level and embedded consoles. Old ordinary fullscreen reproduces Esc exiting without guest delivery.
- Release button, Settings, local exit chord and long Esc exit pass. Complete production JavaScript/DOM smoke checks cover top-level and embedded capture/release without page errors. Controller regressions cover Chromium ownership/denial, unsupported options and late resolution after cancellation. All 22 CTest gates pass.
- Physical macOS and Safari were not available; the no-F11 workflow and API path are implemented, not a real-Mac hardware qualification. This is a viewer update; worker/media/input protocols and Exact typing/clicks remain unchanged from stock4.3.

### Upgrade

```sh
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone \
  qsm-pve-direct-lxc qsm-pve-direct-gateway
```

Update installed profiles together. QSM service restarts interrupt open consoles; guests keep running. **Reload the PVE page with cache bypass and reopen consoles**, then click Capture keyboard. No guest reboot or guest-agent update is required. Do not install qsm-console-guest on the PVE host.

Complete source, five matching packages, signed checksums, instructions, provenance and qualification are attached. The standalone preview requires official PVE 9 / Debian 13 amd64; it is not part of the Debian archive. No discussion messages are posted by this release.
