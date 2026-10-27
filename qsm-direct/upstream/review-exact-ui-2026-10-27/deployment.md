# Experimental exact UI enabled on the requested node

Installed **0.4.0+stock4.14~exp6** on the requested test node: core, standalone,
gateway and LXC helper. No Proxmox/dependency upgrade or guest power action
was performed. Guest packages were not required to change for this mode.

The node default is enabled through
`/etc/systemd/system/qsm-pve-direct-terminal.service.d/exact-ui-default.conf`:

```ini
[Service]
Environment=QSM_DIRECT_EXACT_UI_DEFAULT=1
```

The new bridge advertises this default. A capable viewer automatically
subscribes unless the user has explicitly chosen otherwise in Settings.
Older implicitly saved false preferences migrate correctly. Explicit user
choices survive reload and periodic server announcements. Merely saving
other settings does not convert an automatic node default into an explicit
user choice. Other nodes keep the off default without this environment flag.

All **27 CTest** and **83 bridge tests** passed for this revision. The media
tile encoder/classification is unchanged from the earlier exp4 mixed-content
qualification; exp6 adds node-default advertisement and preference handling.

The installed path was checked with Firefox 157 through the **actual PVE
interface, QSM menu, standalone signalling and TURN/TCP 8006**, using the owned
test LXC. Authentication used PVE's normal ticket-login path and populated
its normal GUI capabilities. No PVE/media API was stubbed for this check.

* Seeded an old browser preference with `exactUi:false` and no explicit-choice
  marker; the new option automatically became available and checked.
* Video connected, exact-pixel PATCH records arrived, and backend policy
  reported `default_enabled:true`, `enabled:true`, with zero source gaps.
* Explicitly unchecked it, waited through periodic announcements, and verified
  the persisted opt-out. Re-enabling worked. No page errors or requests to
  another node port occurred. The browser used relay ICE.
* The package installation restarted QSM terminal/signalling services. Host boot ID, running
  VM states/PIDs and CT states stayed identical. All four QSM services are
  active and `dpkg --audit` is empty. The owned test display was restored to
  1280×720@60.
* An obsolete, already orphaned worker for VM 103 was stopped after confirming
  PPID 1, the deleted old executable and its original media-socket arguments.
  QEMU and its display bus were preserved.

[Browser result](production-browser-exp6.json),
[installed-node result](deployment-exp6.json),
[deployed source hashes](deployment-exp6-source-fingerprints.json).
The production browser check used the test LXC's configured libx264 encoder;
the previous overlapping-window measurements used NVENC. This is an installed
smoke check, not a new mixed-content performance benchmark or live VM test.

Reload the PVE page with cache bypass and reopen QSM consoles to receive the
new JavaScript and server policy. **Console settings → Experimental exact UI**
can disable the mode for that browser.

The former stock4.13 debs and pre-upgrade node configuration are retained in
`/root/qsm-exact-ui-install-20261027/rollback/` and
`/root/qsm-exact-ui-install-20261027/qsm-config-before.tar.gz`.
Removing the environment override and restarting only the QSM terminal service
restores an off node default; explicit browser opt-ins still take precedence.

## Additional node rollout

At the user's request the same exp6 package set and explicit node default
were installed on the other two existing QSM nodes, preserving their profiles:
one native/shared API node (core + LXC only), and one stock-PVE standalone
node with the existing single-TCP-8006 gateway (four host profiles). Real
Firefox connections through the normal PVE UI confirmed automatic enable,
lossless PATCH delivery, persisted opt-out and manual re-enable on both.
No browser page errors or requests to another node port occurred. Native
used direct ICE; standalone used relay ICE. These are installed-service
smoke checks, not additional overlapping-video latency benchmarks.
[Sanitized browser results](additional-node-browser-checks.json).
Original test-seat display modes were restored after the checks.
