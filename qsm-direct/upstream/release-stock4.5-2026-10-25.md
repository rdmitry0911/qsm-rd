QSM Direct **0.4.0+stock4.5** fixes a stock4.4 regression: switching to another host application with Alt/Cmd+Tab made QSM exit its fullscreen session.

Temporary focus loss now pauses guest input and releases held keys/buttons, preserving the fullscreen session and its Firefox/Safari keyboard-lock intent. Chromium's separate keyboard lock is released while unfocused and reacquired on return. Returning to the console resumes capture in the existing fullscreen; it does not restore old holds or request fullscreen again. A pending fullscreen request completing while unfocused does not focus the background guest.

Explicit Release keyboard, Settings, Client first, close and long Esc remain exit paths. Capture keyboard still requires no F11 or macOS shortcut remapping. This fixes QSM's own forced exit; browser/OS fullscreen policy can still differ.

Project sponsor: [Takt](https://taktcycles.com/).

### Validation and upgrade

The complete production UI is tested in Firefox and Chromium, in both standalone and embedded contexts: blur preserves fullscreen, focus resumes capture, and explicit release still exits. A real Firefox/SCTP/Sway/Qt LXC check confirms that a held modifier is released, fullscreen persists and resumed typing has no stale modifier. The blur/focus events are injected into the page; physical macOS app switching was not available. All 22 CTest gates pass. The worker/bridge protocols and media payload remain unchanged.

```sh
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone \
  qsm-pve-direct-lxc qsm-pve-direct-gateway
```

Update installed profiles together, reload PVE with cache bypass and reopen consoles. No guest restart or guest-agent update is needed. QSM service restarts interrupt existing consoles. Complete source, matching debs, signatures, instructions and qualification are attached. This is an unofficial PVE 9 / Debian 13 amd64 preview. No forum/mail comments are posted by this release.
