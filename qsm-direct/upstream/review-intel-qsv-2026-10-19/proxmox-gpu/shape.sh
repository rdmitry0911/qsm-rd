#!/bin/bash
set -Eeuo pipefail
# Only UDP leaving this disposable nested Proxmox guest is shaped.
tc qdisc replace dev eth0 root handle 1: prio bands 3
tc qdisc add dev eth0 parent 1:3 handle 30: tbf rate 20mbit burst 12kb latency 100ms
tc filter add dev eth0 parent 1: protocol ip prio 10 u32 match ip protocol 17 0xff flowid 1:3
