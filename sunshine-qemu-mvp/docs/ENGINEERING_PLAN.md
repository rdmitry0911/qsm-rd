# Инженерный план Sunshine–QEMU Desktop

## 1. Цель

Создать удалённую консоль ВМ с UX уровня RDP и медиатрактом уровня Moonlight:

- доступ от firmware/boot до desktop;
- оконный и fullscreen режимы;
- разрешение, соответствующее viewport клиента;
- низкая input-to-photon latency;
- аппаратное кодирование на GPU хоста;
- прямой ввод в конкретную ВМ без зависимости от focus хостового окна;
- индивидуальный звук ВМ;
- затем live resize, clipboard и local cursor.


## 1.1 Текущий статус реализации

По состоянию на 2026-09-01 первоначальный standalone CPU-first срез расширен
до функционального native E2E. Завершены:

- pinned Sunshine patches 0001..0006, включая direct QEMU input, guest audio
  и headless EGL DMA-BUF CPU readback;
- actual Moonlight pairing/HTTPS/RTSP/RTP, fullscreen/windowed decode and
  guest evdev input through KVM + virtio-vga-gl + VirGL (NVIDIA);
- QSF companion with strict UTF-8 clipboard, constrained files, Weston DRM
  wl-copy/wl-paste and observed 1280x800 -> 1280x720 resize;
- no-X11/no-Wayland/no-Pulse/no-ALSA Sunshine deployment artifact.
- standalone Qt/QML desktop shell -> clean stock Moonlight Qt child, qualified
  twice against the native KVM/VirGL/Weston guest with controlled
  windowed-to-physical-fullscreen reconnect and mTLS QSF operations.

Точные результаты проверок и оставшиеся границы приведены в
`VALIDATION.md`; последующие разделы сохраняют исходный инженерный план и
будущие работы по аппаратному кодированию и эксплуатации.

Исторический baseline от 2026-08-31 включал standalone CPU-first WP1 и
значимые части WP3/WP4:

- реальный QEMU Display1 D-Bus client/listener;
- `Scanout`/`Update` и Unix `ScanoutMap`/`UpdateMap`;
- курсор, `SetUIInfo`, keyboard/mouse;
- AudioOut PCM;
- latest-frame mailbox;
- software H.264 diagnostic encode;
- release и ASan/UBSan test matrix без GPU.

WP2-A выполнен: источник QEMU Display1 встроен в Sunshine `platf::display_t`,
а stock Moonlight получает поток через software H.264. Выполнен и рабочий
VirGL путь: DMA-BUF импортируется в headless EGL, читается в BGRX на CPU и
кодируется libx264. Следующая производительная задача — GPU-конвертация и
аппаратный encoder без полного CPU readback; она не является условием
функционального E2E-сценария.

Исторический принимаемый CPU-сценарий MVP:

```text
Linux host + QEMU/KVM
       → QEMU D-Bus ScanoutMap/UpdateMap или Scanout/Update
       → Sunshine software encoder
       → stock Moonlight client
```

Текущий проверенный VirGL-сценарий:

```text
virtio-vga-gl/VirGL → ScanoutDMABUF → headless EGL → CPU BGRX → libx264
```

Целевой будущий performance path:

```text
virtio-vga-gl/VirGL → ScanoutDMABUF → GPU conversion → host-native encoder
```

## 2. Архитектурные решения

### ADR-001 — Sunshine находится на хосте

Видеокадр, созданный VirGL/virglrenderer на host GPU, не возвращается в гость для повторного захвата. QEMU отдаёт scanout внешнему listener, а Sunshine импортирует его в существующий GPU capture/encode тракт.

### ADR-002 — один Sunshine-процесс на одну ВМ в MVP

Причины:

- существующие пути ввода и аудио Sunshine во многом предполагают один platform context;
- отдельные процессы дают естественную изоляцию FD, D-Bus, портов и сертификатов;
- отказ одной ВМ не останавливает остальные;
- не требуется сразу проектировать multi-tenant scheduler внутри Sunshine.

Ограничение MVP: одна активная управляющая Moonlight-сессия на ВМ. Несколько view-only клиентов — отдельная задача.

### ADR-003 — private D-Bus на одну ВМ

