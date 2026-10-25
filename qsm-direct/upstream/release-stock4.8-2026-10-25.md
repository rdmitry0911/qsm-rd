QSM Direct **0.4.0+stock4.8** fixes two console-control regressions reported after stock4.7.

The controls handle is fixed at the left screen edge. The toolbar has an independent inset so its buttons cannot overlap the handle. Opening/closing the strip no longer moves the handle by the width of a status message. The handle toggles visibility consistently and exposes its expanded state.

**Capture keyboard** now only enables or resumes capture and retains its label. Repeated clicks, including the Settings action, focus the guest without exiting fullscreen or creating another fullscreen request. An ordinary existing fullscreen can be upgraded to keyboard capture. **Release keyboard** is a distinct Settings action; the **Exit Full Screen** toolbar button exits native fullscreen in both separate and embedded consoles. The embedded in-page maximize remains available outside native fullscreen. Settings continues to preserve fullscreen.

All 22 CTest gates pass. Complete production UI checks pass in Firefox and Chromium, top-level and embedded, at 1280x800 and 640x480: repeated handle toggles stay at x=0, controls do not overlap, ordinary fullscreen upgrade/repeated Capture/Settings Capture preserve fullscreen, and explicit exit/release work. The embedded in-page maximize/capture/return round trip passes. A headful Firefox 157/Xvfb/Openbox/XTEST run confirms ordinary fullscreen Esc exits, while after capture upgrade native Esc is delivered to content and fullscreen remains, in both contexts. API/Ext stubs are used; no new physical Mac, guest transport or hardware qualification is claimed. Worker, bridge and input protocol are unchanged.

Upgrade matching installed profiles together:

```
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone qsm-pve-direct-gateway qsm-pve-direct-lxc
```

Omit profiles not installed. Reload the PVE page without cache and reopen consoles. Click Capture keyboard; use the separate Release/Exit action to leave. No guest restart/update needed. QSM service restarts interrupt existing consoles. Matching source, debs, signatures, instructions and qualification attached. Unofficial PVE 9 / Debian 13 amd64 preview, sponsored by [Takt](https://taktcycles.com/). No discussion messages posted by this release.
