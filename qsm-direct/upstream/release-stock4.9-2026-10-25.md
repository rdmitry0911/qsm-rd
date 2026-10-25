QSM Direct **0.4.0+stock4.9** fixes a leading Alt press reaching the guest before a host application switch. The earlier protection covered macOS Command, while Alt was still sent immediately.

All shortcut modifiers are now deferred outside Raw, after negotiating atomic-key support. If the host consumes Tab before DOM and the console loses focus, no pending Alt/Ctrl/Shift/Command is sent to the guest. Exact typing carries the modifier snapshot in its complete key action, with no debounce on the main key. A deliberate solo modifier or modifier-only chord completes on focused release; releases while unfocused/hidden/suspended never invent menu taps. Physical main-key holds, dragging and wheel input flush pending modifiers before the actual guest action. Raw retains immediate physical edges. Logical macOS remapping and explicit host Cmd+Tab routing remain unchanged; this cannot prevent OS/global shortcuts that are also delivered as real guest actions.

All 22 CTest gates pass. Unit cases cover host-swallowed Tab, both modifier sides, reverse switching, heartbeat exclusion, focus/release races, solo/modifier-only chords, guest keyboard/mouse/wheel actions, clipboard, Raw and legacy negotiation. Full production UI checks pass in Firefox/Chromium, separate and embedded, desktop/narrow sizes. A headful Firefox/XTEST/real SCTP bridge/worker/owned CT108 fixture comparison qualifies the input path: stock4.8 emits Alt before a guest action; the fixed source keeps pending prefixes out of guest events, while solo Alt, guest Alt+F, physical holds and Raw remain functional. Tab consumption is modelled as absence of a DOM Tab; a second owned host window and, where needed, explicit blur drive the handoff. No native macOS chord or new hardware qualification is claimed. Worker, bridge and protocol are unchanged.

Upgrade installed matching profiles together:

```
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone qsm-pve-direct-gateway qsm-pve-direct-lxc
```

Omit profiles not installed. Reload PVE without cache and reopen consoles; choose Guest first and Exact typing. No guest update/restart needed. QSM service restarts interrupt consoles. Matching source, debs, signatures, instructions and qualification attached. Unofficial PVE 9 / Debian 13 amd64 preview, sponsored by [Takt](https://taktcycles.com/). No discussion messages posted by this release.
