#!/usr/bin/env bash
# Build a private libva compatible with Sunshine's pinned FFmpeg binaries.
# This never installs into /usr and never changes the system libva loader path.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LIBVA_TAG="${LIBVA_TAG:-2.21.0}"
LIBVA_COMMIT="${LIBVA_COMMIT:-0b01aed44ef1a6ad660261284ff266fa812829ef}"
LIBVA_SOURCE_DIR="${LIBVA_SOURCE_DIR:-$ROOT_DIR/.upstream/libva-$LIBVA_TAG}"
LIBVA_BUILD_DIR="${LIBVA_BUILD_DIR:-$ROOT_DIR/.upstream/build-libva-$LIBVA_TAG}"
LIBVA_PREFIX="${LIBVA_PREFIX:-$ROOT_DIR/.upstream/prefix-libva-$LIBVA_TAG}"

for required in git meson ninja pkg-config; do
    command -v "$required" >/dev/null || {
        echo "missing required command: $required" >&2
        exit 1
    }
done

if [[ ! -e "$LIBVA_SOURCE_DIR" ]]; then
    git clone --depth 1 --branch "$LIBVA_TAG" \
        https://github.com/intel/libva.git "$LIBVA_SOURCE_DIR"
fi

if ! git -C "$LIBVA_SOURCE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "LIBVA_SOURCE_DIR is not a Git checkout: $LIBVA_SOURCE_DIR" >&2
    exit 1
fi
if ! git -C "$LIBVA_SOURCE_DIR" diff --quiet ||
   ! git -C "$LIBVA_SOURCE_DIR" diff --cached --quiet; then
    echo "refusing to alter a libva checkout with local changes: $LIBVA_SOURCE_DIR" >&2
    exit 1
fi

# The release tag is annotated.  Validate its peeled commit so a moved local
# tag cannot silently alter the ABI selected for Sunshine's bundled FFmpeg.
git -C "$LIBVA_SOURCE_DIR" fetch --depth 1 origin \
    "refs/tags/$LIBVA_TAG:refs/tags/$LIBVA_TAG"
tag_commit="$(git -C "$LIBVA_SOURCE_DIR" rev-parse "refs/tags/$LIBVA_TAG^{}")"
if [[ "$tag_commit" != "$LIBVA_COMMIT" ]]; then
    echo "libva tag $LIBVA_TAG expected $LIBVA_COMMIT, got $tag_commit" >&2
    exit 1
fi
if [[ "$(git -C "$LIBVA_SOURCE_DIR" rev-parse HEAD)" != "$LIBVA_COMMIT" ]]; then
    git -C "$LIBVA_SOURCE_DIR" checkout --detach "$LIBVA_COMMIT"
fi

if [[ -e "$LIBVA_BUILD_DIR" && ! -e "$LIBVA_BUILD_DIR/meson-private/coredata.dat" ]]; then
    echo "LIBVA_BUILD_DIR exists but is not a Meson build tree: $LIBVA_BUILD_DIR" >&2
    exit 1
fi
if [[ -e "$LIBVA_BUILD_DIR/meson-private/coredata.dat" ]]; then
    meson configure "$LIBVA_BUILD_DIR" --prefix "$LIBVA_PREFIX" --libdir lib
else
    meson setup "$LIBVA_BUILD_DIR" "$LIBVA_SOURCE_DIR" \
        --prefix "$LIBVA_PREFIX" --libdir lib --buildtype release
fi
meson compile -C "$LIBVA_BUILD_DIR" -j "${JOBS:-2}"
meson install -C "$LIBVA_BUILD_DIR"

LIBVA_LIBRARY_DIR="$LIBVA_PREFIX/lib"
if [[ ! -r "$LIBVA_LIBRARY_DIR/libva.so.2" ]]; then
    echo "isolated libva did not produce $LIBVA_LIBRARY_DIR/libva.so.2" >&2
    exit 1
fi

printf 'ISOLATED_LIBVA_OK tag=%s commit=%s prefix=%s library_dir=%s\n' \
    "$LIBVA_TAG" "$LIBVA_COMMIT" "$LIBVA_PREFIX" "$LIBVA_LIBRARY_DIR"
printf 'Use for a Sunshine build only: -DCMAKE_EXE_LINKER_FLAGS:STRING=%q\n' \
    "-L$LIBVA_LIBRARY_DIR -Wl,-rpath,$LIBVA_LIBRARY_DIR"
