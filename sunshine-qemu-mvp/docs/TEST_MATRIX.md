# Матрица тестирования

> Ниже — целевая расширенная матрица MVP и production hardening. Базовый CPU
> lane и native KVM/VirGL/QSF acceptance уже пройдены; точные завершённые
> прогоны и ограничения зафиксированы в [`VALIDATION.md`](VALIDATION.md).
> Наличие строки в этой таблице не означает, что соответствующий future/soak
> gate уже выполнен.

## 1. Минимальная поддерживаемая матрица MVP-0A без GPU

### Host

- Linux x86-64;
- QEMU/KVM;
- Wayland/X11 хостовый desktop не должен влиять на capture;
- CPU-visible QEMU framebuffer через `Scanout` или `ScanoutMap`;
- Sunshine `encoder = software`;
- отсутствие `/dev/dri` не должно мешать запуску backend;
- минимум одна реальная QEMU-конфигурация с `virtio-vga`;
- GPU lanes являются отдельной последующей матрицей MVP-0B.

### Guest

- Linux Wayland desktop;
- Linux text console/bootloader;
- Windows 11 desktop/login;
- UEFI firmware.

### Client

- Moonlight-Qt Windows;
- Moonlight-Qt Linux;
- Moonlight-Qt macOS.

## 2. Видео: функциональный CPU gate

| Case | Geometry | FPS | Codec | Ожидание |
|---|---:|---:|---|---|
| C1 | 640×360 | 30 | H.264 software | CI/self-test, обязательный |
| C2 | 1280×720 | 30 | H.264 software | реальный QEMU smoke |
| C3 | 1920×1080 | 30 | H.264 software | функциональный desktop gate |
| V5 | mode switch burst | 60 | H.264 | latest request wins |
| V7 | guest reboot | 60 | H.264 | stream recovers + IDR |

CPU gate не задаёт жёсткой производительности на 1080p60: скорость зависит от
CPU и software encoder. Обязательны корректность, bounded queues и отсутствие
накопления задержки.

## 2.1 Видео: GPU performance gate MVP-0B

| Case | Geometry | FPS | Codec | Ожидание |
|---|---:|---:|---|---|
| G1 | 1920×1080 | 60 | H.264 | обязательный fast path |
| G2 | 2560×1440 | 60 | H.264 | обязательный fast path |
| G3 | 3840×2160 | 60 | HEVC | stretch после MVP gate |
| G4 | 1920×1080 | 120 | H.264/HEVC | stretch latency/cadence |
| G5 | unsupported modifier | 60 | H.264 | GPU blit/CPU fallback, без crash |

Проверять:

- dropped/latest-frame counters;
- frame age;
- CPU copy/encode time в MVP-0A;
- GPU conversion/encode time в MVP-0B;
- CPU utilization;
- memory bandwidth/readback evidence;
- surface generation/FD lifetime.

## 3. Input

| Case | Ожидание |
|---|---|
| UEFI navigation | arrows/Enter/Esc работают |
| Linux TTY | key up/down и modifiers корректны |
| Windows login | Ctrl/Alt/Shift, layout baseline |
| Absolute tablet | края и углы без clipping |
| Relative mode | отсутствие acceleration от host desktop |
| Wheel | вертикальный/горизонтальный mapping |
| Disconnect with held key | release-all выполнен |
| Two VM processes | события не пересекаются |

## 4. Audio

- continuous sine/reference stream 8 h;
- silence→sound transitions;
- guest sample-rate changes;
- mute/volume notifications;
- QEMU audio device reset;
- induced encoder stall: FIFO остаётся bounded;
- соседняя ВМ воспроизводит другой tone — cross-talk отсутствует.

## 5. Fault injection

- kill/restart QEMU;
- kill/restart Sunshine worker;
- close D-Bus socket;
- revoke/close DMA-BUF FD producer side после listener duplication;
- malformed dimensions/fourcc;
- encoder device reset;
- network loss 1/5/30 seconds;
- client reconnect;
- disk full for logs;
- process reaches configured memory/FD limits.

## 6. Performance instrumentation

Минимальные timestamps:

```text
qemu_update_received
mailbox_published
capture_pop
gpu_import_begin/end
gpu_convert_begin/end
encode_submit/complete
packet_send
client_decode_begin/end
client_present
input_client_send
input_qemu_method
```

Correlation fields:

```text
vm_id, session_id, frame_sequence, surface_generation, request_id
```

Тестовый отчёт обязан показывать p50/p95/p99, а не только среднее.
