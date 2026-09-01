# Isolated libva for the pinned Sunshine build

The pinned Sunshine source uses a prebuilt static FFmpeg whose codec registry
still references the core VA ABI, including `vaMapBuffer2`, even when the
Sunshine VAAPI backend is disabled. Ubuntu 24.04's system `libva 2.20` does
not export that symbol. This is a linker/runtime ABI issue in the bundled
dependency, not permission to replace the host's libva.

`scripts/build-isolated-libva-2.21.sh` builds the exact `libva 2.21.0` peeled
commit `0b01aed44ef1a6ad660261284ff266fa812829ef` below the ignored project
`.upstream/` directory. It requires only `git`, `meson`, `ninja`, a C compiler,
and the normal libva build dependencies; it uses no `sudo` and does not write
outside its selected prefix.

```bash
./scripts/build-isolated-libva-2.21.sh
```

The final `ISOLATED_LIBVA_OK` line provides the prefix and library directory.
Use that directory only for the matching Sunshine test build, both at link and
runtime. The repository helper makes this reproducible, builds the QEMU
guest-audio-only profile, and sets `CMAKE_INSTALL_PREFIX` to the build tree so
the binary and staged `assets/apps.json` agree:

```bash
./scripts/build-upstream-sunshine-qemu.sh
```

The helper defaults to `.upstream/Sunshine`,
`.upstream/build-sunshine-qemu`, and `.upstream/prefix-libva-2.21.0`; override
`SUNSHINE_SOURCE_DIR`, `SUNSHINE_BUILD_DIR`, or `ISOLATED_LIBVA_PREFIX` only
for a deliberately separate worktree/prefix. It verifies the compiled assets
path, that `ldd` resolves `libva.so.2` from the isolated prefix, and that the
full resolver closure contains none of X11, Wayland, PulseAudio, or ALSA. Core
`libva`/`libva-drm` remain expected; `va-x11` is not linked in this profile.

The `rpath` is deliberate: it makes the test binary resolve the isolated
`libva.so.2` without changing `LD_LIBRARY_PATH` for QEMU, Moonlight, or the
host. A distribution Sunshine package whose FFmpeg and libva already match
does not need this workaround.
