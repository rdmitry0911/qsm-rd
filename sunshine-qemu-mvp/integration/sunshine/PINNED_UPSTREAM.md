# Pinned Sunshine source

The QEMU Display1 patch series is generated against the following exact
upstream base:

```text
repository: https://github.com/LizardByte/Sunshine.git
tag:        v2026.830.223700
commit:     4f39fc116294abf8241bcd30e1b1e23d371e6e7b
patches:
  - patches/0001-platform-linux-add-QEMU-Display1-CPU-capture.patch
    SHA-256: 717258e53c73f40a51433067326b50375e91b511e53d9dec767ae5e84e2f3bdd
  - patches/0002-platform-linux-route-QEMU-Display1-input.patch
    SHA-256: 3c560789403846773c0122974a681f18469585b00a431b6ce4f790955dbc4166
  - patches/0003-platform-linux-add-QEMU-Display1-guest-audio-source.patch
    SHA-256: 2bfedbf4475125f9f469145f29f3c059d875681a4ceb61509b2fac8b17ca230f
  - patches/0004-platform-linux-qemu-dmabuf-egl-readback.patch
    SHA-256: 46c06c088d7642486d79ed40496bb54a9b9b0bbf081df10508c06e3831a19715
  - patches/0005-cmake-avoid-X11-FFmpeg-glue-for-software-builds.patch
    SHA-256: edc8a9580d40505c076a95ee9e2300cccc24802f5de1a5f3692e61668d6e52ec
  - patches/0006-platform-linux-add-QEMU-guest-audio-only-build.patch
    SHA-256: 91439272fc8c8f7713a560cd3071a279af873748f4c7e82f2623ac254ca76ffc
  - patches/0007-platform-linux-close-QEMU-listener-transport-before-teardown.patch
    SHA-256: bdef88093a2b2f00b51c7e3a06533e7e62ef8fb00d2eaa8c52b718a14f8b107b
```

The patch is an opt-in Linux build feature: configure upstream with
`-DSUNSHINE_ENABLE_QEMU_DBUS=ON`, then select it at runtime with
`capture=qemu_dbus` and `encoder=software`.  `0004` additionally enables
single-plane native QEMU DMA-BUF import when `gbm` and `libdrm` development
packages are present; use `-DSUNSHINE_ENABLE_QEMU_DBUS_DMABUF=ON` (the default
for this backend). It still presents a bounded CPU BGRX frame to Sunshine's
software encoder and requires neither X11 nor Wayland. It consumes a per-process
`SUNSHINE_QEMU_DBUS_ADDRESS`; it does not place a VM D-Bus address in Sunshine's
public network configuration.

Check that the complete ordered series still replays before updating the pin.
Use a disposable worktree so the production source tree is left untouched;
Sunshine's required third-party sources are Git submodules and must be
initialized before CMake is run:

```bash
cd /path/to/sunshine-qemu-mvp
PROJECT_ROOT=$PWD
REPLAY=$(mktemp -d)
git clone --recurse-submodules https://github.com/LizardByte/Sunshine.git "$REPLAY"
git -C "$REPLAY" checkout --detach 4f39fc116294abf8241bcd30e1b1e23d371e6e7b
git -C "$REPLAY" submodule update --init --recursive
git -C "$REPLAY" am "$PROJECT_ROOT"/integration/sunshine/patches/000{1,2,3,4,5,6,7}-*.patch

# Then run the strict software/QEMU build gate against "$REPLAY".
SUNSHINE_SOURCE_DIR="$REPLAY" \
  SUNSHINE_BUILD_DIR="$REPLAY-build" \
  "$PROJECT_ROOT"/scripts/build-upstream-sunshine-qemu.sh
```

The build helper is the replay gate: it requires the ordered series to compile
with QEMU D-Bus DMA-BUF and guest-audio-only enabled, verifies that staged
assets match the binary's compiled prefix, rejects direct X11/XTest/Xi/Xext/
Xrender/Xrandr/Wayland ELF dependencies, and rejects those desktop libraries
plus PulseAudio and ALSA in the complete `ldd` closure. Core `libva` and
`libva-drm` remain intentionally present because the pinned static FFmpeg
requires them; the helper requires `libva.so.2` to resolve from the isolated
2.21.0 prefix rather than replacing a system library.

This pin is a source-integration checkpoint, not an upstream Sunshine release
or an endorsement by the Sunshine project.