QEMU и соответствующий Sunshine instance работают на отдельном Unix D-Bus address. Сокет доступен только сервисному пользователю ВМ. D-Bus никогда не публикуется в TCP-сеть.

### ADR-004 — latest frame wins

Очередь capture→encoder имеет глубину один. Новый кадр заменяет старый, если encoder ещё занят. В интерактивной сессии предпочтительнее потерять устаревший кадр, чем накопить задержку.

### ADR-005 — damage используется как сигнал пробуждения, а не как partial-video update

Moonlight передаёт видеокадры, поэтому MVP кодирует актуальную полную поверхность. Damage rectangles нужны для cadence control и будущих оптимизаций, но не формируют очередь прямоугольников.

### ADR-006 — QEMU D-Bus заменяет custom guest agent в MVP-0

Для первого среза QEMU предоставляет display, cursor, keyboard/mouse,
SetUIInfo и PCM audio. QEMU Clipboard broker остаётся reference-only
вариантом: реализованный data path — QSF custom guest agent через
virtio-serial, который уже покрывает constrained files и strict UTF-8
clipboard. Более широкие DPI, IME/Unicode и OS session semantics остаются
отдельной работой.

### ADR-007 — Qt shell отделён от Moonlight media process

Первый поток работает с обычным Moonlight. Resize, clipboard и files идут
через отдельный QSF companion и не меняют GameStream. Реализованный
`clients/qsunshine-qt` не встраивает SDL drawable и не патчит Moonlight-Qt:
Qt shell запускает stock Moonlight как child process для media/input, а сам
владеет profile/QSF UX. Это исключает конфликт Qt/SDL event loops на macOS и
Wayland. Версионируемый QMDP side-channel остаётся будущим предложением, но
его отсутствие не нарушает обычный GameStream flow.

## 3. Границы MVP

### MVP-0A: функциональный удалённый QEMU console без GPU

Входит:

- Linux host;
- одна QEMU console;
- CPU-visible QEMU display (`virtio-vga` как базовый вариант);
- `Scanout`/`Update` и Unix `ScanoutMap`/`UpdateMap`;
- один Sunshine process на ВМ;
- H.264 через Sunshine `encoder = software`;
- stock Moonlight на Windows/Linux/macOS;
- клавиатура, absolute/relative mouse, кнопки и wheel;
- stereo 48 kHz guest audio;
- установка UI size один раз при старте сессии;
- latest-frame queue depth one и bounded audio FIFO;
- BIOS/UEFI/boot screen;
- systemd template unit и health metrics.

Цель MVP-0A — не высокая частота кадров, а полноценный корректный поток через
Sunshine/Moonlight на машине без GPU. При медленном software encoder backend
обязан отбрасывать устаревшие кадры, а не накапливать задержку.

### MVP-0B: GPU performance path

Добавляются без изменения control/input/audio архитектуры:

- `virtio-vga-gl`/VirGL;
- single-plane RGB DMA-BUF (`XRGB8888`/`ARGB8888`/`BGRX8888` после проверки драйверов);
- EGL/Vulkan/VAAPI import;
- H.264/HEVC/AV1 через уже поддержанный Sunshine hardware encoder;
- отсутствие полного CPU readback;
- performance/latency gates.

Не входит:

- несколько scanout/мониторов;
- VFIO GPU scanout;
- cross-GPU zero-copy;
- HDR;
- микрофон клиент→ВМ;
- file transfer;
- полноценный Unicode IME;
- isolated user sessions;
- seamless apps;
- WAN relay/NAT traversal сверх штатного Sunshine.

### MVP-1: desktop UX

Добавляются:

- standalone Qt desktop shell that launches stock Moonlight Qt as a child;
- capability negotiation;
- live resize с debounce и ACK;
- двусторонний `text/plain; charset=utf-8` clipboard;
- local cursor shape/position;
- reconnect token и сохранение desktop session;
- toolbar с fixed/match/follow-window/follow-monitor;
- clipboard permissions и лимиты.

## 4. Рабочие потоки

### WP0 — воспроизводимая среда

**WP0.1.** Зафиксировать версии QEMU, Sunshine, Moonlight-Qt, Mesa, libdrm, EGL и GPU driver.

