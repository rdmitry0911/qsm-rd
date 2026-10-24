QSM Direct **0.4.0+stock4.3** adds **Exact clicks** by default for mouse, trackpad clicks/taps and touchscreen taps, alongside Exact typing.

A short click is one complete action sent on release with its position and modifier snapshot. Moving 4 CSS pixels or holding for 250 ms confirms a drag/hold. Its movement stays on the ordered channel through release acknowledgement, then idle motion resumes on the fast pointer channel. Both channels share a per-viewer coordinate serial filter. This prevents delayed button releases and older pointer packets from extending a click or dragging to a later idle coordinate. Raw/Physical buttons retains immediate held input.

Fractional trackpad scroll deltas accumulate instead of being truncated. Horizontal scrolling now reaches QEMU and wlroots. Side buttons map to QEMU side/extra rather than wheel slots. Existing touchscreen gesture bindings and hold-to-drag remain.

Project sponsor: [Takt](https://taktcycles.com/).

### Checks and limits

- Real Firefox 157 / SCTP / CT108 Sway / Qt5: a 200-ms press plus a 900-ms control delivery gap produced 2 activations of an auto-repeat test button with physical edges and watchdog, versus 1 with Exact clicks. Guest hold shortened from 333 ms to 0.59 ms.
- Click position followed by fast movement, normal double click, right click, deliberate hold, drag with delayed release and later motion, and fractional two-axis scrolling pass. Trackpad-compatible browser events were tested; no physical trackpad qualification is claimed.
- 73 bridge tests, 22 CTest gates, production worker/fake-QEMU input/video/cursor/resize E2E pass. Mouse ownership, expiry, action deduplication, shared coordinate ordering, side-button mapping and old-server fallback have regression coverage.
- Short button-down is intentionally deferred until release. Physical button holds/Raw is available for immediate physical input. A 200-ms confirmation timeout can end a drag early; stale queued snapshots never re-press it. Native double-click timing can still change under delivery jitter.

[Detailed qualification](https://github.com/rdmitry0911/qsm-rd/blob/v0.4.0-qsm-direct-20261024-stock4.3/qsm-direct/upstream/review-mouse-2026-10-24/qualification.txt).

### Upgrade

Use the signed APT repository or the matching attached debs on PVE 9 / Debian 13 amd64. Keep official dependency repositories enabled.

```sh
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone \
  qsm-pve-direct-lxc qsm-pve-direct-gateway
```

`--only-upgrade` skips uninstalled profiles. QSM service restarts interrupt open consoles. **Reload PVE with cache bypass and reopen consoles.** Cached viewers keep their previous behavior. Select Guest first + Exact clicks if Raw/Physical buttons was previously selected. No guest reboot, agent update or desktop reprovisioning is required. Never install qsm-console-guest on the PVE host; it is embedded in the LXC helper.

The complete tracked source, five matching debs, signed checksums, build provenance, instructions and QA are attached. This is an unofficial preview; Debian contrib review remains separate. Forum text is prepared only; no discussion messages are sent by this release.
