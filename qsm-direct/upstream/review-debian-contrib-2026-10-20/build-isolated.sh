#!/bin/sh
set -eu
case "$1" in /var/tmp/qsm-dev-trixie|/var/tmp/qsm-debian-sid) ;; *) exit 2;; esac
mount --make-rprivate /
mount -t proc proc "$1/proc"
for device in null zero random urandom tty; do
    [ -e "$1/dev/$device" ] || touch "$1/dev/$device"
    mount --bind "/dev/$device" "$1/dev/$device"
done
ip link set lo up
exec chroot "$1" runuser -u qsm-build -- sh -c 'cd "$1"; export DEB_BUILD_OPTIONS=parallel=4; exec dpkg-buildpackage -us -uc "$2" -j4' sh "$2" "${3:--b}"
