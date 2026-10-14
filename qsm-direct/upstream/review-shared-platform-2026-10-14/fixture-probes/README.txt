These are the actual isolated-lab probes used for qualification, not portable
production scripts. They reference nested PVE at192.168.76.1, browser VM101
at .2, GL VM102 at .3, CPU VM106 at .6 and local SSH port12222. Passwords
are read from private files on the browser VM and are never included here.
Browser runner: Playwright, /usr/bin/google-chrome. Fixtures use synthetic
clipboard markers, a 440Hz generated tone and Chrome's fake microphone.

run-media.py coordinates probe-media.cjs with guest xclip/aplay/arecord.
Use a test guest only. Set up Xvfb :99 and spice-vdagentd/client in that
guest, unmute ALSA Master and Capture and provide /tmp/shared-tone.wav.
Stop guest service/socket first; remove its stale run socket before starting
the fixture daemon. Redirect xclip stdout/stderr when invoking it over SSH
so its selection owner does not hold the orchestration stream open.
The Copy toolbar requests a fresh guest selection: change the guest marker
after CLIPBOARD_COPY_WAITING, then check GUEST_CLIPBOARD_MATCH and guest paste.
SPEAKER_RMS must be nonzero; inspect guest-microphone.txt for recorded mic RMS.

run-concurrent.py starts an independent QEMU display probe only after the
browser reports video ready, then checks continued frame decoding. It uses
/root/shared-display-probe, built from tools/qemu_display_probe.cpp.
probe-acl.cjs checks denied offers and authorized policy write/read.
probe-console-only.cjs uses a role with only VM.Console on the GL VM.
The portable two-viewer gate is lab/proxmox9/qualify-pve-direct-embedded-window-e2e.cjs.
