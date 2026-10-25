# QSM Direct APT repository

An unofficial, signed preview repository for **Proxmox VE 9 / Debian 13
(trixie), amd64**. The standalone console works with official Proxmox packages;
it still requires Proxmox VE. This is not part of the Debian or Proxmox archives.
No patched Proxmox packages are distributed by this repository.

Current preview: **0.4.0+stock4.7** keeps fullscreen when opening/editing
Settings and resumes guest capture on Close. Capture keyboard is a text
button near the top of the toolbar and the first Settings action. The reveal
handle no longer overlaps toolbar controls. Logical macOS modifier routing
from stock4.6, focus preservation and browser keyboard capture remain.
Guest first selects routing; click Capture keyboard to acquire full-screen
browser capture. No F11 or macOS assignment changes are required.
Exact mouse/trackpad clicks are the
default too: complete clicks include their position and modifiers. Confirmed
drags remain ordered and guarded; fractional two-axis scrolling is retained.
Exact typing is the default: one complete
key action per client press/repeat prevents delayed releases from creating
extra guest repeats. Physical holds/Raw remain available. The stock4.1 video
reference-detail fix is retained. Update all installed QSM profiles together,
reload PVE with cache bypass and reopen/reload consoles. VM/LXC reboot or
desktop reprovisioning is not needed.

Project sponsor: Takt — https://taktcycles.com/

Repository: https://rdmitry0911.github.io/qsm-rd/

Public key fingerprint:

    BA99 5281 82B7 EADD 0199 B698 6A09 741B 1DBE AEF2

## Install on a stock PVE 9 node

Run as root. Keep the official Debian/PVE repositories enabled for dependencies.
Review the standalone instructions first:
https://github.com/rdmitry0911/qsm-rd/blob/main/qsm-direct/packaging/debian/README.standalone

    apt install ca-certificates curl gpg
    install -d -m 0755 /usr/share/keyrings
    curl --fail --show-error --location \
      https://rdmitry0911.github.io/qsm-rd/qsm-archive-keyring.gpg \
      -o /usr/share/keyrings/qsm-archive-keyring.gpg
    chmod 0644 /usr/share/keyrings/qsm-archive-keyring.gpg
    gpg --show-keys --with-fingerprint /usr/share/keyrings/qsm-archive-keyring.gpg

Compare the primary fingerprint above before continuing. Configure a scoped
key and limit this hostname to the six listed QSM packages:

    cat > /etc/apt/sources.list.d/qsm-direct.sources <<'EOF'
    Types: deb
    URIs: https://rdmitry0911.github.io/qsm-rd/
    Suites: trixie
    Components: main
    Architectures: amd64
    Signed-By: /usr/share/keyrings/qsm-archive-keyring.gpg
    EOF

    cat > /etc/apt/preferences.d/qsm-direct.pref <<'EOF'
    Package: qsm-archive-keyring qsm-pve-direct qsm-pve-direct-standalone qsm-pve-direct-gateway qsm-pve-direct-lxc qsm-console-guest
    Pin: origin "rdmitry0911.github.io"
    Pin-Priority: 100

    Package: *
    Pin: origin "rdmitry0911.github.io"
    Pin-Priority: -1
    EOF

    apt update
    apt install qsm-archive-keyring qsm-pve-direct-standalone
    qsm-pve-direct-ui status
    qsm-pve-direct-endpoint --show

The core package is installed as a dependency. Reload PVE with Ctrl+F5. The
guarded UI overlay must report active; an unreviewed PVE UI version fails closed.
Installation starts QSM services. It does not stop VMs, configure guest displays,
provision LXC desktops or enable the optional gateway. Follow the standalone
guide for VM/LXC configuration and the configurable HTTPS/UDP network profile.

The preferences apply to all repositories on rdmitry0911.github.io, including
other paths if you later add any. Installed QSM packages can receive preview
updates through normal APT upgrades. QSM service restarts can interrupt consoles.

## Optional profiles

