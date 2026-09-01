# Headless VirGL guest qualification

`scripts/run-virgl-guest-e2e.sh` is the reproducible guest-side gate for the
headless GPU path.  It deliberately does not start X11, Wayland, GTK, Xvfb, or
a guest desktop session.

```text
Alpine guest GL / DRM
        |  virtio-gpu + VirGL
        v
QEMU EGL render node, virtio-vga-gl
        |  QEMU Display1 D-Bus (DMABUF on the native route)
        v
q-sunshine GBM readback or headless EGL import -> CPU/encoder consumer
```

The runner makes a fresh copy-on-write disk for every run, uses a private
session D-Bus, and has two independent guest observability paths:

- `/dev/ttyS0` is retained as `guest-serial.log` for boot and Mesa output.
- A virtio-serial port named `org.qsunshine.virgl.telemetry` writes concise,
  host-readable qualification markers to `guest-telemetry.log`.

The latter is intentionally not a clipboard or file-transfer channel.  Those
features belong to the authenticated QSF guest-agent lane.

## Run the native route

Install the normal host tools (`qemu-system-x86`, `qemu-utils`,
`cloud-image-utils`, `dbus`, FFmpeg and the project-built
`qemu-display-probe`).  A usable `/dev/dri/renderD*` and `/dev/kvm` are needed
for the default KVM run.  The QEMU user must have access to both; after adding
a user to `kvm`, a fresh `sudo -n -u "$USER"` process sees the new group even
if the calling shell does not.

```bash
./scripts/provision-alpine-virgl-guest.sh
cmake --build build-runtime --target qemu-display-probe
./scripts/run-virgl-guest-e2e.sh
```

The provisioner pins Alpine 3.20.10's official BIOS/cloud-init image and its
SHA-512:

```text
dbf008c5910e22d2c9c2268ea9ce2dfef8b12e6f5e303c7515bdc1c4540d1b01
cc7452fd351910b349162af6364f183af41ff01acdc2f6cb38a3652ee7f7e56e
```

On first boot the NoCloud script installs the small guest-side set of Mesa
tools (`mesa-dri-gallium`, `mesa-egl`, `mesa-utils`, `kmscube`), so outbound
access to the Alpine package mirror is needed once.  It then requires a bound
`virtio_gpu` device, a VirGL OpenGL renderer, and a real KMS/GBM `kmscube`
scene.  The final runner also requires a nonzero Display1 capture and an H.264
segment verified by `ffprobe`.

The command prints an ignored evidence directory under
`vm/alpine-virgl-3.20.10/e2e/run.*/`.  Its useful files are:

- `trace.txt` — compact, final assertion trace;
- `guest-telemetry.log` and `guest-serial.log` — guest-side proof;
- `probe.log` and `encoded/*.mkv` — actual Display1 traffic and encoded video;
- `qemu.log` — QEMU-side diagnostics, including a missing-DMABUF-ingest
  diagnosis if the probe was built without it.

The native QEMU invocation is intentionally explicit about the render node:

```text
-machine q35,accel=kvm
-vga none -device virtio-vga-gl
-display dbus,gl=on,rendernode=/dev/dri/renderD128
```

It also uses `-boot order=c`; an early keyboard event can cancel the Alpine
Syslinux countdown, so input tests should wait for
`QMDP_VIRGL_GUEST_READY`.

## What the markers prove

The fixture emits these in order:

```text
QMDP_VIRGL_GUEST_E2E
guest_drm_driver=virtio_gpu
guest_gl_renderer=virgl (...)
QMDP_VIRGL_GUEST_READY
kmscube_status=0 or 124
QMDP_VIRGL_GUEST_E2E_OK
```

`READY` is after the guest DRM and renderer checks, and six seconds before the
KMS scene.  This gives the host enough time to attach a Display1 listener;
`kmscube` itself is not treated as a process-lifetime signal because the
tested Alpine build may cleanly return after presenting its scene.

