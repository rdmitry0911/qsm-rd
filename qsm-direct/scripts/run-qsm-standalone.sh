#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attach the browser console to ordinary QEMU; no PVE services are loaded.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
worker_args=()
if [[ -x "${BUILD_DIR:-$ROOT/build}/qsm-direct-media-worker" ]]; then
  worker_args=(--worker "${BUILD_DIR:-$ROOT/build}/qsm-direct-media-worker")
fi
exec "${PYTHON:-python3}" -m extensions.standalone.server "${worker_args[@]}" "$@"
