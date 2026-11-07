# Two-pane file manager

The console gets a file manager mode like AnyDesk's: the left pane is the computer
running the browser, the right pane is the guest, and files and folders are copied
between them.

## Goals and limits of the design

* **The console stays responsive.** A transfer yields to keys, exact UI tiles, video and
  sound. Nothing runs and no process exists until the file manager is opened.
* **No new network port.** The transfer goes where the page's signal requests go, which is
  TCP 8006 on a node with the gateway.
* **No guest restart.** A VM keeps its existing virtio-serial port. Only the guest
  package has to be updated.
* **It does not get around a guest's login or lock.** Files are served by a process in
  the logged-in user's session, with that user's permissions. There is no service before
  login, and a locked session refuses new operations.
* **The guest is untrusted.** Everything it sends is bounded and validated by the host
  and the browser.

## Why the transfer has its own WebSocket

A transfer must take whatever the console leaves, on a 2 Mbit/s path as on a fast LAN. The
first version used its own WebRTC connection, and on a 10 Gbit/s LAN reached a median of
6 MiB/s up and 4.5 down: aiortc sends from one FIFO, in Python, at about a core per 12 MiB/s.
It could also stop for good, in 2 of 44 uploads, with no cause found. A WebSocket has neither
limit, carries the same frames, reaches the node on the path the signal requests already use,
and shares no queue with the console, whose connection stays WebRTC.

The peer still runs apart from the terminal, one per session, because it parses what comes
from the network. All consoles of a node share one asyncio loop.

## Path of the data

```text
browser ── WebSocket, TLS ── [HAProxy on the gateway] ── qsm-direct-signal (relays bytes)
                                                              │ socketpair
                                            qsm_file_peer.py (unprivileged, one per file session)
                                                              │ socketpair, lines
                                                   qsm-direct-terminal (relay, adds the session id)
                                                              │ QsmGuestChannel, QSF1 lines
              VM:  virtio-serial ── qsm-desktop-agent ── files.sock ──┐
              CT:  /run/qsm-login/clip-N/files.sock ──────────────────┤
                                                       qsm_desktop_files.py (user session)
```

## Authorization

`POST /nodes/<node>/<qemu|lxc>/<vmid>/qsm-direct-files` on the signal service, with the
same PVE ticket handling as the console:

| Privilege on `/vms/<vmid>` | Result |
| --- | --- |
| `VM.Console` and `VM.GuestAgent.FileRead` | required; browse and download |
| `VM.GuestAgent.FileWrite` | also upload, new folder, rename, delete |

`PVEVMUser`, `PVEVMAdmin`, `PVEAdmin` and `Administrator` hold all of these on PVE 9. A
custom role with `VM.Console` alone gets no file access. The file session also needs an
open console of the same guest, and in a container it must belong to the same PVE user.

The answer is `{socket, rights}`. `socket` is a 128-bit token that opens this session's
WebSocket, once, at `/qsm-direct-files/socket/<token>` on the same service. A WebSocket from a
browser cannot carry the `Authorization` header, so the token is its credential. The request
that returned it was authorized a moment before. The signal service presents the token to the
terminal, gets the peer's socket back as a file descriptor, answers the handshake and relays
bytes. A token the signal service did not hand out in the last 35 s stops there, as this route
has no other credential and the terminal runs every console's media. A session that nobody
connects to in 30 s ends.

The terminal sends the rights to the guest as `FS <sid> O r|rw`. The session id is added
by the terminal, so `qsm_file_peer.py` cannot choose it or raise its rights.

A node runs at most 6 file sessions, and a console starts one at a time. A session's peer
is limited to 512 MiB of address space and runs as `nobody`. The terminal service has
`OOMPolicy=continue`, so one peer over its limit does not stop every console.

## QSF1 lines between host and guest

Added to the guest agent's line protocol. `<sid>` is 1 to 16 characters of `[0-9a-f]`.

