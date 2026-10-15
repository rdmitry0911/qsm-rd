Tablet cursor/selection qualification, 2026-10-15

QSM and matching host LXC packages: 0.4.0+shared10. Proxmox platform
packages are unchanged from shared9. The runtime change is browser JS;
C++ and guest payload implementation are unchanged.

Behavior checks:
node qsm-direct/tests/proxmox_pve9/test_touch_gestures.mjs
node qsm-direct/tests/proxmox_pve9/test_direct_console_overlay.mjs

These execute the actual gesture recognizer, including old saved binding
migration, custom bindings, small and slow cursor motion, tap, hold-drag,
hold-lift, cancellation, lost capture, blur/visibility and two-finger
scrolling. Both pass.

Chromium 145 with actual CDP touch events and touch/mobile browser context:
Vilnius VM103 and pve-asrock VM100, native authenticated PVE HTTPS8006.
Live video: VM103 1024x768; VM100 1280x800 in the CPU display test,
both readyState 4. Saved legacy default mapping seeded
in an isolated browser profile. Real RTCDataChannel.send is observed,
not replaced with a fake connection or input sink. Checks pass:
- A slide sends pointer positions and no mouse button.
- Hold 650ms then drag sends exactly left down/up.
- Hold and lift sends exactly right down/up, with no early click.
- Cancelling a selection releases the left button.

Runner: chromium-touch.py. It obtains a temporary PVE ticket over root
SSH in memory and never logs credentials. It sends real pointer gestures
at the upper left of the guest display; use a suitable test desktop.
Example (Playwright installed, use its compatible Chromium executable):
python chromium-touch.py --host 192.168.64.20 --node Vilnius --vmid 103 \
  --chromium /path/to/chromium

Shared10 installed on both nodes without restarting any VM or container.
On pve-asrock, VM100 initially failed the virtio-gl host-library check.
The host was missing libegl1 and libegl-mesa0; these were installed.
Its amdgpu driver was blacklisted and there was no DRM render device.
Explicitly loading amdgpu bound the free Raphael iGPU at 0000:11:00.0
and created renderD128. The RX6600 at 0000:03:00.0 stayed with vfio-pci
and running VM103. VM100 then started successfully with virtio-gl.
The initial GL connection passed the touch checks, but the iGPU then hung
in gfx_0.0.0 while executing QEMU rendering. At 11:22:23 the kernel reset
it; QEMU lost its graphics context and the VM exited. This configuration
is therefore not qualified for stable AMD GL operation.

For a working desktop, VM100 was subsequently switched to plain virtio
and H.264 software encoding, with the previous configuration and media
policy saved privately on the node. The temporary iGPU boot-load service
was removed after finding the instability; the original driver blacklist
is retained. No host reboot, kernel upgrade or VFIO rebinding was done.
Firefox 155 connected to this CPU display through the real native HTTPS
API and reconnected successfully after closing its peer connection;
video resumed with state connected. See firefox-asrock100.txt.

Physical tablet hardware/browser testing remains for the user. These
checks qualify the browser event path and real transport, not every
mobile browser's gesture dispatch.