On the recorded native KVM run in this environment, the relevant telemetry
was:

```text
guest_drm_driver=virtio_gpu
guest_gl_renderer=virgl (NVIDIA GeForce RTX 3080/PCIe/SSE2)
QMDP_VIRGL_GUEST_E2E_OK
```

This distinguishes the actual guest VirGL renderer from a guest-only
`llvmpipe` fallback.  The default runner rejects `llvmpipe` for its native
render-node lane.

Two retained native KVM runs from 2026-09-01 demonstrate the complete route:

| Evidence | Result |
| --- | --- |
| `vm/alpine-virgl-3.20.10/e2e/run.AoYkyx/trace.txt` | `virgl (NVIDIA GeForce RTX 3080/PCIe/SSE2)`, 40/39/1 published/encoded/dropped frames, 7/33/0 DMA-BUF scanouts/updates/failures, H.264 1280x800 |
| `vm/alpine-virgl-3.20.10/e2e/run.sik9tk/trace.txt` | the same native renderer plus a real `Console.SetUIInfo(1280x720)` transition from H.264 1280x800 to 1280x720 |

## Display1 DMABUF boundary

With `dbus,gl=on`, stock QEMU's normal graphics route delivers
`ScanoutDMABUF`/`UpdateDMABUF` to the Display1 listener.  That is expected and
is evidence that QEMU is using the GL route rather than an inline CPU display.
The adapter imports the single-plane buffer with GBM and copies it to the
bounded CPU framebuffer.  Some NVIDIA GBM allocations import successfully but
return `EAGAIN` when mapped; for that case the same render node has a headless
EGL `EGL_EXT_image_dma_buf_import` readback path which normalizes into RGBA
before the CPU encoder.  Neither branch creates an X11, Wayland, GTK, or
desktop surface.

Use a build with `QMDP_ENABLE_DMABUF_READBACK=ON`; the passing evidence above
used `.build-dmabuf/qemu-display-probe`.  The runner deliberately fails rather
than claiming a capture if it sees zero video frames, a nonzero DMA-BUF failure
count, or the earlier `DMA-BUF capture is not enabled in the CPU-first MVP`
diagnostic.

An explicit development-only software fallback exists separately in
[`integration/qemu/README.md`](../integration/qemu/README.md).  It is selected
only with `QEMU_EGL_SURFACELESS_FALLBACK=1` and the patched QEMU binary, for
example:

```bash
QEMU_BINARY="$PWD/.upstream/qemu-8.2/build-qsf/qemu-system-x86_64" \
QEMU_EGL_SURFACELESS_FALLBACK=1 \
./scripts/run-virgl-guest-e2e.sh
```

That lane remains headless but normally uses Mesa software rendering and
ordinary inline Display1 updates.  It is useful for restricted CI, not a
substitute for the native render-node/NVIDIA-VirGL proof above.

## Optional mode request and scope

`VIRGL_REQUEST_SIZE=1280x720` passes a `Console.SetUIInfo` request through the
Display1 probe.  The second retained native run above proves the effect on the
guest's actual VirGL scanout: FFmpeg recorded one 1280x800 H.264 segment, then
a 1280x720 segment after the request, with 12/34/0 DMA-BUF
scanouts/updates/failures and zero session errors.  A single stale damage
update was safely discarded during that geometry transition.

The QSF guest-agent E2E lane still owns bidirectional clipboard/file hashes.
Moonlight fullscreen remains a client-presentation property; it is not
represented as a VirGL renderer claim.

Useful overrides are `VIRGL_ACCEL=tcg` for a slow diagnostic run,
`VIRGL_QEMU_RUN_AS=<user>`, `VIRGL_QEMU_USE_SUDO=0` for an already refreshed
group session, `VIRGL_RENDER_NODE=<path>`, and `VIRGL_OUTPUT_DIR=<directory>`.
