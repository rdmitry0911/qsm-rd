#!/usr/bin/env bash
# Install only the packages needed by the CPU/TCG real-QEMU E2E lane.
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
  apt=(apt-get)
else
  command -v sudo >/dev/null || {
    echo "run as root or install sudo before invoking this script" >&2
    exit 1
  }
  apt=(sudo apt-get)
fi

if [[ ! -r /etc/os-release ]]; then
  echo "cannot identify the package manager distribution" >&2
  exit 1
fi
. /etc/os-release
if [[ ${ID:-} != debian && ${ID:-} != ubuntu && ${ID_LIKE:-} != *debian* ]]; then
  echo "this installer supports Debian/Ubuntu-family systems only (found ${ID:-unknown})" >&2
  exit 1
fi

# ui-dbus.so and audio-dbus.so are supplied by this split package on both
# current Debian and Ubuntu.  It is substantially smaller than a GTK/SDL GUI
# install and is all that the headless test needs.
packages=(
  qemu-system-x86
  qemu-system-modules-opengl
  qemu-utils
  seabios
  dbus-daemon
  binutils
  cmake
  ninja-build
  ffmpeg
)

"${apt[@]}" update
"${apt[@]}" install -y --no-install-recommends "${packages[@]}"

command -v qemu-system-x86_64 >/dev/null
qemu-system-x86_64 -display help | grep -Fxq dbus
qemu-system-x86_64 -audiodev help | grep -Fxq dbus

printf 'QEMU_INSTALL_OK qemu=%s\n' "$(qemu-system-x86_64 --version | head -n1)"
