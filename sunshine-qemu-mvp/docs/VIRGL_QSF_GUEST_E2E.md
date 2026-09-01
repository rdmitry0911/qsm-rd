# Native VirGL guest + QSF companion E2E

`scripts/run-virgl-qsf-guest-e2e.sh` qualifies the guest-side data and display
paths together, without involving Sunshine or a Moonlight client:

```text
authenticated local QSF client
  -> 0600 token-protected qsf-control socket
  -> QEMU virtio-serial org.qsunshine.agent
  -> verified static qsf-guest-agent in Alpine

Alpine virtio-gpu / VirGL
  -> QEMU Display1 DMABUF
  -> GBM or headless-EGL CPU readback -> H.264 probe
```

It uses KVM, `virtio-vga-gl`, `-display dbus,gl=on,rendernode=/dev/dri/renderD128`,
a private D-Bus session, and no X11, Wayland, GTK, Xvfb, SSH, or host desktop
session.

## Run

The native render node, KVM access, the pinned Alpine 3.20.10 cloud image, and
the DMA-BUF-capable display probe are required. On this host the probe was
built in `.build-dmabuf` with `QMDP_ENABLE_DMABUF_READBACK=ON`.

```bash
./scripts/provision-alpine-virgl-guest.sh
cmake --build .build-dmabuf --target qemu-display-probe
QEMU_DISPLAY_PROBE="$PWD/.build-dmabuf/qemu-display-probe" \
VIRGL_QEMU_RUN_AS=dima VIRGL_QEMU_USE_SUDO=1 \
./scripts/run-virgl-qsf-guest-e2e.sh
```

Each run creates an ignored, mode-0700 directory below
`vm/alpine-virgl-3.20.10/qsf-e2e/run.*/`. Do not publish a live run directory:
it contains the ephemeral local QSF token. The cloud guest installs Mesa and
`kmscube` from Alpine during first boot, so it needs outbound access to the
Alpine package mirror once.

## Deterministic static-agent delivery

Cloud-init sees exactly one NoCloud `cidata` CD. The agent and fixed QSF test
payload are placed on a separate **read-only virtio block device**, not a
second CD, so the cloud datasource cannot be confused by another optical
volume.

For every run the host builds `guest/qsf_guest_agent.c` with `-static`, creates
a SHA-256 manifest for the agent and all QSF fixtures, and materializes the
manifest hash plus agent hash into the NoCloud bootstrap. Before copying or
executing the agent, the guest verifies the manifest hash, verifies every
payload member, verifies the agent hash, then installs the agent at mode 0700.
The guest telemetry records both hashes and the installed mode.

## Recorded native KVM result

`vm/alpine-virgl-3.20.10/qsf-e2e/run.MCTule/trace.txt` passed on 2026-09-01:

| Layer | Evidence |
| --- | --- |
| static delivery | manifest `4471538e…df7cb7f`, agent `8445d42d…d8f983`, guest mode `0700` |
| guest GPU | `guest_drm_driver=virtio_gpu`; `guest_gl_renderer=virgl (NVIDIA GeForce RTX 3080/PCIe/SSE2)`; `kmscube_status=0` |
| QSF clipboard client → guest | guest SHA-256 `7a86b46c203c32ccbc5234b82bd7d05c404e9c4910a36093a3b9c788aecd6548` |
| QSF clipboard guest → client | exact returned bytes; guest SHA-256 `af2fd4676d16c4ffbcbc946d461ec21f16820dd948fdab1cc84cb715013dcfc6` |
| QSF file client → guest | guest SHA-256 `c42a9b7de80bc704a517486501b41e44a589700f13ea09b4775cc7c90de6a94e` |
| QSF file guest → client | exact returned bytes; guest SHA-256 `7ad50987bb70f63abecaf196fd72e62e49a886f8136289549f80f95b5ef980cc` |
| QSF resize | guest state `1280x720`, QEMU `Console.SetUIInfo` reply `applied` |
| real screen transition | H.264 segments changed from 1280x800 to 1280x720; Display1 DMA-BUF 12/137/0 scanouts/updates/failures, 149/147/2 published/encoded/dropped frames, zero session errors |

The runner checks all of these independently, including the 0600 control
socket and token modes, the guest's own filesystem-derived operation markers,
the non-llvmpipe VirGL renderer, zero DMA-BUF readback failures, and the two
encoded geometries.

## Scope boundary

The clipboard evidence is **QSF guest-agent clipboard state**, not a desktop
toolkit clipboard. `qsf-guest-agent` intentionally has no X11, Wayland, GTK,
Qt, or compositor dependency; it stores constrained UTF-8 bytes in its private
guest state and a future guest desktop adapter may explicitly bridge that state
to a native clipboard. The files are likewise constrained agent payloads, not
a general guest filesystem mount.

This gate proves that a QSF `resize` reaches both guest agent state and QEMU's
Display1 `SetUIInfo`, with the same native VirGL guest visibly switching
scanout geometry. It does not assert that GameStream carries clipboard or
file-transfer packets, that Moonlight fullscreen is negotiated by QSF, or that
a desktop GUI clipboard bridge exists. Those are separate client/desktop
integration layers.
