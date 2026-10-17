#!/bin/bash
set -euo pipefail
# Run after the browser in this dedicated namespace has exited.
ip link del qsm-pace-host
ip netns del qsm-pacing
nft delete table ip qsm_pacing_lab