For LXC, install the helper on the PVE HOST, then configure only the chosen CT:

    apt install qsm-pve-direct-lxc
    qsm-pve-direct-lxc enable VMID --session wayland

The helper embeds and installs the matching guest package INSIDE that container.
Never install qsm-console-guest on the PVE host. SDDM/local-cursor setup has
additional requirements and is not performed by installing this helper.

For a router/NAT forwarding only TCP 8006, first read the experimental,
single-node gateway guide:
https://github.com/rdmitry0911/qsm-rd/blob/main/qsm-direct/packaging/debian/README.gateway

    apt install qsm-pve-direct-gateway

This installs tools only. The guide covers HAProxy/coturn/nftables installation,
listener checks, explicit activation with --relay-ip, encryption and rollback.
Activation restarts pveproxy; plan for that interruption. HTTPS signalling,
SRTP media and DTLS data remain encrypted. The outer TURN control connection
uses plain TCP, so this is not an HTTPS-only/TLS-only proxy traversal profile.

Existing native/shared-preview nodes need the staged migration in the standalone
guide before adding standalone/gateway. APT does not replace patched PVE packages
or convert dbus-display VM configurations automatically.

## Updates and removal

    apt update
    apt install --only-upgrade qsm-archive-keyring qsm-pve-direct qsm-pve-direct-standalone

Add qsm-pve-direct-lxc and qsm-pve-direct-gateway to that transaction when they
are installed. Profiles depend on the exact core version. Enabled gateway
configuration is retained; installing an inactive profile does not enable it.

To stop receiving updates, remove /etc/apt/sources.list.d/qsm-direct.sources
and /etc/apt/preferences.d/qsm-direct.pref, then run apt update. Installed
packages remain. Read the standalone/gateway removal instructions before
removing the console itself; guest display configurations need planned cleanup.

## Archive maintenance

The approved release and SHA256 values are committed in releases.json. Only the
five allowlisted QSM packages and qsm-archive-keyring enter the archive; every
download is checked before its package metadata is read. Keep older releases in
the manifest so clients can still download previously indexed package versions.
Source, corresponding release archives and validation evidence are public at:
https://github.com/rdmitry0911/qsm-rd/releases

The publish-apt GitHub Actions workflow runs after relevant main-branch changes,
on manual dispatch, and weekly to renew metadata. It does not sign pull requests
or automatically promote arbitrary GitHub release assets. To publish a new
version, review/build/test its release, append its exact package inventory and
hashes to releases.json, then commit that change. This is a binary APT archive;
deb-src indexes are not provided. Source releases remain available separately.

InRelease and Release.gpg are signed by a dedicated Ed25519 signing subkey,
expiring 2028-10-19. The primary certification key expires 2029-10-19; its secret
part and revocation certificate stay in the maintainer's local private key store,
outside Git. Only the signing subkey is in the GitHub Actions secret
QSM_APT_SIGNING_SUBKEY. Private key material never enters Pages, Git or artifacts.

Release metadata expires after 30 days. If GitHub disables an idle repository's
scheduled workflows, re-enable the workflow and dispatch it manually. Expiry is
an error to fix at the publisher; do not disable APT signature or expiry checks.
Monitor failed Actions runs. Rotate/extend the public certificate and bump the
keyring package version before expiry; distribute the updated keyring while the
old signing key is still valid, with an overlap before changing signing keys.
Keep an offline backup of the certification key and revocation certificate.

Build locally with the dedicated signing key available in GNUPGHOME:

    python3 qsm-direct/packaging/apt/publish.py --output /tmp/qsm-apt-site --cache /tmp/qsm-apt-cache
    python3 qsm-direct/packaging/apt/verify.py --site /tmp/qsm-apt-site

Requires python3, dpkg-dev, apt-utils and gnupg. The output directory must be
empty. The keyring deb is deterministic for a given key/copyright/version;
increment keyring_version when its contents change.

Repository setup follows Debian's third-party archive recommendations:
https://wiki.debian.org/DebianRepository/UseThirdParty
