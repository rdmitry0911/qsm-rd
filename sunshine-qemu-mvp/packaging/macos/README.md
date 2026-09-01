# macOS Tahoe client package

Run `./packaging/macos/build-tahoe-dmg.sh` on the target macOS machine after
installing CMake, Ninja, Qt 6 with `macdeployqt`, and the normal signed
Moonlight.app.  The script accepts the following optional variables:

```bash
QSUNSHINE_QT_PREFIX="$(brew --prefix qtbase)" \
QSUNSHINE_MOONLIGHT_APP=/Applications/Moonlight.app \
./packaging/macos/build-tahoe-dmg.sh
```

`qtbase` is the current Homebrew formula exposing `macdeployqt`; a legacy
`qt@6` or `qt` prefix also works when supplied explicitly.

It builds the q-sunshine Qt shell, deploys its Qt/QML frameworks, embeds an
unmodified Moonlight.app under `Contents/Resources`, and creates an ad-hoc
signed DMG by default.  `QSUNSHINE_MACOS_CODESIGN_IDENTITY` can name a
Developer ID identity for distribution signing.  Notarization is intentionally
separate because it requires the distributor's Apple credentials.

The builder verifies every requested architecture in both the q-sunshine
launcher and embedded Moonlight before producing the DMG.  An Intel Mac creates
an `x86_64` package by default; use a universal Qt and universal Moonlight with
`QSUNSHINE_MACOS_ARCHS='x86_64;arm64'` to make a universal distribution.

The bundle defaults to `QSUNSHINE_MACOS_DEPLOYMENT_TARGET=26.0`, so a build on
a newer Tahoe point release does not silently require that newer point release.
It accepts only a Tahoe `26.x` deployment target.  The Qt target includes the
profile-negotiation coordinator; no Moonlight fork or additional client runtime
is packaged for that feature.
