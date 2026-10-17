#!/bin/bash
set -euo pipefail
ip netns add qsm-pacing
ip link add qsm-pace-host type veth peer name qsm-pace-peer
ip link set qsm-pace-peer netns qsm-pacing
ip addr add 172.31.253.1/30 dev qsm-pace-host
ip link set qsm-pace-host up
ip -n qsm-pacing link set lo up
ip -n qsm-pacing addr add 172.31.253.2/30 dev qsm-pace-peer
ip -n qsm-pacing link set qsm-pace-peer up
ip -n qsm-pacing route add default via 172.31.253.1
nft add table ip qsm_pacing_lab
nft 'add chain ip qsm_pacing_lab postrouting { type nat hook postrouting priority srcnat; policy accept; }'
nft add rule ip qsm_pacing_lab postrouting ip saddr 172.31.253.2 ip daddr 192.168.64.20 masquerade
# Isolated interfaces only; only UDP is shaped. HTTPS/SSH bypass the impairment.
for side in host peer; do
 if [[ $side == host ]]; then
  prefix=(); dev=qsm-pace-host; options=(delay 40ms rate 8mbit limit 50)
 else
  prefix=(ip netns exec qsm-pacing); dev=qsm-pace-peer; options=(delay 40ms limit 50)
 fi
 "${prefix[@]}" tc qdisc add dev "$dev" root handle 1: prio
 "${prefix[@]}" tc qdisc add dev "$dev" parent 1:3 handle 30: netem "${options[@]}"
 "${prefix[@]}" tc filter add dev "$dev" protocol ip parent 1: prio 1 u32 match ip protocol 17 0xff flowid 1:3
done
