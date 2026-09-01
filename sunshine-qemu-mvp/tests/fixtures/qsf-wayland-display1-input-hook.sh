#!/usr/bin/env bash
# Diagnostic post-agent hook for the native VirGL + Weston fixture.  This
# exercises QEMU Display1 input directly; it is deliberately not a Moonlight
# or Sunshine client test.
set -euo pipefail

: "${QSF_WAYLAND_DBUS_ADDRESS:?missing private Display1 bus address}"
: "${QSF_WAYLAND_DBUS_DESTINATION:?missing Display1 destination}"

destination="$QSF_WAYLAND_DBUS_DESTINATION"
console=/org/qemu/Display1/Console_0
keyboard=org.qemu.Display1.Keyboard
mouse=org.qemu.Display1.Mouse
busctl_args=(--address="$QSF_WAYLAND_DBUS_ADDRESS" --no-pager)

# Display1 names these numbers as QEMU xtkbd key numbers.  30 is the normal
# XT make code for A, and QEMU maps it to Linux KEY_A in the guest.
busctl "${busctl_args[@]}" get-property "$destination" "$console" "$mouse" IsAbsolute \
  | grep -Fqx 'b true'
busctl "${busctl_args[@]}" call "$destination" "$console" "$keyboard" Press u 30
busctl "${busctl_args[@]}" call "$destination" "$console" "$keyboard" Release u 30
busctl "${busctl_args[@]}" call "$destination" "$console" "$mouse" SetAbsPosition uu 240 180
busctl "${busctl_args[@]}" call "$destination" "$console" "$mouse" Press u 0
busctl "${busctl_args[@]}" call "$destination" "$console" "$mouse" Release u 0

printf '%s\n' 'QSF_WAYLAND_DISPLAY1_DIRECT_INPUT_HOOK_OK'
