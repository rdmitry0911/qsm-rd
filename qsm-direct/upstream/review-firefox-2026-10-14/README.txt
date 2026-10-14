Firefox regression qualification on 192.168.64.20, VM103

Before: Firefox supplied10 mDNS candidates; none resolved. aioice received
end-of-candidates, pruned its only ICE component and gathered zero server
addresses. The response had no candidates. Video capture/encoding continued,
but ICE never established. This reproduced the reported failure locally;
the user's connection also crosses VPN and mDNS need not cross that route.

After: QSM keeps remote discovery open for offers with mDNS names. The
browser contacts a numeric server candidate; authenticated incoming STUN
learns the actual endpoint (including NAT). DTLS fingerprint validation and
VM.Console authorization are unchanged. The existing20s negotiation lease
bounds an absent peer. No browser/VPN policy, guessed client address, TURN
service or firewall change is required by this fix.

Reconnect button explicitly accepts pointer events inside the click-through
control panel. A normal browser click, followed by decoded video, was checked.

Unit regression hides all candidate addresses behind unresolved .local names
and verifies actual ICE/DTLS and opening a data channel. Bridge21 tests and
UI lifecycle/ICE tests pass. Native authenticated HTTP checks use a temporary
root PVE ticket minted locally through already authorized SSH; neither the
ticket nor SDP is printed or saved by the runner.
Firefox155's isolated automation profile was supplied Cisco OpenH2642.6.0
from the manifest bundled in Mozilla's browser, with SHA512 validation. This
adds codec support to that disposable test profile, not to the user's browser.

Firefox native HTTP: before response0 candidates/no video; after response3
candidates, connected, 1805 decoded frames over32s. The runner is a separate
LAN client; the actual user's VPN connection still requires their own retest.
Code commit48b16b4; deployed packages qsm-pve-direct and LXC0.4.0+shared6.
The earlier shared4 update fixed the shutdown message but did not solve this
mDNS failure. Original shared3 build/test records remain historical.

The native PVE Console tab was also tested in Firefox using the authenticated
PVE UI, selecting VM103 through its resource tree. Embedded video826x434
arrived. Closing only that WebRTC peer produced Reconnect; a normal click
reloaded the frame and restored a connected video stream for another12s.

Fixture prerequisites: Python Playwright, cached Firefox155 (v1543), SSH
root access to the already-authorized node, and Python on the browser runner.
Run prepare-firefox-codec.py, then qsm-firefox-layout-probe.py for a separate
window or qsm-firefox-embedded-reconnect.py for the actual PVE Console tab.
These fixture scripts target this exact node and VM; adapt them for another
lab. Passwords, PVE tickets, SDP and user clipboard contents are not bundled.