```text
host -> guest
  FS_CAPS                 -> OK FS_CAPS 1      (an older agent answers ERR ...)
  FS <sid> O r|rw         open a session
  FS <sid> D <base64>     one frame
  FS <sid> C              the viewer left: abort its work and remove its temporary files
  FS * C                  a new host connected: end everything an earlier one left open

guest -> host
  FS <sid> R              session is open
  FS <sid> D <base64>     one frame
  FS <sid> C <reason>     session ended: nohelper | limit | gone
  FS * C <reason>         every session on this link ended
```

**VM.** `qsm-desktop-agent` answers `FS_CAPS` and forwards every other `FS ` line
unchanged to `<state-dir>/files.sock`, connecting when the first line arrives. It never
parses a frame. If it cannot connect it replies `FS <sid> C nohelper`. If the helper goes
away it sends `FS * C gone`. Lines from the helper reach the virtio port whole, never
interleaved with the agent's own lines. An agent blocked writing to the port can miss that
the host went away, so a new connection sends `FS * C` once, after `FS_CAPS` has shown that
the agent understands it. The agent then drops its connection to the helper.

**Container.** The host connects to `/run/qsm-login/clip-N/files.sock` directly. The
helper there greets with `READY QSF1`, answers `FS_CAPS` itself and speaks the same lines.

The host reads a guest's lines in a thread of the process that runs every console's media. A
guest that sends more than 100000 lines in 10 s that move no file data loses the channel.

The helper always listens. It accepts several connections, each with its own sessions, and
ending a connection ends its sessions. Up to 8 sessions per connection; beyond that it
replies `FS <sid> C limit`.

## Frames

```text
frame = u16be header_len | header (JSON, ASCII) | payload
```

