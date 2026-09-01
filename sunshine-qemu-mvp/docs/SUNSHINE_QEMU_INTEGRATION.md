# Sunshine QEMU Display1 integration

The first in-tree Sunshine layer is a CPU-output Linux display backend. Its
portable patch series is under `integration/sunshine/patches/`; it is pinned to
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

When built with `SUNSHINE_ENABLE_QEMU_DBUS_DMABUF=ON` and `gbm`/`libdrm`
development packages, the same CPU encoder path also accepts QEMU 8.2's
single-plane `ScanoutDMABUF`/`UpdateDMABUF`. It retains a CLOEXEC duplicate of
the D-Bus FD-list entry, imports it through a GBM render node and
`EGL_EXT_image_dma_buf_import`, then synchronously reads a BGRX FBO. This is a
headless EGL path. The QEMU guest-audio-only deployment also removes host
PulseAudio and libvirtualhid XTest fallback, so its complete resolver closure
needs no X11, Wayland, PulseAudio, or ALSA. It supports XRGB/ARGB/XBGR/ABGR
32-bit scanouts, validates dimensions/bytes, honors `y0_top`, and deliberately
rereads the full frame for each damage notification. It is not a zero-copy or
multi-plane (`ScanoutDMABUF2`) encoder implementation.

The patch intentionally advertises no `Listener.Unix.Map` capability. The
portable baseline remains `-display dbus,gl=off`, using inline
`Scanout`/`Update`. A native VirGL test uses `-display dbus,gl=on` and a
render node; `SUNSHINE_QEMU_DBUS_RENDER_NODE` overrides the default
`/dev/dri/renderD128` if needed.

## Reproduce the integration gate

Build an unmodified, pinned upstream checkout, apply the patch, then configure
it as a CPU-only Sunshine build.  The following records the exact local
validation shape; `CMAKE_INSTALL_PREFIX` makes the generated `assets/` folder
available beside the test binary.

```bash
git clone --recurse-submodules https://github.com/LizardByte/Sunshine.git .upstream/Sunshine
git -C .upstream/Sunshine checkout --detach 4f39fc116294abf8241bcd30e1b1e23d371e6e7b
git -C .upstream/Sunshine submodule update --init --recursive
git -C .upstream/Sunshine am ../../integration/sunshine/patches/000{1,2,3,4,5,6}-*.patch

./scripts/build-isolated-libva-2.21.sh

cmake -S .upstream/Sunshine -B .upstream/build-sunshine-qemu -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_INSTALL_PREFIX="$PWD/.upstream/build-sunshine-qemu" \
  -DBUILD_DOCS=OFF -DBUILD_TESTS=OFF -DSUNSHINE_ENABLE_TRAY=OFF \
  -DSUNSHINE_ENABLE_CUDA=OFF -DCUDA_FAIL_ON_MISSING=OFF \
  -DSUNSHINE_ENABLE_DRM=OFF -DSUNSHINE_ENABLE_VAAPI=OFF \
  -DSUNSHINE_ENABLE_VULKAN=OFF -DSUNSHINE_ENABLE_WAYLAND=OFF \
  -DSUNSHINE_ENABLE_X11=OFF -DSUNSHINE_ENABLE_KWIN=OFF \
  -DSUNSHINE_ENABLE_PORTAL=OFF -DSUNSHINE_ENABLE_QEMU_DBUS=ON \
  -DSUNSHINE_ENABLE_QEMU_DBUS_DMABUF=ON \
  -DSUNSHINE_ENABLE_QEMU_DBUS_AUDIO_ONLY=ON \
  -DLIBVIRTUALHID_ENABLE_XTEST=OFF \
  -DCMAKE_EXE_LINKER_FLAGS="-L$PWD/.upstream/prefix-libva-2.21.0/lib -Wl,-rpath,$PWD/.upstream/prefix-libva-2.21.0/lib"
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
which system `libva 2.20` lacks even though Sunshine VAAPI is disabled. Build
an isolated exact `libva 2.21.0` prefix and pass its `-L`/rpath only to the test
build as described in `integration/sunshine/ISOLATED_LIBVA.md`; it does not
replace the system library. Core `libva`/`libva-drm` remain necessary to link
the bundled static FFmpeg, but the software profile omits `va-x11`, X11, and
PulseAudio. The repository helper is the preferred exact invocation:

```bash
./scripts/build-isolated-libva-2.21.sh
./scripts/build-upstream-sunshine-qemu.sh
```

For the explicit strict deployment artifact used by the decoded-client audio
trace, set `SUNSHINE_BUILD_DIR=.upstream/build-sunshine-qemu-no-x11` while
running the same helper, then pass that binary as `SUNSHINE_BINARY` to
`scripts/run-moonlight-sunshine-qemu-audio-e2e.sh`. The helper rejects direct
`libX11`, `libXtst`, `libXi`, `libXext`, `libXrender`, `libXrandr`, and
`libwayland` entries and rejects those libraries plus `libpulse` and
`libasound` across the full `ldd` closure. It also requires the runtime
`libva.so.2` to resolve from the isolated prefix.

The strict artifact was qualified end-to-end at
`artifacts/validation/moonlight-sunshine-qemu-audio-e2e/run.gki7t0/`: KVM
guest tone, QEMU Display1 `AudioOutListener`, Sunshine Opus, and Moonlight's
decoded SDL-disk PCM. Its `audio-verdict.txt` reports 13.909 seconds of 48 kHz
stereo S16LE, max `0.0 dB`, mean `-3.3 dB`, and `decoded_pcm_non_silent=yes`.

## Deliberate exclusions

The original CPU gate proves only the inline display/software-encoder boundary.
The separate native VirGL/Moonlight DMA-BUF gate has passed; its exact evidence
is in `MOONLIGHT_SUNSHINE_VIRGL_E2E.md` and `VALIDATION.md`. The separate
decoded-audio gate is documented in `MOONLIGHT_AUDIO_E2E.md`; neither video
gate alone proves cursor composition, Unix shared maps, restart reconnect, or
release-all.
