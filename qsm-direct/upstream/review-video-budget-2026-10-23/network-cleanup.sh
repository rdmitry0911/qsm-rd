#!/bin/bash
set -u
iptables -D FORWARD -i qsm-b23h -d 192.168.64.20/32 -m comment --comment qsm-bands23-test -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -o qsm-b23h -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment qsm-bands23-test -j ACCEPT 2>/dev/null || true
nft delete table ip qsm_bands23 2>/dev/null || true
ip link del qsm-b23h 2>/dev/null || true
ip netns del qsm-bands23 2>/dev/null || true
