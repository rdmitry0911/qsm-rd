# QSM Direct 0.4.0+stock4.14~exp223: two-pane file manager

Unofficial preview for **PVE 9 / Debian 13, amd64**, now the current version on the
[signed APT repository](https://rdmitry0911.github.io/qsm-rd/). It needs no patched
Proxmox packages. The debs are the ones installed on the test node, unchanged.

## What changed since stock4.14~exp216

* **A two-pane file manager in the console**, like AnyDesk's. **Files** in the console's
  toolbar shows this computer on the left and the guest on the right. Files and folders
  copy both ways, and on the guest's side there is new folder, rename and delete. The
  console keeps running underneath.
* **It needs no new port.** Transfers use a WebSocket on the same path as the console's
  signal requests, which is TCP 8006 on a node with the gateway. The console's own
  connection stays WebRTC and shares no queue with the files.
* **A transfer takes what the console leaves.** Keys, exact UI tiles, video and sound go
  first. On a fast LAN a transfer runs at about 100 MiB/s.
* **It does not get around the guest's login or lock.** A helper in the logged-in user's
  desktop session serves the files, with that user's permissions. Nothing is served
  before login, and a locked session refuses new work.
* **PVE decides who may use it.** Browse and download need `VM.Console` and
  `VM.GuestAgent.FileRead` on the guest. Upload, new folder, rename and delete also need
  `VM.GuestAgent.FileWrite`. `PVEVMUser`, `PVEVMAdmin`, `PVEAdmin` and `Administrator`
  hold all three. A role with `VM.Console` alone gets no file access. In a container the
  console must belong to the same PVE user.
* **Capture keyboard and Files are ordinary toolbar buttons** now, the size of the others.

Other fixes, which matter without the file manager:

* A TCP connection that sent nothing could hold the signal service's TLS handshake and
  keep every new console out of a node without the gateway. The handshake now runs per
  connection, with a timeout.
* On a container's native seat, a slow logind, for example under memory pressure, ended
  the seat's control service and every desktop on both seats with it. The seat now keeps
  going. This fix is in `qsm-console-guest` and takes effect at the container's next
  restart.
* A guest that floods the agent channel with lines that do nothing loses the channel,
  and a partial line is no longer searched again on every read.

## The browser's side

* **Chrome and Edge** open a folder of this computer with the File System Access API.
  Chrome refuses the home folder, the Desktop, Documents and Downloads folders themselves
  and system folders, and says the folder *contains system files*. A folder inside
  Desktop, Documents or Downloads works. The dialog first opens in Downloads. Make a folder there,
  for example `QSM`, and choose it. The empty pane says this too.
* **Firefox** has no folder access for a web page. **Add files…** and **Add folder…**
  choose what to upload, or drop files on the pane. Downloads go to the browser's
  Downloads folder and are listed in the pane, from where they can be sent back. A folder
  downloads as one `.tar`.

## Measured

On a 10 Gbit/s LAN, with a real PVE login and console popup, TLS through an HAProxy of the
gateway's shape, and the helper in a container, 1 GiB each way, three runs:

| | Up | Down |
| --- | --- | --- |
| Steady, MiB/s | 89-106 | 94-103 |
| Whole file with the ramp, MiB/s | 68-76 | 73-79 |
| The unreleased first version, on its own WebRTC connection, median MiB/s | 6.2 | 4.5 |

Into and out of a real KVM VM through its virtio port: 77.5 and 79.4 MiB/s, 1289 small
files/s. Chromium on loopback: 109 MiB/s up, 141 down, 1286 small files/s. Firefox
downloads through its spool at 29 MiB/s. No node process came near a full core. On the
terminal's media loop, which serves every console, a 10 ms timer ran late by about 2 ms
at p99 during a transfer, as when idle.

On a shaped path with the console in view, medians of two runs:

| Path | Download, Mbit/s | Upload, Mbit/s | Cost to the console |
| --- | --- | --- | --- |
| 20 Mbit/s, 15 ms | 17.6-17.7 | 18.4-18.5 | typing +5-10 ms during a download, otherwise none measurable |
| 20 Mbit/s, 100 ms | 12.5-13.2 | 14.9-16.9 | 0-8 ms |
| 2 Mbit/s, 15 ms | 1.4-1.5 | 1.5-1.7 | +8-20 ms, drag lag +23 ms during an upload |
| 2 Mbit/s, 100 ms | 1.1-1.3 | 1.7-1.8 | 0-16 ms for a download, 15-24 ms for an upload |

Typing, click, wheel and drag lag were measured each time. The cost is against the same
action with no transfer, on a flat backdrop. A clip, in distinct source frames per second:

| Phase of the clip run | No transfer, two runs | With a download | With an upload |
| --- | --- | --- | --- |
| Open path | 46.9, 47.4 | 48.4 | 47.5 |
| Narrow | 42.7, 43.7 | 41.8 | 42.6 |
| Recovered | 46.6, 47.6 | 47.9 | 43.6 |

The installed packages were checked on one stock node through a normal PVE login, with
the console on TURN over TCP 8006 and the files on their WebSocket on the same port. A
throwaway account logged in through the container's real login screen, Chromium and
Firefox each moved 40 MiB each way with equal SHA-256 and used new folder, rename and
delete. A real screen lock refused new work and paused a copy, which finished whole
after unlock. The file peer ran as `nobody`. 52 of 53 checks passed. The one failure was
the harness looking for a temporary file that a fast upload had already removed. None
was left behind. The maintainer then used it by hand in Firefox and Chrome, which found
the two problems fixed in this version: an empty Firefox pane, and Chrome's refusal of
the first folders one picks.

Automated: 40 of 41 CTest gates pass in the isolated build environment.
`qsm_desktop_metadata_wire` needs a pty, which that environment does not have, and
passes outside it.

## Known limits

* **About 100 MiB/s** fills a 1 Gbit/s LAN, but only a tenth of 10 Gbit/s. The browser's
  main thread is the likely limit, not proven.
* **A lost link starts the file over** from its beginning. There is no resume.
* During an upload **into a VM**, a clipboard request can wait about 200 ms (p95),
  because both share the one virtio port.
* **On 2 Mbit/s** a transfer costs the console up to about 25 ms, as above.
* **Not covered by an automated run:** a VM through a real PVE console (VMs ran with the
  real agent and virtio port, without PVE), the real folder dialog, which automation
  replaces, the console embedded in the PVE page rather than its popup, a PVE user
  without the file privileges (unit tests only), a lossy path, a Mac, a phone or a tablet.
* **Linux guests only.** There is no Windows agent, and macOS guests are not supported
  by the file manager.
* Browsers cannot set a local file's time, so a downloaded file gets the current time.
  Closing the console ends its transfers without asking.
* The file manager's texts are in English only.
* The page changes after the measured candidate, the Firefox listing, the hints and the
  toolbar buttons, were checked by scenario tests and by hand, not by a new measurement
  run.
* Everything in the [exp216 notes](release-stock4.14-exp216-2026-11-06.md) still applies,
  including that VMs are not at the container's responsiveness.

## Update

**The node**, through the signed repository, with every installed QSM profile in one
transaction:

```sh
apt update
apt install --only-upgrade qsm-archive-keyring qsm-pve-direct qsm-pve-direct-standalone
# add qsm-pve-direct-lxc and qsm-pve-direct-gateway if they are installed
```

Or use the debs attached here, with `apt install ./file.deb ...`, not one `dpkg -i` with
several files. GitHub stores `~` in asset names as `.`, so
`qsm-pve-direct_0.4.0+stock4.14.exp223_amd64.deb` contains version
`0.4.0+stock4.14~exp223`. Then reload PVE with cache bypass and reopen the consoles.
Restarting QSM services interrupts open consoles while guests keep running.

**The guest**, only for the file manager. Everything else works with the guest packages
you have.

* **Container with the native login:** update `qsm-console-guest` inside it. The helper
  starts with the next desktop login.
* **VM:** update `qsm-desktop-agent` inside it, then:

  ```sh
  sudo qsm-desktop-agent-setup USER                # an upgrade does not turn the helper on
  sudo systemctl restart qsm-desktop-agent@USER    # the running agent forwards files only after this
  ```

  The desktop session survives that restart. The helper starts with the next login, or
  at once with `systemctl --user start qsm-desktop-files` in that session.
  `qsm-desktop-agent-setup --no-files USER` keeps it off.

Never install `qsm-console-guest` on the PVE host.

## Go back

`apt install --allow-downgrades` with `=0.4.0+stock4.14~exp216` for **every** installed QSM
host package in one transaction. APT removes an installed profile that the command
leaves out, so read its plan before confirming. Updated guest packages can stay. Running
them with the older host was not tested.

## Review material

Attached: the source of the release commit, the seven debs, signed checksums, build
provenance, install guides and `review-stock4.14-exp223-20261107.tar.gz` with the raw runs
behind this page. Summaries are in `qsm-direct/docs/`: `FILE_MANAGER.md`,
`file-manager-websocket-qualification-20261107.json` and
`rdp-deployment-exp219-20261107.json`. Signing key:
`BA99528182B7EADD0199B6986A09741B1DBEAEF2`.

Project sponsor: [Takt](https://taktcycles.com/).
