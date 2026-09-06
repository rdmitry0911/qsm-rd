# QSM Direct: stress plan for the PVE browser console

This plan qualifies the PVE 9 Display editor and the browser-native console
as one lifecycle. A successful API save is not sufficient: PVE must launch
the selected private Display1 backend, the guest must receive input, and the
browser must retire its popup when that backend disappears.

## Configuration matrix

For the **VirGL GPU (GL)** profile, repeat eight transitions for every stock
PVE 9 Graphic card value (`std`, `vmware`, `qxl`, `qxl2`, `qxl3`, `qxl4`,
`virtio`, `virtio-gl`, `serial0` through `serial3`, and `none`):

1. choose the stock value and save it;
2. enable QSM Display1 with a valid render node;
3. reopen Hardware → Display and verify the QSM checkbox and render node;
4. disable QSM Display1 and verify the selected stock value is restored.

While enabled, `vga: none` is the expected stored value.  PVE otherwise adds
VNC alongside the GL Display1 backend, which QEMU rejects.  Each enabled
configuration must contain exactly one owned `virtio-vga-gl` and one private
`-display dbus` argument.  The UI must migrate the historical unlabelled
owned GPU in place, and reject a foreign GPU, duplicate owned GPU, or another
QEMU display rather than silently modifying it.

For the **CPU — Standard VGA or VirtIO (no GL)** profile, repeat the same
enable/reopen/disable sequence for `std` and `virtio` only. Each enabled
configuration must preserve its selected `vga` value, contain exactly one
private `-display dbus,...,gl=off` argument, and contain no `virtio-vga-gl`
argument or render node. QEMU Standard VGA is expected to keep a fixed
scanout because it does not implement `Console.SetUIInfo`: Chrome video and
input must remain live rather than the worker exiting. Non-GL VirtIO must
additionally pass window → fullscreen → window geometry transitions.

## Browser and lifecycle matrix

For a running VirGL guest and non-GL VirtIO guest, exercise each item in both
a normal resizable popup and browser full screen. For Standard VGA, retain the
same lifecycle/input checks at its fixed guest mode:

1. first VM-page render enables the Console menu without a reload;
2. popup blocked by the browser produces an actionable message;
3. resizing the popup sends one trailing, even Display1 size after 150 ms;
4. pointer motion uses an unordered non-retransmitted latest-state channel;
   clicks, keys and resize remain on the ordered reliable channel;
5. H.264 is low-delay with no B-frames and a maximum 30-frame IDR interval;
6. VM shutdown exits the media worker, closes the server WebRTC peer and
   closes the popup from track, connection, or data-channel termination;
7. a VM restart can create a fresh console without restarting the terminal
   service; terminal-package restart while the VM is live preserves its D-Bus
   bus.
8. drag a local file onto the guest image and require it in the guest agent's
   `incoming` exchange directory; then place a guest file in `outgoing`,
   refresh **Files**, and require a byte-exact browser download. Chromium
   host drag-out is exercised where available; an explicit browser download
   is the portable fallback because browser sandboxing cannot write an
   arbitrary host path without user consent.

## Automated gates and acceptance

* `tests/proxmox_pve9/test_direct_console_overlay.mjs` covers PVE 9 display
  serialization, legacy migration, unsafe arguments, popup/full-screen UI,
  resize debounce and pointer transport shape.
* `tests/proxmox_pve9/test_direct_terminal_autoprovision.py` covers saved
  Display1 bus provisioning and prompt cleanup of an exited worker.
* `tests/dbus_integration_tests.cpp` closes a live QEMU peer and requires
  `DesktopSession::display_failed()` before teardown.
* `lab/proxmox9/qualify-qsm-direct-worker-e2e.py` runs ordinary Chrome,
  H.264/Opus WebRTC and both ordered input plus `qsm-pointer`. It samples the
  decoded video canvas and rejects an all-black/flat frame: SDP connection,
  intrinsic video dimensions and advancing media time alone are insufficient
  evidence of a usable guest image.
* The PVE 9 lab gate must additionally prove a real Chrome session at the
  selected resolution, then `qm stop` leaves no media worker or session
  directory and a following `qm start` succeeds.

A release is accepted only when every relevant static, unit, D-Bus and lab
gate passes.  Clipboard and file transfer are a separate guest-agent feature:
they must be tested in both directions, with empty/non-ASCII clipboard text,
2 MiB boundary files, exchange-folder manifests, rejected path traversal, and
guest shutdown during a transfer.
