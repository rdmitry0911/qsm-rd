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
│       └── sanitizers-ctest.log
├── config/
│   ├── qemu-vm.cpu.example.toml
│   └── qemu-vm.example.toml
├── docs/
│   ├── ARCHITECTURE.md
│   ├── ENGINEERING_PLAN.md
│   ├── IMPLEMENTATION_STATUS.md
│   ├── INTEGRATION_MAP.md
│   ├── MVP_ACCEPTANCE.md
│   ├── PROJECT_TREE.md
│   ├── TEST_MATRIX.md
│   ├── THREAT_MODEL.md
│   └── VALIDATION.md
├── integration/
│   ├── README.md
│   └── sunshine/
│       ├── CPU_BACKEND_CONTRACT.md
│       └── PATCH_SERIES.md
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
│   ├── run-demo.sh
│   ├── run-no-gpu-selftest.sh
│   ├── run-qemu-display-probe.sh
│   └── run-sanitizers.sh
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
- `integration/sunshine`: exact contract for replacing the diagnostic FFmpeg sink
  with Sunshine's software encoder and Moonlight transport.
