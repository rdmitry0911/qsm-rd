LXC105 QSM Direct menu recovery, 2026-10-14

Installed on192.168.64.20: qsm-pve-direct and qsm-pve-direct-lxc
0.4.0+shared7. CT105 and VM103 remain running; neither was restarted.

The old one-shot availability request silently disabled the menu after a
TLS/network/auth failure. shared7 retries when the menu reopens, offers a
credential-free TLS connection check, explains authorization/configuration
failures, bounds GET requests to10 seconds and guards destroyed views.
No console is enabled until the existing VM.Console-authorized check passes.

Behavior regression tests cover late rendering, network failure, coalesced
retries, permission denial, recovery and navigation during a pending probe.

The browser runner uses real Firefox and real authenticated HTTP on PVE
8006 and QSM8007. Its initial settings fetch is deliberately aborted; later
requests go to the real service. TLS errors are accepted in the disposable
runner. This tests recovery behavior and LAN media, not the user's VPN or
certificate exceptions. Tickets remain in memory and are not printed.
The user has been asked to inspect8007; the exact client-side failure is
not confirmed.8006 and8007 serve identical node certificates.

Installed-package Firefox result: first aborted GET leaves QSM Direct
unavailable and exposes the connection-check hint. Opening the menu sends
a new authenticated GET:200. The item enables without page reload; normal
menu click sends a real SDP POST:200 and decodes1280x726 H.264 video with
ICE/peer connected. No guest credentials were entered or input sent.
The connection-check URL was separately observed to open the service root
without credentials during the first diagnostic pass.

Implementation commit: e7dcb23; consumer patch:0004-*.patch.
