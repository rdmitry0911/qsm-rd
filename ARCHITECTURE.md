# Архитектура

## 1. Процессы MVP-0

```text
┌─────────────────────────────────────────────────────────────┐
│ Moonlight client (без изменений)                            │
│ Windows / macOS / Linux                                     │
└───────────────┬───────────────────────────────┬─────────────┘
                │ video/audio                  │ input
                ▼                              ▼
┌─────────────────────────────────────────────────────────────┐
│ sunshine-qemu@vm42                                          │
│                                                             │
│  GameStream server                                          │
│      ▲                    ▲                    │              │
│      │ encoded frames     │ Opus               │ input        │
│  native encoder      audio adapter        input adapter      │
│      ▲                    ▲                    ▼              │
│  EGL/Vulkan/VAAPI     bounded FIFO      QEMU D-Bus methods   │
│      ▲                    ▲                                   │
│  latest-frame box     QEMU AudioOut                           │
│      ▲                                                        │
│  QEMU Display Listener                                       │
└───────────────┬─────────────────────────────────────────────┘
                │ private Unix D-Bus
                ▼
┌─────────────────────────────────────────────────────────────┐
│ QEMU/KVM VM42                                                │
│ virtio-vga-gl → virglrenderer → host GPU scanout             │
│ USB tablet/keyboard + HDA audio                              │
└─────────────────────────────────────────────────────────────┘
```

Каждая ВМ имеет собственные:

- D-Bus address/socket;
- Sunshine config/data directory;
- GameStream port base;
- logs и metrics labels;
- capture/input/audio context;
- pairing policy.

## 2. Видео

### 2.1 Surface lifetime

`ScanoutDMABUF` объявляет новую поверхность или новый generation. Она живёт дольше отдельных damage/update notifications. Listener дублирует необходимые FD и хранит их в RAII-объекте.

`UpdateDMABUF` означает, что содержимое текущей поверхности обновилось. Callback не выполняет импорт, цветоконверсию или encode. Он лишь публикует lightweight `FrameToken` в mailbox.

```text
D-Bus callback                   capture/encoder thread
──────────────                   ──────────────────────
receive update
read current surface
publish token ────────────────► pop latest token
return immediately              import/reuse image
                                RGB→NV12/P010
                                encode
```

### 2.2 Latest-frame mailbox

Инварианты:

- глубина `0..1`;
- producer никогда не ждёт consumer;
- новый token заменяет непрочитанный старый;
- surface object reference-counted;
- FD закрывается только после ухода последнего token/import reference;
- queue depth, dropped count и frame age измеряются.

### 2.3 Форматный тракт

MVP fast path:

```text
QEMU RGB DMA-BUF
  → EGL/Vulkan import
  → Sunshine existing RGB→NV12/P010 converter
  → VAAPI/NVENC/Vulkan encoder
```

Термин «zero-copy» используется осторожно. Полного CPU readback быть не должно, но GPU blit/цветоконверсия допустимы и обычно необходимы.

Fallback:

```text
QEMU shared memory / mapped image
  → CPU-visible Sunshine image
  → existing software/upload path
```

Fallback нужен для bring-up и диагностики, но не проходит производительный gate.

### 2.4 Reconfiguration

Новая комбинация `(width, height, fourcc, modifier, planes)` создаёт новый surface generation.

```text
new generation
  → stop consuming old surface after in-flight encode
  → rebuild import/converter resources
  → encoder reconfigure
  → request IDR
  → continue same control/audio session
```

На клиенте во время перестройки остаётся последний кадр, масштабированный renderer'ом. Black clear не отправляется.

## 3. Ввод

Ввод не проходит через host compositor.

```text
Moonlight packet
  → Sunshine input dispatcher
  → per-VM QEMU input adapter
  → QEMU Display1 Keyboard/Mouse
  → guest virtual keyboard/tablet
```

Политики:

- absolute pointer по умолчанию для desktop;
- relative pointer для игр/3D mode;
- wheel преобразуется в press/release соответствующей кнопки;
- при disconnect отправляется release для удерживаемых key/button;
- coordinates нормализуются по актуальной guest surface geometry;
- host input devices не создаются.

## 4. Аудио

QEMU AudioOut listener даёт PCM конкретной ВМ. Adapter приводит его к формату Sunshine audio pipeline.

```text
QEMU PCM chunk
  → format conversion/resample
  → bounded FIFO (30–50 ms)
  → Sunshine Opus encoder cadence
```

При переполнении удаляются самые старые samples. Это принципиально: длинная непрерывная очередь улучшила бы полноту, но разрушила бы интерактивную синхронизацию.

MVP поддерживает guest→client stereo. Client microphone→guest — отдельный upstream audio input workstream.

## 5. Изменение разрешения

### MVP-0

При старте stream Sunshine вызывает `SetUIInfo` один раз с запрошенной клиентом geometry. Guest может:

1. изменить mode и выдать новый scanout;
2. проигнорировать запрос — тогда Sunshine/клиент масштабирует исходный кадр.

### MVP-1

```text
client window resize
  → immediate local scaling
  → debounce 250 ms
  → viewport.set(request_id, geometry, scale)
  → host validates/coalesces
  → QEMU SetUIInfo
  → guest changes mode
  → new ScanoutDMABUF generation
  → encoder reconfigure + IDR
  → viewport.applied(actual geometry)
```

Только последний pending request применяется. Старый ACK с меньшим `request_id` игнорируется клиентом.

## 6. Clipboard

QEMU предоставляет host-side clipboard broker, но GameStream не даёт универсальной системной синхронизации clipboard. Поэтому MVP-1 использует отдельный authenticated side-channel.

```text
client clipboard
  → QMDP offer/request/data
  → Sunshine QEMU session
  → QEMU Clipboard interface
  → guest clipboard integration
```

Первая версия поддерживает только UTF-8 plain text. Для файлов clipboard передаёт лишь metadata/offer; payload должен идти будущим file-transfer engine.

## 7. Cursor

MVP-0 использует cursor metadata QEMU и композитит cursor в кадр на GPU, чтобы stock Moonlight видел корректный указатель.

MVP-1 передаёт shape/hotspot/visibility отдельно и рисует cursor локально. Это уменьшает субъективную задержку движения указателя.

## 8. Reconnect

Сессия различает:

- QEMU D-Bus reconnect;
- encoder reinit;
- Moonlight transport reconnect;
- guest reboot.

Guest reboot не должен завершать Sunshine pairing. D-Bus disappearance переводит pipeline в `reconnecting`, удерживает последнюю frame/status page и повторно регистрирует listener с backoff. После нового scanout запрашивается IDR.

## 9. Масштабирование

MVP:

```text
VM1 → Sunshine process 1 → encoder session 1
VM2 → Sunshine process 2 → encoder session 2
VM3 → Sunshine process 3 → encoder session 3
```

Поздний broker может объединить discovery и единый UI, но per-VM workers следует сохранить как fault/security boundary.
