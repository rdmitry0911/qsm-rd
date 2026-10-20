#!/bin/sh
set -eu
root="$1"
shift
case "$root" in /var/tmp/qsm-dev-trixie|/var/tmp/qsm-debian-sid) ;; *) exit 2;; esac
mount --make-rprivate /
mount -t proc proc "$root/proc"
for device in null zero random urandom tty; do
    [ -e "$root/dev/$device" ] || touch "$root/dev/$device"
    mount --bind "/dev/$device" "$root/dev/$device"
done
ip link set lo up
exec chroot "$root" runuser -u qsm-build -- "$@"
