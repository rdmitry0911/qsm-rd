#!/bin/bash
set -Eeuo pipefail
! ip link show qsm-b23h 2>/dev/null
! nft list table ip qsm_bands23 2>/dev/null
systemd-run --unit=qsm-bands23-cleanup --on-active=20m /bin/bash /tmp/qsm-rate-bands-20261023/network-cleanup.sh
trap 'bash /tmp/qsm-rate-bands-20261023/network-cleanup.sh' ERR
ip netns add qsm-bands23
ip link add qsm-b23h type veth peer name qsm-b23b
ip link set qsm-b23b netns qsm-bands23
ip addr add 172.31.250.1/30 dev qsm-b23h
ip link set qsm-b23h mtu 1280 up
ip -n qsm-bands23 link set lo up
ip -n qsm-bands23 addr add 172.31.250.2/30 dev qsm-b23b
ip -n qsm-bands23 link set qsm-b23b mtu 1280 up
ip -n qsm-bands23 route add default via 172.31.250.1
nft add table ip qsm_bands23
nft 'add chain ip qsm_bands23 postrouting { type nat hook postrouting priority srcnat; policy accept; }'
nft add rule ip qsm_bands23 postrouting ip saddr 172.31.250.2 ip daddr 192.168.64.20 masquerade
iptables -I FORWARD 1 -i qsm-b23h -d 192.168.64.20/32 -m comment --comment qsm-bands23-test -j ACCEPT
iptables -I FORWARD 1 -o qsm-b23h -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment qsm-bands23-test -j ACCEPT
tc qdisc add dev qsm-b23h root handle 1: prio bands 3
tc qdisc add dev qsm-b23h parent 1:3 handle 30: tbf rate 20mbit burst 12kb latency 100ms
tc filter add dev qsm-b23h parent 1: protocol ip prio 10 u32 match ip protocol 17 0xff flowid 1:3
