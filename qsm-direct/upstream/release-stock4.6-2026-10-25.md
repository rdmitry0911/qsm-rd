QSM Direct **0.4.0+stock4.6** fixes modifier routing with a macOS Option/Command swap. QSM previously mapped modifiers only from physical `event.code`, allowing their guest meaning to differ from macOS's logical assignment.

Guest first now maps modifier meaning from `event.key`, retaining side/location; printable keys stay physical for the guest layout. Logical Command+Tab stays on the Mac and is not sent to the guest. Logical Option+Tab sends guest Alt+Tab. With swapped assignments, physical Option means Command (host), while physical Command means Option (guest).

Outside Raw, macOS Command is deferred until a guest action, preventing a lone guest Alt/Super/menu effect when a host shortcut is filtered. Complete keyboard/mouse actions carry logical modifiers; physical main-key holds, dragging and modifier-wheel flush the deferred modifier as needed. A genuine solo Command tap still sends guest Super. Focus cleanup and foreign repeats never invent that tap. Late source keydowns while suspended/unfocused are ignored. Raw deliberately retains physical positions/edges and bypasses host-only routing.

This cannot disable native macOS or third-party global hotkeys. The Send to guest Alt+Tab button remains available. Fullscreen/capture behavior from stock4.5 is retained.

Project sponsor: [Takt](https://taktcycles.com/).

### Validation and upgrade

A simulated swapped macOS modifier profile in Firefox, through real SCTP/bridge/worker, confirms zero guest key packets for logical host Command+Tab, exactly Alt/Tab for logical guest Option+Tab, and a logical Meta chord without extra menu taps. Both separate and embedded consoles pass. Full production UI initialization/capture/focus checks pass in Firefox and Chromium. Unit regressions cover both modifier sides, Raw/physical modes, solo Command, mouse/drag/wheel, physical keyboard input and suspended/foreign-repeat cases. All 22 CTest gates pass. The host profile and DOM remap events are simulated on Linux, not a physical Mac qualification. Worker and bridge payloads remain unchanged.

```sh
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone \
  qsm-pve-direct-lxc qsm-pve-direct-gateway
```

Reload PVE without cache and reopen consoles; select **Guest first** to follow macOS remapping. Update installed profiles together. No guest restart/update is needed. QSM service restarts interrupt existing consoles. Complete source, matching debs, signatures, instructions and qualification are attached. This is an unofficial PVE 9 / Debian 13 amd64 preview. No discussion messages are posted by this release.
