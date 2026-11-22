# QSM browser console for ordinary QEMU

The source launcher `scripts/run-qsm-standalone.sh` attaches QSM to a QEMU
guest without Proxmox or libvirt integration. It reuses the shipped QSM
viewer, Display1 media worker, H.264 WebRTC transport, exact UI, keyboard,
mouse, touch controls and display scaling. Run it **on the QEMU host**, as a
user allowed to connect to the guest's private D-Bus. No root service or
guest software is required.

The server attaches to an already running QEMU and never starts, stops or
reconfigures that process. It owns its encoder worker and allows one browser
console at a time. Closing the tab releases input and stops that worker;
reopening attaches to the same running guest.

This is separate from `qsm-pve-direct-standalone`, which installs the PVE
console on stock Proxmox. Do not install PVE packages on a plain QEMU host.
The new launcher is available from source; exp242 binaries/APT are unchanged.

## Build and Python environment

On Debian/Ubuntu, install the native build dependencies (and QEMU if needed):

```sh
sudo apt install g++ cmake ninja-build pkg-config libsystemd-dev libopus-dev \
    libavcodec-dev libavutil-dev libswscale-dev zlib1g-dev ffmpeg \
    python3-venv dbus qemu-system-x86 qemu-system-gui
cd qsm-direct
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --target qsm-direct-media-worker -j4
python3 -m venv .venv-standalone
.venv-standalone/bin/pip install --only-binary=av -r extensions/standalone/requirements.txt
```

The default encoder is CPU `libx264`. GPU capture development libraries are
optional for this path. The worker links the host's FFmpeg; PyAV in the
Python environment only packetizes its encoded output. The pinned aiortc
version matches the API qualified by QSM's bridge.

## Export the guest's screen

Check that `qemu-system-x86_64 -display help` lists `dbus`. The QEMU process
must export `org.qemu.Display1` on a **local Unix D-Bus message bus**, under
the `org.qemu` name. QSM captures console 0. VNC/SPICE addresses and QMP
sockets are not Display1 addresses.

For an existing QEMU launch command, keep its machine, disk, firmware and
device arguments. Run it inside a private `dbus-run-session`, save
`DBUS_SESSION_BUS_ADDRESS` to a private file, and select `-display dbus,gl=off`
instead of its GTK/SDL display backend. An already running QEMU without
the D-Bus display backend normally needs a QEMU process restart to enable it.
Do not add a second virtual graphics device to an existing guest just for QSM.

For a new CPU-display test guest, the repository provides a complete example.
In terminal 1, from `qsm-direct`:

```sh
ACCEL=kvm VM_NAME=guest DISK_IMAGE=/path/to/guest.qcow2 \
    bash scripts/launch-qemu-dbus-cpu-example.sh
```

This example selects a non-GL VirtIO VGA and USB input, creates a private bus,
and writes its address to `/tmp/qmdp-guest/dbus.address`. The script remains
running for the guest's lifetime. Use `ACCEL=tcg` when KVM is unavailable.
Use your guest's own launch command for custom firmware/device configurations.

## Open the browser console

In terminal 2, from `qsm-direct`:

```sh
PYTHON="$PWD/.venv-standalone/bin/python" \
    bash scripts/run-qsm-standalone.sh \
    --dbus-address-file /tmp/qmdp-guest/dbus.address --name guest
```

The launcher finds `build/qsm-direct-media-worker`. Set `BUILD_DIR` or pass
`--worker /absolute/path/to/qsm-direct-media-worker` for another build.
`--dbus-address 'unix:path=…'` can replace the address file.

The server prints its URL, initially `http://127.0.0.1:18008/`, and the path
to a mode-0600 access-token file. Read that file in another terminal and paste
its contents into the browser login form. The token itself is never printed
by the server or included in a URL. It stays in page memory, so reloading
asks for it again. A generated token file is deleted when the server exits.

For a stable token, generate your own private file once:

```sh
mkdir -p "$HOME/.config/qsm"
chmod 700 "$HOME/.config/qsm"
(umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
    > "$HOME/.config/qsm/guest.token")
chmod 600 "$HOME/.config/qsm/guest.token"
```

