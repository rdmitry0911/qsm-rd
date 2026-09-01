# Build and run guide

## Packages

Typical Debian/Ubuntu development packages:

```bash
sudo apt install build-essential cmake ninja-build dbus-daemon ffmpeg
```

For the self-contained real-QEMU CPU lane, use the project installer. It
selects the small QEMU module package that provides `ui-dbus.so` and
`audio-dbus.so` instead of requiring a desktop GUI:

```bash
./scripts/install-qemu-debian.sh
```

The standalone code uses the stable sd-bus ABI from `libsystemd.so.0`. It ships
a small declaration-only compatibility header so a minimal test machine does
not require all of `libsystemd-dev`. An in-tree Sunshine patch should use the
headers and dependency policy already selected by Sunshine.

## Complete no-GPU validation

```bash
./scripts/run-no-gpu-selftest.sh
```

Sanitizer matrix:

```bash
./scripts/run-sanitizers.sh
```

Optional environment variables:

```text
BUILD_DIR=/path/to/build
OUTPUT_DIR=/path/to/results
BUILD_TYPE=Debug|RelWithDebInfo|Release
JOBS=2
CXX=clang++|g++
```

## Useful individual programs

```bash
./build-no-gpu/qmdp_core_tests
./build-no-gpu/qmdp_dbus_integration_tests
./build-no-gpu/qmdp_dbus_selftest --output /tmp/qmdp-encoded
./build-no-gpu/qmdp-fake-qemu --help
./build-no-gpu/qemu-display-probe --help
```

## Real QEMU CPU path

### Self-contained TCG qualification

```bash
./scripts/run-real-qemu-selftest.sh
```

This creates a 512-byte BIOS boot sector and 1.44-MiB floppy image, launches
standard VGA under TCG, and proves real inline Display1 capture, relative
input, and software H.264 encoding. The complete trace is retained in
`artifacts/validation/real-qemu/`; see `REAL_QEMU_E2E.md` for exclusions.

### Attach to a supplied guest

Terminal 1:

```bash
DISK_IMAGE=/var/lib/libvirt/images/test.qcow2 \
ACCEL=kvm \
./scripts/launch-qemu-dbus-cpu-example.sh
```

Terminal 2:

```bash
ENCODE=1 DURATION_MS=10000 REQUEST_SIZE=1280x720 \
./scripts/run-qemu-display-probe.sh \
  /tmp/qmdp-qmdp-cpu-demo/dbus.address \
  /tmp/qmdp-probe-output
```

Expected outputs:

```text
/tmp/qmdp-probe-output/final.ppm
/tmp/qmdp-probe-output/encoded/qmdp-probe-000.mkv
/tmp/qmdp-probe-output/probe.log
```

`SetUIInfo` is optional. `run-qemu-display-probe.sh` does not send it unless
`REQUEST_SIZE` is set. A guest/device that does not support it may reject the
call; omit the variable in that case and capture will continue at the current
mode.

## Patched upstream Sunshine gate

The portable CPU display patch is pinned rather than vendored. Apply it to the
exact Sunshine revision, build with `SUNSHINE_ENABLE_QEMU_DBUS=ON`, and run:

```bash
SUNSHINE_BINARY=/path/to/patched/sunshine \
  ./scripts/run-upstream-sunshine-qemu-e2e.sh
```

The runner requires `capture=qemu_dbus`, `encoder=software`, QEMU `gl=off`,
and a local `assets/apps.json` beside the test binary. It records a real-QEMU
TCG → Sunshine Display1 → libx264 encoder-probe trace, not a Moonlight session.
Full pin/build details are in `SUNSHINE_QEMU_INTEGRATION.md`.

## Debugging D-Bus

The probe accepts either a message-bus address plus destination or an already
connected peer FD:

```text
--dbus-address ADDRESS --destination org.qemu
--dbus-address-file FILE --destination org.qemu
--fd N
```

Use `--no-audio` when the QEMU instance was launched without a D-Bus audio
backend. Use `--require-audio` to make absent audio fatal.
