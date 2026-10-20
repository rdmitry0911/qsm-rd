#!/usr/bin/python3
"""Install source-built payloads into debhelper's per-package staging roots."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging/debian"
ACTIVE = set(subprocess.check_output(["dh_listpackages"], text=True).split())


def install(package, source, destination, executable=False):
    if package not in ACTIVE:
        return
    path = ROOT / "debian" / package / destination.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / source, path)
    path.chmod(0o755 if executable else 0o644)


def doc(package, source, name="README.Debian"):
    install(package, source, f"usr/share/doc/{package}/{name}")


def main(build, version):
    core = "qsm-pve-direct"
    lib = "usr/lib/qsm-pve-direct"
    install(core, f"{build}/qsm-direct-media-worker", f"{lib}/bin/qsm-direct-media-worker", True)
    modules = {
        "browser_bridge": ("__init__.py", "qsm_browser_bridge.py"),
        "direct_guest": ("__init__.py", "qsm_guest_channel.py"),
        "direct_terminal": ("qsm_direct_terminal.py", "qsm_direct_encoder_probe.py", "qsm_shared_migration.py"),
        "signal": ("__init__.py", "qsm_direct_signal.py", "qsm_direct_endpoint.py", "qsm_direct_endpoint_cli.py", "qsm_gateway.py"),
    }
    for directory, names in modules.items():
        destination = "signal_service" if directory == "signal" else directory
        for name in names:
            install(core, f"extensions/{directory}/{name}", f"{lib}/{destination}/{name}")
    install(core, "extensions/signal/qsm_gateway.py", f"{lib}/browser_bridge/qsm_gateway.py")
    install(core, "extensions/signal/qsm_direct_endpoint.py", f"{lib}/pve9-ui/qsm_direct_endpoint.py")
    for name, directory in (("qsm-pve-direct-terminal", "usr/bin"),
                            ("qsm-pve-direct-signal", "usr/bin"),
                            ("qsm-pve-direct-ui", "usr/sbin"),
                            ("qsm-pve-direct-endpoint", "usr/sbin"),
                            ("qsm-pve-direct-migrate", "usr/sbin")):
        install(core, f"packaging/debian/{name}", f"{directory}/{name}", True)
    install(core, "packaging/debian/qsm-pve-direct-cert-reload", f"{lib}/bin/qsm-direct-cert-reload", True)
    for name in ("terminal.service", "signal.service", "cert-reload.service", "cert-reload.timer"):
        name = f"qsm-pve-direct-{name}"
        install(core, f"packaging/debian/{name}", f"usr/lib/systemd/system/{name}")
    ui = "integration/proxmox/pve9/direct_ui"
    install(core, f"{ui}/qsm_pve_direct_ui.py", f"{lib}/pve9-ui/qsm_pve_direct_ui.py", True)
    install(core, f"{ui}/pve-manager-index-template.sha256",
            "usr/share/qsm-pve-direct/pve9-ui/pve-manager-index-template.sha256")
    for destination in ("usr/share/pve-manager/js/qsm-pve-direct-console.js",
                        "usr/share/pve-webrtc-console/qsm-direct-console.js"):
        install(core, f"{ui}/qsm-direct-console.js", destination)
    install(core, "packaging/webrtc-viewer/index.html.tpl", "usr/share/pve-webrtc-console/index.html.tpl")
    doc(core, "packaging/debian/README.qsm-pve-direct")
    doc("qsm-pve-direct-standalone", "packaging/debian/README.standalone")

    gateway = "qsm-pve-direct-gateway"
    install(gateway, "extensions/gateway/qsm_direct_gateway.py", "usr/sbin/qsm-pve-direct-gateway", True)
    for name in ("gateway.service", "turn.service", "gateway-certs.service", "gateway-certs.timer"):
        name = f"qsm-pve-direct-{name}"
        install(gateway, f"packaging/debian/{name}", f"usr/lib/systemd/system/{name}")
    doc(gateway, "packaging/debian/README.gateway")

    helper = "qsm-pve-direct-lxc"
    install(helper, "packaging/debian/qsm-pve-direct-lxc", "usr/sbin/qsm-pve-direct-lxc", True)
    install(helper, "packaging/debian/qsm-native-vkms", f"{lib}/qsm-native-vkms", True)
    install(helper, "packaging/debian/qsm-native-vkms.service", "usr/lib/systemd/system/qsm-native-vkms.service")
    doc(helper, "packaging/lxc/guest/README", "README.guest")
    doc(helper, "packaging/lxc/README.native-sddm", "README.native-sddm")
    if helper in ACTIVE:
        (ROOT / "debian" / helper / "usr/share/qsm-pve-direct-lxc").mkdir(parents=True, exist_ok=True)

    guest = "qsm-console-guest"
    for name in ("qsm-login", "qsm-greeter", "qsm-session-launch", "qsm-seat-keeper", "qsm-consoles",
                 "qsm-display-start", "qsm-clipboard-agent", "qsm-native-seat", "qsm-native-session", "qsm-native-profile"):
        install(guest, f"packaging/lxc/{name}", f"usr/lib/qsm-console/{name}", True)
    install(guest, "packaging/lxc/qsm_native_keyboard.py", "usr/lib/qsm-console/qsm_native_keyboard.py")
    install(guest, "packaging/lxc/qsm-display-sway.conf", "usr/share/qsm-console/sway.conf")
    for name in ("qsm-display@.service", "qsm-seat-keeper@.service", "qsm-login@.service", "qsm-consoles.service",
                 "qsm-native-seat-udev.service", "qsm-native-seat-control@.service", "qsm-native-sddm.service"):
        install(guest, f"packaging/lxc/guest/{name}", f"usr/lib/systemd/system/{name}")
    install(guest, "packaging/lxc/guest/qsm-nested.conf",
            "usr/lib/systemd/user/plasma-kwin_wayland.service.d/qsm-nested.conf")
    for name in ("qsm-login", "qsm-direct-session"):
        install(guest, f"packaging/lxc/guest/pam-{name}", f"etc/pam.d/{name}")
    doc(guest, "packaging/lxc/guest/README")
    for name in ("README.native-sway", "README.native-sddm"):
        doc(guest, f"packaging/lxc/{name}", name)


if __name__ == "__main__":
    main(*sys.argv[1:])
