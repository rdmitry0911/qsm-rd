QSM Direct **0.4.0+stock4.7** fixes Settings exiting a keyboard-captured fullscreen console and makes keyboard capture easy to find.

Opening/editing Settings releases held guest input and keeps fullscreen. Close beside the panel title returns focus to the guest and resumes capture. Chromium temporarily unlocks its separate keyboard controller; Firefox/Safari retain their fullscreen-bound session while Settings owns DOM focus. A Settings panel opened during a pending fullscreen request cannot cancel the request or have its focus stolen on completion.

**Capture keyboard** is a readable text button near the top of the left toolbar and the first action in Settings. Reopening Settings restores its top position. Guest first selects input routing; browser capture still requires the explicit Capture keyboard click. The reveal handle now stays beside the visible toolbar, avoiding a reproducible overlap with Settings in an embedded console. Explicit Release keyboard, Client first, console close and long Esc remain exits. The embedded in-page maximize action retains its behavior; Capture keyboard enters native full screen.

The complete production UI passes Firefox and Chromium checks in both separate and embedded contexts: capture control visibility without scrolling, no handle/control overlap, capture from Settings, editing settings without leaving fullscreen, close/refocus/resume, blur/focus, and explicit release. All 22 CTest gates pass, including pending-request and controller ownership checks. Browser API/Ext stubs are used for UI tests; no new physical Mac, media transport or hardware qualification is claimed. Worker, bridge and protocol are unchanged. Logical remapping from stock4.6 remains.

Update installed matching profiles together:

```
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone qsm-pve-direct-gateway qsm-pve-direct-lxc
```

Omit profiles that are not installed. Reload the PVE page without cache, reopen the console, choose Guest first, then click Capture keyboard in the left toolbar or at the top of Settings. No guest restart/update is needed. QSM service restarts interrupt existing consoles. Matching source, five debs, checksums/signature, instructions and qualification are attached. Unofficial PVE 9 / Debian 13 amd64 preview, sponsored by [Takt](https://taktcycles.com/). No discussion messages posted by this release.
