# Критерии приёмки MVP

## 1. Обязательные функциональные gates

### A1 — Console lifecycle

- Видны UEFI/BIOS, bootloader, загрузка ОС, login screen и desktop.
- Guest reboot не требует перезапуска Moonlight pairing.
- Остановка QEMU переводит endpoint в понятное состояние, а повторный запуск восстанавливает stream.

### A2 — Video

- 1920×1080@60 и 2560×1440@60 работают на fast path.
- Поддержаны минимум H.264 и один из HEVC/AV1 там, где его уже предоставляет выбранный Sunshine encoder.
- Никакого полного framebuffer readback в CPU RAM на fast path.
- При смене surface generation нет use-after-close DMA-BUF FD.
- После reconfigure отправляется IDR.

### A3 — Input

- Keyboard работает в UEFI, Linux console и Windows login.
- Absolute pointer не зависит от focus host desktop.
- Relative mode, buttons и wheel работают.
- Disconnect освобождает все удерживаемые keys/buttons.
- Инъекция не затрагивает host input devices.

### A4 — Audio

- Только звук выбранной ВМ попадает клиенту.
- 48 kHz stereo воспроизводится без длительного drift.
- Переполнение FIFO не увеличивает latency: отбрасываются старые samples.
- Guest audio restart восстанавливается автоматически.

### A5 — Resolution

- Начальный запрос клиента передаётся в `SetUIInfo`.
- При поддержке guest stack mode меняется без разрыва Moonlight control session.
- При отсутствии реакции гостя работает scaling fallback.

### A6 — Isolation/operations

- Каждая ВМ имеет отдельный local D-Bus и Sunshine instance.
- Остановка/авария одной ВМ не останавливает соседние.
- D-Bus socket не слушает TCP и недоступен другим Unix users.
- Логи содержат VM id, session id и surface generation.

## 2. Производительные gates

Измерения выполняются отдельно для CPU fallback и GPU fast path. Продуктовый gate относится к fast path.

| Метрика | MVP gate | Метод |
|---|---:|---|
| Capture queue depth | ≤ 1 | встроенный gauge |
| Возраст кадра при начале encode, p95 | ≤ 1 frame period + 3 ms | timestamps producer/consumer |
| Host capture+convert+encode, 1080p60 p95 | ≤ 12 ms | Sunshine timestamps/GPU profiler |
| Желательная host latency, 1080p60 p95 | ≤ 8 ms | stretch target |
| LAN input-to-photon p95 | ≤ 35 ms | high-speed camera/LED or client telemetry |
| Audio FIFO target | 30–50 ms | queued frames gauge |
| Audio accumulated drift over 8 h | < 20 ms after correction | synchronized recording |
| Full-frame CPU readback fast path | 0 | perf/GPU trace/memory bandwidth evidence |
| Resize black interval | 0–1 displayed frame | capture/client recording |
| FD leak after 100 reconnects | 0 net growth | `/proc/<pid>/fd` |
| RSS growth after 8 h | < 5% after warm-up | process metrics |

Значения — инженерные gates первой версии, а не утверждение о гарантии на любом оборудовании.

## 3. Надёжность

- 8-часовой video+audio soak.
- 100 guest mode changes.
- 100 QEMU stop/start or D-Bus reconnect cycles в автоматизированном тесте.
- 10 network disconnect/reconnect cycles Moonlight.
- Force encoder reinit не оставляет black stream.
- Повреждённый/неподдержанный fourcc переводит backend в диагностируемый fallback/failure, а не crash.
- Закрытие DMA-BUF producer'ом не приводит к use-after-close: consumer владеет duplicated FD до завершения in-flight work.

## 4. MVP-1 gates

- 100 live resize cycles с burst coalescing.
- Последний viewport request всегда побеждает; stale ACK не меняет UI.
- Text clipboard в обоих направлениях, UTF-8, до 1 MiB.
- Loop suppression подтверждён тестом 1000 clipboard events.
- Clipboard можно отключить и задать направление per paired client.
- Local cursor shape/hotspot корректен для arrow/text/resize/busy.
- Отсутствующий QMDP side-channel не мешает обычному Moonlight stream.

## 5. Непроходные доказательства

Следующее не считается завершением:

- захват окна QEMU через X11/Wayland вместо D-Bus scanout;
- работа только через software encoder;
- один скриншот без soak/latency/FD evidence;
- использование host-wide audio monitor sink;
- ввод через host mouse/keyboard focus;
- очередь кадров глубже одного без обоснования и latency measurement;
- сообщение «zero-copy» без trace, подтверждающего отсутствие CPU readback.
