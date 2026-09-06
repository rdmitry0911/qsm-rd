# Proxmox VE upstream RFC preparation

This directory is a prepared RFC, not an upstream pull request. Proxmox VE
does not accept GitHub pull requests for its own repositories. Contributors
coordinate design on `pve-devel@lists.proxmox.com` and then send a signed
patch series from a branch based on the relevant `git.proxmox.com` repository.

The review baseline for this RFC is `pve-manager` commit `2db9c2ab`
(`bump version to 9.2.17`). The current console path is deliberately small and
centralised:

- `www/manager6/button/ConsoleButton.js` owns the Console split-button menu.
- `www/manager6/Utils.js` maps `html5`, `vv`, and `xtermjs` to viewers.
- `www/manager6/qemu/Config.js` enables menu choices by VM state.
- `PVE/API2.pm` constrains the default viewer preference to those choices.

QSM Direct currently requires an out-of-tree node service, a protected media
endpoint, and a guest/display contract. It must **not** be proposed upstream
as a hard-coded `qsm` option or by carrying a third-party transport into
`pve-manager`. The first upstreamable question is whether PVE wants a generic,
reviewed browser-console provider contract at all.

`RFC_CONSOLE_PROVIDER.md` is ready to paste into a new message to `pve-devel`.
It requests architectural direction before implementation. Do not send a code
series until maintainers answer that request.

Once an approach is agreed, prepare each accepted scope in a clean
`pve-manager` checkout:

```bash
git switch -c rfc-console-provider origin/master
# make small, self-contained commits; each commit includes -s
git format-patch -s -o patches/ \
  --subject-prefix='PATCH pve-manager' --cover-letter origin/master..HEAD
git send-email --to=pve-devel@lists.proxmox.com patches/00*.patch
```

A valid Proxmox CLA is required before inclusion. Do not send this RFC or a
patch series from the product build repository. Use the upstream checkout so
the base, diff, sign-off, and test results are unambiguous.
