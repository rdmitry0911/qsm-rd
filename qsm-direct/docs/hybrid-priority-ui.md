# Hybrid UI priority and capture fences

This extends the experimental exact layer. Video keeps its existing encoder,
reference chain and RTP-to-capture mapping. Advisory guest geometry selects
which pixel changes to prioritize; no application text is transported.

## Negotiation and wire compatibility

The bridge first probes worker mode 5, then 4 and 3 for older workers. Mode 5
advertises capability bit 8 in addition to the existing bits 1, 2 and 4.
Browser UI protocol 5 supports this layout-aware path. Older browser versions
receive their existing records: the bridge strips the extra stamp/flag and
does not send layout-boundary records to them.

An advanced QUI1 record has a 16-byte stamp following the 28-byte header:
the existing u64 capture timestamp, followed by a u64 layout identity. Both
must fit JavaScript safe integers. Encoding bit 0 still means grayscale;
bit 1 marks a candidate for early presentation. Opcode 5 is a layout boundary
with no tile or payload. Layout 0 revokes early-presentation eligibility.

The host reduces validated geometry to private input opcode 13, a 36-byte
`!B3xQIIIIII` body: mode, layout, desktop width/height and an unoccluded
priority rectangle. Mode 0 means unavailable; mode 1 supplies layout only;
mode 3 includes a text priority area. This opcode has no browser JSON route.
Older workers ignore it. VM and LXC use the same worker path and no additional
network port; one forwarded TCP/8006 remains sufficient.

## Selecting an area

The host validates the complete bounded guest schema, then uses the visible,
opaque focused window and its known client origin. An accessible text caret
or focused empty text control supplies the priority area. It is clipped to
the desktop, focused client and all windows above it. Missing or ambiguous
geometry retains the ordinary pixel/video path.

A host-created layout identity changes when the session, geometry, stack,
focus or availability changes. The host also compares the actual geometry
fields, so an untrusted guest cannot preserve an identity merely by reusing
its own generation number. Caret movement and heartbeats alone do not change
the layout identity.

The worker requires a matching capture aspect ratio, a fresh capture after
the geometry change and 120 ms of layout stability. Metadata expires after
three seconds. Its priority area can refine before typing using the ordinary
background budget; only actual keyboard activity enables early edits and
the interactive budget share.

## Presentation safety

Only an already published exact tile can be an early candidate. The worker
requires a sparse change whose changed pixels lie in the unoccluded priority
area. Unchanged pixels may extend outside it, allowing a text field to begin
partway through a tile. Dense changes and new tiles retain capture fences.

The browser independently checks the layout against the displayed base,
requires an existing exact image in that layout and compares the actual RGB
pixels. At most one eighth of a tile may change. A future layout boundary
revokes old early eligibility immediately; old early images are retired
when the corresponding base capture exists. Delayed old-layout decodes cannot
resurrect them. All ordinary clears and new/dense images keep their capture
fences and the single sRGB composition introduced in exp35.

On a layout change the worker also republishes previously early tiles with
ordinary capture fences, including tiles whose pixels did not change. This
restores exact coverage after the browser retires an old early image outside
the current video crop. Other unchanged exact tiles stay cached.

Eight of the 32 browser presentation slots are reserved for early candidates.
The existing byte budget, video accounting and bounded SCTP buffering still
apply. A small pending edit retains its prior image for at most 120 ms from
the oldest unserved edit, rather than from the old image's publication time.
Continuous new edits cannot renew that lease indefinitely. This avoids an
immediate blur/clear when a small edit waits briefly for byte credit.

## Qualification status

The exp35 baseline is in `rdp-baseline-20261028.json`. Private KWin5/NVENC
trials exercise real guest metadata, keyboard input, capture, encoding,
TURN/TCP8006 and visible-raster glyph timing. The launch/ACL bootstrap is
mocked. The exp42 short run improves ordinary overlap typing on both fast
and controlled 2 Mbit/s links; the separate no-accessibility run verifies
fallback. Results and limits are in `rdp-hybrid-20261028.json`. Cropped video,
real loss, longer runs, real-VM, other-backend, Mac and RDP comparisons remain
tracked in `rdp-next-plan.json`. Unit checks alone do not prove those broader
requirements.
