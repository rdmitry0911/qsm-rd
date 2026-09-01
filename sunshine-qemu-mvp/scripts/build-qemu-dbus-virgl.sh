#!/usr/bin/env bash
# Build the optional, pinned QEMU fallback used by the q-sunshine integration.
# It does not install packages or replace the system QEMU binary.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH="$ROOT/integration/qemu/patches/0001-ui-add-opt-in-surfaceless-inline-dbus-virgl-fallback.patch"
QEMU_REF="${QEMU_REF:-v8.2.2}"
QEMU_SOURCE_DIR="${QEMU_SOURCE_DIR:-$ROOT/.upstream/qemu-$QEMU_REF}"
QEMU_BUILD_DIR="${QEMU_BUILD_DIR:-$ROOT/.upstream/build-qemu-dbus-virgl}"

for required in git ninja pkg-config; do
    command -v "$required" >/dev/null || {
        echo "missing required command: $required" >&2
        exit 1
    }
done

if [[ ! -f "$PATCH" ]]; then
    echo "missing integration patch: $PATCH" >&2
    exit 1
fi

if [[ ! -e "$QEMU_SOURCE_DIR" ]]; then
    git clone --depth 1 --branch "$QEMU_REF" \
        https://gitlab.com/qemu-project/qemu.git "$QEMU_SOURCE_DIR"
fi

if ! git -C "$QEMU_SOURCE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "QEMU_SOURCE_DIR is not a Git checkout: $QEMU_SOURCE_DIR" >&2
    exit 1
fi
actual_base="$(git -C "$QEMU_SOURCE_DIR" rev-parse HEAD)"
expected_base="11aa0b1ff115b86160c4d37e7c37e6a6b13b77ea"
if [[ "$actual_base" != "$expected_base" ]]; then
    echo "QEMU source must be v8.2.2 ($expected_base), got $actual_base" >&2
    exit 1
fi

if git -C "$QEMU_SOURCE_DIR" diff --quiet &&
   git -C "$QEMU_SOURCE_DIR" diff --cached --quiet; then
    git -C "$QEMU_SOURCE_DIR" apply --check "$PATCH"
    git -C "$QEMU_SOURCE_DIR" apply "$PATCH"
elif git -C "$QEMU_SOURCE_DIR" diff --cached --quiet &&
     git -C "$QEMU_SOURCE_DIR" apply --check --reverse "$PATCH"; then
    # Verify that the only existing local change is precisely this patch.
    # Reversing and immediately restoring it avoids accepting an unrelated
    # dirty QEMU tree on a re-run.
    git -C "$QEMU_SOURCE_DIR" apply --reverse "$PATCH"
    if ! git -C "$QEMU_SOURCE_DIR" diff --quiet; then
        git -C "$QEMU_SOURCE_DIR" apply "$PATCH"
        echo "refusing to alter a QEMU checkout with unrelated changes" >&2
        exit 1
    fi
    git -C "$QEMU_SOURCE_DIR" apply "$PATCH"
else
    echo "QEMU patch neither applies nor matches a clean checkout" >&2
    exit 1
fi

mkdir -p "$QEMU_BUILD_DIR"
(
    cd "$QEMU_BUILD_DIR"
    "$QEMU_SOURCE_DIR/configure" \
        --target-list=x86_64-softmmu \
        --enable-opengl \
        --enable-virglrenderer \
        --enable-dbus-display \
        --enable-slirp \
        --disable-werror \
        --prefix="$QEMU_BUILD_DIR/install"
)
ninja -C "$QEMU_BUILD_DIR" qemu-system-x86_64

printf 'QEMU_DBUS_VIRGL_BUILD_OK binary=%s\n' \
    "$QEMU_BUILD_DIR/qemu-system-x86_64"