At most 65536 bytes in total. File data is carried in payloads of at most 60000 bytes. Paths are JSON
strings. A name that is not valid UTF-8 travels as lone surrogates (`\udcXX`,
Python's `surrogateescape`), so it survives a round trip through JSON in Python and
JavaScript. Paths are absolute, contain no NUL and are at most 4096 bytes when encoded.

Every frame from the browser has `op` and `id` (integer, 1..2^31-1, chosen by the browser).
Every frame from the helper has `id`. A final response is `{"id":N,"ok":true,...}` or
`{"id":N,"ok":false,"err":"ENOENT","msg":"..."}`.

`err` is one of `ENOENT EACCES EPERM EEXIST ENOTDIR EISDIR ENOTEMPTY ENOSPC EDQUOT EROFS
EINVAL ELOCKED ERIGHTS ENOTREG ECANCELED EBUSY ENAMETOOLONG EIO`. Any other OS error is
`EIO`, with `msg` holding the system's text.

| op | Right | Request | Response |
| --- | --- | --- | --- |
| `hello` | | | `ok, v:1, home, user, rights:"r"\|"rw", locked, chunk:60000` |
| `list` | r | `path` | frames `{id, part:[[name,type,size,mtime,mode,target],...]}`, each header at most 48 KiB, then `ok, path, total, truncated` |
| `stat` | r | `path` | `ok, type, size, mtime, mode, target` |
| `mkdir` | w | `path, parents` | `ok`. An existing directory is `EEXIST` unless `parents` is true |
| `rename` | w | `from, to` | `ok`. Never replaces: an existing `to` is `EEXIST` |
| `delete` | w | `path, recursive` | frames `{id, progress:n}` at most every 200 ms, then `ok, removed` |
| `put` | w | `path, size, mtime?, mode?, overwrite:"fail"\|"replace", last?` with payload | frames `{id, ack:bytes}` with the running total after each write, which holds at most 1 MiB and what has queued up during the last one, then `ok, size` |
| `data` | w | `id, o` with payload | no response of its own |
| `end` | w | `id, size` | the final response of the `put` |
| `get` | r | `path, credit, chunk?, offset?` | `{id, meta:true, size, mtime, mode}`, data frames `{id, o}` with payload, then `ok, size, changed` |
| `credit` | r | `id, n, chunk?` | none |
| `cancel` | | `id` | the request's final response, `ECANCELED` |

`type`: `f` file, `d` directory, `l` symlink, `o` other. `target` is the type a symlink
points at, `x` if dangling, and null for anything else. `mtime` is integer milliseconds.
`mode` is the permission bits.

Rules the helper enforces:

* **Rights.** An operation outside the session's rights gets `ERIGHTS`.
* **Lock.** While the session is locked every request except `hello`, `credit`, `cancel`,
  `data` and `end` gets `ELOCKED`. Transfers already open finish. The state is read only
  while a session is open: `org.freedesktop.ScreenSaver`, then `org.gnome.ScreenSaver`,
  then `loginctl LockedHint`, at most every 2 s. A locker that exists but fails counts as
  locked. No locker at all counts as not locked.
* **Limits.** At most 64 requests and 16 transfers in progress per session, otherwise
  `EBUSY`. A directory lists at most 200000 entries or 8 MiB of names, then `truncated`.
  A list is not paced by credit, and the host holds 16 MiB per session.
* **Frame size.** `chunk`, 1024 to 60000, sets the size of later data frames of a `get`.
* **Uploads are atomic.** Data goes to `.<name>.qsm-part-<8 hex>` in the same directory,
  created with `O_EXCL|O_NOFOLLOW`, mode 0600. `end` checks the size, syncs files of 1 MiB
  or more, sets mtime and the final mode, and renames. `overwrite:"fail"` does not replace
  anything, even in a race. `"replace"` keeps the mode of the file it replaces. A target
  that is a directory is `EISDIR`. Cancel, a size mismatch, a closed session or a lost
  connection remove the temporary file. A data frame whose `o` is not the next offset fails
  the upload with `EINVAL`. A helper killed outright, or a guest losing power, leaves its
  temporary file behind. It is a hidden file, and nothing sweeps it.
* **Frames may be pipelined.** `data` and `end` can follow `put` without waiting for its
  reply. If `put` failed, they are dropped and only one failure is sent. `put` with
  `last:true` is the whole file, opened, written and finished in one frame.
* **Downloads only read regular files.** `get` looks at the type before opening, since
  merely opening a FIFO wakes its writer, and opens with `O_NONBLOCK` in case of a swap in
  between (`ENOTREG`). It sends no more than `credit` has granted. `changed` is true if size
  or mtime moved during the read.
* **Links are not followed.** `delete` never follows one at any depth, and a recursive copy
  skips them. A symlink selected directly is followed for reading.
* **Blocking work runs off the frame reader**, so a slow disk or a hung mount cannot stop
  frames for other requests, and `cancel` always gets through.

## Messages on the WebSocket

Each frame is one binary message, in both directions. Text messages are the rest:

* `{"t":n}` from the browser is echoed unchanged, ahead of anything waiting.
* `{"rate":n}` from the browser sets the peer's pace toward the browser, in bytes per second.
* `{"op":"status","state":"ready"|"closed","reason":...}` from the peer.

The peer reads a browser's frames as RFC 6455 requires: masked, possibly fragmented, with
pings between fragments, of which only the latest is answered. Anything else ends the session,
as does silence for 90 s. A message holds at most 65536 bytes.

## Taking the bandwidth the console leaves

A transfer has two duties: it gives way to the console, and it takes everything the console
does not use. A low rate that harms nothing is a failure too.

A controller in the browser sends an echo ten times a second while
something is moving, and twice a second otherwise. It takes the lowest of the last five
echoes as the present delay. An echo waits for the page's main thread, and a busy page delays
many of them, while a queue on the path delays all of them.

The excess is measured over the path's own delay, which is only visible with the queue
empty, and which can change. So every 15 s the transfer drops to a quarter of its rate for
half a second and takes what it then sees as the new floor, as BBR does. That costs about
2.5% of what it carries. Three cuts in a row with no effect start a drain early, at most
every 5 s, since either the queue is deep or the path has just become longer.

The excess is kept near a target: **25 ms while the console is in view** and **100 ms while
the file manager covers it**. The 25 ms target sits below the 80 ms standing queue at which
the video controller cuts its own rate, so a transfer gives way first.

* **When the excess reaches the target**, the path is full, and the bytes the far end took
  over the last half second are what it carries beside the console. The rate goes a little
  below that, by enough to drain the queue, and holds for 400 ms so one queue is paid for
  once. Far too much on two echoes in a row does not wait for the filter, and an echo that
  does not return in time counts as well: the path has just become much narrower.
* **Below the capacity it saw**, the rate returns quickly. That much is known to fit.
* **Past it**, the console may have stopped using its share. The probe starts small and
  doubles every two seconds while the path holds, so freed bandwidth is taken back within
  seconds without a lasting queue.
* **Before any congestion** the rate grows by 5% every 100 ms up to 1 MiB/s, by 10% up to
  8 MiB/s and by 20% beyond. A narrow path shows while the rate is low, and past that the same
  care would spend many seconds of a fast link on getting up to speed. On a path of under 5 ms
  it grows by 40%, as a queue on a LAN shows at once and costs little.
* The rate grows only while it is below twice what really arrives, so a slow disk does not
  store up a burst. It never falls below 16 KiB/s, so a transfer cannot stop for good.
  Growth follows time and not samples, because a hidden tab fires timers once a second.

This is how p2p clients yield too, in uTP. Strict priorities in one queue would not do it:
the queue where console and files meet is in the network or in TURN, out of reach. Video,
though, says when the path holds it back: its target is below its maximum while it is really
sending. A download then stands aside at 16 KiB/s until video has what it wants, and takes the
rest after that. An upload does not share video's direction, and with the file manager
covering the console nobody is watching that video, so neither stands aside. A desktop that
is idle after a narrow spell sends next to nothing and holds nothing up.

Where the queue is ours there are three levels: the console, whose connection never shares a
queue with files; then commands (listings, acknowledgements, errors), which the peer sends
ahead of waiting file data, unless they must stay behind data of their own request; then file
data.

The browser paces uploads itself and sends the rate to the peer for downloads. A frame is a
burst on the wire and whatever comes next waits behind it, so frames last about 20 ms at
the current rate, between 2 and 60 KB. The window of unacknowledged bytes is half a second
at the current rate, between 64 KiB and 8 MiB, so a cancel on a narrow link does not wait
for a long queue. An upload reads ahead 50 ms of the current rate, at most 8 MiB, since one
read of a local file costs far more than a frame of it.

## Failures

A job ends even if the guest never answers: a cancel tells the guest and stops waiting, also
while a new connection comes up. If a connection goes away, or has data on its way and no frame
back for 20 s, it is replaced and the file starts over on the new one, up to three times. A
hostile guest cannot send more than was granted or than it declared for a file, and a
listing stops at the helper's limits. A file that changed while it was read is not kept and
does not replace anything, and a new local file that fails does not stay behind empty. Two
guest names that are one name locally (case, or bytes that are not UTF-8) both survive, the
second under a changed name. A folder that fails to open leaves the pane where it was,
because every action names its files by that folder.

## Local pane

* **Chromium** uses the File System Access API. The user picks a folder, the pane browses
  inside it, and downloads are written with `createWritable`, which only replaces the
  target on close.
* **Firefox and Safari** have no folder picker. The pane holds what the user drops or
  chooses for upload. Downloads go through the browser's Downloads folder, spooled through
  the origin private file system, and a folder arrives as one `.tar`.

## Not in this version

macOS and Windows guests, resuming an interrupted file, a pane for the PVE node's own
files, and copying inside one pane. Browsers cannot set a local file's mtime, so a
downloaded file gets the current time. Closing the console ends its transfers without
asking.
