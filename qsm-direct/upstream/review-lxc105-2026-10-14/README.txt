LXC105 qualification on192.168.64.20,2026-10-14

The container was stopped when this check began; it was started to test the
requested console and left running for the user's checks. QSM was already
configured. Both display/login services are active, both console slots are
on the graphical login screen. No failed guest services. NVIDIA RTX3080 is
available in the container. Host QSM/LXC0.4.0+shared6; existing guest package
0.4.0+git176.direct, whose installed functionality was tested directly.

Actual authenticated PVE UI in Firefox: select lxc/105, open the top Console
split menu, choose QSM Direct. This uses the existing TLS signalling service
on8007 and retains PVE ticket/VM.Console authorization. The ordinary left
Console tab remains the terminal. The same mDNS/ICE fix is used by this path.

Graphical login stream1280x726, WebRTC connected, 1185 decoded video frames
in the first20s sample. Separate input check: one pointer move and one Shift
press/release, then20s video;1366 decoded frames. Host capture reported
pointer_events=1,key_events=2,errors=0,display_failed=no,capture_failures=0.
The test made no guest login and entered no text/password; desktop audio and
clipboard need a logged-in desktop and are not qualified by this check.
The browser test context was closed and the console slot released. Both
slots remain available for the user. VM103 was left running.

The fixture uses the disposable Firefox155/OpenH264 test profile documented
in review-firefox-2026-10-14, a temporary locally signed PVE ticket and existing
SSH authorization. It prints no credentials, SDP or guest screen pixels.
No guest credentials or browser session are bundled.
