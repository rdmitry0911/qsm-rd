# Pinned Moonlight upstream

The q-sunshine desktop package rebuilds Moonlight rather than embedding a
released Moonlight.app. The native media route requires the explicit
`--qsm-system-auth` marker and an ephemeral GameStream mTLS lease; a stock
Moonlight binary must not be used as a fallback.

| Field | Value |
| --- | --- |
| Upstream | `https://github.com/moonlight-stream/moonlight-qt.git` |
| Revision | `0eff3b9b4dd685e07b15a383519e1972e626c9b3` |
| Subject | `Update CI builds to Qt 6.11.2` |
| Patches | `patches/0001-system-auth-gamestream-lease.patch`, `patches/0002-browser-encoded-video-tap.patch` |
| SHA-256 | `5d49b262ebd2ce138743b621f156acd5667d4643df68cf5e297098aea63b06d8` |

Replay on a fresh checkout:

```bash
git clone --recursive https://github.com/moonlight-stream/moonlight-qt.git moonlight-qt
cd moonlight-qt
git checkout --detach 0eff3b9b4dd685e07b15a383519e1972e626c9b3
git submodule update --init --recursive
git apply --check --index ../q-sunshine/integration/moonlight/patches/0001-system-auth-gamestream-lease.patch
git apply --index ../q-sunshine/integration/moonlight/patches/0001-system-auth-gamestream-lease.patch
git apply --check ../q-sunshine/integration/moonlight/patches/0002-browser-encoded-video-tap.patch
git apply ../q-sunshine/integration/moonlight/patches/0002-browser-encoded-video-tap.patch
git diff --check
```

`--index` verifies the recorded preimage blob IDs and, together with the
detached revision check, prevents replay on a merely similar upstream tree.
The tracked upstream changes are encoded as a Git binary delta so their exact
preimages are checked; the two new source files remain readable unified diffs.
Do not replace these commands with a fuzzy `patch` invocation.
