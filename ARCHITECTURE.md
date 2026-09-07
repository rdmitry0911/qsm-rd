# Historical architecture note

This file described the pre-QSM Direct experimental transport architecture. It is retained only as a stable path in Git history and is not a specification for the current product.

The current supported architecture is documented in [QSM Direct](qsm-direct/README.md): PVE authentication and `VM.Console` ACLs protect a same-origin browser WebRTC connection; a node-local terminal service and media worker consume QEMU Display1 over private Unix sockets. No separate pairing service, public listener, native client, or host desktop session is part of the current design.

For deployment and verification, use [installation](qsm-direct/docs/INSTALLATION.md), [testing](qsm-direct/docs/TESTING.md), and the [PVE 9 laboratory guide](qsm-direct/lab/proxmox9/README.md).
