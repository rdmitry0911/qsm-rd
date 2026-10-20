# Debian contrib packaging — October 20, 2026

Project sponsor: [Takt](https://taktcycles.com/).
Maintainer and ITP owner: Dmitry R <rdmitry0911@gmail.com>.

## Procedure started

[ITP #1151422](https://bugs.debian.org/1151422) is registered against WNPP.
It proposes qsm-pve-direct for contrib because the standalone console still
requires external pve-manager/qemu-server packages. It credits Takt as project
sponsor, discloses AI assistance and asks for feedback on the scope.

The ITP was submitted through reportbug.debian.org with verified STARTTLS.
The earlier attempt through the local Takt relay bounced before reaching
Debian because that relay does not accept Gmail senders; no duplicate report
was filed. The existing maintainer/owner email remains unchanged.

Takt's project sponsorship is recorded in both project READMEs, the APT site,
debian/upstream/metadata (Funding), README.source, copyright commentary,
changelog and manual pages. This does not assert that Takt is a Debian
Developer or that anyone has agreed to sponsor an archive upload.

## Source packaging

The conventional [debian directory](../debian/) builds five binary packages
and the worker's debug-symbol package from source using debhelper:

- qsm-pve-direct: source-built media worker, Python services and web UI assets;
- qsm-pve-direct-standalone: signalling profile;
- qsm-pve-direct-gateway: explicitly activated optional gateway;
- qsm-pve-direct-lxc: host helper with the matching guest deb built locally;
- qsm-console-guest: container-only desktop/session payload.

The source export omits QEMU sources, lab captures, downloaded debs, old native
PVE patch series and optional third-party patched Sway/SDDM build experiments.
The embedded guest package is generated from the same source during the build.
External Proxmox packages are runtime dependencies, not build dependencies.
Architecture-independent profiles permit a same-source binary NMU of the
architecture-dependent core, while excluding a different source release.

The preview version is 0.4.0+20261020-0exp1, aimed at initial experimental
review. It is separate from the existing stock3 APT feed. It is not an upload
to Debian experimental and is not installed on production nodes.

## Review still required

The source package needs an independent human review and a Debian Developer
willing to sponsor the upload. Maintainer-script integration and install,
upgrade, removal/purge on an actual PVE testbed need review in addition to the
existing stock3 runtime tests: debhelper-generated packaging has different
activation scaffolding. The clean-build/unit tests do not substitute for that.
Admission through NEW remains the Debian archive maintainers' decision.

Debian stable inclusion is a later process; an ITP/RFS and an external review
release do not make the package part of Debian 13. The existing signed APT
repository remains the supported preview installation route for PVE 9.
