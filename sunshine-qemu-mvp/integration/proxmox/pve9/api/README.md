# PVE 9 protected Console route

q-sunshine adds one protected `POST` method to PVE's existing QEMU API
resource:

```text
POST /api2/extjs/nodes/<node>/qemu/<vmid>/q-sunshine
```

The browser already has a normal authenticated PVE session. PVE evaluates the
same `VM.Console` ACL predicate used for the stock console endpoints, forwards
the protected request to the selected node, and executes the handler in the
root `pvedaemon` worker. The route is intentionally POST-only and has no PIN,
pairing, PVE password, VNC-ticket, cookie, CSRF-token, or raw QEMU socket
parameter.

## Boundary

```text
PVE browser session
  -> PVE VM.Console ACL / protected pvedaemon forwarding
  -> PVE::API2::QSunshine (root)
  -> root:root 0600 AF_UNIX /run/q-sunshine-terminal/pve-launch.sock
  -> q-sunshine terminal broker
  -> short-lived .qsm launch descriptor
```

After confirming that the VM exists and is running, the PVE handler sends one
bounded JSON line to the local terminal service:

```json
{"version":1,"op":"pve_acl_launch","node":"pve-a","vmid":100,"subject":"user@realm"}
```

It validates the returned descriptor exactly before it is returned through
PVE's normal API response. An error is always the generic `q-sunshine console
is unavailable`; neither local socket details nor authorization data are
reflected to the browser.

## Package-owned activation

PVE 9 has no supported third-party API-route plug-in ABI. The package keeps
the integration reversible by supplying launchers rather than diverting or
editing a `pve-manager` file:

```text
/usr/lib/q-sunshine/pve9-api/PVE/API2/QSunshine.pm
/usr/lib/q-sunshine/pve9-api/PVE/QSunshine/Compatibility.pm
/usr/lib/q-sunshine/pve9-api/q-sunshine-pveproxy
/usr/lib/q-sunshine/pve9-api/q-sunshine-pvedaemon
/usr/lib/q-sunshine/pve9-api/q-sunshine-pvesh
/usr/lib/systemd/system/pveproxy.service.d/q-sunshine-api.conf
/usr/lib/systemd/system/pvedaemon.service.d/q-sunshine-api.conf
```

The two drop-ins replace only `ExecStart`, `ExecStop`, and `ExecReload`. They
leave all vendor unit dependencies and hardening in place. `postinst` reloads
systemd and tries to restart `pvedaemon` before `pveproxy`, so both processes
get the same route registry. During removal `prerm` removes only those two
package paths, reloads systemd, and tries the same restart order while the
wrapper files still exist; subsequent service starts then use PVE's stock
executables directly.

An administrator-owned `/etc/systemd/system/*.service.d/` override is never
created, changed, or removed by this package.

The package also declares narrow dpkg file triggers for every PVE file used by
the two service-role compatibility checks. An update to one of those files
re-runs `postinst`, which restarts the two wrappers so an unqualified update
falls back to stock immediately. The separate PVE Web-template trigger only
reconciles the UI; it does not restart healthy PVE services.

## Fail-closed compatibility profile

Before either wrapper imports `PVE::API2::QSunshine`,
`PVE::QSunshine::Compatibility` checks the exact reviewed ABI: PVE Manager
`9.2.11`, QEMU Server `9.2.7`, and SHA-256 digests of the PVE API, service, and
launcher files that the route uses. The profile is deliberately an allow-list,
not a version range.

On any mismatch, a missing module, or a failed extension load,
`q-sunshine-pveproxy`/`q-sunshine-pvedaemon` immediately `exec` the matching
untouched `/usr/bin/pveproxy` or `/usr/bin/pvedaemon`. Therefore an unreviewed
PVE update removes the q-sunshine route but does not make the PVE daemon depend
on the extension. The Console item will report its normal unavailable state
until that PVE release is qualified and its exact profile is added.

`q-sunshine-pvesh` is an optional root diagnostic launcher for inspecting the
same route. It follows the same compatibility profile and otherwise delegates
to stock `pvesh`.

Every cluster node that can own a VM console needs the package and terminal
service installed. Because the PVE method uses `proxyto => 'node'`, a partial
cluster installation intentionally does not provide a remote-node fallback.
