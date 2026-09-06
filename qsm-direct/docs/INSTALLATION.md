# Установка QSM Direct на Proxmox VE 9

Пакет `qsm-pve-direct` устанавливается только на PVE-узел. Клиенту достаточно
современного браузера с WebRTC H.264/Opus; ничего дополнительно устанавливать
не нужно.

```bash
apt install ./qsm-pve-direct_*.deb
systemctl enable --now qsm-pve-direct-terminal.service
systemctl status qsm-pve-direct-terminal.service
```

Пакет безопасно добавляет небольшой скрипт к PVE UI только для проверенных
версий `pve-manager`. При неподдержанной версии он восстанавливает штатный
шаблон вместо рискованного патчинга.

В Hardware → Display → Advanced включите QSM Display1.

- **VirGL GPU (GL)**: QSM создаёт private `virtio-vga-gl` и GL D-Bus display.
  VNC одновременно с этим GL backend не используется.
- **CPU — Standard VGA or VirtIO (no GL)**: выбранный PVE `std` или non-GL
  `virtio` остаётся, а QSM добавляет non-GL D-Bus display. GPU/render node не
  требуется; штатный VNC может работать параллельно.

Параметры кодировщика сохраняются отдельно для каждой VM в
`/etc/qsm-pve-direct/instances.d/<VMID>.conf` с правами `0600`. Режим
Automatic проверяет NVENC, QSV, VA-API и затем `libx264`; можно явно выбрать
hardware-only или software. Browser WebRTC-ветка пока поддерживает H.264/Opus;
HEVC не принимается как ложная настройка.

Для двухстороннего clipboard в Linux-госте установите matching пакет
`qsm-desktop-agent_*.deb`, затем один раз настройте desktop-пользователя:

```bash
apt install ./qsm-desktop-agent_*.deb
qsm-desktop-agent-setup USER
```

QEMU Guest Agent остаётся полезным для штатных операций PVE, но не заменяет
desktop-session companion: ему недоступен clipboard графической сессии.
