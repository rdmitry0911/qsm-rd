# Архитектура

> Статус на 2026-09-01: CPU-first схема ниже остаётся переносимым baseline,
> однако дальнейшие слои из этого документа уже реализованы в функциональном
> варианте: Sunshine получает QEMU `ScanoutDMABUF` через headless GBM/EGL,
> Moonlight проходит native KVM/VirGL E2E, а QSF bridge проверен с Weston DRM
> clipboard, файлами и `SetUIInfo`-сменой scanout. Текущий DMA-BUF тракт всё
> ещё выполняет CPU BGRX readback перед `libx264`; это не zero-copy и не
> hardware-encode. Актуальные acceptance evidence и границы приведены в
> [`VALIDATION.md`](VALIDATION.md) и
> [`MOONLIGHT_SUNSHINE_VIRGL_E2E.md`](MOONLIGHT_SUNSHINE_VIRGL_E2E.md).

## 1. Процессы MVP-0A: CPU-first baseline

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
│  software encoder    audio adapter        input adapter      │
│      ▲                    ▲                    ▼              │
│  CPU BGRA image       bounded FIFO      QEMU D-Bus methods   │
│      ▲                    ▲                                   │
│  latest-frame box     QEMU AudioOut                           │
│      ▲                                                        │
│  QEMU Display Listener                                       │
└───────────────┬─────────────────────────────────────────────┘
                │ private Unix D-Bus
                ▼
┌─────────────────────────────────────────────────────────────┐
│ QEMU/KVM VM42                                                │
│ virtio-vga or another CPU-visible display device             │
│ USB tablet/keyboard + HDA audio                              │
└─────────────────────────────────────────────────────────────┘
```

Этот путь является обязательным первым функциональным gate. Он использует
`Scanout`/`Update` либо `ScanoutMap`/`UpdateMap` и Sunshine
`encoder = software`, поэтому не требует GPU и `/dev/dri`.

## 1.1 Процессы MVP-0B: native DMA-BUF capture и последующая GPU-оптимизация

После работающего stock-Moonlight потока меняется хранение и обработка кадра.
Первый native вариант уже работает, но его финальный readback/encode остаётся
CPU/software; показанный ниже native-converter/encoder — последующая
оптимизация:

```text
virtio-vga-gl / VirGL
  → QEMU ScanoutDMABUF
  → Sunshine EGL/Vulkan/VAAPI import
  → native hardware encoder
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

CPU-first реализация принимает полную CPU-visible поверхность через inline
byte array или Unix-FD-backed map. Новый `Scanout`/`ScanoutMap` создаёт surface
generation; `Update`/`UpdateMap` обновляет содержимое и публикует новый
`FrameToken`.

В GPU-оптимизации `ScanoutDMABUF` объявляет новую поверхность или новый
generation. Она живёт дольше отдельных damage/update notifications. Listener
дублирует необходимые FD и хранит их в RAII-объекте.

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

Первый принимаемый тракт:

```text
QEMU inline/shared-map framebuffer
  → validated CPU surface
  → one CPU copy into Sunshine image
  → Sunshine software encoder
```

Это намеренно не performance gate. Его назначение — доказать display/input/
audio/lifecycle и получить настоящий поток stock Moonlight без зависимости от
GPU.

Поздний fast path:

```text
QEMU RGB DMA-BUF
  → EGL/Vulkan import
  → Sunshine existing RGB→NV12/P010 converter
  → VAAPI/NVENC/Vulkan encoder
```

Термин «zero-copy» используется осторожно. Полного CPU readback быть не должно, но GPU blit/цветоконверсия допустимы и обычно необходимы.

CPU baseline:

```text
QEMU shared memory / mapped image
  → CPU-visible Sunshine image
  → existing software/upload path
```

CPU baseline остаётся поддерживаемым режимом для CI, headless-хостов и
диагностики; производительный gate применяется только к GPU-оптимизации.

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

### MVP-1 (будущее предложение QMDP)

Следующий QMDP flow является проектом будущего API, а не текущей реализацией.
Проверенный runtime использует QSF: `resize` вызывает `SetUIInfo`, а guest
Weston DRM явно перезапускается для выбора нового режима. QSF не создаёт
QMDP WebSocket endpoint и не меняет stock GameStream protocol.

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

GameStream не даёт универсальной системной синхронизации clipboard. Текущая
реализация использует отдельный authenticated QSF side-channel, а не QEMU
Clipboard interface и не QMDP WebSocket:

```text
client clipboard
  → QSF local socket / optional mTLS gateway
  → token-protected host broker
  → QEMU virtio-serial
  → guest agent → Weston wl-copy/wl-paste bridge
```

QSF поддерживает non-NUL UTF-8 plain text до 1 MiB и отдельные binary files до
2 MiB с безопасными basename; оба направления проверены в госте Weston.
QMDP `offer/request/data` и QEMU Clipboard interface ниже по документу —
future/reference-only design.

## 7. Cursor

MVP-0 использует cursor metadata QEMU и композитит cursor в CPU-кадр либо в
Sunshine image, чтобы stock Moonlight видел корректный указатель. GPU fast path
позднее выполняет тот же шаг на GPU.

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
