# Moonlight → Sunshine → QEMU decoded-audio E2E

`scripts/run-moonlight-sunshine-qemu-audio-e2e.sh` qualifies the real
GameStream audio route without a host sound server:

```text
KVM BIOS PC-speaker 1 kHz tone
  -> QEMU `-audiodev dbus` / Display1 AudioOutListener
  -> patched Sunshine `capture=qemu_dbus`, bounded PCM FIFO, Opus
  -> Moonlight Embedded SDL/Opus decode
  -> SDL disk driver raw S16LE evidence
```

Xvfb exists only to give Moonlight's SDL video platform a disposable window.
QEMU uses the private session D-Bus audiodev, Sunshine explicitly selects
`audio_sink=qemu-dbus`, and Moonlight writes decoded samples with
`SDL_AUDIODRIVER=disk`; PulseAudio, PipeWire, ALSA hardware, and an X11 audio
bridge are neither inputs to the test nor linked into the QEMU guest-audio-only
Sunshine build.

## Reproducible build and run

From `sunshine-qemu-mvp`, first build the isolated ABI-compatible libva and
the patched pinned Sunshine worktree.  The build helper sets both the compiled
assets prefix and an rpath to the isolated prefix; it does not replace system
libva.

```bash
./scripts/build-isolated-libva-2.21.sh
./scripts/build-upstream-sunshine-qemu.sh

MOONLIGHT_BINARY="$PWD/.upstream/build-moonlight-embedded/moonlight" \
STREAM_SECONDS=14 QEMU_ACCEL=kvm \
./scripts/run-moonlight-sunshine-qemu-audio-e2e.sh
```

To reproduce the strict deployment trace rather than the helper's default
build-directory name, build and select the same explicit artifact:

```bash
SUNSHINE_BUILD_DIR="$PWD/.upstream/build-sunshine-qemu-no-x11" \
  ./scripts/build-upstream-sunshine-qemu.sh
SUNSHINE_BINARY="$PWD/.upstream/build-sunshine-qemu-no-x11/sunshine" \
MOONLIGHT_BINARY="$PWD/.upstream/build-moonlight-embedded/moonlight" \
STREAM_SECONDS=14 QEMU_ACCEL=kvm \
  ./scripts/run-moonlight-sunshine-qemu-audio-e2e.sh
```

The default Sunshine binary is
`.upstream/build-sunshine-qemu/sunshine`, produced by the helper.  Override
`SUNSHINE_BINARY`, `MOONLIGHT_BINARY`, or `OUTPUT_DIR` only when intentionally
testing another explicitly built artifact.  The runner requires QEMU's `dbus`
display and audio backends plus KVM access through group `kvm`; `QEMU_ACCEL=tcg`
is available for diagnosis but is not the default qualification.

The helper's final gate checks both direct ELF entries and the full `ldd`
closure: it rejects X11, Wayland, PulseAudio, and ALSA. Xvfb therefore remains
strictly client-test-only, not a Sunshine or guest-audio dependency.

Do not run another Sunshine GameStream session concurrently: Sunshine uses
fixed RTP ports around `48200`, so changing only its HTTP base port does not
isolate simultaneous local runs.

## Passing criteria

Each invocation generates fresh pairing keys, Sunshine state, a session D-Bus,
and a 512-byte BIOS floppy fixture.  It passes only if all of these hold:

- the guest writes `AUDIO_TONE_ON` to QEMU debugcon;
- Moonlight completes pairing, RTSP, video startup, and receives a video RTP
  packet, establishing a genuine GameStream session rather than a standalone
  audio listener test;
- Sunshine logs `AudioOutListener` registration, compatible 48 kHz stereo
  PCM initialization, accepted guest PCM, and its Opus encoder startup;
- Moonlight logs `Received first audio packet` and uses SDL's disk audio
  driver;
- the retained `moonlight-decoded.s16le` is whole 48 kHz stereo S16LE frames,
  contains at least one second of samples, and `ffmpeg volumedetect` reports
  finite non-silent peak/RMS values (peak > -45 dB and mean > -55 dB).

This final check is over Moonlight's decoded PCM, not a QEMU write count or a
Sunshine audio callback.  Continuous-silence fallback packets therefore do
not qualify.

## Recorded KVM evidence

The latest strict no-X11/no-Pulse local run is retained at:

```text
artifacts/validation/moonlight-sunshine-qemu-audio-e2e/run.gki7t0/
```

It used `.upstream/build-sunshine-qemu-no-x11/sunshine` (SHA-256
`80d22a83a552f65cbbfa594b2a4ce2a02180ba13571a43558797e8ec83e9a920`).
Its `trace.txt` records `host_audio_dependency=none`, QEMU/KVM's BIOS
PC-speaker → Display1 AudioOutListener → Sunshine Opus → Moonlight SDL disk
route, and `Received first audio packet after 400 ms`.

Its `audio-verdict.txt` records:

```text
decoded_pcm_format=s16le/48000Hz/2ch
decoded_pcm_bytes=2670592
decoded_pcm_seconds=13.909
decoded_pcm_max_volume_db=0.0
decoded_pcm_mean_volume_db=-3.3
decoded_pcm_non_silent=yes
```

`sunshine.log` shows listener registration, `48000 Hz, 2 channels, 16-bit
integer`, accepted PCM, and Opus initialization.  `moonlight-stream.log`
shows the SDL disk driver and first audio packet after 400 ms.  `trace.txt`
records the exact QEMU, Sunshine, Moonlight, configuration, and transport
chain; the raw PCM SHA-256 is
`f6c65b44678259d83d2c465c294ee3569986c96c7bce06fbbe0d5e9a0211e915`.

## Scope limits

The fixture proves stereo 48 kHz output and a non-silent decoded client
artifact.  It does not assess subjective quality, A/V synchronization,
microphone return, surround layouts, reconnect/soak behavior, or a guest OS
desktop's normal audio stack.  It also does not add clipboard or file transfer
to stock GameStream; those require the separate QSF companion protocol.
