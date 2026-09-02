# Historical Embedded-Moonlight → Sunshine → VirGL E2E

> **Historical compatibility diagnostic.** This runner uses legacy private
> pairing and Embedded Moonlight. It is retained for codec/input regression
> work only; it is not evidence for the current PIN-free system-auth media
> route. Use the patched-Moonlight Qt composite documented in
> [`QT_DESKTOP_CLIENT.md`](QT_DESKTOP_CLIENT.md#native-qtmoonlightvirgl-e2e-gate),
> with final evidence in `run.qAMtPU`, for current acceptance.

`scripts/run-moonlight-sunshine-virgl-e2e.sh` is the strict native graphics
gate for the complete video and input route:

```text
Moonlight Embedded SDL client (disposable Xvfb only)
  -> private pairing, HTTPS launch, RTSP/RTP
  -> patched Sunshine: capture=qemu_dbus, libx264 software encoder
  -> QEMU Display1 ScanoutDMABUF / UpdateDMABUF
  -> KVM Q35, virtio-vga-gl, dbus,gl=on,rendernode=/dev/dri/renderD128
  -> Alpine DRM/GBM kmscube guest: virtio_gpu + VirGL (NVIDIA)
```

The X server is strictly a short-lived **client test harness** for Moonlight
SDL. The reproducible Sunshine profile rejects X11, Wayland, PulseAudio, and
ALSA across its complete `ldd` closure; Sunshine, QEMU, the Display1 observer,
and the guest therefore need no host desktop or sound-server stack. The QEMU
invocation unsets the client Mesa/llvmpipe variables and rejects
`QEMU_EGL_SURFACELESS_FALLBACK=1`.

The verified deployment artifact is exactly
`$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine`, not the convenience
`build-sunshine-qemu` tree. Its checked `ldd` closure contains none of
`libX11`, `libXtst`, `libXi`, `libXext`, `libXrender`, `libXrandr`, Wayland,
PulseAudio, or ALSA. Xvfb remains an intentional dependency of the Moonlight
client test process only.

## Required build inputs

Build the project Display1 observer with native DMA-BUF readback, for example:

```bash
cmake -S . -B .build-dmabuf -G Ninja -DQMDP_ENABLE_DMABUF_READBACK=ON
cmake --build .build-dmabuf --target qemu-display-probe -j2
```

Build Moonlight Embedded with its real SDL/FFmpeg platform as documented in
[`MOONLIGHT_E2E.md`](MOONLIGHT_E2E.md). Build the pinned Sunshine tree with
this ordered patch series, then produce `build-sunshine-qemu-no-x11`:

1. `0001-platform-linux-add-QEMU-Display1-CPU-capture.patch`
2. `0002-platform-linux-route-QEMU-Display1-input.patch`
3. `0003-platform-linux-add-QEMU-Display1-guest-audio-source.patch`
4. `0004-platform-linux-qemu-dmabuf-egl-readback.patch`
5. `0005-cmake-avoid-X11-FFmpeg-glue-for-software-builds.patch`
6. `0006-platform-linux-add-QEMU-guest-audio-only-build.patch`

Configure it with:

```text
-DSUNSHINE_ENABLE_QEMU_DBUS=ON
-DSUNSHINE_ENABLE_QEMU_DBUS_DMABUF=ON
-DSUNSHINE_ENABLE_QEMU_DBUS_AUDIO_ONLY=ON
-DLIBVIRTUALHID_ENABLE_XTEST=OFF
-DSUNSHINE_ENABLE_X11=OFF
-DSUNSHINE_ENABLE_WAYLAND=OFF
-DSUNSHINE_ENABLE_DRM=OFF
-DSUNSHINE_ENABLE_VAAPI=OFF
-DSUNSHINE_ENABLE_CUDA=OFF
-DSUNSHINE_ENABLE_VULKAN=OFF
```

On Ubuntu 24.04, Sunshine's pinned FFmpeg may require the isolated libva ABI
build; use [`ISOLATED_LIBVA.md`](../integration/sunshine/ISOLATED_LIBVA.md).
Set `CMAKE_INSTALL_PREFIX` to the Sunshine build directory so its compiled
asset path and the copied `assets/apps.json` agree.

## Run

The default is the strict client-fullscreen lane.  Run it and the separate
ordinary-window lane; a native render node and KVM access are mandatory.  The
script starts QEMU under a fresh `sudo -u` process by default so a newly
assigned `kvm` group is visible.

```bash
SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine" \
MOONLIGHT_BINARY="$PWD/.upstream/build-moonlight-embedded/moonlight" \
QEMU_DISPLAY_PROBE="$PWD/.build-dmabuf/qemu-display-probe" \
STREAM_SECONDS=10 \
./scripts/run-moonlight-sunshine-virgl-e2e.sh

SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine" \
MOONLIGHT_BINARY="$PWD/.upstream/build-moonlight-embedded/moonlight" \
QEMU_DISPLAY_PROBE="$PWD/.build-dmabuf/qemu-display-probe" \
MOONLIGHT_WINDOW_MODE=windowed STREAM_SECONDS=10 \
./scripts/run-moonlight-sunshine-virgl-e2e.sh
```

It creates a fresh NoCloud ISO and QCOW2 overlay from the pinned existing
Alpine provisioner input. The derived cloud fixture adds root guest `evtest`
watchers for the VM's virtio keyboard/mouse/tablet devices. It does not need a
guest desktop or network listener.

The successful terminal line gives an ignored evidence directory.  Its most
useful files are `trace.txt`, `sunshine.log`, `moonlight-*.log`, client/root
PNG screenshots and signal statistics, `guest-telemetry.log`, and the
parallel `display1-observer.log` plus H.264 segments.

## Historical deployment evidence

Fresh runs made with the exact no-X binary above passed on 2026-09-01:

- fullscreen: `artifacts/validation/moonlight-sunshine-virgl-e2e/run.vCQmck/trace.txt`;
- windowed: `artifacts/validation/moonlight-sunshine-virgl-e2e/run.gNCula/trace.txt`.

Both traces identify the no-X binary, record their presentation geometry,
non-black decoded-client luma, final QEMU input statistics, guest evdev
input markers, zero-failure DMA-BUF counters, and the `1280x800` to
`1280x720` observed scanout transition.

## Assertions

The gate requires all of the following, not just process startup:

- fresh private Moonlight/Sunshine pairing material and successful HTTPS
  pairing, application listing, and launch;
- RTSP handshake, RTP video start, first video packet, and Moonlight FFmpeg
  H.264 decoder;
- a valid, non-black decoded SDL PNG; the fullscreen lane requires a
  `1280x720` window at root coordinate `(0,0)` on a `1280x720` root, while the
  windowed lane requires that same window inside a `1600x900` root;
- a live-client `a`, pointer move, and left click injected into the Moonlight
  SDL window; the guest requires Linux evdev `KEY_A` press/release, motion
  (ABS or REL), and `BTN_LEFT` press/release;
- guest `virtio_gpu`, a VirGL renderer containing `NVIDIA`, and a real
  `kmscube` scene;
- Sunshine's `Console.SetUIInfo(1280x720)` acceptance, its own observed QEMU
  scanout geometry change, and nonzero/zero-failure DMA-BUF counters emitted
  at shutdown;
- a separate QEMU Display1 observer which sees nonzero DMA-BUF traffic with
  zero failures and encodes both the pre-request `1280x800` and resulting
  `1280x720` H.264 guest modes.

The parallel observer is a QEMU-supported second listener, not a replacement
for Sunshine: Sunshine's independent DMA-BUF import banner, counters, and
Moonlight stream are all required too.

## CPU-readback and QSF boundary

The video path is deliberately **not** advertised as zero-copy GPU encoding:
QEMU publishes a ScanoutDMABUF, Sunshine imports it through headless EGL,
reads it back to CPU BGRX memory, and `libx264` software-encodes that CPU
frame for GameStream. The guest's `virgl (NVIDIA ...)` renderer proves the
guest VirGL workload, not an NVIDIA host encoder in Sunshine.

Stock Moonlight/GameStream has no interoperable bidirectional clipboard or
file-transfer protocol. Those claims are exclusively owned by the
authenticated QSF companion and guest-agent gate; this video/input runner
neither sends clipboard/file data nor treats the CPU readback as a QSF data
path. The combined QSF evidence is documented separately in
[`MOONLIGHT_SUNSHINE_VIRGL_QSF_WAYLAND_HOOK.md`](MOONLIGHT_SUNSHINE_VIRGL_QSF_WAYLAND_HOOK.md).