**WP0.2.** Создать одну контрольную Linux-гостевую ВМ и одну Windows-гостевую
ВМ с CPU-visible `virtio-vga`, USB tablet и HDA audio. `virtio-vga-gl`
добавляется отдельным GPU-оптимизационным профилем после прохождения WP2-A.

**WP0.3.** Создать private D-Bus launcher и systemd template:

```text
qemu-vm@<id>.service
sunshine-qemu@<id>.service
```

**Выход:** повторяемый boot, известный D-Bus address, console 0 видна внешнему listener.

**Сложность:** 2 points.

### WP1 — QEMU D-Bus probe

Создать отдельную утилиту `qemu-display-probe` до модификации Sunshine.

Она должна:

- найти `/org/qemu/Display1/Console_0`;
- зарегистрировать Display Listener;
- принять CPU `Scanout`/`Update` и Unix-FD-backed
  `ScanoutMap`/`UpdateMap`;
- вывести параметры `ScanoutDMABUF`/`UpdateDMABUF`, если QEMU их предлагает,
  но не требовать их для gate WP1;
- корректно принять/закрыть Unix FD;
- получить cursor shape/position;
- отправить key/mouse события;
- вызвать `SetUIInfo`;
- принять AudioOut PCM;
- пережить reset/reboot QEMU.

Сначала обязателен shared-map/CPU dump и software encode; затем добавляется EGL
import smoke test.

**Gate WP1:** 30 минут непрерывной работы без роста FD/RSS и без блокировки QEMU main loop.

**Сложность:** 5 points.

### WP2 — Sunshine QEMU capture backend

Добавить backend `qemu_dbus` в Linux platform layer.

Предлагаемые файлы:

```text
src/platform/linux/qemu_dbus.h
src/platform/linux/qemu_dbus.cpp
src/platform/linux/qemu_audio.h
src/platform/linux/qemu_audio.cpp
src/platform/linux/qemu_input.h
src/platform/linux/qemu_input.cpp
```

Backend должен:

1. получить persistent DMA-BUF surface;
2. дублировать/владеть FD по чётким RAII-правилам;
3. заполнить существующий Sunshine EGL surface descriptor;
4. вернуть frame descriptor capture pipeline;
5. при смене geometry/fourcc/modifier вернуть `capture_e::reinit`;
6. не ждать encoder в D-Bus callback;
7. иметь CPU fallback и метрику fallback reason.

**Gate WP2-A:** stock Moonlight показывает guest console через software/shared-map path.

**Gate WP2-B:** GPU DMA-BUF path работает без полного readback кадра в CPU RAM.

**Сложность:** 10 points для CPU-среза плюс 16 points для DMA-BUF/encoder interop; основной риск fast path — modifiers/import.

### WP3 — QEMU input sink

Преобразовать Moonlight input в QEMU D-Bus:

- key down/up → QEMU key number;
- absolute pointer → `SetAbsPosition`;
- relative pointer → `RelMotion`;
- button down/up → `Press`/`Release`;
- wheel → короткие button pulses;
- release-all при disconnect/focus loss.

Использовать mapping из QEMU remote viewer как справочную реализацию, но оформить отдельную тестируемую таблицу.

**Gate WP3:** input работает в UEFI, bootloader, Linux console, Windows login; ни одно событие не попадает в host desktop.

**Сложность:** 5 points.

### WP4 — индивидуальный звук ВМ

Реализовать adapter QEMU AudioOut → Sunshine `mic_t`/audio encoder:

- конверсия QEMU PCM в interleaved float32;
- resample только при необходимости;
- MVP output: 48 kHz, stereo;
- bounded FIFO 30–50 ms;
- overflow: drop oldest;
- underflow: тишина, счётчик события;
- reset буфера при guest audio restart.

**Gate WP4:** 8-часовой playback без drift/накопления latency; звук одной ВМ не смешивается со звуком хоста или другой ВМ.

**Сложность:** 5 points.

### WP5 — initial resolution match

При GameStream launch Sunshine уже знает требуемые width/height/FPS. QEMU backend вызывает `SetUIInfo`:

```text
width/height = requested stream mode
width_mm/height_mm = derived from requested DPI or a conservative 96-DPI default
```

Если guest меняет mode, новый scanout вызывает converter/encoder reinit и IDR. Если guest не реагирует, Sunshine масштабирует текущий scanout.

