# Remaining work ranked by user value

Status: 2026-08-31, after the `0.6.0` session-lifecycle slice.

The ranking is based on what turns the implementation into a useful, safe VM
remote-desktop product. The CPU/software-encoder path remains the acceptance
path; DMA-BUF is an optimization, not a prerequisite.

| Rank | Layer | User value | State |
|---:|---|---|---|
| 1 | **Full Sunshine source-tree integration and stock Moonlight session** | Produces the first normal endpoint that a Windows, Linux or macOS Moonlight client can connect to. Video, VM-specific audio and QEMU input must cross the real Sunshine network stack. | Highest remaining product layer. The capture/audio adapter is standalone ABI-tested; factory registration and a complete upstream build remain. |
| 2 | **QEMU reconnect and input safety** | A VM reboot or QEMU restart must not leave a permanent black screen or a logically held Ctrl/Alt/Shift/mouse button. | **Closed in 0.6.0.** |
| 3 | **Per-VM service packaging and supervision** | Gives each VM a stable endpoint, private D-Bus, independent configuration/certificates, restart policy and boot-time recovery. | Not started; the lifecycle core it depends on is now implemented. |
| 4 | **Launch geometry and client-driven live resize** | Makes windowed/fullscreen use behave like an RDP desktop rather than a fixed-resolution game stream. | Initial `SetUIInfo`, generation reinit and viewport replay exist; client feedback and live resize remain. |
| 5 | **Bidirectional UTF-8 clipboard** | Removes the largest day-to-day administration inconvenience after basic connectivity. | Protocol design exists; QEMU/guest and client bridges remain. |
| 6 | **Client-side cursor** | Reduces perceived pointer latency and avoids encoding the cursor into every frame. | QEMU cursor shape/position metadata is captured; QMDP transport and Moonlight rendering remain. |
| 7 | **Unicode/IME and desktop text channel** | Makes cross-layout typing reliable for terminals, passwords and non-Latin text. | Not started. |
| 8 | **DMA-BUF and native hardware encoder path** | Lowers CPU load, latency and power use and increases VM density, but does not unlock basic usability while software encoding is acceptable. | Intentionally deferred until the complete CPU product path works. |
| 9 | **Files, microphone and multi-monitor** | Adds higher-level RDP conveniences after the single-display administration path is stable. | Not started. |

## Cross-cutting release qualification

Testing against a real `qemu-system-x86_64` process is a mandatory qualification
gate, not a separate user feature. The repository now contains a self-contained
TCG test with a project-owned BIOS/VGA boot sector, so no GPU, KVM, guest ISO or
disk image is required. Its execution is still open in this particular runtime
because the QEMU package could not be downloaded; protocol-faithful in-process
and independent-process D-Bus tests pass.

## Layer closed in this revision

`ResilientQemuDisplay` now owns a generation-scoped QEMU transport while keeping
the outer desktop source stable. It:

- reconnects with bounded exponential backoff;
- drops input while no QEMU transport is live instead of replaying it later;
- tracks pressed keys/buttons and issues best-effort release-all before teardown;
- re-applies only the newest viewport request after reconnect;
- clears queued video/audio and rejects callbacks from retired connections;
- namespaces surface generations across QEMU replacements, forcing capture
  reinitialization and an IDR boundary even when geometry is unchanged;
- preserves the outer `QemuSource` and Sunshine-facing adapter during process
  replacement.

Acceptance evidence includes a deterministic 100-reconnect stress test and a
three-process message-bus test with three successive fake-QEMU service owners.
