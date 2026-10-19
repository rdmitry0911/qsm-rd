#!/usr/bin/env bash
# Optional gateway tools. Activation is explicit because pveproxy must restart.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${QSM_DIRECT_DEB_OUTPUT_DIR:-$ROOT_DIR/dist}"
version="${QSM_DIRECT_DEB_VERSION:?set QSM_DIRECT_DEB_VERSION}"
dpkg --validate-version "$version"
package=qsm-pve-direct-gateway
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$OUTPUT_DIR" "$stage/DEBIAN" "$stage/usr/share/doc/$package" "$stage/usr/sbin" "$stage/usr/lib/systemd/system"
install -m 0644 "$ROOT_DIR/packaging/debian/README.gateway" "$stage/usr/share/doc/$package/README.Debian"
install -m 0755 "$ROOT_DIR/extensions/gateway/qsm_direct_gateway.py" "$stage/usr/sbin/qsm-pve-direct-gateway"
for unit in qsm-pve-direct-gateway.service qsm-pve-direct-turn.service qsm-pve-direct-gateway-certs.service qsm-pve-direct-gateway-certs.timer; do
    install -m 0644 "$ROOT_DIR/packaging/debian/$unit" "$stage/usr/lib/systemd/system/$unit"
done
cat > "$stage/DEBIAN/control" <<EOF
Package: $package
Version: $version
Section: net
Priority: optional
Architecture: all
Maintainer: Dmitry R <rdmitry0911@gmail.com>
Depends: qsm-pve-direct-standalone (= $version), adduser, python3, systemd
Suggests: haproxy, coturn, nftables
Description: Optional TCP 8006 HTTPS and WebRTC relay gateway for QSM Direct
 Explicitly activated single-node stock-PVE profile. Multiplexes HTTPS and
 authenticated TURN/TCP on 8006; no media UDP ingress is needed. Requires
 HAProxy, coturn and nftables binaries. Activation restarts pveproxy and is
 reversible. Generic HAProxy/coturn services are never managed by this package.
EOF
cat > "$stage/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "${1:-}" = configure ]; then
    if ! getent passwd qsm-turn >/dev/null; then
        adduser --system --group --no-create-home --home /nonexistent qsm-turn
    fi
    if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi
    echo 'Gateway tools installed. Activation is explicit; read /usr/share/doc/qsm-pve-direct-gateway/README.Debian.'
fi
EOF
cat > "$stage/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "${1:-}" = remove ] && [ -e /etc/qsm-pve-direct/gateway.json ]; then
    /usr/sbin/qsm-pve-direct-gateway disable
fi
EOF
chmod 0755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm"
(cd "$stage" && find usr -type f -print0 | sort -z | xargs -0 md5sum) > "$stage/DEBIAN/md5sums"
dpkg-deb --root-owner-group --build "$stage" "$OUTPUT_DIR/${package}_${version}_all.deb"
