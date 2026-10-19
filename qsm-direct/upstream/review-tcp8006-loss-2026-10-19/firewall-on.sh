#!/bin/bash
set -Eeuo pipefail
if nft list table inet qsm_8006_qualification >/dev/null 2>&1; then exit 1; fi
cat > /root/qsm-gateway-lab/firewall-off.sh <<'OFF'
#!/bin/sh
nft delete table inet qsm_8006_qualification
OFF
chmod 700 /root/qsm-gateway-lab/firewall-off.sh
systemd-run --unit=qsm-gateway-firewall-watchdog --on-active=20m /root/qsm-gateway-lab/firewall-off.sh
nft -f - <<'NFT'
add table inet qsm_8006_qualification
add chain inet qsm_8006_qualification input { type filter hook input priority 100; policy drop; }
add rule inet qsm_8006_qualification input iifname lo counter accept
add rule inet qsm_8006_qualification input ct state established,related counter accept
add rule inet qsm_8006_qualification input tcp dport 8006 counter accept
add rule inet qsm_8006_qualification input counter drop
NFT
