# Проверка QSM Direct

Быстрый source-level набор:

```bash
cmake -S . -B build-direct-tests -G Ninja -DBUILD_TESTING=ON
cmake --build build-direct-tests
ctest --test-dir build-direct-tests --output-on-failure
```

Он включает core/D-Bus тесты, guest channel, package layout, выбор encoder,
автонастройку terminal service и статическую проверку PVE Console overlay.

Для PVE-узла используется реальный stress suite. Он не подменяет QEMU,
WebRTC-пир или browser input:

```bash
python3 lab/proxmox9/run-qsm-direct-stress-suite.py \
  --vmid 103 \
  --browser-host BROWSER_HOST --browser-user USER --browser-key KEY \
  --browser-script /absolute/path/browser-webrtc-receiver.cjs
```

Набор покрывает первичное видео, несколько одновременных viewers, mouse/keys,
window ↔ fullscreen, изменение viewport, clipboard, VM reboot и restart
terminal service. Для visual evidence запускайте peer с `--browser-headful
--visual-evidence`.

Проверка именно интеграции PVE UI должна дополнительно подтвердить, что выбор
левого пункта **Console** создаёт iframe в существующей области, а не popup.
Для регрессии порядка запуска embedded Console и отдельного окна есть отдельный
browser gate. Он обязан удержать две одновременно подключённые сессии в обоих
порядках (`frame → window` и `window → frame`) и отклоняет iframe с высотой
пустой ExtJS panel:

```bash
node lab/proxmox9/qualify-pve-direct-embedded-window-e2e.cjs \
  --pve-url https://PVE_NODE:8006 --user TEMP_USER@pve \
  --password-file /secure/pve.password --vmid 103 \
  --chrome /usr/bin/google-chrome
```

Временной учётной записи достаточно `PVEVMUser` только на тестовой VM.
