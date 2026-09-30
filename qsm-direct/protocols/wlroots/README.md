# wlroots Wayland protocols (vendored)

Client-side protocol definitions used by the QSM Direct media worker to capture
and drive a headless wlroots compositor (sway) that hosts an LXC container's
desktop session. They are not packaged by Debian, so they are vendored here and
turned into C bindings at build time with `wayland-scanner`.

| File | Origin | License |
|------|--------|---------|
| `wlr-screencopy-unstable-v1.xml` | wlroots/wlr-protocols `unstable/` | MIT (see file header) |
| `wlr-virtual-pointer-unstable-v1.xml` | wlroots/wlr-protocols `unstable/` | MIT (see file header) |
| `wlr-output-management-unstable-v1.xml` | wlroots/wlr-protocols `unstable/` | HPND-style permissive (Purism; see file header) |
| `virtual-keyboard-unstable-v1.xml` | wlroots `0.17` `protocol/` | MIT (see file header) |
