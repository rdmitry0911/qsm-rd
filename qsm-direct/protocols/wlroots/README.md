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

Separate cursor capture uses the following unmodified MIT-licensed XML files
from wayland-protocols tag `1.44`, `staging/` (copyright in each file):

- `ext-image-capture-source-v1.xml`
- `ext-image-copy-capture-v1.xml`
- `ext-foreign-toplevel-list-v1.xml` (interface dependency of the source protocol)

These bindings do not require upgrading the host Wayland library. The cursor
extensions are discovered at runtime; older compositors retain cursor-in-video
capture.
