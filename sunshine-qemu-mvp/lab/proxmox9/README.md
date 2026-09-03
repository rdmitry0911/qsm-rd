# Nested Proxmox VE 9 lab

`run-lab.sh` boots a KVM-backed Debian 13 cloud guest, adds the official
Proxmox VE 9 no-subscription repository, and installs a single-node PVE lab.
It is intentionally an isolated qualification environment, not an installer
for a production node.

The outer lab's PVE web UI is forwarded to `https://127.0.0.1:18006`; SSH is
forwarded to `127.0.0.1:12222`. The script uses the existing
`/home/dima/.ssh/id_ed25519.pub` solely for the disposable lab root account.
It requires `/dev/kvm` and passwordless `sudo` for QEMU. Runtime disks are
kept under `lab/proxmox9/runtime/` and the reusable Debian cloud image under
`lab/proxmox9/cache/`.

Start and wait for PVE:

```bash
./lab/proxmox9/run-lab.sh start
./lab/proxmox9/run-lab.sh wait
./lab/proxmox9/run-lab.sh ssh 'pveversion -v'
```

The eventual integration gate provisions an inner QEMU VirGL guest using
`qm`, installs the q-sunshine package from the read-only `qsm-src` 9p share,
and validates the PVE Console menu action, ACL boundary, one-time launch file,
native Moonlight/Sunshine media, resize, clipboard, and file transfer.

## Real-browser PVE Console qualification

`qualify-pve-console-launch.cjs` is the non-mocked browser gate for the PVE
handoff. It uses a fresh Playwright Chromium context to submit the visible PVE
login form, selects the live `qemu/<vmid>` record in the PVE resource tree,
opens that VM's visible **Console** split menu, clicks **q-sunshine**, and
waits for the actual `.qsm` download.

It observes but never intercepts browser traffic. The gate requires exactly
one same-origin protected PVE `POST /api2/extjs/nodes/<node>/qemu/<vmid>/q-sunshine`
request. It rejects a VNC or external terminal launch route, then strictly
parses the downloaded file:

- exactly the v1 `q-sunshine-pve-launch` envelope and endpoint fields;
- a live, short-lived `qsd1` claim and valid public certificate PEM; and
- no supplied PVE password, PVE ticket, or `PVEAuthCookie` value.

The script never prints a password, PVE ticket, cookie, request body, claim,
or arbitrary browser error. It also deliberately contains no Playwright route
or API mock. The PVE account must have access to the target QEMU VM and
`VM.Console`; `qsmconsole@pve` is the lab identity. PVE's default `pam` realm
is not assumed: the script splits `qsmconsole@pve`, finds the actual loaded
realm (shown as **Proxmox VE authentication server** in the PVE 9 lab), and
selects it through the visible realm control.

Install the test-only dependency and browser once from this directory:

```bash
cd lab/proxmox9
npm install --no-save --no-package-lock playwright@1.52.0
npx playwright install chromium
cd ../..
```

After installing/reconciling the Proxmox package and configuring the terminal
node map, create an empty private directory for the one-use launch file and
run the qualification:

```bash
download_dir="$(mktemp -d /tmp/qsm-pve-download.XXXXXX)"
chmod 700 "$download_dir"

node lab/proxmox9/qualify-pve-console-launch.cjs \
  --pve-url https://127.0.0.1:18006 \
  --user qsmconsole@pve \
  --password-file lab/proxmox9/runtime/pve-console-user.password \
  --vmid 100 \
  --download-dir "$download_dir" \
  --ignore-https-errors
```

`--ignore-https-errors` is only for the nested lab's self-signed PVE and
terminal certificates. Omit it on a deployment with browser-trusted HTTPS.
The destination directory must be owned by the invoking user and not writable
by group/other; the script retains the validated descriptor as mode `0600` for
the following native-client test and refuses to overwrite an existing file.

For the forwarded single-node lab, the shared map must have an exact
`qsm-pve9-lab` entry whose `host`/`port` are reachable from the native client
(for example `127.0.0.1:58123`). The browser run intentionally fails if the
package overlay, protected PVE API route, PVE ACL, running VM, or terminal
descriptor issuer is missing or inconsistent.

The no-network source-level checks for this gate are:

```bash
node --check lab/proxmox9/qualify-pve-console-launch.cjs
node tests/proxmox_pve9/test_browser_qualification_static.cjs
```

## Display1 Advanced-form qualification

`qualify-pve-display1-ui.cjs` opens the real PVE **Hardware → Display** editor
and verifies the shipped Advanced fields without clicking **OK** or issuing a
VM update.  Unlike the Console-only test identity, this check needs an account
with `VM.Config.HWType` for the selected VM because PVE correctly hides its
Display editor from a console-only role.

```bash
node lab/proxmox9/qualify-pve-display1-ui.cjs \
  --pve-url https://127.0.0.1:18006 \
  --user qsmconfig@pve \
  --password-file /secure/qsmconfig.password \
  --vmid 100 \
  --ignore-https-errors
```

