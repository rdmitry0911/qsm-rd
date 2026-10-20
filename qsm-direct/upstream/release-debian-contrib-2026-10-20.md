This is the **source-package review preview for Debian contrib**, version
`0.4.0+20261020-0exp1`. It is separate from the stock3 APT installation feed.

Project sponsor: [Takt](https://taktcycles.com/).
Maintainer: Dmitry R. [Debian ITP #1151422](https://bugs.debian.org/1151422)
is registered; a Debian Developer upload sponsor is still needed.

The release contains the upstream `.orig.tar.xz`, Debian packaging tarball,
signed `.dsc`, source `.changes`/`.buildinfo`, export provenance and review
evidence. There are **no binary debs** in this review release. Existing PVE 9
installations should continue to use [stock3 and the signed APT feed](https://rdmitry0911.github.io/qsm-rd/).

The package is proposed for contrib because it needs external `pve-manager`,
`qemu-server`, and optionally `pve-container` at runtime. It builds using
Debian-main dependencies and does not bundle patched PVE packages, proprietary
drivers or prebuilt guest debs. Takt sponsorship does not imply Debian upload
rights, Debian acceptance or an agreed upload sponsor.

The source and packaging were prepared with AI assistance and need independent
human review before a Debian upload. The new debhelper lifecycle integration
also needs PVE installation/upgrade/removal review; unit and build checks do
not replace that. This external source release is not admission to Debian
experimental or contrib.

Validated: unprivileged builds with networking disabled in Debian 13 and
unstable; 19 CTest tests; clean Lintian source/binary checks; passing real
autopkgtest `source-units` on unstable; valid project signatures.
Full evidence is in `review-debian-contrib-20261020.tar.xz`.

Verify/import the included project public certificate before `dget -x`:

```sh
gpg --show-keys --with-fingerprint qsm-archive-keyring.asc
# Primary fingerprint: BA99528182B7EADD0199B6986A09741B1DBEAEF2
install -d -m 0700 "$HOME/.gnupg"
gpg --no-default-keyring --keyring "$HOME/.gnupg/trustedkeys.gpg" --import qsm-archive-keyring.asc
dget -x https://github.com/rdmitry0911/qsm-rd/releases/download/v0.4.0-debian-contrib-20261020-review2/qsm-pve-direct_0.4.0+20261020-0exp1.dsc
```

The signatures identify the QSM project release key, not a Debian Developer.
The `trustedkeys.gpg` import is for developer source verification with
`dget`/`dscverify`, separate from APT's scoped repository key.
`export-provenance.json` records the source commit, file inventory and orig
tarball hash. `SHA256SUMS` and its detached signature cover the release assets.
