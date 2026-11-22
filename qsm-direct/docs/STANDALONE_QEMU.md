# QSM browser console for ordinary QEMU

QSM can attach to QEMU guests outside Proxmox. Open the web page, sign in
with your **QEMU host username and password**, and choose a running guest.
The list contains only that Unix user's QEMU processes with Display1 enabled,
including guests whose screen is a qdev-host device (for example an
accelerated GPU).
The browser receives a session automatically; no token copying is needed.

The same QSM viewer, H.264 WebRTC media worker, exact UI, keyboard, mouse,
touch controls and scaling are used. QSM runs on the QEMU host and attaches
to existing guests. It never starts, stops or reconfigures their QEMU processes.
One browser console per guest is allowed; different guests can be used at once.

This source launcher is separate from `qsm-pve-direct-standalone`, which
installs the PVE console on stock Proxmox. Do not install PVE packages on a
plain QEMU host. The exp242 binaries/APT do not contain this source addition.

## Build and install

On Debian/Ubuntu:

```sh
sudo apt install g++ cmake ninja-build pkg-config libsystemd-dev libopus-dev \
    libavcodec-dev libavutil-dev libswscale-dev zlib1g-dev ffmpeg \
    python3-venv libpam0g libpam-modules dbus qemu-system-x86 qemu-system-gui
cd qsm-direct
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --target qsm-direct-media-worker -j4
sudo bash scripts/install-qsm-standalone.sh
```

The installer puts a root-owned, user-readable runtime in
`/opt/qsm-standalone`, creates its Python environment and installs the
`qsm-standalone` PAM policy. Pass another absolute prefix as its first
argument, or set `BUILD_DIR` for another native build. An existing PAM policy
is preserved. No existing host account or password is changed.

The default encoder is CPU `libx264`; GPU capture libraries are optional.
The native worker links the host's FFmpeg. Python/PyAV only packetize its
encoded output. The Python requirements pin the aiortc API qualified by QSM.

## Export each guest's screen

`qemu-system-x86_64 -display help` must list `dbus`. Each guest must expose
Display1 on a **local Unix D-Bus message bus**, under the `org.qemu` name.
Use a separate private bus per guest. QSM captures console 0. A VNC/SPICE
address or QMP socket is not a Display1 address.

Keep the guest's own machine, disk, firmware and device arguments. Select
`-display dbus,gl=off` instead of its GTK/SDL display backend and run QEMU in
a private `dbus-run-session`. Either pass the bus with
`-display dbus,addr=unix:path=…,gl=off` or inherit `DBUS_SESSION_BUS_ADDRESS`.
Discovery reads these settings from the owner's QEMU process. A QEMU started
without the D-Bus backend normally needs a process restart to enable it.
Do not add another graphics device just for QSM.

For a new CPU-display test guest, the repository's example creates the
private bus, non-GL VirtIO VGA, USB input and D-Bus audio:

```sh
ACCEL=kvm VM_NAME=guest DISK_IMAGE=/path/to/guest.qcow2 \
    bash scripts/launch-qemu-dbus-cpu-example.sh
```

Run it as the user who will log in to QSM. Use `ACCEL=tcg` without KVM.
For guests with custom firmware/devices, use their own launch command.

## Guests whose screen is a qdev-host device

