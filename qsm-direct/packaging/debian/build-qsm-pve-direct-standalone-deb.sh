#!/usr/bin/env bash
# Standalone installation profile; media code is supplied by qsm-pve-direct.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${QSM_DIRECT_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
version="${QSM_DIRECT_DEB_VERSION:?set QSM_DIRECT_DEB_VERSION to match qsm-pve-direct}"
dpkg --validate-version "$version"
package=qsm-pve-direct-standalone
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$OUTPUT_DIR" "$stage/DEBIAN" "$stage/usr/share/doc/$package"
install -m 0644 "$ROOT_DIR/packaging/debian/README.standalone" "$stage/usr/share/doc/$package/README.Debian"
cat > "$stage/DEBIAN/control" <<EOF
Package: $package
Version: $version
Section: net
Priority: optional
Architecture: all
Maintainer: Dmitry R <rdmitry0911@gmail.com>
Depends: qsm-pve-direct (= $version)
Description: Standalone QSM Direct console profile for stock Proxmox VE 9
 Enables the independent PVE-authorized HTTPS signalling service without
 replacing Proxmox packages. The initial port is chosen from free ports
 starting at 18007. Bind address, port and public URL are configurable with
 qsm-pve-direct-endpoint. LXC desktop setup is an optional separate package.
EOF
cat > "$stage/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "${1:-}" = configure ]; then
    /usr/sbin/qsm-pve-direct-endpoint --initialize
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload
        systemctl enable --now qsm-pve-direct-terminal.service
        systemctl enable qsm-pve-direct-signal.service
        # An already-running legacy service must pick up a newly chosen port.
        systemctl restart qsm-pve-direct-signal.service
    fi
    echo 'QSM standalone is configured. Run qsm-pve-direct-endpoint --show for its address; reload the PVE web page.'
fi
EOF
cat > "$stage/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "${1:-}" = remove ] && [ -d /run/systemd/system ]; then
    systemctl disable --now qsm-pve-direct-signal.service || true
fi
EOF
chmod 0755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm"
(cd "$stage" && find usr -type f -print0 | sort -z | xargs -0 md5sum) > "$stage/DEBIAN/md5sums"
artifact="$OUTPUT_DIR/${package}_${version}_all.deb"
dpkg-deb --root-owner-group --build "$stage" "$artifact"
echo "QSM_STANDALONE_DEB_BUILD_OK artifact=$artifact"
