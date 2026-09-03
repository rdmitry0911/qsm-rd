# macOS Tahoe client package

Run `./packaging/macos/build-tahoe-dmg.sh` on macOS Tahoe after installing
Xcode command-line tools, CMake, Ninja, Git, Python 3, Qt 6 (including
`qmake` and `macdeployqt`), and the normal Moonlight build prerequisites.
The builder downloads the exact pinned Moonlight source revision, verifies and
applies [`integration/moonlight/patches/0001-system-auth-gamestream-lease.patch`](../../integration/moonlight/patches/0001-system-auth-gamestream-lease.patch), builds it locally, and embeds that
`Moonlight.app` in the q-sunshine bundle.

It deliberately does not accept `QSUNSHINE_MOONLIGHT_APP` and never embeds an
installed/stock Moonlight application.  The desktop launcher always passes
`--qsm-system-auth`; a stock binary must fail rather than silently falling
back to PIN pairing. At runtime the launcher resolves only the exact signed
`q-sunshine-client.app/Contents/Resources/Moonlight.app/Contents/MacOS/Moonlight`
child; it has no `PATH` or per-user-settings executable fallback.

```bash
QSUNSHINE_QT_PREFIX="$(brew --prefix qtbase)" \
./packaging/macos/build-tahoe-dmg.sh
```

`qtbase` is the current Homebrew formula exposing `macdeployqt`; a legacy
`qt@6` or `qt` prefix also works when supplied explicitly.  Moonlight's own
`setup-deps.py` obtains its upstream pinned universal macOS dependency bundle.

The builder verifies every requested architecture in the q-sunshine launcher,
the freshly built patched Moonlight binary, and the final embedded binary. An
Intel Mac creates an `x86_64` package by default. Use a universal Qt install
with `QSUNSHINE_MACOS_ARCHS='x86_64;arm64'` to produce a universal package.

The output is an ad-hoc signed DMG by default. Set
`QSUNSHINE_MACOS_CODESIGN_IDENTITY` to a Developer ID identity for distribution
signing. Notarization remains separate because it requires the distributor's
Apple credentials. The build refuses a non-Tahoe host unless
`QSUNSHINE_MACOS_ALLOW_OTHER=1` is set for development, and defaults to
`QSUNSHINE_MACOS_DEPLOYMENT_TARGET=26.0`.

## Opening a Proxmox VM launch

The bundle registers the local `.qsm` document type with LaunchServices. From
the Proxmox VM Console menu, download the one-use launch file and double-click
it in Finder; q-sunshine receives the file-open event and redeems it over the
descriptor-pinned TLS connection. No Proxmox password, PIN, media endpoint,
or CA path is entered in the client.

For a shell invocation, use the bundle's receiver explicitly:

```bash
/Applications/qsunshine-client.app/Contents/MacOS/qsunshine-client \
  --launch-file "$HOME/Downloads/vm-100.qsm"
```

The launch file is a short-lived one-use authorization. Do not copy it into a
profile or pass it through a URL scheme; the bundle intentionally declares no
custom scheme. On Linux builds that install the Qt client, the accompanying
desktop/MIME metadata runs the same required `--launch-file %f` invocation for
`application/x-q-sunshine-launch` files.

For an audited mirror, set `QSUNSHINE_MOONLIGHT_GIT_URL`; the builder still
requires the exact pinned commit and applies the canonical patch with
`git apply --index`, so a different source revision cannot be
accepted accidentally.

The release-consumer order for installing a verified DMG, opening a Proxmox
Console-generated launch file, checking QSF features, uninstalling, and
handling Developer ID/notarization caveats is in
[`docs/RELEASE_NOTES.md`](../../docs/RELEASE_NOTES.md). That document does not
claim that a particular DMG has been published or notarized.
