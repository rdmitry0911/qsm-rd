#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cmake -S "$ROOT" -B "$ROOT/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$ROOT/build" -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
ctest --test-dir "$ROOT/build" --output-on-failure
"$ROOT/build/qmdp_demo"