The result is a bounded marker such as
`QSM_PVE_DISPLAY1_UI_E2E_OK vmid=100 rendernode=/dev/dri/renderD128`.  It
checks the live PVE UI, not a mocked ExtJS component, and never prints a
password, PVE ticket, cookie, VM argument string, or API response body.

## Deploy the one-service terminal topology

`deploy-terminal-service.sh` is the bounded transition from the lab's earlier
manual QEMU/D-Bus/QSF harness to the packaged one-node
`q-sunshine-terminal.service`. It accepts an explicit freshly built amd64
`q-sunshine-pve` `.deb`; it does not build an artifact itself:

```bash
./lab/proxmox9/deploy-terminal-service.sh \
  --deb "$PWD/dist/q-sunshine-pve_<fresh-version>_amd64.deb"
```

It first waits for the existing nested PVE through `run-lab.sh`, checks the
artifact's package identity and SHA-256 on both sides of the lab SSH boundary,
then verifies the fixed `qsm-pve9-lab` / VM `100` VirGL and QSF topology. The
script refuses an unexpected node, an unmanaged terminal/map/policy file, a
live legacy systemd unit, or a manual PID that does not exactly match the
known lab harness.

Only after the package, PVE Console overlay, ticket key, VM lease material,
node endpoint map, and root-only VM policy have been checked does it stop VM
100 and the validated temporary QSF and private D-Bus processes. It removes
only the two exact stale VM sockets after their owners stop, enables the one
terminal service, waits for its D-Bus/readiness marker, and restarts VM 100.
The resulting non-secret marker has the form:

```text
QSM_LAB_TERMINAL_DEPLOY_READY node=qsm-pve9-lab vmid=100 …
```

The lab policy is deliberately fixed to the forwarded topology: browser PVE
origin `https://127.0.0.1:18006`, terminal descriptor endpoint
`127.0.0.1:58123`, and inner broker listener `48123`. VM 100 uses the
software H.264 qualification lane with a 1920×1080/60 FPS QSF host envelope;
this is a lab control, not a production encoder recommendation. No PVE
password, PVE ticket, cookie, ticket key, or lease private key is printed.
On a failure the root-only staged package remains under
`/var/tmp/q-sunshine-lab-deploy/` for inspection; the script deliberately
does not recursively delete any lab or PVE directory.

## Full descriptor-to-VirGL/QSF acceptance lane

`run-descriptor-qt-virgl-qsf-e2e.sh` is the independent full acceptance lane:
it uses the actual PVE browser Console menu to download one `.qsm`, passes that
file alone to the real Qt driver, and requires the Qt driver to start the
patched Moonlight child inside a private Xvfb. It retains a private evidence
directory with the three visual captures, Browser/Qt logs, redacted Moonlight
route metadata, phase protocol, guest virtserial telemetry, and final PVE/QEMU
state.

First deploy the terminal topology with the current package. The deploy step
also adds the fixed second VM-100 virtserial port
`org.qsunshine.virgl.wayland.telemetry`; the guest fixture uses that one-way
port to prove raw evdev input independently from the QSF agent channel.

The runner requires the existing real Qt driver and a **patched** Moonlight Qt
binary at `.upstream/moonlight-qt-clean/app/moonlight` unless overridden. It
rejects Moonlight Embedded and a stock Moonlight binary: it verifies both the
compiled `q-sunshine system-auth lease` string and the live
`moonlight stream --help` `--qsm-system-auth` option under Xvfb. To reconstruct
that binary, replay the exact pinned patch instructions in
[`../../integration/moonlight/PINNED_UPSTREAM.md`](../../integration/moonlight/PINNED_UPSTREAM.md)
and build the checked-out tree as documented in
[`../../docs/QT_DESKTOP_CLIENT.md`](../../docs/QT_DESKTOP_CLIENT.md).

```bash
./lab/proxmox9/deploy-terminal-service.sh \
  --deb "$PWD/dist/q-sunshine-pve_<fresh-version>_amd64.deb"

./lab/proxmox9/run-descriptor-qt-virgl-qsf-e2e.sh
```

VM 100 is deliberately rebooted by default, because the disposable guest
fixture emits its VirGL/Weston bootstrap proof and starts with no stale
clipboard/file/input marks. Use `--no-fresh-boot` only for a diagnostic rerun
against an already-known clean guest; it cannot recreate boot-time evidence.
This fixed image deliberately locks the first negotiated profile to
1280×720 and both media profiles to software H.264 at no more than 60 FPS /
16000 Kbps; those are payload and VM-policy facts, not generic client limits.
The runner never deletes the evidence directory and only stops its own Xvfb,
Qt-driver, and telemetry-capture processes. It does not start Sunshine/QSF
workers itself: their presence is checked after descriptor redemption through
the terminal service. A successful `trace.txt` ends with
`QSM_PVE_DESCRIPTOR_QT_VIRGL_QSF_E2E_OK`.
