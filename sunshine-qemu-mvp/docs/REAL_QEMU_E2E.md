# Real-QEMU CPU E2E

## Scope

`qmdp_real_qemu_e2e` is the qualification lane that replaces the former
``QEMU binary not installed`` gap with a real `qemu-system-x86_64` process.
It runs entirely under TCG and needs neither `/dev/kvm`, a GPU, a guest ISO,
nor a downloaded disk image.

The test compiles `tests/fixtures/qmdp_vga_smoke.S` into an exact 512-byte
BIOS boot sector and inserts it into a 1.44-MiB floppy image.  The guest keeps
repainting VGA memory.  The harness then starts a private session D-Bus,
launches QEMU with `-display dbus,gl=off`, and attaches the production
`qemu-display-probe` through `Console.RegisterListener`.

```text
TCG/SeaBIOS boot fixture
  -> QEMU Display1 on private Unix D-Bus
  -> RegisterListener with a passed peer-socket FD
  -> real inline Scanout CPU frames
  -> one-slot mailbox
  -> FFmpeg/libx264 H.264 segment
  -> ffprobe + retained trace
```

The probe also sends a keyboard press/release, one capability-selected mouse
motion call, and a mouse button press/release.  It runs twice: once with the
default PS/2 relative pointer, and once with `i8042=off` plus a USB tablet so
that QEMU reports `Mouse.IsAbsolute=true` even before a guest OS driver is
loaded.  This proves that the client reads the capability and never sends the
invalid opposite movement method.  Standard VGA does not implement the
optional `Console.SetUIInfo`, and the BIOS fixture does not generate audio;
neither is claimed by this gate.

## Run

On Debian or Ubuntu:

```bash
./scripts/install-qemu-debian.sh
./scripts/run-real-qemu-selftest.sh
```

The second command configures a clean build, runs the CTest target, and copies
the evidence to `artifacts/validation/real-qemu/`.  A normal CMake build also
registers `qmdp_real_qemu_e2e` automatically when `qemu-system-x86_64`,
`dbus-run-session`, `as`, `ld`, and `ffprobe` are available.

```bash
ctest --test-dir build --output-on-failure \
  -R '^qmdp_real_qemu(_absolute)?_e2e$'
```

## Evidence and limits

The generated directory contains:

```text
environment.txt            host/QEMU capabilities and forced-TCG record
ctest.log                  CTest invocation result
trace.txt                  compact end-to-end trace and ffprobe result
probe.log / qemu.log       raw process output
qmdp_vga_smoke.bin         512-byte boot sector (signature 55 aa)
qmdp-vga-smoke.img         reproducible 1.44-MiB floppy
encoded/qemu-probe-*.mkv   real-QEMU-derived H.264 evidence
ffprobe.txt                codec, geometry, pixel format and duration
```

QEMU 8.2 on Linux selects the inline `Scanout`/`Update` lane.  During a mode
transition it may send a damage update for the old geometry immediately before
the new `Scanout`; the listener acknowledges and counts that stale update,
then waits for the authoritative replacement scanout.  Its Unix
`Listener.Unix.Map` support is not available there, so shared-map remains
covered by the protocol-faithful fake-QEMU E2E.  Test it against QEMU 9.2 or
newer before claiming a real Unix-map qualification.

This is a transport/capture/input/H.264 qualification gate.  It does **not**
prove Sunshine/Moonlight networking, QEMU audio output, guest resize support,
reconnect after QEMU replacement, DMA-BUF, or hardware encoding.
