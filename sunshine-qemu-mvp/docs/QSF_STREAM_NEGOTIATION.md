# QSF negotiated stream profile

`connection_optimize` keeps four different responsibilities separate. In this
document, the **client** is the machine running Qt/Moonlight; the **guest** is
the QEMU VirGL desktop. They are not interchangeable capability sources.

| Participant | Authoritative information |
| --- | --- |
| Qt/Moonlight client | The size selected by the user, selected display refresh ceiling, and verified hardware/software decoder codecs. |
| Sunshine host | Codecs which its live encoder probe accepted, plus a separately configured, tested encoder resolution/FPS/bitrate envelope. |
| QEMU/VirGL guest | The virtual display ceiling and the actual compositor scanout which it can apply. This is the guest screen capability, not a host encoder capability. |
| QEMU Display1 | A preferred virtual mode request only; `Console.SetUIInfo` alone is not a scanout acknowledgement. |

The user-selected `WIDTHxHEIGHT` is the requested guest scanout.  If the
intersection cannot represent it, the broker preserves the selected aspect
ratio and returns the lower resolved profile to the client.  It never chooses
a screen size from the host GPU or an NVIDIA device node.

## Safe client lifecycle

A QSF display profile changes the same guest scanout from which Sunshine is
capturing. It must not be negotiated beneath a live old stream. The Qt shell
allows **Choose optimal stream profile** only while a visible Moonlight stream
has a verified normal QSF session, then performs this handoff:

1. End normal QSF and cancel its clipboard, file-transfer, and diagnostic
   resize requests.
2. Stop the visible Moonlight process, wait for it to exit, and wait a bounded
   host capture-retirement interval before touching the guest mode.
3. Establish a temporary TLS 1.3 mTLS **profile-only** lease. It can perform
   only `connection_optimize`; it cannot move clipboard or file data or submit
   a standalone resize.
4. Complete the broker/guest transaction below. On success, close that
   temporary lease before launching a fresh Moonlight process with the resolved
   profile.
5. After replacement video is visibly verified, explicitly activate a new
   normal QSF session for clipboard and files.

After the Qt client has queued the encrypted `connection_optimize` request,
the broker/gateway may already be executing a state-changing QEMU/guest
transaction. A user cancellation at that point is recorded but keeps the
profile-only lease, stopped Moonlight process, and locked connection controls
in place until the authoritative terminal reply arrives; a successful reply
then closes the lease without launching a new graphics process. If transport
failure loses that reply, the client closes its local lease and retains a
90-second remote-settlement guard before allowing a manual reconnect. This is
intentional: aborting a local TLS socket cannot cancel an already-running
gateway worker.

The coordinator synchronously writes a durable record for the current
Moonlight desktop profile immediately before the encrypted request enters
`QSslSocket`. Thus an application exit or crash cannot turn the next startup
into a bypass: construction restores that profile's record before QML can
enable controls and blocks normal Moonlight/QSF admission for a conservative
180-second crash-recovery deadline. Terminal replies and proven settlement
remove the record; expiry removes it synchronously. The record is keyed by the
normalized profile identifier's SHA-256, not by a mutable host name or the QSF
client's startup-default profile.
While this handoff/recovery gate is live, the client also rejects public
desktop-profile selection/saving, Moonlight executable and decoder changes,
and pairing. These are not merely disabled QML buttons: the controller keeps
the captured launch target immutable so an in-process caller cannot switch to
a clean profile after a marker was restored or mutate a profile beneath an
in-flight guest acknowledgement.
The public writable QSF `sessionActive` property is covered too: both an
activation and a deactivation are status-only refusals while the guard is
live. Only the coordinator's private lease teardown can close the mTLS socket,
so a direct `sessionActive = false` cannot discard the authoritative reply
after `connection_optimize` has reached the broker.

`fullscreen` is passed to Moonlight as a client-presentation mode. It can be
used with a selected guest profile, but it is not evidence of a guest scanout
change and does not bypass this lifecycle.

## Transaction

1. The Qt client sends its selected size, display refresh ceiling, and
   verified decoder codecs over the mTLS QSF gateway.
2. The guest replies with its display ceiling.  The broker intersects it with
   the explicit tested Sunshine encoder envelope and client information.
