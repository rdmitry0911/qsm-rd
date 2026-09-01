# QEMU headless VirGL route

The normal production route needs no X11, Wayland, GTK, or display server on
the q-sunshine host:

```text
virtio-vga-gl -> EGL/GBM render node -> QEMU Display1 D-Bus -> Sunshine
```

With a render node exposed to the LXC, use the distribution QEMU directly:

```bash
dbus-run-session -- qemu-system-x86_64 \
  -machine accel=kvm -device virtio-vga-gl \
  -display dbus,gl=on,rendernode=/dev/dri/renderD128
```

`dbus-run-session` provides a private D-Bus session; it does not start X11.
The pinned Alpine guest runner selects this route when `/dev/dri/renderD128`
is usable.

## Explicit fallback for restricted environments

`patches/0001-ui-add-opt-in-surfaceless-inline-dbus-virgl-fallback.patch` is
based exactly on QEMU `v8.2.2`
(`11aa0b1ff115b86160c4d37e7c37e6a6b13b77ea`).  It adds an opt-in CPU-readback
fallback for a host that has no usable DRM render node.  It is intended for
CI and constrained LXC environments, not as a replacement for the normal
GPU route.  The GL implementation is normally Mesa llvmpipe and output is
ordinary inline Display1 updates, not DMABUF.

Build it without installing over the system package:

```bash
./scripts/build-qemu-dbus-virgl.sh
```

The Ubuntu build dependencies are `build-essential ninja-build pkg-config`,
`libglib2.0-dev libpixman-1-dev libepoxy-dev libvirglrenderer-dev`,
`libgbm-dev libdrm-dev`, and the development packages requested by QEMU's
configure diagnostics.  They are build-time dependencies only.

Use the resulting binary only when the fallback is explicitly wanted:

```bash
QEMU_EGL_SURFACELESS_FALLBACK=1 \
__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json \
LIBGL_ALWAYS_SOFTWARE=1 \
dbus-run-session -- .upstream/build-qemu-dbus-virgl/qemu-system-x86_64 \
  -machine accel=kvm -device virtio-vga-gl -display dbus,gl=on
```

The fallback remains disabled unless the environment variable is exactly `1`.
It has no X11/Wayland runtime dependency.
