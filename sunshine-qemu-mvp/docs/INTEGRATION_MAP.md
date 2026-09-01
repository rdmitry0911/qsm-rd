# Карта интеграции

Документ указывает логические точки врезки; точные строки зависят от выбранного commit Sunshine/QEMU. CPU-first baseline и его точный pin описаны в `SUNSHINE_QEMU_INTEGRATION.md`. С 2026-09-01 также реализованы Sunshine input/audio, single-plane DMA-BUF headless EGL CPU readback и native Moonlight/KVM/VirGL gate; актуальный фактологический статус — в `IMPLEMENTATION_STATUS.md` и `VALIDATION.md`. Остальные пункты ниже остаются roadmap, в частности hardware encode, multi-plane DMA-BUF и production operations.

## 1. QEMU D-Bus Display

### Console discovery и listener

Объекты вида:

```text
/org/qemu/Display1/Console_0
/org/qemu/Display1/Console_1
```

Используемые методы/события:

- регистрация `org.qemu.Display1.Listener`;
- `Scanout` / `Update` — inline CPU framebuffer;
- `ScanoutMap` / `UpdateMap` — preferred CPU-first Unix-FD-backed framebuffer;
- `ScanoutDMABUF` — preferred single-plane scanout;
- `ScanoutDMABUF2` — позднее для multi-plane;
- `UpdateDMABUF` — damage/update notification;
- `Disable` — scanout unavailable;
- `MouseSet` — cursor position/visibility;
- `CursorDefine` — ARGB cursor + hotspot;
- `SetUIInfo` — желаемая geometry viewport.

### Input

- Keyboard `Press`/`Release` с QEMU key number;
- Mouse `SetAbsPosition`;
- Mouse `RelMotion`;
- Mouse `Press`/`Release`;
- query absolute capability.

### Audio

- register AudioOut listener;
- принять negotiated PCM format;
- принять volume/mute и write chunks;
- для будущего microphone зарегистрировать AudioIn listener.

### Clipboard

QEMU Clipboard peer (`Grab`, `Release`, `Request`) остаётся reference-only
вариантом для будущего QMDP. Функциональная реализация использует QSF через
virtio-serial: non-NUL UTF-8 text до 1 MiB и отдельные files до 2 MiB с
безопасными basename; Wayland guest bridge и оба направления проверены E2E.

### Референс внутри QEMU

Standalone QEMU VNC server уже использует D-Bus Display для
display/input/audio/clipboard. Его код полезен как проверка семантики
интерфейсов и key/button mapping. Для MVP-0A намеренно используется
CPU-oriented rendering path; DMA-BUF→GPU encoder остаётся отдельной
оптимизацией MVP-0B.

## 2. Sunshine Linux platform

### Capture factory

В Linux platform source registry добавить новый источник:

```text
capture = qemu_dbus
encoder = software
```

Адрес private message bus передаётся текущему patch через environment, а не через
публичную Sunshine configuration:

```text
SUNSHINE_QEMU_DBUS_ADDRESS=unix:path=...
SUNSHINE_QEMU_DBUS_DESTINATION=org.qemu  # optional
```

Factory возвращает `display_t`-совместимый объект.

### CPU image path MVP-0A

Добавить `qemu_cpu_img_t : platf::img_t` с BGR0 storage. Listener поддерживает
актуальную нормализованную CPU-поверхность, а Sunshine capture thread копирует
последний полный кадр в свободный `img_t`. Это ровно одна CPU-копия и совместимо
с `encoder = software`.

### Existing GPU image abstractions MVP-0B

Использовать существующие abstractions Sunshine:

- `platf::display_t` для capture lifecycle;
- `platf::img_t`/encoded image variants;
- EGL surface/image descriptors с:
  - width/height;
  - DMA-BUF fds;
  - fourcc;
  - modifier;
  - pitches;
  - offsets;
  - y-invert;
  - cursor metadata.

QEMU listener не должен изобретать ещё один GPU encoder-facing frame type. В
MVP-0B он заполняет уже используемый EGL descriptor так же, как KMS capture, и
передаёт его существующим VAAPI/CUDA/Vulkan conversion paths.

### Suggested classes

```cpp
class qemu_display_t final : public platf::display_t {
public:
    capture_e capture(const push_captured_image_cb_t&, ... ) override;
    std::shared_ptr<img_t> alloc_img() override;
    int dummy_img(img_t*) override;
    void* get_hwdevice_ctx() override;
    bool is_hdr() override;
};

class qemu_dbus_listener_t {
    // Own D-Bus connection, listener object, duplicated DMA-BUF FDs,
    // current surface generation, cursor and update mailbox.
};
```

### Capture result policy

- `ok` — актуальный frame token передан;
- `timeout` — за capture interval не было нового update;
- `reinit` — geometry/fourcc/modifier/generation требует rebuild;
- `error` — D-Bus disconnected or import failed without fallback.

### Input

Текущий Sunshine input dispatcher должен направляться не в host platform injection, а в QEMU adapter данного процесса.

Для MVP допускается compile/runtime branch:

```text
input_backend = qemu_dbus
```

Позднее предпочтительнее сделать `input_context` настоящим per-session объектом вместо глобального platform state.

### Audio

Создать реализацию, совместимую с Sunshine audio source abstraction:

```cpp
class qemu_mic_t final : public platf::mic_t {
public:
    status_e sample(std::vector<float>& samples) override;
};
```

Она читает ровно cadence-sized chunks из bounded FIFO, куда D-Bus callback помещает PCM после conversion.

## 3. Moonlight-Qt MVP-1

Добавить, не меняя base GameStream media path:

```text
app/streaming/DesktopSessionController.*
app/streaming/QmdpClient.*
app/streaming/ClipboardBridge.*
app/streaming/ViewportTracker.*
app/streaming/RemoteCursor.*
```

### Viewport source

Qt window даёт logical size и device pixel ratio:

```text
physical_stream_width  = logical_width  × devicePixelRatio × renderScale
physical_stream_height = logical_height × devicePixelRatio × renderScale
```

Затем применяются limits/alignment host capabilities. Во время drag локальный renderer масштабирует старый frame; сетевой resize посылается после debounce.

### Clipboard

Qt clipboard events превращаются в offer, а payload отдаётся только по request. Нужны origin id, generation и hash для подавления циклов.

### Local cursor

Remote cursor рисуется поверх decoded video в клиенте. Host-baked cursor отключается после подтверждённого capability switch, чтобы избежать двойного указателя.

## 4. Orchestrator

MVP может обходиться systemd template и статическими профилями. Следующая версия получает broker:

```text
GET /vms
POST /vms/{id}/session
DELETE /sessions/{id}
```

Broker не обрабатывает frame/audio сам. Он запускает и контролирует per-VM workers, выдаёт endpoint и применяет admission control по encoder capacity.
