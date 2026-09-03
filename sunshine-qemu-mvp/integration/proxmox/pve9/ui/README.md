# Proxmox VE 9 Console integration

This small PVE Web UI integration adds **q-sunshine** to the existing
**Console** split menu for QEMU VMs. It does not replace noVNC, SPICE, or
xterm.js; it adds one more console transport choice.

It also adds **q-sunshine Display1** to the **Advanced** section of the QEMU
**Display** editor and an equivalent **Display (Advanced)** page in the QEMU
creation wizard. PVE 9 supplies a `VirGL GPU` choice but has no stock form
field for QEMU Display1's D-Bus backend.

When enabled, the overlay selects PVE-owned `virtio-gl` and manages precisely
one argument:

```text
-display dbus,addr=unix:path=/run/q-sunshine/<VMID>/qemu-display1.bus,gl=on,rendernode=/dev/dri/renderD128
```

It preserves unrelated `args:` bytes and refuses to touch a VM which already
has another `-display` argument or a non-canonical q-sunshine Display1
argument. It never appends a second `virtio-vga-gl` device: PVE creates that
device from `vga: virtio-gl`. A fixed VMID is required in the creation wizard
so the socket path cannot be guessed or retargeted. The VM must be stopped and
the matching q-sunshine instance must have been provisioned before it is
started. QSF virtio-serial devices remain separate explicit VM hardware.

The browser stays inside the normal Proxmox security boundary:

```text
authenticated PVE browser session
  -> POST /api2/extjs/nodes/<node>/qemu/<vmid>/q-sunshine
       (API2Request supplies the ordinary PVE session and CSRF handling)
  -> PVE checks VM.Console and forwards the protected method to pvedaemon
  -> root-only local Unix handoff to q-sunshine-terminal
  -> one-use .qsm descriptor download
```

The overlay never calls `vncproxy`, never receives a `PVEVNC` ticket, and
never knows a terminal address, PVE cookie, CSRF token, password, private key,
or QEMU socket path. The descriptor is validated before it is written to a
Blob URL and contains only its one-use claim and public terminal TLS endpoint.

The native fallback menu action downloads a `.qsm` launch file for the Qt /
Moonlight transport client. The browser-first Console transport is built as a
separate WebRTC-compatible server-side media bridge; GameStream RTP is not
decoded directly by JavaScript and a generic browser extension cannot open
its arbitrary UDP/TCP transport sockets.

## Server-side node endpoint policy

The terminal service, not the browser, reads the root-managed cluster mapping
`/etc/pve/q-sunshine-node-endpoints.json`. It selects the TLS endpoint placed
inside an already-authorized descriptor. The exact schema is:

```json
{
  "version": 1,
  "nodes": {
    "pve-a": {
      "host": "terminal-a.example.net",
      "port": 48123,
      "server_name": "terminal-a.example.net"
    }
  }
}
```

It contains public routing/TLS-name metadata only, is root-owned and not
group/world writable (`0644` is suitable), and must never contain a PVE
credential, a ticket, a CA private key, or a launch claim.

## Managed PVE template patch

PVE 9 currently has no supported GUI plug-in hook at this location. The
package therefore uses a guarded `dpkg-divert` of only:

```text
/usr/share/pve-manager/index.html.tpl
```

The stock template is retained at
`/usr/share/pve-manager/index.html.tpl.q-sunshine-pve-orig`. The replacement
adds one static script immediately after PVE's own `pvemanagerlib.js`:

```html
<script src="/pve2/js/q-sunshine-console.js?ver=[% version %]-qsm1"></script>
```

Before changing a file, `q-sunshine-pve-ui` checks both exact installed
`pve-manager` version and the SHA-256 of the original template against its
allow-list. On an unreviewed PVE update it restores the untouched stock
template, so the additional Console item disappears rather than guessing at a
new UI layout. It also removes the obsolete generated
`q-sunshine-console-config.js` asset from older package versions.

Useful operator commands:

```bash
sudo q-sunshine-pve-ui status
sudo q-sunshine-pve-ui reconcile
```

## Debian package staging

| Source | Installed path |
| --- | --- |
| `q-sunshine-console.js` | `/usr/share/pve-manager/js/q-sunshine-console.js` |
| `q_sunshine_pve9_ui.py` | `/usr/lib/q-sunshine/pve9-ui/q_sunshine_pve9_ui.py` |
| `pve-manager-index-template.sha256` | `/usr/share/q-sunshine/pve9-ui/pve-manager-index-template.sha256` |
| `debian/q-sunshine-pve-ui` | `/usr/sbin/q-sunshine-pve-ui` |
| `debian/triggers` | `DEBIAN/triggers` |

The PVE API route itself is documented in `../api/README.md`. It is guarded
separately because PVE does not yet provide a stable third-party API-route
plug-in ABI.

## Focused tests

```bash
python3 tests/proxmox_pve9/test_pve9_ui.py
node tests/proxmox_pve9/test_console_overlay.mjs
```

The browser protocol fixture asserts one same-origin protected PVE API request
and a validated Blob `.qsm` download. It rejects the legacy `vncproxy`,
`PVEVNC`, external-browser-broker, and endpoint-map paths.
