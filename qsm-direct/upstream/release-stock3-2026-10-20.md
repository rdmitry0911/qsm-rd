Stock3 publishes the standalone UI compatibility used in the regular-node deployment, with complete installation, router/NAT, encryption and migration instructions. It includes stock2's confirmed-loss adaptive bitrate fix and optional TCP 8006 gateway.

### Choose the installation profile

- **Official PVE 9, direct UDP:** install matching core + standalone. Signalling selects a free HTTPS port starting at 18007; media needs direct UDP. Follow **INSTALL-STOCK.txt**.
- **Official PVE 9 behind NAT / TCP 8006 only:** add the optional gateway and follow **INSTALL-GATEWAY.txt**. PVE HTTPS, signalling and authenticated TURN/TCP share node TCP 8006. Forward the router's public TCP 8006 to node TCP 8006; `--relay-ip` is the node's **LAN IPv4**, not the public IP. A different outside port is taken from the actual PVE page URL.
- **Existing patched native preview:** update core + matching LXC helper while retaining the matched Proxmox backend packages. Installing standalone/gateway does **not** automatically convert the patched viewer or VM display configs. See the migration section in INSTALL-STOCK.txt.

Fresh stock-PVE base installation, as root:

```sh
apt install ./qsm-pve-direct_0.4.0+stock3_amd64.deb \
            ./qsm-pve-direct-standalone_0.4.0+stock3_all.deb
qsm-pve-direct-endpoint --show
qsm-pve-direct-ui status
```

For LXC install the host helper `qsm-pve-direct-lxc_0.4.0+stock3_all.deb`. It embeds the matching guest payload. **Never install `qsm-console-guest` on the PVE host.** Update every installed version-matched QSM profile/helper in the same transaction; the guides include the full gateway/LXC upgrade command. No patched Proxmox debs are needed for a stock-PVE setup.

### Optional gateway and encryption

The gateway is an explicitly enabled, experimental **single-node** profile. It changes the pveproxy listener, briefly restarts the web service and affects external SPICE after spiceproxy restarts. It refuses an existing native patched UI and custom non-comment pveproxy defaults. Clusters/custom source ACLs/reverse proxies need manual review. HAProxy/coturn/nftables are Suggested dependencies: follow the guide to install binaries while preserving existing services. QSM uses dedicated units/configuration.

After those prerequisites, enable during a maintenance window:

```sh
qsm-pve-direct-gateway enable --relay-ip <node-LAN-IPv4>
```

Hard-reload PVE. The browser must be able to send raw TCP to the forwarded port; an HTTPS-only application proxy is insufficient. No public UDP or second signalling port is required in gateway mode. Host loopback/established traffic must remain allowed. To restore the pre-gateway listener/configuration:

```sh
qsm-pve-direct-gateway disable
```

**User session contents are encrypted, but not every protocol message is.** UI, login and signalling use HTTPS/TLS; media uses SRTP and input/clipboard use DTLS-protected data channels between browser and QSM. The outer TURN connection is **plain TCP, not TURN/TLS**. Its service messages and relay addresses remain visible. Stock3 does not implement an all-TLS transport. Direct UDP remains the normal option; TCP can add latency under loss.

### Existing shared-display VMs

When a separately planned transition restores a reviewed official pve-manager UI while retaining native qemu-server for running `dbus-display=1` VMs, stock3 now keeps their QSM console available through standalone signalling. Display saves preserve stock VGA fields and do not add a duplicate private display; the native dbus audio backend stays selectable.

This compatibility is **not full removal of native dependencies**. Keep the native backend until each shared-display VM has a planned stopped-VM conversion to a private standalone display. The packages do not perform that conversion or downgrade Proxmox packages automatically. Fully stock installations continue to use private Display1 from the start.

### Validation and inherited fixes

- Six JavaScript suites, 24 signal/endpoint/gateway tests (one ownership fixture rerun as root) and two package-state tests pass. Deb payloads, exact dependency versions, embedded guest payload and packaged instructions checked.
- The runtime code was deployed as `stock2.compat1` on the regular node. Actual LXC greeter video and close/reopen pass in Chromium 156 and Firefox 155 over LAN, selecting TURN `relayProtocol=tcp`, with increasing decoded frames, zero decoder drops during the checks and no page errors. Running VMs were preserved; native backend dependencies were retained. The user's own router/Internet check is not claimed complete.
- [Stock3 deployment/validation details](https://github.com/rdmitry0911/qsm-rd/blob/v0.4.0-qsm-direct-20261020-stock3/qsm-direct/upstream/review-stock3-2026-10-20/qualification.txt).
- [Stock2 controlled qualification](https://github.com/rdmitry0911/qsm-rd/blob/v0.4.0-qsm-direct-20261020-stock3/qsm-direct/upstream/review-tcp8006-loss-2026-10-19/qualification.txt): real VM/LXC video/reopen in Chrome/Firefox through NAT with an actual 8006-only INPUT policy; relay ACL/lease failure, reboot and static-loss/congestion tests. This is inherited evidence, not a new hardware test for stock3.
- H.264/HEVC NVENC static text with adaptation enabled stays stable under occasional injected loss. Sustained congestion still lowers bitrate and recovery returns to the configured maximum. Severe bandwidth drops can still cause freezes. The common fix also serves VM/LXC, software, QSV and VA-API; no new Intel hardware result is claimed here.

The media-worker binary is byte-identical to stock2/shared24. Source archive, both installation guides, review bundle, forum draft, source commit and SHA256 checksums are attached. Release remains a preview.
