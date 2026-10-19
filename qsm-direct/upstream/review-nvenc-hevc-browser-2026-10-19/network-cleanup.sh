#!/bin/bash
set -u
iptables -D FORWARD -i qsm-nv24h -d 192.168.64.20/32 -m comment --comment qsm-nvenc24-lab -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -o qsm-nv24h -s 192.168.64.20/32 -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment qsm-nvenc24-lab -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -o qsm-nv24h -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment qsm-nvenc24-lab -j ACCEPT 2>/dev/null || true
nft delete table ip qsm_nvenc24 2>/dev/null || true
ip link del qsm-nv24h 2>/dev/null || true
ip netns del qsm-nvenc24 2>/dev/null || true
