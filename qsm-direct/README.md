# QSM Direct

QSM Direct — браузерная графическая консоль виртуальных машин Proxmox VE 9.
Она использует уже существующую PVE-сессию и право `VM.Console`: отдельные
учётные записи, PIN, внешний listener и приложение на компьютере оператора не
нужны.

```text
PVE Web UI / VM.Console
        │ protected same-origin SDP request
        ▼
qsm-pve-direct-terminal ── private Unix sockets ── qsm-direct-media-worker
        │                                                │
        └──────────────── QEMU Display1 D-Bus ───────────┘
                                                         │
                                             QEMU VM / guest display
```

Видео H.264 и Opus передаются по WebRTC. Ввод идёт через QEMU Display1; для
движения мыши используется неблокирующий канал, поэтому старые координаты не
накапливаются при кратковременной перегрузке. Работает как с VirGL/GL, так и с
обычными `std` и non-GL `virtio` дисплеями. Кодировщик выбирается на хосте:
NVENC, QSV, VA-API или программный `libx264`.

## Установка

Соберите или возьмите `qsm-pve-direct_*.deb` для PVE 9 и установите на узле:

```bash
apt install ./qsm-pve-direct_*.deb
systemctl enable --now qsm-pve-direct-terminal.service
```

В VM откройте **Hardware → Display → Advanced**, включите **QSM Display1** и
выберите профиль. Для VirGL профиль добавляет private `virtio-vga-gl` и D-Bus
Display1; для `std`/`virtio` без GL оставляет выбранный видеоадаптер и добавляет
не-GL D-Bus Display1. После сохранения используйте существующий пункт
**Console** слева между **Summary** и **Hardware**. При подходящей конфигурации
QSM Direct встраивается в эту область; для остальных конфигураций остаётся
обычная noVNC-консоль.

Окно консоли меняет размер гостевого Display1 после окончания resize, а
fullscreen — тот же режим в полный размер viewport. Для guest clipboard
установите в Linux VM дополнительный `qsm-desktop-agent` и выполните
`qsm-desktop-agent-setup USER`. Передача файлов намеренно не входит в
браузерный транспорт: браузер не даёт приложению доступ к локальной файловой
системе без явного выбора пользователя.

Подробности: [установка](docs/INSTALLATION.md) и
[проверки](docs/TESTING.md).

## Производительность относительно noVNC

Ниже сравнение архитектур, а не обещание фиксированного числа миллисекунд:
оно зависит от кодировщика, браузера, сети и гостевой нагрузки. Проверять надо
при одинаковых разрешении и частоте гостя.

| Свойство | QSM Direct | Stock noVNC |
| --- | --- | --- |
| Графический путь | H.264/Opus WebRTC; браузер может декодировать H.264 аппаратно | RFB rectangles по надёжному WebSocket, отрисовка Canvas |
| Поведение при loss | медиапоток восстанавливается с ближайшего IDR; координаты мыши не ждут старые пакеты | TCP head-of-line задерживает последующие RFB-обновления |
| Resize | запрашивает новый Display1 viewport и начинает новый H.264 configuration/IDR boundary | масштабирует или перерисовывает текущий framebuffer |
| Общая нагрузка | один low-latency worker на VM, общий для её viewers | QEMU RFB без видеокодировщика |
| Лучший сценарий | интерактивный desktop, видео, WAN | firmware, recovery, установщик и простой текст |

`lab/proxmox9/run-qsm-direct-stress-suite.py` фиксирует first-video,
key-to-pixel, hover, drag gaps, resize/fullscreen, clipboard и reconnect.
Перед публикацией сравнительных чисел для noVNC надо снять те же показатели
на той же VM и сети.
