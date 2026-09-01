# Project tree

```text
sunshine-qemu-mvp/
├── CHANGELOG.md
├── CMakeLists.txt
├── LICENSE
├── LICENSE.spdx
├── MANIFEST.sha256
├── README.md
├── artifacts/
│   └── validation/
│       ├── no-gpu-selftest/
│       │   ├── encoded/*.mkv
│       │   ├── *.ppm
│       │   └── environment/CTest/self-test/ffprobe logs
│       ├── real-qemu/
│       │   ├── trace.txt
│       │   └── absolute/trace.txt
│       ├── alpine-reference-e2e/trace.txt
│       ├── upstream-sunshine-qemu-e2e/trace.txt
│       └── sanitizers-ctest.log
├── config/
│   ├── qemu-vm.cpu.example.toml
│   └── qemu-vm.example.toml
├── clients/
│   └── qsunshine-qt/
│       ├── CMakeLists.txt
│       ├── qml/Main.qml
│       ├── src/
│       │   ├── main.cpp
│       │   ├── moonlightcontroller.cpp/.h
│       │   └── qsfclient.cpp/.h
│       └── tests/
│           ├── moonlightcontroller_test_main.cpp
│           ├── qsf_e2e_main.cpp
│           ├── qml_smoke_main.cpp
│           └── real_e2e_main.cpp
├── docs/
│   ├── ARCHITECTURE.md
│   ├── BUILD_AND_RUN.md
│   ├── ENGINEERING_PLAN.md
│   ├── IMPLEMENTATION_STATUS.md
│   ├── INTEGRATION_MAP.md
│   ├── MVP_ACCEPTANCE.md
│   ├── PROJECT_TREE.md
│   ├── QT_DESKTOP_CLIENT.md
│   ├── REAL_QEMU_E2E.md
│   ├── REFERENCE_VM.md
│   ├── SUNSHINE_QEMU_INTEGRATION.md
│   ├── TEST_MATRIX.md
│   ├── THREAT_MODEL.md
│   └── VALIDATION.md
├── integration/
│   ├── README.md
│   └── sunshine/
│       ├── CPU_BACKEND_CONTRACT.md
│       ├── PATCH_SERIES.md
│       ├── PINNED_UPSTREAM.md
│       └── patches/
│           └── 0001-platform-linux-add-QEMU-Display1-CPU-capture.patch
├── packaging/
│   └── systemd/
│       └── README.md
├── plan/
│   └── mvp-backlog.yaml
├── protocol/
│   └── qmdp-v1.md
├── references/
│   └── UPSTREAM.md
├── scripts/
│   ├── launch-qemu-dbus-cpu-example.sh
│   ├── launch-qemu-dbus-example.sh
│   ├── install-qemu-debian.sh
│   ├── provision-alpine-reference-vm.sh
│   ├── launch-alpine-reference-vm.sh
│   ├── run-demo.sh
│   ├── run-alpine-reference-e2e.sh
│   ├── run-no-gpu-selftest.sh
│   ├── run-qemu-display-probe.sh
│   ├── run-real-qemu-selftest.sh
│   ├── run-virgl-qsf-wayland-clipboard-e2e.sh
│   ├── run-qsunshine-qt-moonlight-virgl-qsf-wayland-hook.sh
│   ├── run-sanitizers.sh
│   └── run-upstream-sunshine-qemu-e2e.sh
├── src/
│   ├── capture/
│   │   ├── cpu_framebuffer.cpp/.hpp
│   │   └── mapped_region.cpp/.hpp
│   ├── compat/
│   │   └── sd_bus_compat.h
│   ├── core/
│   │   ├── audio_fifo.cpp/.hpp
│   │   ├── frame.hpp
│   │   ├── latest_frame_mailbox.hpp
│   │   ├── pixel_format.cpp/.hpp
│   │   ├── resize_coalescer.cpp/.hpp
│   │   ├── session.cpp/.hpp
│   │   └── unix_fd.hpp
│   ├── dbus/
│   │   └── sd_bus.cpp/.hpp
│   ├── demo/
│   │   └── main.cpp
│   ├── interfaces/
│   │   ├── qemu_display.hpp
│   │   └── sunshine_adapter.hpp
│   ├── mock/
│   │   ├── mock_qemu_display.cpp/.hpp
│   │   └── mock_sunshine_adapter.cpp/.hpp
│   ├── pipeline/
│   │   └── desktop_session.cpp/.hpp
│   ├── qemu/
│   │   ├── qemu_dbus_display.cpp/.hpp
│   │   └── qemu_dbus_protocol.hpp
│   ├── software/
│   │   ├── cpu_frame_sink.cpp/.hpp
│   │   └── ffmpeg_software_adapter.cpp/.hpp
│   └── testing/
│       └── fake_qemu_service.cpp/.hpp
├── tests/
│   ├── core_tests.cpp
│   ├── dbus_integration_tests.cpp
│   ├── run_message_bus_e2e.sh
│   ├── run_real_qemu_e2e.sh
│   ├── fixtures/
│   │   └── qmdp_vga_smoke.S
│   └── support/
│       └── mock_qemu_dbus_server.cpp/.hpp
└── tools/
    ├── fake_qemu_dbus.cpp
    ├── qemu_display_probe.cpp
    └── qmdp_dbus_selftest.cpp
```

## Module boundaries

- `src/qemu`: real QEMU Display1 client and peer listener.
- `src/capture`: validated CPU framebuffer and shared-map lifetime handling.
- `src/pipeline`: non-blocking QEMU-to-encoder session bridge.
- `src/software`: no-GPU sinks; one saves frames and one invokes FFmpeg/libx264.
- `src/testing` and `tests/support`: two complementary fake-QEMU implementations.
- `tools/qemu_display_probe.cpp`: user-facing process for attaching to a real QEMU.
- `integration/sunshine`: pinned, portable CPU-display patch and the contract for
  later input/audio/shared-map work.
- `scripts/run-upstream-sunshine-qemu-e2e.sh`: real QEMU → patched Sunshine
  `display_t` → software/libx264 encoder-probe gate.
- `clients/qsunshine-qt`: optional, standalone Qt desktop shell. It keeps
  Moonlight Qt as a child media/input process and implements profile-scoped
  QSF TLS controls without embedding an SDL surface in a Qt event loop.
- `scripts/run-qsunshine-qt-moonlight-virgl-qsf-wayland-hook.sh`: retained
  composite Qt-shell/stock-Moonlight/Sunshine/QEMU/VirGL qualification hook;
  it owns the disposable client display and transfers QSF operation ownership
  from the legacy outer runner to the production Qt client classes. It proves
  windowed and physical-fullscreen presentations, controlled reconnect, input,
  mTLS clipboard/files, and guest resize in the same powered-on VM.
