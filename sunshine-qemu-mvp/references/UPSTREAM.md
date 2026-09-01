# Upstream references used by the implementation

Checked on 2026-08-31.

- QEMU D-Bus display specification:
  `https://www.qemu.org/docs/master/interop/dbus-display.html`
- QEMU standalone VNC server, an existing out-of-process Display1 consumer:
  `https://www.qemu.org/docs/master/tools/qemu-vnc.html`
- Sunshine repository and compatibility table:
  `https://github.com/LizardByte/Sunshine`
- Sunshine source baseline for the first adapter:
  tag `v2026.830.223700`, especially `src/platform/common.h` and
  `src/platform/linux/misc.cpp`.

No upstream source has been vendored. The project implements the documented
wire interfaces and keeps the Sunshine patch boundary separate.
