# Sunshine QEMU Display1 integration

The first in-tree Sunshine layer is a CPU-only Linux display backend.  Its
portable patch is under `integration/sunshine/patches/`; it is pinned to
Sunshine `v2026.830.223700` (`4f39fc116294abf8241bcd30e1b1e23d371e6e7b`).

It adds the opt-in CMake switch `SUNSHINE_ENABLE_QEMU_DBUS` and selects the
backend only when both of these are true:

```ini
capture = qemu_dbus
encoder = software
```

The per-VM private bus is deliberately process-scoped rather than a public
Sunshine configuration value:

```text
SUNSHINE_QEMU_DBUS_ADDRESS=unix:path=/run/user/1000/qemu-vm42.bus
SUNSHINE_QEMU_DBUS_DESTINATION=org.qemu     # optional, this is the default
```

## Implemented first slice

- message-bus connection and `Console.RegisterListener` Unix-FD handoff;
- p2p D-Bus listener with inline `Scanout` and `Update` callbacks;
- eight supported 32-bit pixman layouts normalized to tight BGR0;
- bounded dimensions and 512-MiB frame validation;
- stale/out-of-bounds damage acknowledged but never copied;
- latest complete CPU frame copied into a Sunshine `img_t` and consumed by the
  existing software/libx264 path;
- listener disconnect becomes `capture_e::reinit`, instead of continuing to
  publish an old frame.

The patch intentionally advertises no `Listener.Unix.Map` capability and
does not implement DMA-BUF.  QEMU must run a CPU display (`-display
dbus,gl=off`); the tested baseline is standard VGA or `virtio-vga` with
`gl=off`.  The current QEMU 8.2 runtime is inline `Scanout`/`Update` only.

## Reproduce the integration gate

Build an unmodified, pinned upstream checkout, apply the patch, then configure
it as a CPU-only Sunshine build.  The following records the exact local
validation shape; `CMAKE_INSTALL_PREFIX` makes the generated `assets/` folder
available beside the test binary.

```bash
git clone https://github.com/LizardByte/Sunshine.git .upstream/Sunshine
git -C .upstream/Sunshine checkout v2026.830.223700
git -C .upstream/Sunshine apply \
  ../../integration/sunshine/patches/0001-platform-linux-add-QEMU-Display1-CPU-capture.patch

cmake -S .upstream/Sunshine -B .upstream/build-sunshine-qemu -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_INSTALL_PREFIX="$PWD/.upstream/build-sunshine-qemu" \
  -DBUILD_DOCS=OFF -DBUILD_TESTS=OFF -DSUNSHINE_ENABLE_TRAY=OFF \
  -DSUNSHINE_ENABLE_CUDA=OFF -DCUDA_FAIL_ON_MISSING=OFF \
  -DSUNSHINE_ENABLE_DRM=OFF -DSUNSHINE_ENABLE_VAAPI=OFF \
  -DSUNSHINE_ENABLE_VULKAN=OFF -DSUNSHINE_ENABLE_WAYLAND=OFF \
  -DSUNSHINE_ENABLE_X11=OFF -DSUNSHINE_ENABLE_KWIN=OFF \
  -DSUNSHINE_ENABLE_PORTAL=OFF -DSUNSHINE_ENABLE_QEMU_DBUS=ON
cmake --build .upstream/build-sunshine-qemu --target sunshine -j2

SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu/sunshine" \
  ./scripts/run-upstream-sunshine-qemu-e2e.sh
```

The runner builds a 512-byte SeaBIOS fixture, launches QEMU under TCG on a
private D-Bus, and starts Sunshine with `capture=qemu_dbus` and
`encoder=software`.  It asserts QEMU screencasting, libx264 creation and the
selected software encoder; the expected bounded shutdown is status `124`.
The retained trace is `artifacts/validation/upstream-sunshine-qemu-e2e/trace.txt`.

On this Ubuntu 24.04 host the upstream prebuilt FFmpeg needs `vaMapBuffer2`,
which system `libva 2.20` lacks even though VAAPI is disabled.  The validation
used an isolated newer libva under `.upstream/libva` and passed its `-L`/rpath
only to the test build.  It did not replace the system libva.  A supported
Sunshine package with a matching FFmpeg/libva pair does not need that local
workaround.

## Deliberate exclusions

This gate proves the patched Sunshine display/CPU encoder boundary.  It does
not yet prove a Moonlight session, direct QEMU input, QEMU audio, cursor
composition, Unix shared maps, DMA-BUF, restart reconnect, or release-all.
