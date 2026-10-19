#!/bin/bash
set -Eeuo pipefail
test "$(cat /proc/sys/net/ipv4/ip_forward)" = 1
! ip netns list | grep -q '^qsm-nvenc24\b'
! ip link show qsm-nv24h 2>/dev/null
! nft list table ip qsm_nvenc24 2>/dev/null
systemd-run --unit=qsm-nvenc24-cleanup --on-active=30m /bin/bash /root/qsm-nvenc-browser24-network-cleanup.sh
trap 'bash /root/qsm-nvenc-browser24-network-cleanup.sh' ERR
ip netns add qsm-nvenc24
ip link add qsm-nv24h type veth peer name qsm-nv24b
ip link set qsm-nv24b netns qsm-nvenc24
ip addr add 172.31.252.1/30 dev qsm-nv24h
ip link set qsm-nv24h mtu 1280 up
ip -n qsm-nvenc24 link set lo up
ip -n qsm-nvenc24 addr add 172.31.252.2/30 dev qsm-nv24b
ip -n qsm-nvenc24 link set qsm-nv24b mtu 1280 up
ip -n qsm-nvenc24 route add default via 172.31.252.1
nft add table ip qsm_nvenc24
nft 'add chain ip qsm_nvenc24 postrouting { type nat hook postrouting priority srcnat; policy accept; }'
nft add rule ip qsm_nvenc24 postrouting ip saddr 172.31.252.2 ip daddr 192.168.64.20 masquerade
iptables -I FORWARD 1 -i qsm-nv24h -d 192.168.64.20/32 -m comment --comment qsm-nvenc24-lab -j ACCEPT
iptables -I FORWARD 1 -o qsm-nv24h -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment qsm-nvenc24-lab -j ACCEPT
tc qdisc add dev qsm-nv24h root handle 1: prio bands 3
tc qdisc add dev qsm-nv24h parent 1:3 handle 30: tbf rate 20mbit burst 12kb latency 100ms
tc filter add dev qsm-nv24h parent 1: protocol ip prio 10 u32 match ip protocol 17 0xff flowid 1:3
