# Changelog

## 0.4.0 — 2026-08-31

- Added a real QEMU Display1 client and peer listener using the stable sd-bus ABI.
- Added CPU capture for inline `Scanout`/`Update` and Unix-FD-backed
  `ScanoutMap`/`UpdateMap`.
- Added QEMU keyboard, mouse, cursor, `SetUIInfo`, and AudioOut handling.
- Added one-slot video back-pressure and a bounded audio FIFO.
- Added a CPU-only FFmpeg/libx264 diagnostic encoder.
- Added in-process and cross-process fake-QEMU integration tests.
- Added release and ASan/UBSan validation with five passing tests.
- Reframed the first Sunshine milestone around `capture = qemu_dbus` and
  `encoder = software`; DMA-BUF is now a later performance optimization.

## 0.1.0–0.3.0

Initial architecture, protocol, backlog, mock pipeline, and executable core
simulation.
