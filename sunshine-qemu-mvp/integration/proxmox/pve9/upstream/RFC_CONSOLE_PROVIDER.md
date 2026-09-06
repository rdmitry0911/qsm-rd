Subject: [RFC pve-manager] ui: define a provider contract for browser consoles
To: pve-devel@lists.proxmox.com

Hello,

I would like feedback before preparing a patch series for a browser-resident
QEMU console transport that is not VNC, SPICE, or xterm.js.

The existing PVE Console split button is the right authorization and lifecycle
boundary: the user already has a PVE session and `VM.Console` permission, and
the console opens in a separate browser window. My out-of-tree prototype adds
another item to that menu, but it currently depends on a node-local service,
a protected media endpoint, and a QEMU Display1/guest-agent arrangement. I do
not think embedding that product or any named third-party transport in
`pve-manager` would be appropriate.

Would maintainers consider a small, generic browser-console-provider contract?
The initial scope I have in mind is intentionally limited:

1. A provider declares a stable opaque identifier, human-facing label, and
   supported resource type (`qemu` initially).
2. The Console split button displays only registered providers for which the
   existing PVE authorization check succeeds; there is no second login, PIN,
   pairing state, or client-side secret.
3. Selecting a provider opens a same-origin PVE console window, as noVNC does.
   PVE remains responsible for obtaining/validating an expiring, VM-bound
   launch ticket before the provider receives anything.
4. The base implementation contains no media codec, GPU, QEMU `args`, guest
   agent, or vendor-specific configuration. A provider must decline cleanly
   when the VM/node cannot support it.
5. Provider lifecycle and failure semantics must match the existing console:
   a stopped VM yields a meaningful waiting/error state; a VM shutdown closes
   the console; multiple simultaneous consoles remain independent.

If this direction is acceptable, I would first send a small RFC series against
current `pve-manager` with a provider registry/interface, the ConsoleButton
and Utils dispatch plumbing, and unit/UI tests. It would not include the
out-of-tree service or QSM branding. A follow-up can then demonstrate one
provider implementation outside the PVE base packages.

Questions on the proposed boundary:

- Is a provider registry an acceptable extension point in `pve-manager`, or
  should such transports remain entirely out-of-tree with a package-owned UI
  overlay?
- Which server-side API and ticket/lifecycle model would be acceptable for a
  same-origin browser console provider?
- Would `qemu`-only support be sufficient for an initial RFC, and are there
  existing extension mechanisms I should use instead?

The prototype is available for design review on request, but this RFC is not a
request to merge it as-is. I will wait for architectural guidance before
posting code.

Thanks,
[Your full name]
[Your email]