A VM whose display is an out-of-process [qdev-host](https://github.com/rdmitry0911/qdev-host)
vfio-user device, for example an accelerated paravirtual GPU, has no QEMU
console to capture: QEMU runs with `-vga none -display none` and the
accelerated picture exists only in qdev-host. qdev-host serves the same
`org.qemu.Display1` interface itself. Such a guest is listed after login next
to ordinary QEMU guests, marked **qdev-host display**, and opens the same
console. Its keyboard and mouse are relayed by qdev-host to the VM.

Run, as the user who logs in to QSM, one private bus per device, qdev-host
on that bus, and give QEMU a QMP socket that only qdev-host uses for input:

```sh
dbus-daemon --session --nofork --address=unix:path=$RUN/display1.bus &
qdev-host --plugin … --socket $RUN/gpu.sock \
    --display-bus unix:path=$RUN/display1.bus --vm-name guest \
    --qemu-qmp $RUN/qmp-input.sock &
qemu-system-x86_64 -name guest … -vga none -display none \
    -device '{"driver":"vfio-user-pci","id":"gpu","socket":{"type":"unix","path":"'$RUN'/gpu.sock"}}' \
    -qmp unix:$RUN/qmp-input.sock,server=on,wait=off
```

`--display-name` may name another bus name than `org.qemu`; a qdev-host with
only `--display-p2p` is not listed. QEMU and qdev-host may be started through
a bundled dynamic loader (`ld-linux-x86-64.so.2 --library-path … PROGRAM`).

How a device display is paired with its guest (design): a qdev-host process
and a QEMU process are paired only when

1. both belong to the logged-in user (all four `Uid:` values in `/proc`);
2. qdev-host declares exactly one `--display-bus` (a local `unix:` address)
   and one absolute `--socket`;
3. the kernel's AF_UNIX table (`sock_diag`, read per request) shows that
   qdev-host holds an accepted connection on that socket path whose peer
   socket is held by that QEMU, the vfio-user connection itself, so a
   hot-plugged device pairs too and a name or path declared on a command line
   never pairs processes on its own;
4. when a console is opened, the bus name's unique owner reports qdev-host's
   PID and the user's UID, and both processes and their connection are read
   again unchanged (same PIDs and start times).

The guest entry is identified by both processes' PIDs and start times, so a
restarted qdev-host or QEMU is a new entry. A device connected to several
QEMU processes, or a kernel without `sock_diag`, lists no device display;
ordinary QEMU guests are unaffected. Processes that are not dumpable (for
example started with file capabilities) cannot be inspected and are not
listed.

## Start the host login page

For local testing:

```sh
sudo env PYTHONPATH=/opt/qsm-standalone \
    /opt/qsm-standalone/.venv/bin/python -m extensions.standalone.server \
    --worker /opt/qsm-standalone/bin/qsm-direct-media-worker
```

Open `http://127.0.0.1:18008/`. Sign in with an existing host account,
choose **Open console** next to a guest, and use **Guests** in the console
controls to return to the list. Refreshing the page keeps the login.
**Sign out** revokes the web session and closes its consoles, leaving QEMU
running. The session expires after eight hours; `--session-hours 1..24`
sets another absolute lifetime.

Running the entry service as root allows host-wide authentication. It uses
PAM `common-auth` and `common-account`, including locked/expired-account
policy. Each media agent and encoder is started with the authenticated
owner's UID, primary group and supplementary groups. The service checks the
QEMU PID, process start time and D-Bus owner before passing its unique D-Bus
connection name to the worker. Client requests cannot choose an address,
UID or arbitrary process to capture.

For a single-user installation, the entry service can instead run as that
user. Only its own host account can then authenticate. The installed source
and virtual environment must be readable by any account whose agent is used;
the installer provides those permissions without exposing a private checkout.

Passwords go only to a short-lived PAM helper through stdin. They are not
put in process arguments, environment variables, browser storage or logs.
An opaque HttpOnly session cookie is issued after login (Secure on HTTPS,
SameSite=Strict). Mutating API calls also require a session CSRF token and
the configured page Origin. Failed login attempts and concurrent PAM helpers
are bounded. The standalone entry does not read PVE gateway configuration.

For another computer, use HTTPS with a certificate trusted by its browser:

```sh
sudo env PYTHONPATH=/opt/qsm-standalone \
    /opt/qsm-standalone/.venv/bin/python -m extensions.standalone.server \
    --worker /opt/qsm-standalone/bin/qsm-direct-media-worker \
    --listen 0.0.0.0 --port 18008 \
    --public-url https://qemu.example.net:18008 \
    --tls-cert /path/to/fullchain.pem --tls-key /path/to/privkey.pem
```

Non-loopback binds require TLS. Wildcard binds also require `--public-url`;
only its configured Host/Origin is accepted. Proxy-provided identity is not
used. WebRTC media/input use direct UDP: the browser needs a route to the
host's ICE candidates, through a LAN or VPN, as well as the HTTPS port. This
launcher does not set up TURN, NAT traversal or the PVE TCP gateway.

## Media options

`--fps 30 --bitrate 8000` sets the frame rate and ceiling in kbit/s.
H.264 is used for browser compatibility. Hardware encoders can be selected
with `--encoder h264_vaapi --vaapi-device /dev/dri/renderD128`,
`--encoder h264_qsv`, or `--encoder h264_nvenc`, with their corresponding
host drivers and FFmpeg support. Explicit hardware failures are reported
rather than silently changing to CPU. GPU/device qualification is separate
from the CPU-mode tests below.

For GL Display1 capture, `--capture-render-node /dev/dri/renderD128` selects
the host capture GPU. Its compatibility is the same as in the core worker.
Browser-local input/display settings remain available; encoder, bitrate and
frame rate are launch options. Standard VGA without `SetUIInfo` keeps its
fixed guest resolution when the browser is resized.

Audio playback is available when QEMU exports D-Bus audio, for example using
`-display dbus,gl=off,audiodev=dbus0 -audiodev dbus,id=dbus0` and a sound
device attached to `dbus0`. The example launch script does this. PVE
agent/PCM adapters are not wired here, so file transfer, agent clipboard and
microphone controls are hidden. Screen and input need no guest software.

## Optional single-guest token mode

The earlier source interface remains available explicitly as `--auth token`.
It needs one D-Bus address and accepts the existing private token file:

```sh
PYTHON=/opt/qsm-standalone/.venv/bin/python \
    /opt/qsm-standalone/scripts/run-qsm-standalone.sh --auth token \
    --worker /opt/qsm-standalone/bin/qsm-direct-media-worker \
    --dbus-address-file /tmp/qmdp-guest/dbus.address --name guest
```

Without `--token-file`, the server generates a private token and prints only
its file path. The file is deleted on shutdown. For a persistent token:

```sh
mkdir -p "$HOME/.config/qsm"
chmod 700 "$HOME/.config/qsm"
(umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
    > "$HOME/.config/qsm/guest.token")
chmod 600 "$HOME/.config/qsm/guest.token"
```

Add `--token-file "$HOME/.config/qsm/guest.token"`. It must be owned by the
service user, private, and not a symlink. This optional mode still asks for
the token after a browser refresh. Normal PAM mode never asks for one.

## Verification

The [host-login validation record](standalone-host-login-validation-20261122.json)
records the real PAM/two-user browser gate and the regression checks.

From the source `qsm-direct` directory, with the Python requirements installed:

```sh
PYTHONPATH=. /opt/qsm-standalone/.venv/bin/python -m unittest discover -s tests/standalone -v
```

The API/discovery tests cover owner filtering, PID reuse, qdev-host device
display pairing (including the kernel socket table), session cookies,
CSRF, logout/expiry, login rate limits and failed-worker/shutdown cleanup.
CMake registers these tests when its Python has the required packages.

`tests/standalone/host_browser_e2e.py` is an explicit root-only integration
gate. It creates two unique temporary Unix accounts, assigns random test-only
passwords via stdin, boots real QEMU/TCG guests under their UIDs, logs in
through the shipped browser page and observes their keyboard/mouse responses.
It checks foreign-guest rejection, hidden non-Display1 guests, session reload,
logout revocation, expired-account rejection and agent/worker cleanup without
stopping QEMU. Accounts/processes are removed in `finally`; existing users
and their passwords are untouched. Use a root-owned installation readable
by those users and a test Python with `playwright` and `pillow` installed.
The browser only accepts a throwaway test certificate in this gate.

For example, run the gate from the checkout with a separate browser-test
environment containing `aiohttp`, `playwright` and `pillow`:

```sh
sudo /path/to/browser-test-venv/bin/python tests/standalone/host_browser_e2e.py \
    --server-root /opt/qsm-standalone \
    --python /opt/qsm-standalone/.venv/bin/python \
    --worker /opt/qsm-standalone/bin/qsm-direct-media-worker \
    --browser /usr/bin/google-chrome --output /tmp/qsm-host-login-check
```

`tests/standalone/browser_e2e.py` checks the optional token mode in real
QEMU/TCG through the complete viewer, with `--tls` for HTTPS. The original
[validation record](standalone-qemu-validation-20261122.json) documents that
mode's screen/input tests and hardware/audio/network coverage limits.