3. `PAIR_CAPABILITIES` validates the result in the static guest agent and
   returns `CONNECTION_PROFILE_ACCEPTED` with a unique generation.  Nothing
   in the guest desktop changes yet.
4. The broker calls QEMU `Console.SetUIInfo` for the resolved size.  Failure
   stops the transaction before a new guest profile is committed.
5. `COMMIT_CONNECTION_PROFILE` atomically writes this exact v2 file:

   ```text
   version=2
   generation=…
   resolution=WIDTHxHEIGHT
   fps=…
   bitrate_kbps=…
   video_codec=H264|HEVC|AV1
   ```

6. The guest-local VirGL display adapter restarts or reconfigures its
   compositor, verifies a real current scanout of that size, and atomically
   copies the unchanged file to `connection-profile-applied`.
7. `AWAIT_CONNECTION_PROFILE` returns only after the exact generation-bound
   acknowledgement exists. Only then does the Qt coordinator close the
   profile-only lease and controlled-reconnect Moonlight with the resolved
   size/FPS/bitrate/codec.

The static agent waits up to 30 seconds by default.  This is intentional:
the client must not reconnect into a disappearing old scanout.  Disable that
safety check only for a deliberately non-graphical test endpoint with
`QSUNSHINE_QSF_GUEST_REQUIRE_PROFILE_APPLY_ACK=0`.

The broker serializes display transactions, so a standalone `resize` cannot
interleave with `connection_optimize`. A timeout or any failed stage leaves QSF
inactive; it does not start a replacement Moonlight stream on an unconfirmed
guest mode.

## Guest adapter

The package ships the source
`/usr/share/q-sunshine/guest/qsf_virgl_display_adapter.sh`.  Run it inside the
guest with an absolute, trusted apply helper, for example:

```sh
/usr/share/q-sunshine/guest/qsf_virgl_display_adapter.sh \
  --state-dir /var/lib/qsf \
  --apply-command /usr/local/libexec/qsf-apply-weston-profile
```

The helper receives `WIDTH HEIGHT FPS SNAPSHOT_PROFILE`.  It must return zero
only after it has reconfigured the guest compositor and independently checked
the *current* DRM/Wayland output mode and a live VirGL workload.  A Weston
helper normally restarts its DRM backend after QEMU has accepted the preferred
mode, then uses `wayland-info` (from Alpine's `wayland-utils`) to confirm the
current mode. The adapter never
uses `eval`, accepts only the canonical profile schema, and refuses to publish
an acknowledgement if a newer generation replaced the snapshot.

## Host encoder envelope

Do not infer NVENC or an encoding ceiling from `/dev/nvidia*`, `/dev/dri`, CPU
count, KVM, or an LXC device passthrough.  Configure a tested per-VM envelope:

```ini
QSUNSHINE_QSF_HOST_MAX_WIDTH=3840
QSUNSHINE_QSF_HOST_MAX_HEIGHT=2160
QSUNSHINE_QSF_HOST_MAX_FPS=60
QSUNSHINE_QSF_HOST_MAX_BITRATE_KBPS=45000
QSUNSHINE_QSF_HOST_ENCODER_CODECS=H264,HEVC,AV1
```

Optionally add a loopback Sunshine endpoint:

```ini
QSUNSHINE_QSF_SUNSHINE_SERVERINFO_URL=http://127.0.0.1:47990/serverinfo
```

The broker then intersects those configured codecs with the actual
`ServerCodecModeSupport` returned by Sunshine.  `/serverinfo` does not expose
a reliable maximum resolution, FPS, bitrate, or hardware/throughput claim, so
the tested envelope remains mandatory.  HTTPS requires
`QSUNSHINE_QSF_SUNSHINE_SERVERINFO_CA_FILE`; arbitrary network URLs are
rejected.

On macOS the Qt client asks VideoToolbox at runtime for H.264, HEVC, and AV1
hardware-codec availability without modifying Moonlight.  On a platform with
no direct verified API it falls back conservatively to H.264 unless an operator
supplies a tested `QSUNSHINE_CLIENT_DECODER_CODECS` override.  Stock Moonlight
still creates the final per-stream decoder for the resolved size and FPS.
