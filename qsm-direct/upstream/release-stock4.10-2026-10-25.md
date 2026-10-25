QSM Direct **0.4.0+stock4.10** adds an editable **Host shortcuts** list to the existing **Settings > Keyboard shortcut priority** section.

The previous fix protected pending modifiers when the host swallowed Tab. It did not cover a host/global shortcut that also delivers the complete Alt-down, Tab-down, Tab-up, Alt-up sequence to the web page. Those DOM events could still emit a guest Alt+Tab and open the guest application switcher.

Host routing now uses one explicit rule list, before keyboard and clipboard forwarding. Each rule selects an exact logical combination and a Host/browser, Release keyboard, Toggle full screen or Show/hide controls action. The editor supports manual aliases (Ctrl/Alt/Option/Cmd/Meta), recording, add/edit/remove, duplicate validation, reset and a per-rule **Send to guest** bypass. Guest first and Client first profiles persist independently; existing preferences gain defaults without losing other settings. Raw bypasses the list. Rules apply immediately. The keyboard section precedes media policy controls.

Defaults reserve Alt+Tab, Alt+Shift+Tab, Meta+Tab and Shift+Meta+Tab, plus Meta+Space, Meta+L and Ctrl+Alt+Delete, and retain Ctrl+Alt+Shift+Escape as a configurable release action. Whole chords are latched through key release even if modifiers are released first. Local actions run once, not on autorepeat or keyup. Browser-native paste after a host Ctrl/Cmd+V cannot re-enter guest clipboard forwarding. No extra Ctrl+Alt+Tab-to-Alt+Tab substitution is introduced. Native OS/browser handling remains subject to platform policy; the list controls QSM forwarding.

The design follows configurable viewer-side actions in [Remmina](https://remmina.gitlab.io/remminadoc.gitlab.io/md__builds__remmina_remmina_ci__remmina_wiki__remmina__preferences.html) and explicit send-key bypasses in [virt-viewer](https://manpages.debian.org/experimental/virt-viewer/remote-viewer.1.en.html), adapted to a browser's input constraints.

**23 CTest gates pass.** Complete production UI Firefox/Chromium checks, standalone and embedded, cover add/edit/remove/record, validation, persistence, separate profiles, Raw, reset and fullscreen editing. A Firefox/real SCTP/installed bridge+worker/owned CT108 fixture comparison delivers the complete reported DOM chord before any blur: stock4.9 emits guest Alt+Tab; the new default list emits zero guest keyboard packets in forward/reverse and modifier-first release orders. Editing/removing a rule changes routing immediately; explicit Send to guest and Raw still emit their intended chords. Logical DOM events and MacIntel are modelled on Linux; native macOS/global-shortcut qualification is not claimed. Worker/bridge/protocol remain unchanged. CT108 output is restored and the owned fixture stopped.

Upgrade installed matching profiles together:

```
apt update
apt install --only-upgrade qsm-pve-direct qsm-pve-direct-standalone qsm-pve-direct-gateway qsm-pve-direct-lxc
```

Omit profiles not installed. Reload PVE without cache and reopen consoles. Use Settings > Keyboard shortcut priority > Host shortcuts. No guest update/restart needed. QSM service restarts interrupt consoles. Matching source, debs, signatures, installation instructions and qualification attached. Unofficial PVE 9 / Debian 13 amd64 preview, sponsored by [Takt](https://taktcycles.com/). No discussion messages posted by this release.