Then add `--token-file "$HOME/.config/qsm/guest.token"`. The file must be
owned by the server user, inaccessible to other users and not a symlink.
Everyone holding this token can view and control this one guest.

For remote browser access, use TLS and a certificate trusted by that browser:

```sh
PYTHON="$PWD/.venv-standalone/bin/python" \
    bash scripts/run-qsm-standalone.sh \
    --dbus-address-file /tmp/qmdp-guest/dbus.address --name guest \
    --listen 0.0.0.0 --port 18008 \
    --public-url https://qemu.example.net:18008 \
    --tls-cert /path/to/fullchain.pem --tls-key /path/to/privkey.pem \
    --token-file "$HOME/.config/qsm/guest.token"
```

Non-loopback binds require TLS. Wildcard binds also require `--public-url`;
the server accepts only its configured Host/Origin. Proxy-forwarded identity
and PVE credentials are not used. WebRTC media/input use direct UDP, so the
browser must be able to reach the host's ICE candidates as well as the HTTPS
port. This launcher does not configure TURN, a PVE TCP gateway or NAT traversal.

## Encoding and supported controls

`--fps 30 --bitrate 8000` chooses the worker frame rate and video ceiling in
kbit/s. H.264 is used for browser compatibility. To choose hardware encoding,
pass `--encoder h264_vaapi --vaapi-device /dev/dri/renderD128`,
`--encoder h264_qsv`, or `--encoder h264_nvenc`, with the corresponding host
driver and FFmpeg encoder available. An explicit hardware encoder failure
is reported in the server log; the default CPU mode requires no GPU.
For GL Display1 capture, the existing worker also accepts
`--capture-render-node /dev/dri/renderD128`; GL/device compatibility is the
same as in the core worker and is not established by the CPU-mode test below.

The browser keeps QSM's local display/input preferences. Encoder, bitrate
and frame rate are server launch options; the PVE media-policy editor is
hidden. Standard VGA without `SetUIInfo` remains connected at its fixed
guest resolution when the browser is resized.

Playback audio is available when QEMU exports D-Bus audio, for example with
`-display dbus,gl=off,audiodev=dbus0 -audiodev dbus,id=dbus0` and a guest
sound device attached to `dbus0`; the example launch script configures this.
The non-PVE entry does not wire the PVE guest-agent/PCM adapters: file
transfer, agent clipboard and microphone controls are hidden. No in-guest
desktop agent is required for the screen and input.

If the console is busy, close its other browser tab before reconnecting. If
the browser disappears without clean shutdown, the connection watchdog
releases the session after transport failure. A failed connection is also
bounded by a 30-second negotiation lease. Stopping the server with Ctrl-C
releases its worker while leaving the attached QEMU running.

## Verification

The [2026-11-22 validation record](standalone-qemu-validation-20261122.json)
contains the qualified versions, HTTP/HTTPS browser results and remaining
hardware/audio/network coverage limits.

The API tests verify token/Host/Origin enforcement, request bounds and
cleanup after a worker fails to start:

```sh
PYTHONPATH=. .venv-standalone/bin/python -m unittest discover -s tests/standalone -v
```

The browser test runs the **complete shipped viewer** with no ExtJS or PVE.
It boots its own 512-byte BIOS guest in real QEMU under TCG, observes a green
half-screen after a browser keypress and a red half-screen after browser
mouse input, resizes and reconnects, checks the second-tab limit and verifies
worker cleanup without killing QEMU. Screenshots and logs go to a new output
directory, and test processes/credentials are removed afterwards:

```sh
.venv-standalone/bin/pip install playwright pillow
.venv-standalone/bin/python tests/standalone/browser_e2e.py \
    --worker "$PWD/build/qsm-direct-media-worker" \
    --output /tmp/qsm-browser-check \
    --browser /usr/bin/google-chrome
```

Add `--tls` and choose another new output directory to exercise HTTPS with a
throwaway certificate. The test browser alone accepts that certificate;
the console's authentication and WebRTC transport checks stay enabled.