**Gate WP5:** подключение 1920×1080 и 2560×1440 приводит к соответствующему guest mode там, где guest stack поддерживает resize; fallback не создаёт black screen.

**Сложность:** 3 points.

### WP6 — packaging и эксплуатация

- systemd template instance на ВМ;
- отдельные Sunshine data/config directories;
- уникальный hostname и port base;
- private D-Bus socket;
- readiness после регистрации Console_0;
- watchdog;
- Prometheus/structured metrics;
- graceful restart при QEMU reconnect;
- конфигурация encode GPU per VM.

**Gate WP6:** reboot хоста автоматически восстанавливает все объявленные VM endpoints; остановка одной ВМ не затрагивает соседние.

**Сложность:** 5 points.

### WP7 — Qt desktop shell

Реализованный и дважды квалифицированный на target runtime client-side слой
после MVP-0:

- standalone `MoonlightController` с профилями и controlled reconnect;
- windowed/fullscreen/borderless CLI presentation;
- QSF TLS 1.3 mTLS lifecycle, clipboard bridge и constrained files;
- явная post-video activation, чтобы child-process startup не выдавался за
  подтверждённую GameStream session;
- профильный toolbar/policies и diagnostics;
- coalesced QSF resize request.

**Закрытый functional gate WP7:** две независимые реальные трассы stock
Moonlight Qt -> Sunshine -> QEMU -> VirGL/Weston подтверждают pair/list,
windowed `1280x800`, physical fullscreen `1600x900`, контролируемый reconnect,
ввод в обеих фазах, mTLS QSF clipboard/files и resize `1280x720` с restart
Weston DRM. **Незакрытый production gate:** 100 последовательных resize и
длительный reconnect/soak без перезапуска гостевой ВМ; session binding
QSF↔GameStream, cursor и IME остаются отдельными задачами.

**Сложность:** 13 points.

## 5. Порядок реализации

```text
WP0 → WP1 → WP2-A → WP3 → WP4 → WP5 → WP2-B → WP6 → MVP-0
                                                   ↓
                                             WP7 → MVP-1
```

Почему GPU path идёт после working CPU path: сначала нужно доказать корректность D-Bus lifetime, input, audio и session recovery. Затем меняется только frame transport, а не весь вертикальный срез одновременно.

## 6. Минимальная команда

- один senior C++/Linux graphics engineer: QEMU D-Bus, DMA-BUF/EGL, Sunshine backend;
- один C++/Qt engineer: standalone Qt desktop UX и lifecycle stock Moonlight;
- part-time QA/DevOps: VM matrix, systemd, soak/failure testing.

Один сильный C++/graphics инженер способен сделать MVP-0 последовательно, но клиентская ветвь будет конкурировать за внимание с GPU hardening.

## 7. Критические риски

### R1 — DMA-BUF modifier несовместим с encoder path

**Снижение риска:** импорт в EGL/Vulkan и GPU blit в собственную linear/encoder-compatible surface; CPU fallback только как диагностический режим; same-GPU requirement в MVP.

### R2 — implicit synchronization недостаточна

**Снижение риска:** сначала implicit DMA-BUF sync; измерять tearing/stale frames; затем добавить explicit fences, если QEMU/driver interface их позволит.

### R3 — guest игнорирует SetUIInfo

**Снижение риска:** fixed resolution fallback; позднее небольшой guest agent для принудительного mode/DPI.

### R4 — глобальное platform state Sunshine

**Снижение риска:** один process на ВМ в MVP. Multi-VM broker допускается только после выделения per-session input/audio/capture contexts.

### R5 — keyboard layout/IME

**Снижение риска:** scancode-first для shortcuts/firmware; text clipboard в MVP-1; Unicode guest agent позже.

### R6 — encoder session limits

**Снижение риска:** обнаружение лимита при startup, admission control и GPU assignment; не обещать больше одновременных потоков, чем подтверждено на конкретном драйвере.

### R7 — clipboard leakage

**Снижение риска:** clipboard выключен по умолчанию, направления и лимиты на paired client, foreground-session binding, очистка metadata при disconnect.

## 8. Definition of Done

MVP-0 считается готовым только после прохождения всех обязательных критериев из `MVP_ACCEPTANCE.md`; демонстрация одного удачного подключения без latency/soak/failure evidence не считается завершением.
