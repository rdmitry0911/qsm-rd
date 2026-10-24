QSM Direct **0.4.0+stock4.2** prevents delayed key releases from turning short presses into unwanted guest repeats. **Exact typing is now the default**, for both VM and LXC.

Each initial key press and genuine browser repeat is one complete action. The worker performs its press/release locally, with the modifier snapshot. The first character is immediate; no heartbeat wait is added. Clipboard/special-key chords use the same atomic path. Physical holds and Raw remain available for games and shortcut editors; Raw forces physical mode. Their 200-ms watchdog bounds stale holds but cannot guarantee zero false native repeats.

The bridge now tracks per-viewer input ownership and releases a departing viewer's keys even when another viewer retains the shared producer. New snapshots/clock counters log no typed content or key identities. Supported native-fullscreen browsers request all available keys through Keyboard Lock, with visible permission failure and explicit unlock on client focus/close. Settings adds Esc, Alt+Tab, Ctrl+Alt+Del and Super buttons; Meta uses correct QEMU extended codes.

Project sponsor: [Takt](https://taktcycles.com/).

### Checks and limits

- In real Firefox/Sway/Qt testing, a 900-ms control-delivery gap gave 8 characters from one short source press with stock4.1, and 1 in Exact typing. A near-threshold gap still gave 5 with a heartbeat-only guard and 1 in Exact typing. Six source events gave six characters. Raw native repeat and modifier chords pass.
- 68 bridge/controller/input tests, 21 CTest gates and production worker/fake-QEMU input/cursor/resize/video E2E pass. The latter confirms atomic chords, held-Ctrl preservation and EOF cleanup.
- Firefox page fullscreen reserves Esc; browser F11 fullscreen, entered outside guest focus, passes it in the headful X11 test. Chromium lock-all/API/CDP behavior passes; native XTEST injection under lock is inconclusive and is not a real-hardware qualification. OS-reserved sequences depend on platform support.
- Exact typing sends completed ordinary-key actions; choose Physical holds/Raw when an application needs an ordinary key to remain physically down. Timing can still delay delivery. This updates input handling; stock4.1 video behavior and its documented bandwidth-collapse limits remain.

[Detailed qualification](https://github.com/rdmitry0911/qsm-rd/blob/v0.4.0-qsm-direct-20261024-stock4.2/qsm-direct/upstream/review-keyboard-2026-10-24/qualification.txt) distinguishes production read-only observations from the injected test gap. No guest reboot, desktop reprovisioning or guest-agent update is needed.

### Upgrade

Use the [signed APT repository](https://rdmitry0911.github.io/qsm-rd/) or the five attached matching debs on PVE 9 / Debian 13 amd64. Keep official dependency repositories enabled.

```sh
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone \
  qsm-pve-direct-lxc qsm-pve-direct-gateway
```

`--only-upgrade` skips uninstalled profiles. QSM service restarts interrupt open consoles. **Reload the PVE page with cache bypass and reopen/reload consoles.** Existing cached viewers retain their previous behavior. If Raw was selected, choose **Guest first + Exact typing** for text entry; Raw intentionally overrides exact mode.

Read INSTALL-STOCK.txt for setup/migration, INSTALL-GATEWAY.txt for the optional TCP-8006 profile and INSTALL-APT.txt for scoped signing-key/repository setup. Never install qsm-console-guest on the PVE host; it is embedded in the LXC helper. Existing native previews retain their backend dependencies until separately migrated.

The signed tag/checksums, complete tracked source, all five debs, build provenance and QA bundle are attached. The standalone update requires no new Proxmox package patch. Debian contrib review remains separate and unchanged. This is an unofficial preview. The forum text is prepared only; nothing is posted or mailed by this release.
