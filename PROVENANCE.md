# Provenance

This Git repository was initialized in this directory on 2026-09-01 from the
materials supplied with the workspace.

## Immutable supplied archive

- File: `sunshine-qemu-mvp(1).zip`
- SHA-256: `12e601b38305e37bfe6c205246505fd6baaf9bc1cee59951825b2e063fdbb504`
- The archive's extracted project identifies itself as version `0.4.0`.

`sunshine-qemu-mvp/` began as that extraction and is now the canonical working
source tree. Its archive `MANIFEST.sha256` verifies only an exact extraction;
it is not a checksum manifest for the modified working tree.

## Historical root materials

The root-level planning and status files were supplied alongside the archive and
are retained verbatim as historical input. Some claim version `0.6.0`, ten
CTest tests, reconnect, and release-all behavior. Those claims conflict with
the supplied `0.4.0` source and its original five-test CMake matrix, so they
are not treated as implementation evidence. In particular, `environment.txt`
records an earlier unavailable-QEMU environment and is not the environment used
for the current validation.

## Reproducible local dependencies

- QEMU, the Alpine ISO/qcow2 reference VM, generated traces, build trees and
  the upstream Sunshine checkout are intentionally ignored by Git.
- The Sunshine integration is represented by a portable patch plus an exact
  upstream pin in `sunshine-qemu-mvp/integration/sunshine/`, rather than by
  vendoring Sunshine.
- `qemu-probe-last.ppm` is left in the workspace but ignored because it has no
  command/version sidecar. Reproducible evidence is generated under the source
  tree's `artifacts/validation/` paths.
